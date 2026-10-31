#!/usr/bin/env python3
"""Build the Cartographer viewer: model + facts -> one self-contained HTML file.

  model/model.json        authored by agents (narrative, structure, snippets, evidence)
  facts/inventory.json    collect.py   files, languages, ages
  facts/history.json      collect.py   commits with fix / revert / AI / PR flags
  facts/graph.json        graph.py     import graph, interfaces, tests        (optional)
  facts/prs-history.json  prs.py       pull-request flow analytics             (optional)
  facts/prs/*.json        pr.py        individual PR / range analyses          (optional)
        |
        v  derive: activity, hotspots, coupling, defects, knowledge, age, AI share,
        v          structure checks, drift
  dist/cartograph.html    open in any browser; no server
  dist/cartograph.json    the same data, for fleet.py and other tools

Usage:
  python3 .cartographer/bin/build.py
  python3 .cartographer/bin/build.py --sample    # build the bundled sample
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import glob
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import carto  # noqa: E402
import check  # noqa: E402

RECENT_DAYS = 90
WEEKS = 13
PLACEHOLDER = "/*__CARTOGRAPH_DATA__*/null"
INACTIVE_DAYS = 90
FOLLOW_UP_DAYS = 14

load = carto.load
parse_date = carto.parse_date
Mapper = carto.Mapper
bus_factor = carto.bus_factor


def since_model(repo, commit, mapper):
    if not commit or not repo:
        return None
    raw = carto.git(repo, "log", "--no-merges", "--format=@@cartograph@@%h", "--name-only", "%s..HEAD" % commit, check=False)
    if raw is None:
        return {"unknownCommit": commit}
    commits, files = 0, set()
    for l in raw.splitlines():
        if l.startswith("@@cartograph@@"):
            commits += 1
        elif l.strip():
            files.add(l.strip())
    comps = Counter(mapper(f) or "_unmapped" for f in files)
    return {"commits": commits, "files": len(files), "components": dict(comps)}


def pct_share(a, b):
    return round(100.0 * a / b, 1) if b else 0.0


def derive(model, inv, hist, repo, graph=None, prs_history=None, prs=None):
    comps = model.get("components", [])
    comp_ids = {c["id"] for c in comps}
    external_ids = {c["id"] for c in comps if c.get("layer") == "external"}
    mapper = Mapper(comps)
    facts = {"head": None, "inventory": None, "coverage": None, "repo": None, "components": {},
             "hotspots": [], "recent": [], "sinceModel": None, "snippets": {}, "files": {},
             "coupling": None, "defects": None, "knowledge": None, "age": None, "ai": None,
             "structure": None, "prHistory": prs_history, "prs": prs or []}

    # ------------------------------------------------------------ inventory + age
    loc_of, mod_of = {}, {}
    ref = None
    if hist and hist.get("commits"):
        ref = parse_date(hist.get("commitDate") or hist["commits"][0][1])
    elif inv and inv.get("commitDate"):
        ref = parse_date(inv["commitDate"])
    if inv:
        facts["head"] = {"commit": inv.get("commit"), "branch": inv.get("branch"), "date": inv.get("commitDate"), "remote": inv.get("remote")}
        facts["inventory"] = {"files": inv["totalFiles"], "loc": inv["totalLoc"], "languages": inv["languages"][:8]}
        mapped, unmapped_dirs = 0, Counter()
        age_buckets = Counter()
        comp_age = defaultdict(list)
        for row in inv["files"]:
            path, _lang, loc = row[0], row[1], row[2]
            modified = row[4] if len(row) > 4 else None
            loc_of[path] = loc
            cid = mapper(path)
            if cid:
                st = facts["components"].setdefault(cid, {})
                st["files"] = st.get("files", 0) + 1
                st["loc"] = st.get("loc", 0) + loc
                mapped += loc
            else:
                unmapped_dirs["/".join(path.split("/")[:2]) if "/" in path else "(root)"] += loc
            if modified and ref:
                days = (ref.date() - dt.date.fromisoformat(modified)).days
                mod_of[path] = days
                b = "<90d" if days < 90 else "90d–1y" if days < 365 else "1–2y" if days < 730 else "2–5y" if days < 1826 else ">5y"
                age_buckets[b] += loc
                if cid:
                    comp_age[cid].append((days, loc))
        total = inv["totalLoc"] or 1
        facts["coverage"] = {"mappedPct": round(100.0 * mapped / total, 1),
                             "unmapped": [{"dir": d, "loc": n} for d, n in unmapped_dirs.most_common(10)]}
        if mod_of:
            order = ["<90d", "90d–1y", "1–2y", "2–5y", ">5y"]
            facts["age"] = {"buckets": [{"label": b, "loc": age_buckets.get(b, 0), "pct": pct_share(age_buckets.get(b, 0), total)} for b in order],
                            "components": {}}
            for cid, rows in comp_age.items():
                tl = sum(l for _, l in rows) or 1
                facts["age"]["components"][cid] = {
                    "medianDays": int(statistics.median([d for d, _ in rows])),
                    "freshPct": pct_share(sum(l for d, l in rows if d < 90), tl),
                    "stalePct": pct_share(sum(l for d, l in rows if d >= 730), tl)}

    # ------------------------------------------------------------ history
    if hist and hist.get("commits"):
        ref_week = carto.week_start(ref)
        cutoff = ref - dt.timedelta(days=RECENT_DAYS)
        weekly = [0] * WEEKS
        repo_recent = repo_prior = 0
        recent_authors, files_recent = set(), set()
        added_l = deleted_l = 0
        first_seen = {}
        per_file = defaultdict(lambda: {"commits": 0, "add": 0, "del": 0, "authors": set(), "last": None})
        per_comp = defaultdict(lambda: {"recent": 0, "prior": 0, "authors": set(), "weekly": [0] * WEEKS,
                                        "owners": Counter(), "last": None, "fixes": 0, "all": 0, "ai90": 0, "lastBy": {}})
        file_fixes = Counter()
        fix_dates = defaultdict(list)
        recent_list = []
        reverted = set()
        ai_month = defaultdict(lambda: [0, 0])
        ai_tools = Counter()
        commits_parsed = list(carto.iter_commits(hist))
        for sha, d, author, subject, changes, flags in commits_parsed:
            if flags.get("reverts"):
                reverted.add(flags["reverts"][:7])
            if flags.get("fix"):
                for p, _, _ in changes:
                    fix_dates[p].append(d)
        for p in fix_dates:
            fix_dates[p].sort()
        follow = {"ai": [0, 0], "human": [0, 0]}  # [followed by fix, total]
        rev = {"ai": [0, 0], "human": [0, 0]}
        for sha, d, author, subject, changes, flags in commits_parsed:
            bot = flags.get("bot")
            person = not bot and not flags.get("agent")   # AI agents authoring commits are not people who hold knowledge
            age = (ref - d).total_seconds() / 86400.0
            is_recent, is_prior = age < RECENT_DAYS, RECENT_DAYS <= age < 2 * RECENT_DAYS
            wk = int((ref_week - carto.week_start(d)).days // 7)
            touched = set()
            for p, add, dele in changes:
                cid = mapper(p)
                if cid:
                    touched.add(cid)
                if flags.get("fix"):
                    file_fixes[p] += 1
                if is_recent:
                    files_recent.add(p)
                    added_l += add
                    deleted_l += dele
                    pf = per_file[p]
                    pf["commits"] += 1
                    pf["add"] += add
                    pf["del"] += dele
                    if person:
                        pf["authors"].add(author)
                    if pf["last"] is None:
                        pf["last"] = {"date": d.strftime("%Y-%m-%d"), "author": author, "sha": sha[:7]}
            if person:
                first_seen[author] = d
            if is_recent:
                repo_recent += 1
                if person:
                    recent_authors.add(author)
                if 0 <= wk < WEEKS:
                    weekly[WEEKS - 1 - wk] += 1
            elif is_prior:
                repo_prior += 1
            for cid in touched:
                pc = per_comp[cid]
                pc["all"] += 1
                if flags.get("fix"):
                    pc["fixes"] += 1
                if person:
                    pc["owners"][author] += 1
                    if author not in pc["lastBy"]:
                        pc["lastBy"][author] = d
                if pc["last"] is None:
                    pc["last"] = {"date": d.strftime("%Y-%m-%d"), "author": author, "sha": sha[:7]}
                if is_recent:
                    pc["recent"] += 1
                    if flags.get("ai"):
                        pc["ai90"] += 1
                    if person:
                        pc["authors"].add(author)
                    if 0 <= wk < WEEKS:
                        pc["weekly"][WEEKS - 1 - wk] += 1
                elif is_prior:
                    pc["prior"] += 1
            # AI analytics
            if not bot:
                grp = "ai" if flags.get("ai") else "human"
                months = (ref.year - d.year) * 12 + ref.month - d.month
                if 0 <= months < 12:
                    m = ai_month[months]
                    m[1] += 1
                    if flags.get("ai"):
                        m[0] += 1
                if flags.get("ai"):
                    ai_tools[flags["ai"]] += 1
                if not flags.get("fix") and not flags.get("revert") and age > FOLLOW_UP_DAYS:
                    hit = False
                    for p, _, _ in changes:
                        ds = fix_dates.get(p)
                        if ds:
                            i = bisect.bisect_right(ds, d)
                            if i < len(ds) and ds[i] <= d + dt.timedelta(days=FOLLOW_UP_DAYS):
                                hit = True
                                break
                    follow[grp][1] += 1
                    follow[grp][0] += 1 if hit else 0
                rev[grp][1] += 1
                rev[grp][0] += 1 if sha[:7] in reverted else 0
            if len(recent_list) < 40:
                recent_list.append({"sha": sha[:7], "date": d.strftime("%Y-%m-%d"), "author": author, "subject": subject,
                                    "files": len(changes), "components": sorted(touched),
                                    "ai": flags.get("ai"), "fix": bool(flags.get("fix")), "pr": flags.get("pr")})

        window_covers_prior = hist.get("days", 0) >= 2 * RECENT_DAYS
        added_recent = [p for p, w in hist.get("added", []) if parse_date(w) >= cutoff and p in loc_of]
        deleted_recent = [p for p, w in hist.get("deleted", []) if parse_date(w) >= cutoff]
        week_starts = [(ref_week - dt.timedelta(weeks=WEEKS - 1 - i)).strftime("%Y-%m-%d") for i in range(WEEKS)]
        total_nb = sum(1 for c in commits_parsed if not c[5].get("bot"))
        facts["repo"] = {
            "windowDays": RECENT_DAYS, "historyDays": hist.get("days"), "truncated": hist.get("truncated", False),
            "commits": repo_recent, "prior": repo_prior if window_covers_prior else None,
            "authors": len(recent_authors),
            "newAuthors": len([a for a in recent_authors if first_seen.get(a) and first_seen[a] >= cutoff]) if hist.get("days", 0) > RECENT_DAYS else None,
            "filesChanged": len(files_recent), "filesAdded": len(added_recent), "filesDeleted": len(deleted_recent),
            "linesAdded": added_l, "linesDeleted": deleted_l, "weekly": weekly, "weekStarts": week_starts,
            "fixShare": pct_share(sum(1 for c in commits_parsed if c[5].get("fix")), len(commits_parsed)),
            "botCommits": sum(1 for c in commits_parsed if c[5].get("bot")),
        }

        # knowledge
        inactive_cut = ref - dt.timedelta(days=INACTIVE_DAYS)
        last_active = carto.author_activity(hist)
        know = {"inactiveDays": INACTIVE_DAYS, "components": {}, "inactiveAuthors": 0}
        know["inactiveAuthors"] = sum(1 for a, d in last_active.items() if d < inactive_cut)
        for cid, pc in per_comp.items():
            st = facts["components"].setdefault(cid, {})
            total = sum(pc["owners"].values()) or 1
            active = Counter({a: n for a, n in pc["owners"].items() if last_active.get(a) and last_active[a] >= inactive_cut})
            inactive_share = pct_share(sum(n for a, n in pc["owners"].items() if a not in active), total)
            st.update({
                "commits": pc["recent"], "prior": pc["prior"] if window_covers_prior else None,
                "authors": len(pc["authors"]), "weekly": pc["weekly"], "last": pc["last"],
                "owners": [{"name": n, "share": round(100.0 * v / total), "lastActive": last_active[n].strftime("%Y-%m-%d") if n in last_active else None,
                            "active": bool(last_active.get(n) and last_active[n] >= inactive_cut)} for n, v in pc["owners"].most_common(6)],
                "busFactor": bus_factor(pc["owners"]), "activeBusFactor": bus_factor(active),
                "fixes": pc["fixes"], "fixRatio": pct_share(pc["fixes"], pc["all"]),
                "aiShare": pct_share(pc["ai90"], pc["recent"]) if pc["recent"] else None,
            })
            know["components"][cid] = {"orphanedPct": inactive_share, "activeBusFactor": st["activeBusFactor"], "busFactor": st["busFactor"]}
        facts["knowledge"] = know

        # hotspots (change frequency x size)
        hot = []
        for p, pf in per_file.items():
            if p not in loc_of or pf["commits"] < 2:
                continue
            hot.append({"path": p, "component": mapper(p), "commits": pf["commits"], "add": pf["add"], "del": pf["del"],
                        "authors": len(pf["authors"]), "loc": loc_of[p], "isNew": p in added_recent, "fixes": file_fixes.get(p, 0),
                        "ageDays": mod_of.get(p), "score": carto.hotspot_score(pf["commits"], loc_of[p])})
        hot.sort(key=lambda h: -h["score"])
        top = hot[:30]
        mx = top[0]["score"] if top else 1
        for h in top:
            h["score"] = round(100.0 * h["score"] / mx)
        facts["hotspots"] = top
        facts["recent"] = recent_list
        for p, pf in per_file.items():
            facts["files"][p] = {"commits": pf["commits"], "last": pf["last"]}

        # defects (fix-commit density over the whole history window)
        dfiles = [{"path": p, "component": mapper(p), "fixes": n, "loc": loc_of[p], "score": n * math.log2(loc_of[p] + 1)}
                  for p, n in file_fixes.items() if n >= 2 and p in loc_of]
        dfiles.sort(key=lambda x: -x["score"])
        dfiles = dfiles[:20]
        mx = dfiles[0]["score"] if dfiles else 1
        for x in dfiles:
            x["score"] = round(100.0 * x["score"] / mx)
        facts["defects"] = {"windowDays": hist.get("days"), "fixCommits": sum(1 for c in commits_parsed if c[5].get("fix")),
                            "commits": len(commits_parsed), "files": dfiles}

        # coupling
        coup = carto.coupling(hist, mapper)
        model_pairs = {(e.get("from"), e.get("to")) for e in model.get("edges", [])}
        comp_static = carto.component_graph(graph, mapper) if graph else {}
        cpairs = []
        for p in coup["components"][:60]:
            a, b = p["a"], p["b"]
            p["inModel"] = (a, b) in model_pairs or (b, a) in model_pairs
            p["inCode"] = (a, b) in comp_static or (b, a) in comp_static if graph else None
            p["hidden"] = (not p["inModel"]) and (p["inCode"] is False) and a not in external_ids and b not in external_ids \
                and p["degree"] >= 20 and p["shared"] >= 5
            cpairs.append(p)
        def stem(p):
            b = p.rsplit("/", 1)[-1].lower()
            b = b.split(".")[0]
            for pre in ("test_", "tests_"):
                if b.startswith(pre):
                    b = b[len(pre):]
            for suf in ("_test", "test", "tests", "spec", "it"):
                if b.endswith(suf) and len(b) > len(suf):
                    b = b[: -len(suf)]
            return b.rstrip("_-.")

        edge_set = {(x[0], x[1]) for x in graph["fileEdges"]} if graph else set()
        fpairs = []
        for p in coup["files"]:
            if p["degree"] < 40:
                continue
            ca, cb = carto.category(p["a"]), carto.category(p["b"])
            if "test" in (ca, cb) and stem(p["a"]) == stem(p["b"]):
                continue  # a file and its own test: expected, not insight
            if ca in ("build", "config", "docs") and cb in ("build", "config", "docs"):
                continue  # lockstep version bumps and the like
            p["compA"], p["compB"] = mapper(p["a"]), mapper(p["b"])
            p["inCode"] = ((p["a"], p["b"]) in edge_set or (p["b"], p["a"]) in edge_set) if graph else None
            p["crossDir"] = p["a"].rsplit("/", 1)[0] != p["b"].rsplit("/", 1)[0]
            fpairs.append(p)
        fpairs.sort(key=lambda p: (p["inCode"] is True, not p["crossDir"], -p["degree"], -p["shared"]))
        fpairs = fpairs[:60]
        facts["coupling"] = {"components": cpairs, "files": fpairs, "maxFilesPerCommit": carto.MAX_FILES_PER_COMMIT}

        # AI-assisted
        ai_total = sum(1 for c in commits_parsed if c[5].get("ai"))
        ai_recent = sum(1 for c in commits_parsed if c[5].get("ai") and (ref - c[1]).days < RECENT_DAYS)
        rec_nb = sum(1 for c in commits_parsed if not c[5].get("bot") and (ref - c[1]).days < RECENT_DAYS)
        months = []
        for i in range(11, -1, -1):
            y, m = ref.year, ref.month - i
            while m <= 0:
                m += 12
                y -= 1
            ai_n, tot = ai_month.get(i, [0, 0])
            months.append({"month": "%04d-%02d" % (y, m), "ai": ai_n, "total": tot})
        facts["ai"] = {
            "commits": ai_total, "share": pct_share(ai_total, total_nb), "recent": ai_recent, "recentShare": pct_share(ai_recent, rec_nb),
            "tools": [{"name": n, "commits": c} for n, c in ai_tools.most_common()], "monthly": months,
            "followUpFix": {"days": FOLLOW_UP_DAYS, "ai": follow["ai"], "human": follow["human"],
                            "aiPct": pct_share(*follow["ai"]), "humanPct": pct_share(*follow["human"])},
            "reverted": {"ai": rev["ai"], "human": rev["human"], "aiPct": pct_share(*rev["ai"]), "humanPct": pct_share(*rev["human"])},
        }

    # key-file stats for files the model names
    named = {f.get("path") for c in comps for f in c.get("keyFiles", [])}
    facts["files"] = {p: dict(facts["files"].get(p, {}), loc=loc_of.get(p)) for p in named}

    # ------------------------------------------------------------ structure (static graph)
    if graph:
        comp_static = carto.component_graph(graph, mapper)
        lidx = carto.layer_index(model)
        model_edges = model.get("edges", [])
        model_pairs = {(e.get("from"), e.get("to")) for e in model_edges}
        cedges = []
        for (a, b), v in sorted(comp_static.items(), key=lambda kv: -kv[1]["count"]):
            cedges.append({"from": a, "to": b, "count": v["count"], "example": v["example"],
                           "inModel": (a, b) in model_pairs,
                           "violation": lidx.get(a, 99) > lidx.get(b, 99) and b not in external_ids and a not in external_ids})
        scanned = defaultdict(int)
        for a, b, _ in graph.get("fileEdges", []):
            pass
        code_comps = set()
        fam_ext = (".java", ".kt", ".kts", ".groovy", ".scala", ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".vue", ".svelte", ".go", ".cs")
        for p in loc_of:
            if p.endswith(fam_ext):
                cid = mapper(p)
                if cid:
                    code_comps.add(cid)
        verification = []
        for e in model_edges:
            a, b, kind = e.get("from"), e.get("to"), e.get("kind", "calls")
            if a in external_ids or b in external_ids or a not in code_comps or b not in code_comps:
                status = "n/a"
            elif (a, b) in comp_static:
                status = "verified"
            elif kind in ("publishes", "consumes"):
                status = "n/a"
            else:
                status = "unseen"
            verification.append({"from": a, "to": b, "kind": kind, "status": status,
                                 "count": comp_static.get((a, b), {}).get("count", 0)})
        tests = set(graph.get("tests", []))
        reach = Counter()
        seen_pairs = set()
        for a, b, _ in graph.get("fileEdges", []):
            if a in tests or carto.is_test(a):
                cb = mapper(b)
                if cb and (a, cb) not in seen_pairs:
                    seen_pairs.add((a, cb))
                    reach[cb] += 1
        api = []
        for it in graph.get("api", []):
            it = dict(it)
            it["component"] = mapper(it["path"])
            if not carto.is_test(it["path"]):
                api.append(it)
        facts["structure"] = {
            "languages": graph.get("languages", {}), "filesScanned": graph.get("filesScanned"),
            "edgesResolved": len(graph.get("fileEdges", [])),
            "external": sorted(graph.get("external", {}).items(), key=lambda kv: -kv[1])[:24],
            "compEdges": cedges,
            "cycles": carto.sccs(sorted(comp_ids - external_ids), [k for k in comp_static if k[0] not in external_ids and k[1] not in external_ids]),
            "verification": verification,
            "testReach": {c: reach.get(c, 0) for c in code_comps},
            "untested": sorted(c for c in code_comps if not reach.get(c)),
            "api": api[:2000], "apiTotal": len(api),
            "fanIn": [{"path": p, "count": n, "component": mapper(p)} for p, n in graph.get("fanIn", [])[:15]],
            "commit": graph.get("commit"),
        }

    # ------------------------------------------------------------ drift
    gen = (model.get("generated") or {}).get("commit")
    facts["sinceModel"] = since_model(repo, gen, mapper) if repo else None
    for s in model.get("snippets", []):
        facts["snippets"][s["id"]] = check.snippet_status(repo, s) if repo else {"status": "unknown"}
    return facts


def load_prs(dirpath):
    out = []
    for p in sorted(glob.glob(os.path.join(dirpath, "*.json"))):
        d = load(p)
        if d and d.get("kind") == "cartograph-pr":
            out.append(d)
    out.sort(key=lambda x: x.get("analyzedAt", ""), reverse=True)
    return out[:60]


def auto_model(inv, repo):
    """Provisional model when no curated one exists: components = the directories that hold the code.

    Descends through wrapper directories (src/, packages/, a single top folder) until the
    code splits into several directories, then makes each directory holding >= 2% of
    lines a component. Clearly marked provisional in the viewer.
    """
    files = [(r[0], r[2]) for r in (inv or {}).get("files", []) if r[1] not in ("JSON", "Other") and not r[0].endswith((".lock", ".sum"))]
    total = sum(l for _, l in files) or 1
    no_descend = {"test", "tests", "spec", "specs", "examples", "example", "docs", "doc", "benchmarks", "bench", "e2e", "fixtures", "scripts"}
    wrappers = {"packages", "apps", "services", "modules", "libs", "src", "extensions", "projects", "components"}

    def children(prefix):
        g = Counter()
        for p, l in files:
            if prefix and not p.startswith(prefix + "/"):
                continue
            rest = p[len(prefix) + 1:] if prefix else p
            if "/" in rest:
                g[(prefix + "/" if prefix else "") + rest.split("/")[0]] += l
        return g

    prefix = ""
    for _ in range(5):  # descend through wrapper folders that hold nearly everything
        ch = children(prefix)
        if not ch:
            break
        top, n = ch.most_common(1)[0]
        if n >= 0.7 * sum(ch.values()) and top.split("/")[-1] not in no_descend and children(top):
            prefix = top
            continue
        break
    comps = []
    for d, n in children(prefix).most_common():
        if 100.0 * n / total < 2:
            continue
        subs = children(d)
        if d.split("/")[-1] in wrappers and subs:
            for sd, sn in subs.most_common():
                if 100.0 * sn / total >= 2:
                    comps.append((sd, sn))
        else:
            comps.append((d, n))
    if not comps and prefix:
        comps = [(prefix, 0)]
    name = os.path.basename(os.path.abspath(repo)) if repo else "repository"
    return {
        "schemaVersion": 1, "auto": True,
        "repo": {"name": name, "summary": ""},
        "generated": {"at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "commit": (inv or {}).get("commit", ""), "by": "build.py --auto (provisional)"},
        "layers": [{"id": "code", "name": "Directories", "description": "Provisional: one component per directory"}],
        "components": [{"id": d.replace("/", "-").replace(".", "-").lower().strip("-") or "root", "name": d, "layer": "code", "kind": "module", "paths": [d],
                        "summary": "", "confidence": "low", "evidence": []} for d, _ in comps[:40]],
        "edges": [], "flows": [], "snippets": [], "decisions": [], "risks": [], "glossary": [], "questions": [], "readingPath": [],
    }


def guard_facts(gdir, facts_dir, facts, issues, curated):
    """Guardrails, proposals, baseline vs current counts, replay proof, lessons and the last guard run, for the viewer."""
    cfg = load(os.path.join(gdir, "guardrails.json"))
    if not cfg:
        return None
    prop = load(os.path.join(gdir, "guardrails.proposed.json")) or {"rules": []}
    base = load(os.path.join(gdir, "baseline.json"))
    st, coup = facts.get("structure"), facts.get("coupling")
    cur = {}
    if st:
        cur["layerViolations"] = sum(1 for e in st["compEdges"] if e.get("violation"))
        cur["cycles"] = len(st["cycles"])
        cur["untestedComponents"] = len(st["untested"])
        if curated:
            cur["unmodeledDependencies"] = sum(1 for e in st["compEdges"] if not e.get("inModel"))
    if curated and coup:
        cur["hiddenDependencies"] = sum(1 for p in coup["components"] if p.get("hidden"))
    if curated:
        cur["checkErrors"] = sum(1 for i in issues if i["level"] == "error")
    today = dt.date.today()
    exceptions = []
    for ex in cfg.get("exceptions", []):
        state = "incomplete"
        if ex.get("reason") and ex.get("approvedBy") and ex.get("expires"):
            try:
                state = "valid" if dt.date.fromisoformat(ex["expires"]) >= today else "expired"
            except ValueError:
                pass
        exceptions.append(dict(ex, state=state, daysLeft=(dt.date.fromisoformat(ex["expires"]) - today).days if state != "incomplete" else None))
    return {"rules": cfg.get("rules", []), "proposed": prop.get("rules", []), "exceptions": exceptions, "judgment": cfg.get("judgment", []), "events": carto.read_events(gdir, 400), "simulated": carto.read_events(gdir, 600, simulated=True),
            "baseline": base, "current": cur, "replay": load(os.path.join(facts_dir, "guard-replay.json")),
            "lessons": load(os.path.join(facts_dir, "lessons.json")), "last": load(os.path.join(facts_dir, "guard", "last.json"))}


def build(model_path, inv_path, hist_path, viewer_path, out_path, repo, skip_checks=False, graph_path=None, prh_path=None, prs_dir=None, guard_dir=None, facts_dir=None):
    model = load(model_path)
    curated = model is not None
    inv, hist, graph = load(inv_path), load(hist_path), load(graph_path)
    if model is None:
        if not inv:
            raise SystemExit("no model at %s and no facts — run collect.py first" % model_path)
        model = auto_model(inv, repo)
        print("no curated model: using %d provisional directory components (run /cartograph-survey for a real one)" % len(model["components"]))
        skip_checks = True
    issues = [] if skip_checks else check.validate(model, repo)
    for fl in model.get("flows", []):                       # step evidence may be a list; the viewer shows the first
        for st in fl.get("steps", []):
            if isinstance(st.get("evidence"), list):
                st["evidenceAll"] = st["evidence"]
                st["evidence"] = st["evidence"][0] if st["evidence"] else None
    data = {
        "meta": {"tool": "codebase-cartographer", "builtAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
                 "schemaVersion": model.get("schemaVersion")},
        "model": model,
        "facts": derive(model, inv, hist, repo, graph, load(prh_path), load_prs(prs_dir) if prs_dir else []),
        "issues": issues,
    }
    if guard_dir:
        data["facts"]["guard"] = guard_facts(guard_dir, facts_dir or os.path.join(guard_dir, "facts"), data["facts"], issues, curated)
    with open(viewer_path, encoding="utf-8") as fh:
        html = fh.read()
    if PLACEHOLDER not in html:
        raise SystemExit("viewer template is missing the data placeholder")
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    blob = blob.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html.replace(PLACEHOLDER, blob))
    carto.save(os.path.splitext(out_path)[0] + ".json", data)
    errs = sum(1 for i in issues if i["level"] == "error")
    print("built %s (%.0f KB) — %d error(s), %d warning(s) from check" % (out_path, os.path.getsize(out_path) / 1024, errs, len(issues) - errs))
    f = data["facts"]
    if f.get("coverage"):
        print("coverage: %.1f%% of lines mapped to components" % f["coverage"]["mappedPct"])
        if f["coverage"]["mappedPct"] < 98 and f["coverage"]["unmapped"]:
            print("largest unmapped: " + ", ".join("%s (%d)" % (u["dir"], u["loc"]) for u in f["coverage"]["unmapped"][:6]))
    if f.get("structure"):
        st = f["structure"]
        unseen = [v for v in st["verification"] if v["status"] == "unseen"]
        unmodeled = [e for e in st["compEdges"] if not e["inModel"]]
        print("structure: %d component dependencies in code, %d not in the model, %d model edges unseen in imports, %d cycle(s), %d layer violation(s)" % (
            len(st["compEdges"]), len(unmodeled), len(unseen), len(st["cycles"]), sum(1 for e in st["compEdges"] if e["violation"])))
        viol = [e for e in st["compEdges"] if e["violation"]]
        if viol:
            print("  layer violations: " + ", ".join("%s -> %s (%d)" % (e["from"], e["to"], e["count"]) for e in viol[:8]) +
                  "  (a component may depend only on components in its own layer or layers listed after it)")
        if unmodeled:
            print("  in code, not in the model: " + ", ".join("%s -> %s (%d)" % (e["from"], e["to"], e["count"]) for e in unmodeled[:8]))
        for cy in st["cycles"][:3]:
            print("  cycle: " + " -> ".join(cy + cy[:1]))
    if f.get("coupling"):
        hidden = [p for p in f["coupling"]["components"] if p.get("hidden")]
        if hidden:
            print("hidden dependencies (co-change, no code or model edge): " + ", ".join("%s~%s (%d%%)" % (p["a"], p["b"], p["degree"]) for p in hidden[:5]))
    if f.get("knowledge") and f.get("components"):
        K, C = f["knowledge"]["components"], f["components"]
        single = sorted(c for c, k in K.items() if k.get("activeBusFactor", 9) <= 1 and C.get(c, {}).get("commits") and C.get(c, {}).get("owners"))
        orphan = sorted(c for c, k in K.items() if k.get("orphanedPct", 0) >= 50)
        nohuman = sorted(c for c, v in C.items() if v.get("commits") and not v.get("owners"))
        print("knowledge: %d author(s) active in 90 days; single active owner: %s; orphaned: %s%s" % (
            (f.get("repo") or {}).get("authors") or 0, ", ".join(single[:6]) or "none", ", ".join(orphan[:6]) or "none",
            ("; changed only by AI agents: " + ", ".join(nohuman[:6])) if nohuman else ""))
    g = f.get("guard")
    if g:
        active = [r for r in g["rules"] if r.get("status", "active") == "active"]
        over = [k for k, v in g["current"].items() if g.get("baseline") and k in g["baseline"].get("metrics", {}) and v > g["baseline"]["metrics"][k]]
        print("guardrails: %d active, %d proposed, %d exception(s)%s" % (len(active), len(g["proposed"]), len(g["exceptions"]),
              ("; above baseline: " + ", ".join(over)) if over else ""))
    sm = f.get("sinceModel")
    if sm and sm.get("commits"):
        print("model is %d commit(s) behind HEAD — consider /cartograph-refresh" % sm["commits"])
    return errs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--home", default=None, help="Cartographer folder (default: <repo>/.cartographer)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--sample", action="store_true", help="build toolkit/examples sample instead")
    args = ap.parse_args()

    toolkit = os.path.dirname(HERE)
    viewer = os.path.join(toolkit, "viewer", "index.html")
    if args.sample:
        ex = os.path.join(toolkit, "examples")
        out = args.out or os.path.join(ex, "dist", "cartograph-sample.html")
        build(os.path.join(ex, "sample-model.json"), os.path.join(ex, "sample-inventory.json"),
              os.path.join(ex, "sample-history.json"), viewer, out, repo=None, skip_checks=True,
              graph_path=os.path.join(ex, "sample-graph.json"), prh_path=os.path.join(ex, "sample-prs-history.json"),
              prs_dir=os.path.join(ex, "prs"), guard_dir=os.path.join(ex, "guard"), facts_dir=os.path.join(ex, "guard"))
        return 0
    repo = os.path.abspath(args.repo)
    home = args.home or carto.home(repo)
    out = args.out or os.path.join(home, "dist", "cartograph.html")
    facts = os.path.join(home, "facts")
    errs = build(os.path.join(home, "model", "model.json"), os.path.join(facts, "inventory.json"),
                 os.path.join(facts, "history.json"), viewer, out, repo,
                 graph_path=os.path.join(facts, "graph.json"), prh_path=os.path.join(facts, "prs-history.json"),
                 prs_dir=os.path.join(facts, "prs"), guard_dir=home, facts_dir=facts)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
