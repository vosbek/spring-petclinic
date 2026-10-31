#!/usr/bin/env python3
"""Merge Cartographer's hook entries into an existing hooks file without dropping anything already there.

  merge_settings.py <target.json> <snippet.json>
"""
import json
import os
import sys


def merge(dst, src):
    for event, entries in src.get("hooks", {}).items():
        cur = dst.setdefault("hooks", {}).setdefault(event, [])
        for e in entries:
            if json.dumps(e, sort_keys=True) not in {json.dumps(x, sort_keys=True) for x in cur}:
                cur.append(e)
    for k, v in src.items():
        if k != "hooks":
            dst.setdefault(k, v)
    return dst


target, snippet = sys.argv[1], sys.argv[2]
cur = {}
if os.path.exists(target):
    with open(target, encoding="utf-8") as fh:
        cur = json.load(fh)
with open(snippet, encoding="utf-8") as fh:
    add = json.load(fh)
os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
with open(target, "w", encoding="utf-8") as fh:
    json.dump(merge(cur, add), fh, indent=2)
    fh.write("\n")
print("hooks merged into %s" % target)
