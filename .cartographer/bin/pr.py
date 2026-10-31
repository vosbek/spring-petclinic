#!/usr/bin/env python3
"""PR lens: explain a pull request, commit or range in terms of the system.

  python3 .cartographer/bin/pr.py --pr 123              # fetch refs/pull/123/head, diff against the default branch
  python3 .cartographer/bin/pr.py --merged 123          # a merged PR found in history (squash "(#123)" or merge commit)
  python3 .cartographer/bin/pr.py --commit <sha>        # one commit
  python3 .cartographer/bin/pr.py --base v2.3 --head v2.4 --id release-2.4   # a release or any range
  ... --markdown comment.md                             # also write a PR comment

Writes .cartographer/facts/prs/<id>.json (shown on the viewer's Pull requests page after build.py).

What it computes (all deterministic):
  components touched · semantic grouping (API surface, schema, code, tests, config, docs, mechanical)
  review order · key hunks · interface changes · new cross-component dependencies and layer violations
  missed co-changes (files that historically change with these files but are not in the PR)
  area context (hotspots, defect density, knowledge concentration, risks) · suggested reviewers
  blast radius (importers, inbound model edges, flows) · test reach · model drift (pinned snippets, cited lines)
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
import build  # noqa: E402
import carto  # noqa: E402
import graph as graphmod  # noqa: E402

ORDER = ["api", "schema", "code", "test", "build", "config", "docs", "mechanical"]
GROUP_LABEL = {"api": "Interface contracts", "schema": "Schema & migrations", "code": "Code", "test": "Tests",
               "build": "Build & dependencies", "config": "Configuration", "docs": "Docs", "mechanical": "Mechanical (moves, renames, whitespace)"}
LEVEL_ORDER = {"high": 0, "medium": 1, "info": 2}


def git(repo, *a, check=True):
    return carto.git(repo, *a, check=check)


def default_branch(repo):
    ref = git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False)
    if ref:
        return ref.strip().replace("refs/remotes/", "")
    for b in ("origin/main", "origin/master", "main", "master"):
        if git(repo, "rev-parse", "-q", "--verify", b, check=False):
            return b
    return "HEAD"


def owner_repo(remote):
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(\.git)?$", remote or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def gh_json(path):
    try:
        out = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def find_merged(repo, n):
    raw = git(repo, "log", "--format=%H%x1f%P%x1f%s", "-F", "--grep=(#%d)" % n, "--grep=Merge pull request #%d " % n, "-n", "20")
    for line in raw.splitlines():
        sha, parents, subject = line.split("\x1f")
        ps = parents.split()
        if subject.startswith("Merge pull request #%d " % n) and len(ps) == 2:
            return ps[0], sha, subject
        if subject.rstrip().endswith("(#%d)" % n):
            return ps[0] if ps else sha + "^", sha, subject
    raise SystemExit("PR #%d not found in history (looked for '(#%d)' and 'Merge pull request #%d')" % (n, n, n))


# ----------------------------------------------------------------------------- diff parsing

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_diff(text):
    files, cur, hunk = {}, None, None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = {"old": m.group(1), "new": m.group(2), "hunks": []} if m else None
            if cur:
                files[cur["new"]] = cur
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("rename from "):
            cur["old"] = line[12:]
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        else:
            m = HUNK.match(line)
            if m:
                hunk = {"oldStart": int(m.group(1)), "oldLen": int(m.group(2) or 1), "newStart": int(m.group(3)),
                        "newLen": int(m.group(4) or 1), "header": m.group(5).strip(), "lines": []}
                cur["hunks"].append(hunk)
            elif hunk is not None and line[:1] in ("+", "-", " "):
                hunk["lines"].append(line)
    return files


def parse_numstat(text):
    out = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) == 3:
            a, d, p = cols
            p = normalize(p)
            out[p] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    return out


def normalize(p):
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", p)
    if m:
        pre, _old, new, post = m.groups()
        return re.sub(r"/+", "/", pre + new + post).strip("/")
    return p.split(" => ", 1)[1] if " => " in p else p


# ----------------------------------------------------------------------------- import resolution for added lines

class Resolver:
    """Resolve an import found on an added diff line to an in-repo file (best effort, current tree)."""

    def __init__(self, root):
        self.repo = graphmod.Repo(root, carto.DEFAULT_EXCLUDES)
        jvm = [p for p in self.repo.files if p.endswith((".java", ".kt", ".groovy", ".scala"))]
        py = [p for p in self.repo.files if p.endswith(".py")]
        self.fqn, self.pkg = graphmod.jvm_index(self.repo, jvm) if jvm else ({}, {})
        self.pyidx = graphmod.py_index(py) if py else {}

    def resolve(self, path, line):
        s = line[1:].strip()
        m = re.match(r"import\s+(?:static\s+)?([\w.]+)(\.\*)?\s*;?$", s)
        if m and path.endswith((".java", ".kt", ".groovy", ".scala")):
            name = m.group(1)
            if m.group(2) and name in self.pkg:
                return self.pkg[name][0]
            parts = name.split(".")
            for cut in range(0, 3):
                c = ".".join(parts[:len(parts) - cut])
                if c in self.fqn:
                    return self.fqn[c]
            return None
        if path.endswith(".py"):
            m = re.match(r"from\s+(\.*)([\w.]*)\s+import\s+(.+)", s) or re.match(r"import\s+([\w.]+)", s)
            if not m:
                return None
            if len(m.groups()) == 3:
                dots, mod = m.group(1), m.group(2)
                if dots:
                    base = posixpath.dirname(path)
                    for _ in range(len(dots) - 1):
                        base = posixpath.dirname(base)
                    rel = (base + "/" + mod.replace(".", "/")).strip("/")
                    for c in (rel + ".py", rel + "/__init__.py"):
                        if c in self.repo.fileset:
                            return c
                    return None
                name = mod
            else:
                name = m.group(1)
            parts = name.split(".")
            for cut in range(len(parts), 0, -1):
                hit = graphmod.py_resolve(self.pyidx, ".".join(parts[:cut]), path)
                if hit:
                    return hit
            return None
        if path.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".vue", ".svelte")):
            m = re.search(r"""(?:from|require\(|import\()\s*['"`]([^'"`]+)['"`]""", s) or re.match(r"""import\s+['"`]([^'"`]+)['"`]""", s)
            if m and m.group(1).startswith("."):
                return graphmod.js_try(self.repo, posixpath.join(posixpath.dirname(path), m.group(1)))
        return None


IMPORT_LINE = re.compile(r"^\+\s*(import\s|from\s+\S+\s+import\s|.*\brequire\(|.*\bfrom\s+['\"])")


def scan_interfaces(lines, path):
    """Interface declarations on added/removed lines."""
    fam = graphmod.EXT.get(posixpath.splitext(path)[1].lower())
    out = []
    for raw in lines:
        sign = raw[:1]
        if sign not in "+-":
            continue
        s = raw[1:]
        if fam == "jvm":
            m = graphmod.SPRING_MAP.search(s)
            if m:
                out.append({"sign": sign, "kind": "http", "method": m.group(1).upper() if m.group(1) != "Request" else "ANY", "name": m.group(2) or "(class path)"})
        for rfam, kind, rx, mg, ng in graphmod.ROUTE_RULES:
            if rfam != fam:
                continue
            m = rx.search(s)
            if m:
                out.append({"sign": sign, "kind": kind, "method": m.group(mg).upper() if mg else None, "name": (m.group(ng) if ng else "task") or ""})
    return out


# ----------------------------------------------------------------------------- analysis

def analyze(repo, base, head, ident, title=None, meta=None, model=None):
    root = repo
    home = carto.home(root)
    facts_dir = os.path.join(home, "facts")
    inv = carto.load(os.path.join(facts_dir, "inventory.json"))
    hist = carto.load(os.path.join(facts_dir, "history.json"))
    graph = carto.load(os.path.join(facts_dir, "graph.json"))
    if not inv or not hist:
        raise SystemExit("run collect.py first (and graph.py for blast radius and test reach)")
    model = model or carto.load(os.path.join(home, "model", "model.json")) or build.auto_model(inv, root)
    mapper = carto.Mapper(model.get("components", []))
    comp = {c["id"]: c for c in model.get("components", [])}
    lidx = carto.layer_index(model)
    derived = build.derive(model, inv, hist, None, graph)

    mb = git(root, "merge-base", base, head, check=False)
    mb = (mb or base).strip()
    names = git(root, "diff", "-M", "--name-status", mb, head)
    numstat = parse_numstat(git(root, "diff", "-M", "--numstat", mb, head))
    ws = parse_numstat(git(root, "diff", "-M", "-w", "--ignore-blank-lines", "--numstat", mb, head))
    diff = parse_diff(git(root, "diff", "-M", "-U3", "--no-color", mb, head))
    log = git(root, "log", "--no-merges", "--format=%x1e%h%x1f%aN%x1f%aE%x1f%aI%x1f%s%x1f%b", mb + ".." + head)

    commits = []
    for block in log.split("\x1e"):
        if not block.strip():
            continue
        parts = block.split("\x1f")
        if len(parts) < 6:
            continue
        flags = carto.classify(parts[4], parts[5], parts[1], parts[2])
        commits.append({"sha": parts[0], "author": parts[1], "date": parts[3][:10], "subject": parts[4], "ai": flags.get("ai"), "fix": bool(flags.get("fix"))})
    pr_authors = {c["author"] for c in commits}
    if meta and meta.get("author"):
        pr_authors.add(meta["author"])

    hotspots = {h["path"]: i + 1 for i, h in enumerate(derived.get("hotspots") or [])}
    defect_files = {d["path"]: d["fixes"] for d in (derived.get("defects") or {}).get("files", [])}
    snippets = model.get("snippets", [])
    evidence = []
    for c in model.get("components", []):
        for e in c.get("evidence", []):
            evidence.append(("component " + c["name"], e))
    for r in model.get("risks", []):
        for e in r.get("evidence", []):
            evidence.append(("risk: " + r["title"], e))
    for d in model.get("decisions", []):
        for e in d.get("evidence", []):
            evidence.append(("decision: " + d["title"], e))

    resolver = Resolver(root) if graph else None
    static_pairs = set(carto.component_graph(graph, mapper).keys()) if graph else set()
    model_pairs = {(e.get("from"), e.get("to")) for e in model.get("edges", [])}

    files = []
    changed = set()
    for line in names.splitlines():
        cols = line.split("\t")
        if not cols or not cols[0]:
            continue
        st = cols[0][0]
        path = cols[-1]
        old = cols[1] if st in ("R", "C") else path
        sim = int(cols[0][1:]) if st in ("R", "C") and cols[0][1:].isdigit() else None
        add, dele = numstat.get(path, (0, 0))
        wsa, wsd = ws.get(path, (add, dele))
        cat = carto.category(path)
        if path.startswith(".cartographer/"):   # the toolkit and its model are tooling, not product code
            cat = "config" if path.endswith(".json") else "build"
        mechanical = (st == "R" and (sim or 0) >= 95 and add + dele <= 4) or ((add + dele) > 0 and wsa + wsd == 0)
        f = {"path": path, "old": old if old != path else None, "status": st, "similarity": sim, "add": add, "del": dele,
             "category": cat, "group": "mechanical" if mechanical else cat, "component": mapper(path),
             "whitespaceOnly": (add + dele) > 0 and wsa + wsd == 0, "hotspotRank": hotspots.get(path), "fixes": defect_files.get(path, 0),
             "interfaces": [], "newDeps": [], "snippets": [], "evidence": []}
        d = diff.get(path)
        if d:
            all_lines = [l for h in d["hunks"] for l in h["lines"]]
            f["interfaces"] = scan_interfaces(all_lines, path)
            if f["interfaces"] and f["group"] == "code":
                f["group"] = "api"
            if resolver and cat == "code":
                for h in d["hunks"]:
                    n = h["newStart"]
                    for l in h["lines"]:
                        if l.startswith("+") and IMPORT_LINE.match(l):
                            tgt = resolver.resolve(path, l)
                            tc = mapper(tgt) if tgt else None
                            fc = f["component"]
                            if tc and fc and tc != fc and (fc, tc) not in static_pairs:
                                f["newDeps"].append({"to": tc, "file": tgt, "line": n, "inModel": (fc, tc) in model_pairs,
                                                     "violation": lidx.get(fc, 99) > lidx.get(tc, 99) and comp.get(tc, {}).get("layer") != "external"})
                        if not l.startswith("-"):
                            n += 1
            old_path = f["old"] or path
            for sn in snippets:
                if sn.get("path") == old_path:
                    for h in d["hunks"]:
                        lo, hi = h["oldStart"], h["oldStart"] + max(h["oldLen"], 1) - 1
                        if lo <= int(sn.get("end", 0)) and hi >= int(sn.get("start", 0)):
                            f["snippets"].append(sn["id"])
                            break
            for label, e in evidence:
                if e.get("path") == old_path and e.get("line"):
                    for h in d["hunks"]:
                        lo, hi = h["oldStart"], h["oldStart"] + max(h["oldLen"], 1) - 1
                        if lo <= int(e["line"]) <= hi:
                            f["evidence"].append({"label": label, "line": e["line"]})
                            break
        files.append(f)
        changed.add(path)
        if f["old"]:
            changed.add(f["old"])

    # ------------------------------------------------------------- missed co-changes
    coup = carto.coupling(hist)
    loc_of = {r[0]: r[2] for r in inv["files"]}
    missed = {}
    for f in files:
        if f["category"] in ("docs",) or f["group"] == "mechanical":
            continue
        for partner, prob, shared, revs in carto.partners(coup, f["path"], 0.5):
            if partner in changed or partner not in loc_of or carto.category(partner) == "docs":
                continue
            if partner not in missed or missed[partner]["probability"] < prob:
                missed[partner] = {"file": partner, "with": f["path"], "probability": prob, "shared": shared, "revs": revs,
                                   "component": mapper(partner)}
    missed_list = sorted(missed.values(), key=lambda x: (-x["probability"], -x["shared"]))[:8]

    # ------------------------------------------------------------- reviewers
    activity = carto.author_activity(hist)
    ref = carto.parse_date(hist.get("commitDate") or hist["commits"][0][1]) if hist.get("commits") else dt.datetime.now(dt.timezone.utc)
    score, nfiles = Counter(), defaultdict(set)
    for _sha, _d, author, _s, chs, flags in carto.iter_commits(hist):
        if flags.get("bot") or flags.get("agent"):
            continue
        for p, _, _ in chs:
            if p in changed:
                score[author] += 1
                nfiles[author].add(p)
    reviewers = []
    for a, n in score.most_common():
        if a in pr_authors:
            continue
        last = activity.get(a)
        if not last or (ref - last).days > 90:
            continue
        reviewers.append({"name": a, "commits": n, "files": len(nfiles[a]), "lastActive": last.strftime("%Y-%m-%d")})
        if len(reviewers) == 3:
            break

    # ------------------------------------------------------------- components, blast radius, tests
    touched = Counter()
    for f in files:
        if f["component"] and f["group"] != "mechanical":
            touched[f["component"]] += f["add"] + f["del"]
    blast_files, blast_comps, test_hits = set(), Counter(), set()
    tests = set((graph or {}).get("tests", []))
    if graph:
        rev = defaultdict(set)
        for a, b, _ in graph["fileEdges"]:
            rev[b].add(a)
        for f in files:
            if f["category"] != "code" or f["group"] == "mechanical":
                continue
            for imp in rev.get(f["path"], ()):
                if imp in tests or carto.is_test(imp):
                    test_hits.add(imp)
                    continue
                if imp in changed:
                    continue
                else:
                    blast_files.add(imp)
                    c = mapper(imp)
                    if c and c not in touched:
                        blast_comps[c] += 1
    inbound = Counter()
    for e in model.get("edges", []):
        if e.get("to") in touched and e.get("from") not in touched:
            inbound[e["from"]] += 1
    flows = [{"id": fl["id"], "name": fl["name"]} for fl in model.get("flows", [])
             if any(s.get("component") in touched for s in fl.get("steps", []))]
    code_changed = [f for f in files if f["category"] == "code" and f["group"] != "mechanical" and f["status"] != "D"]
    tests_changed = [f for f in files if f["category"] == "test"]
    untested = []
    if graph:
        covered_targets = {b for a, b, _ in graph["fileEdges"] if a in tests or carto.is_test(a)}
        untested = [f["path"] for f in code_changed if f["path"] not in covered_targets and f["add"] + f["del"] >= 10]

    # ------------------------------------------------------------- key hunks
    risky_snips = {s["id"] for s in snippets if s.get("kind") in ("gotcha", "invariant")}
    cand = []
    for f in files:
        if f["group"] in ("mechanical", "docs", "test") or f["category"] == "docs":
            continue
        d = diff.get(f["path"])
        if not d:
            continue
        for h in d["hunks"]:
            changed_lines = sum(1 for l in h["lines"] if l[:1] in "+-")
            if not changed_lines:
                continue
            w = changed_lines
            if f["snippets"]:
                w *= 3
            if f["interfaces"] or f["newDeps"]:
                w *= 2
            if f["hotspotRank"] or f["fixes"]:
                w *= 1.5
            if f["category"] in ("config", "build"):
                w *= 0.5
            cand.append((w, f["path"], h))
    cand.sort(key=lambda x: -x[0])
    key_hunks = []
    for w, path, h in cand[:5]:
        lines = h["lines"]
        trimmed = len(lines) > 60
        key_hunks.append({"path": path, "oldStart": h["oldStart"], "newStart": h["newStart"], "header": h["header"],
                          "lines": lines[:60], "trimmed": trimmed, "component": mapper(path)})

    # ------------------------------------------------------------- signals
    signals = []
    snips_hit = sorted({s for f in files for s in f["snippets"]})
    sn_by = {s["id"]: s for s in snippets}
    for sid in snips_hit:
        s = sn_by[sid]
        signals.append({"level": "high" if sid in risky_snips else "medium", "kind": "model-drift",
                        "title": "Changes pinned snippet “%s”" % s["title"],
                        "detail": "%s:%s–%s is %s in the model. Re-check the annotations, then re-extract with /cartograph-snippets." % (s["path"], s["start"], s["end"], s.get("kind", "key code")),
                        "refs": [s["path"]]})
    ev_hits = [(f["path"], e) for f in files for e in f["evidence"]]
    if ev_hits:
        signals.append({"level": "medium", "kind": "model-drift", "title": "Touches %d line(s) the model cites as evidence" % len(ev_hits),
                        "detail": "; ".join("%s:%s (%s)" % (p, e["line"], e["label"]) for p, e in ev_hits[:4]), "refs": [p for p, _ in ev_hits[:4]]})
    for f in files:
        for nd in f["newDeps"]:
            lvl = "high" if nd["violation"] else "medium"
            signals.append({"level": lvl, "kind": "new-dependency",
                            "title": "New dependency %s → %s%s" % (comp.get(f["component"], {}).get("name", f["component"]), comp.get(nd["to"], {}).get("name", nd["to"]),
                                                                       " (against layering)" if nd["violation"] else ""),
                            "detail": "%s:%d imports %s. %s" % (f["path"], nd["line"], nd["file"], "Already in the model." if nd["inModel"] else "Not in the model or the current code graph."),
                            "refs": [f["path"]]})
    if missed_list:
        top = missed_list[0]
        signals.append({"level": "medium", "kind": "missed-co-change",
                        "title": "%d file(s) usually change with these but aren't in this PR" % len(missed_list),
                        "detail": "%s changed with %s in %d%% of its last %d commits." % (top["file"], top["with"], round(top["probability"] * 100), top["revs"]),
                        "refs": [m["file"] for m in missed_list]})
    removed_ifaces = [(f["path"], i) for f in files for i in f["interfaces"] if i["sign"] == "-"]
    added_ifaces = [(f["path"], i) for f in files for i in f["interfaces"] if i["sign"] == "+"]
    if removed_ifaces:
        signals.append({"level": "high", "kind": "interface", "title": "Removes or changes %d interface declaration(s)" % len(removed_ifaces),
                        "detail": ", ".join("%s %s" % (i.get("method") or i["kind"], i["name"]) for _, i in removed_ifaces[:5]) + ". Check consumers before merging.",
                        "refs": [p for p, _ in removed_ifaces[:5]]})
    elif added_ifaces:
        signals.append({"level": "info", "kind": "interface", "title": "Adds %d interface declaration(s)" % len(added_ifaces),
                        "detail": ", ".join("%s %s" % (i.get("method") or i["kind"], i["name"]) for _, i in added_ifaces[:5]), "refs": [p for p, _ in added_ifaces[:5]]})
    schema = [f for f in files if f["category"] == "schema" and f["group"] != "mechanical"]
    if schema:
        signals.append({"level": "medium", "kind": "schema", "title": "Schema or migration change (%d file%s)" % (len(schema), "" if len(schema) == 1 else "s"),
                        "detail": ", ".join(f["path"] for f in schema[:4]), "refs": [f["path"] for f in schema[:4]]})
    know = (derived.get("knowledge") or {}).get("components", {})
    for cid in touched:
        k = know.get(cid)
        cf = (derived.get("components") or {}).get(cid, {})
        if not k:
            continue
        cname = comp.get(cid, {}).get("name", cid)
        active = [o for o in cf.get("owners", []) if o.get("active")]
        if k["orphanedPct"] >= 50:
            signals.append({"level": "medium", "kind": "knowledge", "title": "%s is largely orphaned" % cname,
                            "detail": "%d%% of its changes in the last year came from people inactive for 90+ days.%s" % (
                                k["orphanedPct"], (" Most active now: " + ", ".join(o["name"] for o in active[:2]) + ".") if active else " No one active has changed it recently."),
                            "refs": []})
        elif k["activeBusFactor"] <= 1 and active:
            top = active[0]
            self_review = top["name"] in pr_authors
            signals.append({"level": "medium" if self_review else "info", "kind": "knowledge",
                            "title": "One active person holds most of the knowledge of %s" % cname,
                            "detail": "%s made %d%% of its changes in the last year%s" % (
                                top["name"], top["share"], " and wrote this PR, so an independent reviewer who knows the area is scarce." if self_review else "; they are the natural reviewer."),
                            "refs": []})
    hot_touched = [f for f in files if f["hotspotRank"] and f["group"] not in ("mechanical", "test", "docs")]
    if hot_touched:
        signals.append({"level": "info", "kind": "hotspot", "title": "Touches %d hotspot file(s)" % len(hot_touched),
                        "detail": ", ".join("%s (#%d)" % (posixpath.basename(f["path"]), f["hotspotRank"]) for f in hot_touched[:4]) + " — large and frequently changed.",
                        "refs": [f["path"] for f in hot_touched[:4]]})
    defect_touched = [f for f in files if f["fixes"] >= 3 and f["group"] not in ("mechanical", "test", "docs")]
    if defect_touched:
        signals.append({"level": "info", "kind": "defects", "title": "Touches defect-dense code",
                        "detail": ", ".join("%s (%d fixes in a year)" % (posixpath.basename(f["path"]), f["fixes"]) for f in defect_touched[:4]),
                        "refs": [f["path"] for f in defect_touched[:4]]})
    code_lines = sum(f["add"] + f["del"] for f in code_changed)
    if untested and not tests_changed and code_lines >= 20:
        signals.append({"level": "medium", "kind": "tests", "title": "No tests changed, and %d changed file(s) have no test importing them" % len(untested),
                        "detail": ", ".join(untested[:4]), "refs": untested[:4]})
    for rk in model.get("risks", []):
        if set(rk.get("components", [])) & set(touched):
            signals.append({"level": "info", "kind": "risk", "title": "Area has a known %s risk: %s" % (rk.get("severity", ""), rk["title"]),
                            "detail": rk.get("detail", ""), "refs": [e["path"] for e in rk.get("evidence", [])][:2]})
    ai_commits = [c for c in commits if c["ai"]]
    if ai_commits:
        signals.append({"level": "info", "kind": "ai", "title": "%d of %d commits are AI-assisted (%s)" % (len(ai_commits), len(commits), ", ".join(sorted({c['ai'] for c in ai_commits}))),
                        "detail": "Review these with the same scrutiny; the test delta is %d test file(s)." % len(tests_changed), "refs": []})
    signals.sort(key=lambda s: LEVEL_ORDER[s["level"]])

    # ------------------------------------------------------------- review order
    def sort_key(f):
        g = ORDER.index(f["group"]) if f["group"] in ORDER else len(ORDER)
        return (g, lidx.get(f["component"], 99) if f["group"] == "code" else 0, -(f["add"] + f["del"]))
    files.sort(key=sort_key)
    groups = []
    for g in ORDER:
        fs = [f for f in files if f["group"] == g]
        if fs:
            groups.append({"group": g, "label": GROUP_LABEL[g], "files": [f["path"] for f in fs],
                           "add": sum(f["add"] for f in fs), "del": sum(f["del"] for f in fs)})

    total = sum(f["add"] + f["del"] for f in files if f["group"] != "mechanical")
    return {
        "kind": "cartograph-pr", "id": ident, "analyzedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "title": title or (commits[0]["subject"] if len(commits) == 1 else "%s..%s" % (base, head)),
        "meta": meta or {}, "base": base, "head": head, "mergeBase": mb[:12],
        "stats": {"files": len(files), "add": sum(f["add"] for f in files), "del": sum(f["del"] for f in files),
                  "commits": len(commits), "aiCommits": len(ai_commits), "testFiles": len(tests_changed),
                  "mechanicalFiles": sum(1 for f in files if f["group"] == "mechanical"), "size": carto.size_class(total)},
        "commits": commits[:50],
        "touched": [{"component": c, "lines": n, "name": comp.get(c, {}).get("name", c)} for c, n in touched.most_common()],
        "unmappedFiles": sum(1 for f in files if not f["component"]),
        "files": files, "groups": groups, "keyHunks": key_hunks, "signals": signals,
        "missedCoChanges": missed_list, "reviewers": reviewers,
        "blast": {"components": [{"component": c, "importers": n, "name": comp.get(c, {}).get("name", c)} for c, n in blast_comps.most_common(10)],
                  "importerFiles": len(blast_files), "inboundModelEdges": [{"component": c, "name": comp.get(c, {}).get("name", c)} for c in inbound],
                  "flows": flows, "testsReaching": len(test_hits), "testsReachingChanged": len(test_hits & changed), "untested": untested},
        "modelDrift": {"snippets": snips_hit, "evidence": [{"path": p, "line": e["line"], "label": e["label"]} for p, e in ev_hits]},
        "autoModel": bool(model.get("auto")),
        "guard": guard_result(root, base, head),
    }


def guard_result(root, base, head):
    """Guardrail verdict for the same range, when the repo has rules (see guard.py)."""
    if not os.path.exists(os.path.join(carto.home(root), "guardrails.json")):
        return None
    import guard
    res = guard.run(root, "range", base, head, log=False)
    return res if res["rules"] else None


def markdown(a, viewer_url=None):
    s = a["stats"]
    L = ["<!-- cartographer -->"]
    L.append("### Cartographer · %s" % a["title"])
    L.append("**%s** · %d files · +%d −%d · %d commit%s%s" % (
        s["size"], s["files"], s["add"], s["del"], s["commits"], "" if s["commits"] == 1 else "s",
        " (%d AI-assisted)" % s["aiCommits"] if s["aiCommits"] else ""))
    if a["touched"]:
        L.append("**Touches:** " + ", ".join("%s (%d lines)" % (t["name"], t["lines"]) for t in a["touched"][:6]))
    if a["blast"]["components"] or a["blast"]["flows"]:
        bits = []
        if a["blast"]["components"]:
            bits.append("imported by " + ", ".join(b["name"] for b in a["blast"]["components"][:5]))
        if a["blast"]["flows"]:
            bits.append("on flows " + ", ".join(f["name"] for f in a["blast"]["flows"][:4]))
        L.append("**Blast radius:** " + "; ".join(bits))
    icon = {"high": "\U0001F7E5", "medium": "\U0001F7E7", "info": "⬜"}
    if a["signals"]:
        L.append("")
        L.append("| | Signal |")
        L.append("|---|---|")
        for sg in a["signals"][:8]:
            L.append("| %s | **%s** — %s |" % (icon[sg["level"]], sg["title"], sg["detail"].replace("|", "\\|")))
    g = a.get("guard")
    if g:
        import guard
        L.append("")
        L.extend(guard.markdown_report(g).split("\n")[1:])
    if a["missedCoChanges"]:
        L.append("")
        L.append("<details><summary>Usually changed together, not in this PR</summary>\n")
        for m in a["missedCoChanges"]:
            L.append("- `%s` — %d%% of the time `%s` changes (%d of %d commits)" % (m["file"], round(m["probability"] * 100), m["with"], m["shared"], m["revs"]))
        L.append("\n</details>")
    if a["reviewers"]:
        L.append("")
        L.append("**Suggested reviewers:** " + ", ".join("%s (%d commits to these files, active %s)" % (r["name"], r["commits"], r["lastActive"]) for r in a["reviewers"]))
    L.append("")
    L.append("<details><summary>Review order</summary>\n")
    for g in a["groups"]:
        L.append("**%s** (+%d −%d)" % (g["label"], g["add"], g["del"]))
        for p in g["files"][:12]:
            L.append("- `%s`" % p)
        if len(g["files"]) > 12:
            L.append("- … %d more" % (len(g["files"]) - 12))
    L.append("\n</details>")
    if viewer_url:
        L.append("")
        L.append("[Open the full PR view](%s#/pr/%s)" % (viewer_url, a["id"]))
    L.append("")
    L.append("<sub>Generated by Codebase Cartographer from git history, the import graph and the repo's model. Numbers are computed, not estimated.</sub>")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pr", type=int, help="open PR number (fetches refs/pull/N/head)")
    g.add_argument("--merged", type=int, help="merged PR number found in history")
    g.add_argument("--commit")
    g.add_argument("--head", help="head ref for a range (use with --base)")
    ap.add_argument("--base", help="base ref (default: the default branch)")
    ap.add_argument("--id", help="identifier for the output file")
    ap.add_argument("--title")
    ap.add_argument("--markdown", help="also write a PR comment to this path")
    ap.add_argument("--viewer-url", help="URL of the published viewer, for the comment link")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    meta = {}
    remote = git(repo, "remote", "get-url", "origin", check=False)
    owner, name = owner_repo(remote or "")
    if a.pr:
        ref = "refs/cartograph/pr-%d" % a.pr
        if git(repo, "fetch", "-q", "origin", "pull/%d/head:%s" % (a.pr, ref), check=False) is None:
            raise SystemExit("could not fetch pull/%d/head from origin" % a.pr)
        head, base, ident = ref, a.base or default_branch(repo), str(a.pr)
        if owner:
            pr = gh_json("repos/%s/%s/pulls/%d" % (owner, name, a.pr))
            if pr:
                meta = {"number": a.pr, "url": pr.get("html_url"), "author": (pr.get("user") or {}).get("login"), "state": pr.get("state"),
                        "createdAt": pr.get("created_at"), "mergedAt": pr.get("merged_at"), "baseRef": (pr.get("base") or {}).get("ref"),
                        "headRef": (pr.get("head") or {}).get("ref"), "body": (pr.get("body") or "")[:600]}
                a.title = a.title or pr.get("title")
                if not a.base and meta.get("baseRef"):
                    base = "origin/" + meta["baseRef"]
        meta.setdefault("number", a.pr)
    elif a.merged:
        base, head, subject = find_merged(repo, a.merged)
        ident = str(a.merged)
        a.title = a.title or re.sub(r"\s*\(#\d+\)\s*$", "", subject)
        meta = {"number": a.merged, "merged": True}
        if owner:
            meta["url"] = "https://github.com/%s/%s/pull/%d" % (owner, name, a.merged)
    elif a.commit:
        head, base = a.commit, a.commit + "^"
        ident = a.id or git(repo, "rev-parse", "--short", a.commit).strip()
    else:
        head, base = a.head, a.base or default_branch(repo)
        ident = a.id or re.sub(r"[^\w.-]+", "-", "%s..%s" % (base, head)).strip("-")
    ident = a.id or ident
    res = analyze(repo, base, head, ident, a.title, meta)
    out = os.path.join(carto.home(repo), "facts", "prs", re.sub(r"[^\w.-]+", "-", ident) + ".json")
    carto.save(out, res)
    s = res["stats"]
    print("%s: %s — %d files +%d −%d, %d commits, size %s" % (ident, res["title"][:70], s["files"], s["add"], s["del"], s["commits"], s["size"]))
    print("touches: " + (", ".join("%s (%d)" % (t["name"], t["lines"]) for t in res["touched"][:6]) or "unmapped files only"))
    for sg in res["signals"][:8]:
        print("  [%s] %s" % (sg["level"], sg["title"]))
    if res.get("guard"):
        g = res["guard"]
        print("guardrails: %s" % ("passed" if g["ok"] else "%d blocking: %s" % (len(g["blocking"]), ", ".join(sorted({v["rule"] for v in g["blocking"]})))))
    if res["reviewers"]:
        print("reviewers: " + ", ".join(r["name"] for r in res["reviewers"]))
    print("wrote %s" % out)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as fh:
            fh.write(markdown(res, a.viewer_url))
        print("wrote %s" % a.markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
