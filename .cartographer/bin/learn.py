#!/usr/bin/env python3
"""Harvest the mistakes this repo keeps making, and check the guardrails against them.

  python3 .cartographer/bin/learn.py                 # history + guard log -> facts/lessons.json
  python3 .cartographer/bin/learn.py --api           # also pull PR review comments with gh (needs a GitHub remote)

A mistake is evidence in git, not an opinion:
  revert           a commit later reverted
  follow-up fix    a non-fix commit whose files got a fix commit within 14 days
  missed partner   a commit changed A without its usual partner B (P(B|A) >= 0.5), and a fix to B followed within 14 days
  migration edit   an applied, versioned migration modified after it was added

Mistakes group into classes (by component, file pair or kind). A class needs 2+ occurrences. Each class is
checked against today's active rules by replay: "covered" means a rule would have fired on every occurrence,
"partly covered" on some, "gap" on none. Gaps are what /cartograph-learn turns into new rules.

Also reports rule health: rules that never fire, fire on a large share of commits (noisy), fire without
lift over the base rate, exceptions that are expired or expire soon, and rules agents keep hitting (guard log).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import posixpath
import re
import subprocess
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import carto  # noqa: E402
import guard  # noqa: E402

DAYS = guard.FOLLOW_UP_DAYS
_LOCK = [guard.glob_re(g) for g in guard.LOCKFILES]


def LOCK(path):
    return any(rx.match(path) for rx in _LOCK)


def within(later, earlier):
    return 0 < (later - earlier).total_seconds() <= DAYS * 86400


def harvest(ctx, history):
    by_sha = {c.sha[:12]: c for c in history}
    fixes_by_file = defaultdict(list)
    for c in history:
        if c.flags.get("fix"):
            for f in c.files:
                fixes_by_file[f.path].append(c)
    coup = carto.coupling(ctx.hist) if ctx.hist else {"files": []}
    partners = defaultdict(list)
    for p in coup["files"]:
        if p["pBgivenA"] >= 0.5:
            partners[p["a"]].append((p["b"], p["pBgivenA"]))
        if p["pAgivenB"] >= 0.5:
            partners[p["b"]].append((p["a"], p["pAgivenB"]))

    occ = []   # (kind, key, commit, detail)
    reverted = {}
    for c in history:
        if c.flags.get("reverts"):
            reverted[c.flags["reverts"][:7]] = c
    for c in history:
        if c.flags.get("bot"):
            continue
        comps = sorted({ctx.mapper(f.path) for f in c.files if ctx.mapper(f.path)})
        if c.sha[:7] in reverted:
            occ.append(("revert", comps[0] if comps else "(unmapped)", c, "reverted by %s (%s)" % (reverted[c.sha[:7]].sha[:9], reverted[c.sha[:7]].subject[:60])))
            continue
        if c.flags.get("fix"):
            continue
        paths = {f.path for f in c.files}
        fixers = {}
        for p in paths:
            for fx in fixes_by_file.get(p, ()):
                if fx.sha != c.sha and within(fx.when, c.when):
                    fixers.setdefault(fx.sha, (fx, set()))[1].add(p)
        for fx, ps in fixers.values():
            for p in sorted(ps):
                if carto.category(p) in ("docs", "build", "config") or carto.is_test(p) or LOCK(p):
                    continue
                occ.append(("follow-up-fix", p, c, "%s fixed it within %d days: %s" % (fx.sha[:9], DAYS, fx.subject[:70])))
        for a in paths:
            if carto.category(a) in ("docs", "build", "config") or carto.is_test(a):
                continue
            for b, prob in partners.get(a, ()):
                if b in paths or carto.category(b) in ("docs", "build", "config"):
                    continue
                for fx in fixes_by_file.get(b, ()):
                    if within(fx.when, c.when):
                        occ.append(("missed-partner", "%s -> %s" % (a, b), c, "changed %s without %s (%d%% partner); %s fixed it: %s" % (
                            posixpath.basename(a), posixpath.basename(b), round(prob * 100), fx.sha[:9], fx.subject[:60])))
                        break
        for f in c.files:
            if f.status == "M" and carto.category(f.path) == "schema" and guard.VERSIONED.search(posixpath.basename(f.path)):
                occ.append(("migration-edit", posixpath.dirname(f.path), c, "modified %s" % f.path))
    return occ


def classify(ctx, history, occ):
    groups = defaultdict(list)
    for kind, key, c, detail in occ:
        groups[(kind, key)].append((c, detail))
    rules = ctx.rules
    classes = []
    for (kind, key), items in groups.items():
        uniq = {}
        for c, d in items:
            uniq.setdefault(c.sha, (c, d))
        items = list(uniq.values())
        if len(items) < 2:
            continue
        covered = Counter()
        for c, _ in items:
            for r in rules:
                if r.get("type") not in guard.REPLAYABLE:
                    continue
                vs, _, _, _ = guard.evaluate(c, ctx, [r], use_exceptions=False)
                if vs:
                    covered[r["id"]] += 1
        best = covered.most_common(1)
        cov_n = best[0][1] if best else 0
        state = "covered" if cov_n == len(items) else "partly covered" if cov_n else "gap"
        cls = {"kind": kind, "key": key, "occurrences": len(items), "coverage": state,
               "coveredBy": [{"rule": r, "occurrences": n} for r, n in covered.most_common(3)],
               "examples": [{"sha": c.sha[:12], "date": c.date, "subject": c.subject[:100], "author": c.author, "detail": d}
                            for c, d in sorted(items, key=lambda x: x[0].date, reverse=True)[:6]],
               "candidate": candidate(ctx, kind, key, items)}
        classes.append(cls)
    order = {"gap": 0, "partly covered": 1, "covered": 2}
    classes.sort(key=lambda x: (order[x["coverage"]], -x["occurrences"]))
    return classes


def candidate(ctx, kind, key, items):
    """The highest-level deterministic encoding the evidence supports; the agent decides whether to adopt it."""
    if kind == "missed-partner":
        a, b = key.split(" -> ")
        return {"level": "guardrail", "rule": {"id": "co-change-" + re.sub(r"[^a-z0-9]+", "-", (guard.stem(a) + "-" + guard.stem(b)).lower()).strip("-")[:48],
                                                "type": "co-change", "when": a, "require": b, "severity": "block", "source": "learned",
                                                "evidence": [c.sha[:12] for c, _ in items[:5]]}}
    if kind == "migration-edit":
        return {"level": "guardrail", "rule": {"id": "applied-migrations", "type": "protected-paths", "paths": [key + "/**"], "allowAdd": True,
                                                "severity": "block", "source": "learned", "evidence": [c.sha[:12] for c, _ in items[:5]]}}
    if kind == "follow-up-fix":
        tested = {b for a, b, _ in (ctx.graph or {}).get("fileEdges", []) if carto.is_test(a) or a in set((ctx.graph or {}).get("tests", []))}
        has_test = key in tested
        return {"level": "test" if not has_test else "judgment",
                "hint": ("No test imports %s. Find the behaviour the fixes restored and pin it with a test, then make tests-required cover this path." % key) if not has_test
                else "Tests import %s yet fixes keep following changes: read the fixes for the behaviour the tests miss." % key,
                "rule": {"id": "tests-" + re.sub(r"[^a-z0-9]+", "-", guard.stem(key).lower()).strip("-")[:40], "type": "tests-required", "paths": key,
                         "minLines": 1, "severity": "block", "source": "learned", "evidence": [c.sha[:12] for c, _ in items[:5]]} if not has_test else None}
    comp = ctx.comp.get(key)
    if kind == "revert" and comp:
        reach = (ctx.graph or {}).get("tests") or []
        return {"level": "test", "hint": "Find the behaviour the fixes restored and pin it with a test in %s; then consider tests-required scoped to component:%s." % (comp.get("name", key), key),
                "rule": {"id": "tests-%s" % key, "type": "tests-required", "paths": "component:%s" % key, "minLines": 10, "severity": "block",
                         "source": "learned", "evidence": [c.sha[:12] for c, _ in items[:5]]},
                "testFilesInRepo": len(reach)}
    return {"level": "judgment", "hint": "Read the examples; if a lint, type or rule can't express it, write it into the agent instructions with the failure example."}


def rule_health(ctx, rep):
    out = []
    for r in rep["rules"]:
        if not r.get("replayable"):
            continue
        if r["fired"] == 0:
            out.append({"rule": r["rule"], "issue": "never fired", "detail": "0 of %d commits in the window. Keep it only if it guards something that has not happened yet; otherwise drop it." % r["commits"]})
        elif r["firedPct"] >= 25:
            out.append({"rule": r["rule"], "issue": "noisy", "detail": "fires on %.0f%% of commits. Narrow its paths or raise its threshold." % r["firedPct"]})
        elif r["fired"] >= 5 and r["lift"] is not None and r["lift"] < 1:
            out.append({"rule": r["rule"], "issue": "no lift", "detail": "commits it fires on are followed by fixes less often (%s%%) than average (%s%%)." % (r["hitRatePct"], r["baseRatePct"])})
    soon = ctx.today + dt.timedelta(days=14)
    for ex in ctx.config.get("exceptions", []):
        state = guard.exception_state(ex, ctx.today)
        if state != "valid":
            out.append({"rule": ex.get("rule"), "issue": "exception %s" % state, "detail": "%s: %s" % (ex.get("path") or "**", ex.get("reason") or "no reason")})
        elif dt.date.fromisoformat(ex["expires"]) <= soon:
            out.append({"rule": ex.get("rule"), "issue": "exception expires soon", "detail": "%s on %s (approved by %s)" % (ex.get("path") or "**", ex["expires"], ex["approvedBy"])})
    return out


def guard_log(ctx):
    hits, runs, edits = Counter(), 0, Counter()
    for e in carto.read_events(ctx.home):
        if e.get("kind") == "run":
            runs += 1
        if e.get("decision") == "block":
            for r in e.get("rules", []):
                (edits if e.get("kind") == "tool" else hits)[r] += 1
    rep = [{"rule": r, "blockedRuns": n} for r, n in hits.most_common() if n >= 2]
    rep += [{"rule": r, "blockedRuns": n, "atEdit": True} for r, n in edits.most_common() if n >= 2]
    return {"runs": runs, "repeats": rep}


def review_comments(repo, limit=60):
    remote = carto.git(repo, "remote", "get-url", "origin", check=False) or ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(\.git)?$", remote.strip())
    if not m:
        return None
    try:
        out = subprocess.run(["gh", "api", "repos/%s/%s/pulls/comments?per_page=100&sort=created&direction=desc" % (m.group(1), m.group(2))],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except ValueError:
        return None
    items = []
    for c in data[:limit]:
        user = (c.get("user") or {}).get("login", "")
        if carto.BOT.search(user):
            continue
        items.append({"path": c.get("path"), "line": c.get("line") or c.get("original_line"), "author": user,
                      "pr": int(re.sub(r".*/", "", c.get("pull_request_url", "0")) or 0), "body": (c.get("body") or "")[:400], "at": (c.get("created_at") or "")[:10]})
    by_path = Counter(i["path"] for i in items if i["path"])
    return {"comments": items, "hotPaths": [{"path": p, "comments": n} for p, n in by_path.most_common(10) if n >= 2]}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--max-commits", type=int, default=800)
    ap.add_argument("--api", action="store_true", help="also fetch PR review comments with gh")
    ap.add_argument("--propose", action="store_true", help="write candidate rules for uncovered classes to guardrails.proposed.json")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    ctx = guard.Context(repo)
    if not ctx.hist:
        raise SystemExit("run collect.py first")
    days = a.days or carto.load_config(repo).get("historyDays", 365)
    history = guard.history_changes(repo, days, a.max_commits)
    occ = harvest(ctx, history)
    classes = classify(ctx, history, occ)
    rep = guard.replay(ctx, ctx.rules, days, a.max_commits, history=history) if ctx.rules else {"rules": [], "window": None}
    res = {"kind": "cartograph-lessons", "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
           "window": {"days": days, "commits": len(history)},
           "occurrences": {k: len({c.sha for kk, _, c, _ in occ if kk == k}) for k in sorted({k for k, _, _, _ in occ})},
           "classes": classes, "ruleHealth": rule_health(ctx, rep), "guardLog": guard_log(ctx),
           "reviewComments": review_comments(repo) if a.api else None}
    carto.save(os.path.join(ctx.home, "facts", "lessons.json"), res)
    if ctx.rules:
        carto.save(os.path.join(ctx.home, "facts", "guard-replay.json"), rep)
    print("%d commits over %d days: %s" % (len(history), days, ", ".join("%d commits with a %s" % (n, k) for k, n in sorted(res["occurrences"].items(), key=lambda x: -x[1])) or "no mistakes found"))
    for cls in classes[:12]:
        cov = cls["coverage"] + ((" by " + cls["coveredBy"][0]["rule"]) if cls["coveredBy"] else "")
        print("  [%-14s] %-15s %-48s x%d  -> %s" % (cov[:14], cls["kind"], cls["key"][:48], cls["occurrences"], cls["candidate"]["level"]))
    for h in res["ruleHealth"]:
        print("  rule %s: %s. %s" % (h["rule"], h["issue"], h["detail"]))
    for r in res["guardLog"]["repeats"]:
        print("  agents hit %s in %d guard runs" % (r["rule"], r["blockedRuns"]))
    if res["reviewComments"]:
        print("  %d review comments; most discussed: %s" % (len(res["reviewComments"]["comments"]),
              ", ".join("%s (%d)" % (h["path"], h["comments"]) for h in res["reviewComments"]["hotPaths"][:4]) or "none twice"))
    if a.propose:
        rules = []
        for cls in classes:
            r = (cls.get("candidate") or {}).get("rule")
            if r and cls["coverage"] != "covered" and cls["candidate"]["level"] == "guardrail":
                rp = guard.replay(ctx, [r], days, a.max_commits, history=history)["rules"][0]
                rules.append((r, {"why": "%d %s occurrences: %s" % (cls["occurrences"], cls["kind"], cls["examples"][0]["detail"]), "replay": rp}))
        n = guard.propose(ctx.home, [r for r, _ in rules], {r["id"]: meta for r, meta in rules})
        print("proposed %d rule(s) in .cartographer/guardrails.proposed.json (guard.py --proposals; a human accepts with --accept)" % n)
    print("wrote %s" % os.path.join(ctx.home, "facts", "lessons.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
