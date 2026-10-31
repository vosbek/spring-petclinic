#!/usr/bin/env python3
"""Extract a verbatim snippet from the working tree and pin it to HEAD.

Agents must use this instead of retyping code: the viewer and check.py compare
snippets byte-for-byte (modulo trailing whitespace) against the source.

Usage:
  # print the snippet JSON
  python3 .cartographer/bin/snippet.py src/Foo.java 40 62 --id foo-retry --title "Retry policy" \
      --kind gotcha --component payments --why "Retries are not idempotent for refunds."

  # add or replace it in model.json (keeps existing annotations if the id exists)
  python3 .cartographer/bin/snippet.py src/Foo.java 40 62 --id foo-retry ... --write \
      --note 44 "Backoff caps at 3 attempts; the 4th failure is swallowed."
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check  # noqa: E402

EXT_LANG = {"py": "python", "js": "javascript", "mjs": "javascript", "ts": "typescript", "tsx": "typescript", "jsx": "javascript",
            "cs": "csharp", "kt": "kotlin", "rb": "ruby", "rs": "rust", "yml": "yaml", "sh": "shell", "ps1": "powershell",
            "cbl": "cobol", "cob": "cobol", "cpy": "cobol", "pls": "plsql", "pkb": "plsql", "pks": "plsql"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("--id", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--kind", default="core-logic", choices=sorted(check.SNIPPET_KINDS))
    ap.add_argument("--component", default=None)
    ap.add_argument("--why", default="")
    ap.add_argument("--note", nargs=2, action="append", metavar=("LINE", "TEXT"), default=[], help="annotation (repeatable)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--model", default=None)
    ap.add_argument("--write", action="store_true", help="add/replace in model.json instead of printing")
    a = ap.parse_args()

    repo = os.path.abspath(a.repo)
    lines = check.read_lines(repo, a.path)
    if lines is None:
        raise SystemExit("file not found: %s" % a.path)
    if not (1 <= a.start <= a.end <= len(lines)):
        raise SystemExit("range %d-%d outside 1-%d" % (a.start, a.end, len(lines)))
    if a.end - a.start > 60:
        print("warning: %d lines — key snippets read best under 40" % (a.end - a.start + 1), file=sys.stderr)
    code = "\n".join(lines[a.start - 1:a.end])
    head = subprocess.run(["git", "-C", repo, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True).stdout.strip()
    ext = a.path.rsplit(".", 1)[-1].lower() if "." in a.path else ""
    sn = {"id": a.id, "title": a.title, "kind": a.kind, "component": a.component, "path": a.path,
          "start": a.start, "end": a.end, "language": EXT_LANG.get(ext, ext), "code": code, "why": a.why,
          "annotations": [{"line": int(l), "note": t} for l, t in a.note],
          "pin": {"commit": head, "sha256": check.code_hash(code)}}
    for n in sn["annotations"]:
        if not a.start <= n["line"] <= a.end:
            raise SystemExit("annotation line %d outside %d-%d" % (n["line"], a.start, a.end))

    if not a.write:
        print(json.dumps(sn, indent=2, ensure_ascii=False))
        return 0
    mpath = a.model or os.path.join(repo, ".cartographer", "model", "model.json")
    with open(mpath, encoding="utf-8") as fh:
        model = json.load(fh)
    snippets = model.setdefault("snippets", [])
    for i, old in enumerate(snippets):
        if old.get("id") == a.id:
            if not sn["annotations"]:
                shift = a.start - int(old.get("start", a.start))
                sn["annotations"] = [{"line": int(x["line"]) + shift, "note": x["note"]} for x in old.get("annotations", [])
                                     if a.start <= int(x["line"]) + shift <= a.end]
            sn["why"] = sn["why"] or old.get("why", "")
            snippets[i] = sn
            break
    else:
        snippets.append(sn)
    if a.component:
        for c in model.get("components", []):
            if c.get("id") == a.component and a.id not in c.setdefault("snippets", []):
                c["snippets"].append(a.id)
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(model, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print("wrote snippet %s (%s:%d-%d) to %s" % (a.id, a.path, a.start, a.end, mpath))
    return 0


if __name__ == "__main__":
    sys.exit(main())
