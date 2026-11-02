---
name: explain-repo
description: Show how this repository is built as interactive C4 diagrams generated from its codebase map (.cartographer/model/model.json) - what it works with, its parts by layer with the dependencies the map proves, and each traced flow step by step. Use when someone asks for an architecture overview or diagram of this repository, wants to see how a flow works, or types /explain-repo, optionally with a part or flow.
argument-hint: "[part or flow id]"
---

# Explain this repository with C4 diagrams

## 1. Make sure the map is there and current

- No `.cartographer/model/model.json`: run `/cartograph-survey` first. The diagrams draw only what the map holds.
- Run `python3 .cartographer/bin/check.py`. If it says the map is behind the code, run `/cartograph-refresh` before
  drawing, or say plainly that the picture is from the map's commit.

## 2. Open the diagrams

```bash
python3 .cartographer/bin/c4.py --from-map .cartographer/model/model.json --open     # Windows: python .cartographer\bin\c4.py ...
```

It writes `.cache/explain/repo.html` (one file, no network, ignored by git) and opens it. If no browser can open from
where you run, give the person the path.

- `--view parts--<component id>` opens with a part selected; `--view flow-<flow id>` opens a flow as a step-by-step
  journey. Ids are in the map's `components` and `flows`.
- The views: what the repository works with (context), its parts grouped by layer, and one journey per traced flow.

## 3. Answer from the code

- The map cites a file for each part and dependency. Read those files before you answer, and name them.
- What the map does not list is not in the picture. Say so rather than guessing at it; offer `/cartograph-architecture`
  to trace it.
- Plain words, the answer first, one idea per sentence. No figures the map or the code does not give you.
