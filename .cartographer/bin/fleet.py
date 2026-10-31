#!/usr/bin/env python3
"""Fleet view: one page across many repositories.

  python3 fleet.py --root ~/src --collect --out fleet.html      # every git repo under ~/src
  python3 fleet.py --repos repoA repoB repoC --out fleet.html

For each repo it uses .cartographer/facts (running collect.py/graph.py/prs.py
first with --collect) and the curated model if one exists, otherwise a
provisional directory model. Output is a single HTML file plus fleet.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402
import carto  # noqa: E402
import check  # noqa: E402

PLACEHOLDER = "/*__CARTOGRAPH_FLEET__*/null"


def run(script, repo, *extra):
    return subprocess.run([sys.executable, os.path.join(HERE, script), "--repo", repo, *extra], capture_output=True, text=True)


def summarize(repo, out_dir):
    home = carto.home(repo)
    f = os.path.join(home, "facts")
    inv, hist = carto.load(os.path.join(f, "inventory.json")), carto.load(os.path.join(f, "history.json"))
    if not inv or not hist:
        return None
    graph, prh = carto.load(os.path.join(f, "graph.json")), carto.load(os.path.join(f, "prs-history.json"))
    model = carto.load(os.path.join(home, "model", "model.json"))
    curated = model is not None
    if not curated:
        model = build.auto_model(inv, repo)
    facts = build.derive(model, inv, hist, repo if curated else None, graph, prh)
    issues = check.validate(model, repo) if curated else []
    R = facts.get("repo") or {}
    comps = facts.get("components") or {}
    know = (facts.get("knowledge") or {}).get("components", {})
    active_comps = [c for c, v in comps.items() if v.get("commits")]
    st = facts.get("structure") or {}
    dist = os.path.join(home, "dist", "cartograph.html")
    link = os.path.relpath(dist, out_dir) if os.path.exists(dist) else None
    last = (facts.get("head") or {}).get("date")
    days_since = (dt.datetime.now(dt.timezone.utc) - carto.parse_date(last)).days if last else None
    hot = (facts.get("hotspots") or [{}])[0] if facts.get("hotspots") else None
    g = build.guard_facts(home, f, facts, issues, curated)
    guard = None
    if g:
        lessons = g.get("lessons") or {}
        guard = {"rules": sum(1 for r in g["rules"] if r.get("status", "active") == "active"), "proposed": len(g["proposed"]),
                 "lastOk": (g.get("last") or {}).get("ok"), "aboveBaseline": [k for k, v in g["current"].items() if g.get("baseline") and k in g["baseline"].get("metrics", {}) and v > g["baseline"]["metrics"][k]],
                 "expiredExceptions": sum(1 for e in g["exceptions"] if e["state"] != "valid"),
                 "gaps": sum(1 for c in lessons.get("classes", []) if c.get("coverage") == "gap")}
    return {
        "name": (model.get("repo") or {}).get("name") if curated else os.path.basename(repo.rstrip("/")),
        "path": repo, "remote": inv.get("remote"), "link": link, "lastCommit": last, "daysSinceCommit": days_since,
        "languages": [{"name": l["name"], "pct": l["pct"]} for l in (facts.get("inventory") or {}).get("languages", [])[:4]],
        "files": inv["totalFiles"], "loc": inv["totalLoc"],
        "commits": R.get("commits"), "prior": R.get("prior"), "authors": R.get("authors"),
        "weekly": R.get("weekly"), "fixShare": R.get("fixShare"),
        "aiShare": (facts.get("ai") or {}).get("recentShare"), "aiTools": [t["name"] for t in (facts.get("ai") or {}).get("tools", [])[:3]],
        "busFactor1": sorted(c for c in active_comps if comps[c].get("activeBusFactor", 9) <= 1) if (R.get("authors") or 0) > 2 else [],
        "orphaned": sorted(c for c, k in know.items() if k.get("orphanedPct", 0) >= 50 and comps.get(c, {}).get("loc", 0) > 0),
        "components": len(model.get("components", [])), "curated": curated,
        "coverage": (facts.get("coverage") or {}).get("mappedPct"),
        "behind": (facts.get("sinceModel") or {}).get("commits") if curated else None,
        "checkErrors": sum(1 for i in issues if i["level"] == "error") if curated else None,
        "hidden": sum(1 for p in (facts.get("coupling") or {}).get("components", []) if p.get("hidden")),
        "cycles": len(st.get("cycles", [])) if st else None, "violations": sum(1 for e in st.get("compEdges", []) if e.get("violation")) if st else None,
        "untested": len(st.get("untested", [])) if st else None, "interfaces": st.get("apiTotal") if st else None,
        "topHotspot": hot.get("path") if hot else None, "guard": guard,
        "prs": {"merged": prh.get("merged"), "medianLines": prh.get("medianLines"), "cycle": (prh.get("cycleHours") or {}).get("median"),
                "lead": (prh.get("leadHours") or {}).get("median"), "open": prh.get("open"), "stale": len(prh.get("stale", []))} if prh else None,
    }


def find_repos(root):
    out = []
    for dirpath, dirnames, _files in os.walk(root):
        if ".git" in dirnames or os.path.isfile(os.path.join(dirpath, ".git")):
            out.append(dirpath)
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "vendor")]
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repos", nargs="*", default=[])
    ap.add_argument("--root", help="scan for git repositories under this directory")
    ap.add_argument("--collect", action="store_true", help="run collect.py, graph.py and prs.py --no-api where facts are missing or stale")
    ap.add_argument("--refresh", action="store_true", help="re-run collection for every repo")
    ap.add_argument("--out", default="fleet.html")
    ap.add_argument("--title", default="Repository fleet")
    a = ap.parse_args()
    repos = [os.path.abspath(r) for r in a.repos] + (find_repos(os.path.abspath(a.root)) if a.root else [])
    out_dir = os.path.dirname(os.path.abspath(a.out))
    rows = []
    for r in repos:
        facts = os.path.join(carto.home(r), "facts", "history.json")
        if a.refresh or (a.collect and not os.path.exists(facts)):
            for script, extra in (("collect.py", ()), ("graph.py", ()), ("prs.py", ("--no-api",))):
                res = run(script, r, *extra)
                if res.returncode != 0:
                    print("  %s failed for %s: %s" % (script, r, res.stderr.strip()[-200:]))
                    break
        dist = os.path.join(carto.home(r), "dist", "cartograph.html")
        if (a.collect or a.refresh) and os.path.exists(facts) and (a.refresh or not os.path.exists(dist)):
            run("build.py", r)
        s = summarize(r, out_dir)
        if s:
            rows.append(s)
            print("%-32s %6s lines  %4s commits/90d  AI %5s%%  %s" % (s["name"][:32], s["loc"], s["commits"], s["aiShare"], "curated model" if s["curated"] else "provisional"))
        else:
            print("%-32s skipped (no facts; use --collect)" % os.path.basename(r))
    data = {"title": a.title, "builtAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "repos": rows}
    with open(os.path.join(os.path.dirname(HERE), "viewer", "fleet.html"), encoding="utf-8") as fh:
        html = fh.read()
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(html.replace(PLACEHOLDER, blob))
    carto.save(os.path.splitext(a.out)[0] + ".json", data)
    print("wrote %s (%d repos)" % (a.out, len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
