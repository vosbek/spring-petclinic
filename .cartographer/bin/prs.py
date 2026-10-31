#!/usr/bin/env python3
"""Pull-request flow analytics for Cartographer.

Writes .cartographer/facts/prs-history.json with throughput, size, cycle time,
review latency, stuck PRs, review concentration and AI-assisted share,
overall and per component.

Two sources, used together when available:
  git      always: squash commits "(#123)" and merge commits "Merge pull request #123".
           Gives merged count, size, components and (for merge commits) lead time
           from first commit to merge.
  GitHub   when `gh` is authenticated for the repo: created/merged timestamps,
           reviews, open PRs. Gives cycle time, time to first review, stuck PRs.

Usage:
  python3 .cartographer/bin/prs.py [--repo .] [--limit 300] [--no-api]

Numbers describe the flow of work at team level. Review load is reported as a
distribution to spot bottlenecks, not as an individual performance measure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402
import carto  # noqa: E402
from pr import owner_repo  # noqa: E402

WINDOW = 90
WEEKS = 13


def gh(path, paginate=False):
    args = ["gh", "api", path] + (["--paginate"] if paginate else [])
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    txt = out.stdout.strip()
    if paginate and txt.startswith("[") and "][" in txt:
        txt = "[" + txt[1:-1].replace("][", ",").replace("]\n[", ",") + "]"
    try:
        return json.loads(txt)
    except ValueError:
        return None


def hours(a, b):
    return round((b - a).total_seconds() / 3600.0, 1)


def med(xs):
    return round(statistics.median(xs), 1) if xs else None


def p75(xs):
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(0.75 * (len(xs) - 1))))], 1)


def from_git(repo, hist, mapper, limit):
    """Merged PRs visible in git history."""
    prs = {}
    for sha, d, author, subject, changes, flags in carto.iter_commits(hist):
        n = flags.get("pr")
        if not n or n in prs:
            continue
        lines = sum(a + dl for _, a, dl in changes)
        prs[n] = {"number": n, "title": re.sub(r"\s*\(#\d+\)\s*$", "", subject), "mergedAt": d.isoformat(), "author": author,
                  "lines": lines, "files": len(changes), "components": sorted({mapper(p) for p, _, _ in changes if mapper(p)}),
                  "ai": flags.get("ai"), "bot": bool(flags.get("bot")), "source": "squash"}
        if len(prs) >= limit:
            break
    # merge commits (excluded from history.json, which is --no-merges)
    raw = carto.git(repo, "log", "--merges", "--first-parent", "--since=" + hist.get("since", "1 year ago"),
                    "--format=%H%x1f%P%x1f%aI%x1f%s", "-n", str(limit), check=False) or ""
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 4:
            continue
        sha, parents, when, subject = parts
        m = re.match(r"Merge pull request #(\d+) from (\S+)", subject)
        ps = parents.split()
        if not m or len(ps) < 2:
            continue
        n = int(m.group(1))
        if n in prs:
            continue
        first = carto.git(repo, "log", "--reverse", "--format=%aI%x1f%aN%x1f%b%x1d", "%s..%s" % (ps[0], ps[1]), check=False) or ""
        entries = [e for e in first.split("\x1d") if e.strip()]
        num = carto.git(repo, "diff", "--numstat", ps[0], sha, check=False) or ""
        files = [l.split("\t") for l in num.splitlines() if l.count("\t") == 2]
        lines = sum(int(a) + int(d) for a, d, _ in files if a.isdigit() and d.isdigit())
        first_at = entries[0].split("\x1f")[0].strip() if entries else None
        author = entries[0].split("\x1f")[1] if entries else m.group(2).split("/")[0]
        ai = None
        for e in entries:
            f = e.split("\x1f")
            if len(f) >= 3:
                ai = ai or carto.ai_tool(f[1], "", f[2])
        prs[n] = {"number": n, "title": subject, "mergedAt": when, "author": author, "lines": lines, "files": len(files),
                  "components": sorted({mapper(p) for _, _, p in files if mapper(p)}), "ai": ai, "bot": bool(carto.BOT.search(author or "")),
                  "firstCommitAt": first_at, "commits": len(entries), "source": "merge"}
    return prs


def from_api(owner, name, limit, detail):
    pulls = gh("repos/%s/%s/pulls?state=all&per_page=100&sort=created&direction=desc" % (owner, name), paginate=True)
    if pulls is None:
        return None
    out = {}
    for p in pulls[:limit]:
        n = p["number"]
        out[n] = {"number": n, "title": p.get("title"), "author": (p.get("user") or {}).get("login"), "state": p.get("state"),
                  "draft": p.get("draft"), "createdAt": p.get("created_at"), "mergedAt": p.get("merged_at"), "closedAt": p.get("closed_at"),
                  "updatedAt": p.get("updated_at"), "url": p.get("html_url"),
                  "bot": bool(carto.BOT.search((p.get("user") or {}).get("login") or "")),
                  "aiBody": bool(carto.GENERATED_WITH.search(p.get("body") or "")) or bool(re.search(r"(copilot-swe-agent|devin-ai-integration|claude\[bot\])", (p.get("user") or {}).get("login") or "", re.I))}
    for n in list(out)[:detail]:
        rv = gh("repos/%s/%s/pulls/%d/reviews?per_page=100" % (owner, name, n))
        if rv:
            subs = [r for r in rv if r.get("submitted_at") and (r.get("user") or {}).get("login") != out[n]["author"]]
            if subs:
                out[n]["firstReviewAt"] = min(r["submitted_at"] for r in subs)
                ap = [r["submitted_at"] for r in subs if r.get("state") == "APPROVED"]
                out[n]["approvedAt"] = min(ap) if ap else None
                out[n]["reviewers"] = sorted({(r.get("user") or {}).get("login") for r in subs if r.get("user")})
            else:
                out[n]["reviewers"] = []
        full = gh("repos/%s/%s/pulls/%d" % (owner, name, n))
        if full:
            out[n]["lines"] = (full.get("additions") or 0) + (full.get("deletions") or 0)
            out[n]["files"] = full.get("changed_files")
            out[n]["commits"] = full.get("commits")
    return out


def analyze(repo, limit=300, detail=150, use_api=True):
    home = carto.home(repo)
    inv = carto.load(os.path.join(home, "facts", "inventory.json"))
    hist = carto.load(os.path.join(home, "facts", "history.json"))
    if not hist:
        raise SystemExit("run collect.py first")
    model = carto.load(os.path.join(home, "model", "model.json")) or build.auto_model(inv, repo)
    mapper = carto.Mapper(model.get("components", []))
    ref = carto.parse_date(hist.get("commitDate") or hist["commits"][0][1]) if hist.get("commits") else dt.datetime.now(dt.timezone.utc)
    prs = from_git(repo, hist, mapper, limit)
    source = ["git"]
    api = None
    if use_api:
        owner, name = owner_repo(hist.get("remote") or (carto.git(repo, "remote", "get-url", "origin", check=False) or ""))
        if owner:
            api = from_api(owner, name, limit, detail)
    if api is not None:
        source.append("github")
        now = dt.datetime.now(dt.timezone.utc)
        ref = max(ref, now) if api else ref
        for n, a in api.items():
            g = prs.get(n, {})
            merged = dict(g)
            merged.update({k: v for k, v in a.items() if v is not None})
            if g.get("components"):
                merged["components"] = g["components"]
            merged["ai"] = g.get("ai") or ("PR body" if a.get("aiBody") else None)
            prs[n] = merged

    cutoff = ref - dt.timedelta(days=WINDOW)
    week0 = carto.week_start(ref)
    merged = [p for p in prs.values() if p.get("mergedAt") and carto.parse_date(p["mergedAt"]) >= cutoff]
    human = [p for p in merged if not p.get("bot")]
    weekly = [0] * WEEKS
    for p in merged:
        w = int((week0 - carto.week_start(carto.parse_date(p["mergedAt"]))).days // 7)
        if 0 <= w < WEEKS:
            weekly[WEEKS - 1 - w] += 1
    cycle = [hours(carto.parse_date(p["createdAt"]), carto.parse_date(p["mergedAt"])) for p in human if p.get("createdAt")]
    lead = [hours(carto.parse_date(p["firstCommitAt"]), carto.parse_date(p["mergedAt"])) for p in human if p.get("firstCommitAt")]
    first_review = [hours(carto.parse_date(p["createdAt"]), carto.parse_date(p["firstReviewAt"])) for p in human if p.get("createdAt") and p.get("firstReviewAt")]
    sizes = Counter(carto.size_class(p.get("lines") or 0) for p in human if p.get("lines") is not None)
    open_prs = [p for p in prs.values() if p.get("state") == "open"]
    stale = []
    for p in open_prs:
        created = carto.parse_date(p["createdAt"])
        age = (dt.datetime.now(dt.timezone.utc) - created).days
        if age >= 7 and not p.get("firstReviewAt") and not p.get("draft"):
            stale.append({"number": p["number"], "title": p.get("title"), "author": p.get("author"), "ageDays": age, "url": p.get("url"), "bot": p.get("bot")})
    stale.sort(key=lambda x: -x["ageDays"])
    load = Counter()
    for p in human:
        for r in p.get("reviewers", []) or []:
            load[r] += 1
    total_reviews = sum(load.values())
    top3 = sum(n for _, n in load.most_common(3))
    by_comp = defaultdict(lambda: {"prs": 0, "lines": [], "cycle": []})
    for p in human:
        for c in p.get("components", []):
            b = by_comp[c]
            b["prs"] += 1
            if p.get("lines") is not None:
                b["lines"].append(p["lines"])
            if p.get("createdAt"):
                b["cycle"].append(hours(carto.parse_date(p["createdAt"]), carto.parse_date(p["mergedAt"])))
            elif p.get("firstCommitAt"):
                b["cycle"].append(hours(carto.parse_date(p["firstCommitAt"]), carto.parse_date(p["mergedAt"])))
    recent = sorted(merged, key=lambda p: p["mergedAt"], reverse=True)[:30]
    return {
        "kind": "cartograph-pr-history", "sources": source, "windowDays": WINDOW, "asOf": ref.isoformat(),
        "merged": len(merged), "mergedHuman": len(human), "botMerged": len(merged) - len(human),
        "weekly": weekly, "weekStarts": [(week0 - dt.timedelta(weeks=WEEKS - 1 - i)).strftime("%Y-%m-%d") for i in range(WEEKS)],
        "cycleHours": {"median": med(cycle), "p75": p75(cycle), "n": len(cycle)},
        "leadHours": {"median": med(lead), "p75": p75(lead), "n": len(lead)},
        "firstReviewHours": {"median": med(first_review), "p75": p75(first_review), "n": len(first_review)},
        "sizes": {k: sizes.get(k, 0) for k in ("XS", "S", "M", "L", "XL")},
        "medianLines": med([p["lines"] for p in human if p.get("lines") is not None]),
        "aiShare": round(100.0 * sum(1 for p in human if p.get("ai")) / len(human), 1) if human else None,
        "open": len(open_prs), "stale": stale[:15],
        "reviewLoad": {"reviews": total_reviews, "reviewers": len(load), "top3Share": round(100.0 * top3 / total_reviews, 1) if total_reviews else None,
                       "top": [{"name": n, "reviews": c} for n, c in load.most_common(8)]},
        "components": {c: {"prs": v["prs"], "medianLines": med(v["lines"]), "medianCycleHours": med(v["cycle"])} for c, v in by_comp.items()},
        "recent": [{k: p.get(k) for k in ("number", "title", "author", "mergedAt", "lines", "files", "components", "ai", "bot", "url", "createdAt", "firstReviewAt")} for p in recent],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--detail", type=int, default=150, help="PRs to enrich with reviews/size via the API")
    ap.add_argument("--no-api", action="store_true")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    res = analyze(repo, a.limit, a.detail, not a.no_api)
    out = os.path.join(carto.home(repo), "facts", "prs-history.json")
    carto.save(out, res)
    print("PRs (%s): %d merged in %d days (%d by bots); median %s lines" % ("+".join(res["sources"]), res["merged"], res["windowDays"], res["botMerged"], res["medianLines"]))
    if res["cycleHours"]["n"]:
        print("cycle time median %sh (p75 %sh); first review median %sh" % (res["cycleHours"]["median"], res["cycleHours"]["p75"], res["firstReviewHours"]["median"]))
    if res["leadHours"]["n"]:
        print("lead time (first commit to merge) median %sh over %d merge commits" % (res["leadHours"]["median"], res["leadHours"]["n"]))
    print("open %d, stale %d; AI-assisted %s%%" % (res["open"], len(res["stale"]), res["aiShare"]))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
