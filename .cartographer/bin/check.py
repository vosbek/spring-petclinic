#!/usr/bin/env python3
"""Validate a Cartographer model against the repository it describes.

Checks structure (ids, references between sections) and grounding (every
evidence path exists, every line is in range, every snippet still matches the
code it was copied from).

Usage:
  python3 .cartographer/bin/check.py                   # report; exit 1 on errors
  python3 .cartographer/bin/check.py --update-snippets # re-pin moved snippets in place
  python3 .cartographer/bin/check.py --json            # machine-readable report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

KINDS = {"service", "module", "library", "job", "ui", "adapter", "datastore", "external", "config", "test"}
EDGE_KINDS = {"calls", "reads", "writes", "publishes", "consumes", "depends"}
SNIPPET_KINDS = {"entry-point", "core-logic", "invariant", "gotcha", "pattern", "integration", "config"}
TARGET_KINDS = {"component", "flow", "snippet", "risk", "decision", "view", "file"}
VIEWS = {"overview", "architecture", "flows", "code", "changes", "risks"}


def norm_lines(text: str) -> list:
    return [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]


def code_hash(code: str) -> str:
    return hashlib.sha256("\n".join(norm_lines(code)).strip("\n").encode("utf-8")).hexdigest()[:16]


def read_lines(repo: str, path: str):
    full = os.path.join(repo, path)
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            return norm_lines(fh.read())
    except OSError:
        return None


def snippet_status(repo: str, sn: dict) -> dict:
    """fresh | moved (same code, new lines) | stale (code changed) | missing (file gone)."""
    lines = read_lines(repo, sn.get("path", ""))
    if lines is None:
        return {"status": "missing"}
    want = norm_lines(sn.get("code", "")).copy()
    while want and not want[-1].strip():
        want.pop()
    if not want:
        return {"status": "stale", "reason": "empty code"}
    start = int(sn.get("start", 1))
    if lines[start - 1:start - 1 + len(want)] == want:
        return {"status": "fresh", "start": start, "end": start + len(want) - 1}
    first = want[0]
    for i, l in enumerate(lines):
        if l == first and lines[i:i + len(want)] == want:
            return {"status": "moved", "start": i + 1, "end": i + len(want)}
    # whitespace-insensitive fallback (reformatting only)
    squash = lambda xs: [" ".join(x.split()) for x in xs]
    sw = squash(want)
    sl = squash(lines)
    for i in range(len(sl) - len(sw) + 1):
        if sl[i:i + len(sw)] == sw:
            return {"status": "moved", "start": i + 1, "end": i + len(sw), "reformatted": True}
    return {"status": "stale"}


def head_commit(repo: str) -> str:
    try:
        return subprocess.run(["git", "-C", repo, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


SHOW_EVIDENCE = False
EVIDENCE_LINES = []
FIX_LINES = False
FIXED = []


def validate(model: dict, repo: str) -> list:
    issues = []

    def err(where, msg):
        issues.append({"level": "error", "where": where, "msg": msg})

    def warn(where, msg):
        issues.append({"level": "warn", "where": where, "msg": msg})

    if model.get("schemaVersion") != 1:
        err("schemaVersion", "expected 1")
    for key in ("repo", "layers", "components"):
        if key not in model:
            err(key, "missing required section")
    if issues:
        return issues

    def ids(section):
        seen = {}
        for i, item in enumerate(model.get(section, [])):
            iid = item.get("id")
            if not iid:
                err("%s[%d]" % (section, i), "missing id")
            elif iid in seen:
                err("%s.%s" % (section, iid), "duplicate id")
            seen[iid] = item
        return seen

    layers = ids("layers")
    comps = ids("components")
    snippets = ids("snippets")
    flows = ids("flows")
    risks = ids("risks")
    decisions = ids("decisions")

    def check_evidence(where, ev):
        if not isinstance(ev, dict) or not ev.get("path"):
            err(where, "evidence needs a path")
            return
        lines = read_lines(repo, ev["path"])
        if lines is None:
            if not os.path.isdir(os.path.join(repo, ev["path"])):
                err(where, "evidence path not found: %s" % ev["path"])
            return
        ln = ev.get("line")
        if ln and int(ln) > len(lines):
            err(where, "line %s beyond end of %s (%d lines)" % (ln, ev["path"], len(lines)))
            return
        if ln and SHOW_EVIDENCE:
            EVIDENCE_LINES.append("%s:%s  %s  <- %s" % (ev["path"], ln, lines[int(ln) - 1].strip()[:100], where))
        exp = ev.get("expect")
        if ln and exp:
            lo, hi = int(ln), int(ev.get("end") or ln)
            window = lines[lo - 1:hi]
            if not any(exp in l for l in window):
                near = [i + 1 for i, l in enumerate(lines) if exp in l]
                by_dist = sorted(near, key=lambda n: abs(n - lo))
                if FIX_LINES and by_dist and (len(by_dist) == 1 or abs(by_dist[0] - lo) < abs(by_dist[1] - lo)):
                    # The cited text moved: follow it to the nearest occurrence. A tie stays an error for a person.
                    span = hi - lo
                    ev["line"] = by_dist[0]
                    if ev.get("end"):
                        ev["end"] = by_dist[0] + span
                    FIXED.append("%s: %s:%s -> %s" % (where, ev["path"], lo, by_dist[0]))
                    return
                err(where, "%s:%s does not contain %r%s" % (ev["path"], ln, exp, (" (found at line %s)" % ", ".join(map(str, near[:3]))) if near else " (not in the file)"))

    for cid, c in comps.items():
        w = "components.%s" % cid
        if c.get("layer") not in layers:
            err(w, "unknown layer '%s'" % c.get("layer"))
        if c.get("kind") and c["kind"] not in KINDS:
            warn(w, "unusual kind '%s'" % c["kind"])
        if not c.get("summary"):
            warn(w, "no summary")
        for p in c.get("paths", []):
            if not os.path.exists(os.path.join(repo, p)):
                err(w, "path not found: %s" % p)
        for f in c.get("keyFiles", []):
            if not os.path.exists(os.path.join(repo, f.get("path", ""))):
                err(w, "key file not found: %s" % f.get("path"))
        for s in c.get("snippets", []):
            if s not in snippets:
                err(w, "unknown snippet '%s'" % s)
        for r in c.get("risks", []):
            if r not in risks:
                err(w, "unknown risk '%s'" % r)
        evs = c.get("evidence", [])
        if c.get("layer") != "external" and not evs:
            warn(w, "no evidence")
        for i, ev in enumerate(evs):
            check_evidence("%s.evidence[%d]" % (w, i), ev)

    for i, e in enumerate(model.get("edges", [])):
        w = "edges[%d]" % i
        for end in ("from", "to"):
            if e.get(end) not in comps:
                err(w, "unknown component '%s'" % e.get(end))
        if e.get("kind") and e["kind"] not in EDGE_KINDS:
            warn(w, "unusual edge kind '%s'" % e["kind"])
        for j, ev in enumerate(e.get("evidence", [])):
            check_evidence("%s.evidence[%d]" % (w, j), ev)

    for fid, f in flows.items():
        w = "flows.%s" % fid
        for i, st in enumerate(f.get("steps", [])):
            if st.get("component") not in comps:
                err("%s.steps[%d]" % (w, i), "unknown component '%s'" % st.get("component"))
            if st.get("snippet") and st["snippet"] not in snippets:
                err("%s.steps[%d]" % (w, i), "unknown snippet '%s'" % st["snippet"])
            if not st.get("snippet") and not st.get("evidence"):
                warn("%s.steps[%d]" % (w, i), "step has neither snippet nor evidence")
            if st.get("evidence"):
                evs = st["evidence"] if isinstance(st["evidence"], list) else [st["evidence"]]   # one object or a list
                for j, ev in enumerate(evs):
                    check_evidence("%s.steps[%d].evidence%s" % (w, i, "[%d]" % j if isinstance(st["evidence"], list) else ""), ev)

    for sid, s in snippets.items():
        w = "snippets.%s" % sid
        if s.get("component") and s["component"] not in comps:
            err(w, "unknown component '%s'" % s["component"])
        if s.get("kind") and s["kind"] not in SNIPPET_KINDS:
            warn(w, "unusual kind '%s'" % s["kind"])
        if not s.get("why"):
            warn(w, "no 'why' — a snippet without a reason is noise")
        st = snippet_status(repo, s)
        if st["status"] == "missing":
            err(w, "file not found: %s" % s.get("path"))
        elif st["status"] == "stale":
            err(w, "code no longer matches %s — re-extract or retire" % s.get("path"))
        elif st["status"] == "moved":
            warn(w, "moved to lines %d-%d (run with --update-snippets)" % (st["start"], st["end"]))
        start = int(s.get("start", 0))
        for a in s.get("annotations", []):
            if not (start <= int(a.get("line", 0)) <= int(s.get("end", start))):
                warn(w, "annotation line %s outside %s-%s" % (a.get("line"), s.get("start"), s.get("end")))

    for rid, r in risks.items():
        for c in r.get("components", []):
            if c not in comps:
                err("risks.%s" % rid, "unknown component '%s'" % c)
        for i, ev in enumerate(r.get("evidence", [])):
            check_evidence("risks.%s.evidence[%d]" % (rid, i), ev)
    for did, d in decisions.items():
        if d.get("status") not in ("documented", "inferred"):
            warn("decisions.%s" % did, "status should be documented or inferred")
        for i, ev in enumerate(d.get("evidence", [])):
            check_evidence("decisions.%s.evidence[%d]" % (did, i), ev)
    for i, g in enumerate(model.get("glossary", [])):
        for j, ev in enumerate(g.get("refs", [])):
            check_evidence("glossary[%d].refs[%d]" % (i, j), ev)

    targets = {"component": comps, "flow": flows, "snippet": snippets, "risk": risks, "decision": decisions}
    for i, rp in enumerate(model.get("readingPath", [])):
        t = rp.get("target", "")
        kind, _, tid = t.partition(":")
        if kind not in TARGET_KINDS:
            err("readingPath[%d]" % i, "bad target '%s'" % t)
        elif kind == "view" and tid not in VIEWS:
            err("readingPath[%d]" % i, "unknown view '%s'" % tid)
        elif kind == "file" and not os.path.exists(os.path.join(repo, tid)):
            err("readingPath[%d]" % i, "file not found '%s'" % tid)
        elif kind in targets and tid not in targets[kind]:
            err("readingPath[%d]" % i, "unknown %s '%s'" % (kind, tid))
    return issues


def update_snippets(model: dict, repo: str) -> int:
    changed = 0
    head = head_commit(repo)
    for s in model.get("snippets", []):
        st = snippet_status(repo, s)
        if st["status"] in ("fresh", "moved"):
            shift = st["start"] - int(s.get("start", st["start"]))
            if shift or s.get("pin", {}).get("sha256") != code_hash(s.get("code", "")):
                s["start"], s["end"] = st["start"], st["end"]
                for a in s.get("annotations", []):
                    a["line"] = int(a["line"]) + shift
                s["pin"] = {"commit": head, "sha256": code_hash(s.get("code", ""))}
                changed += 1
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--model", default=None, help="default: <repo>/.cartographer/model/model.json")
    ap.add_argument("--update-snippets", action="store_true", help="re-pin fresh/moved snippets and shift annotations")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix-lines", action="store_true", help="move evidence whose expected text moved to the nearest line that has it, and save the model")
    ap.add_argument("--show-evidence", action="store_true", help="print every cited line so you can eyeball that it says what the claim says")
    args = ap.parse_args()
    global SHOW_EVIDENCE, FIX_LINES
    SHOW_EVIDENCE = args.show_evidence
    FIX_LINES = args.fix_lines
    repo = os.path.abspath(args.repo)
    path = args.model or os.path.join(repo, ".cartographer", "model", "model.json")
    with open(path, encoding="utf-8") as fh:
        model = json.load(fh)
    if args.update_snippets:
        n = update_snippets(model, repo)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(model, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("re-pinned %d snippet(s)" % n)
    issues = validate(model, repo)
    if FIXED:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(model, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        for f in FIXED:
            print("moved  %s" % f)
    for line in EVIDENCE_LINES:
        print(line)
    if args.json:
        print(json.dumps(issues, indent=2))
    else:
        errs = [i for i in issues if i["level"] == "error"]
        warns = [i for i in issues if i["level"] == "warn"]
        for i in errs + warns:
            print("%-5s %s: %s" % (i["level"].upper(), i["where"], i["msg"]))
        print("%d error(s), %d warning(s)" % (len(errs), len(warns)))
    return 1 if any(i["level"] == "error" for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
