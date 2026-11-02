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
            # An entry that runs the same command is ours from an earlier install: replace it (a new timeout, matcher).
            mine = {h.get("command") for h in e.get("hooks", [])}
            same = [i for i, x in enumerate(cur) if mine & {h.get("command") for h in x.get("hooks", [])}]
            if same:
                cur[same[0]] = e
                for i in reversed(same[1:]):
                    del cur[i]
            elif json.dumps(e, sort_keys=True) not in {json.dumps(x, sort_keys=True) for x in cur}:
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
