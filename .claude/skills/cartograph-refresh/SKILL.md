---
name: cartograph-refresh
description: Bring an existing Cartographer model up to date with the code incrementally — re-collect facts, re-pin moved snippets, re-extract or retire stale ones, and re-verify only the components touched since the model's commit. Use when the viewer says the model is behind, check.py reports drift, the user says the map is out of date, or on a schedule after merges.
---

# Cartograph refresh

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

Update only what changed. A refresh should take minutes, not repeat the survey.

## 1. Measure the drift

```bash
python3 .cartographer/bin/collect.py
python3 .cartographer/bin/graph.py                     # refresh the import graph and interface inventory
python3 .cartographer/bin/check.py --update-snippets   # re-pins snippets that only moved
python3 .cartographer/bin/check.py --fix-lines         # follows evidence whose cited text only moved
python3 .cartographer/bin/check.py                     # what is still broken
BASE=$(python3 -c "import json;print(json.load(open('.cartographer/model/model.json'))['generated']['commit'])")
git diff --stat "$BASE"..HEAD
git log --no-merges --format='%h %s' "$BASE"..HEAD
```

If `$BASE` is not in this clone (shallow clone or rewritten history), fetch more history (`git fetch --depth=1000`) or fall back to `--since=<generated.at>`.

For a quick, structured view of what the drift touches, run the PR lens over the range — it reports pinned snippets and cited lines that changed, new cross-component dependencies and interface changes:

```bash
python3 .cartographer/bin/pr.py --base "$BASE" --head HEAD --id since-model
```

## 2. Map changes to components

For every changed path, find its component (longest matching prefix in `components[].paths`). Bucket into:

- **Touched components** — re-read their key files and the diff. Update `summary`, `responsibilities`, `keyFiles`, `interfaces`, `tech` only where the code now says something different.
- **Unmapped new paths** — new modules or directories. Either extend an existing component's `paths` or create a component (follow `cartograph-architecture`).
- **Deleted paths** — remove them from `paths`/`keyFiles`; if a component's code is gone, remove the component and every reference to it (edges, flows, risks, snippets, reading path).

## 3. Fix what check.py reports

- `stale` snippet → re-extract or retire (see `cartograph-snippets`).
- Evidence path missing / line out of range → find the new location (`git log --follow -p -- <old path>` or search for the symbol) and update it; if the claim is no longer true, change or remove the claim.
- Edges: re-verify edges touching changed components. Add new ones the diff introduces (new client, new topic, new table).
- Risks and decisions: if a change fixes a risk, remove it (mention it in your report); if it introduces one, add it with evidence.

## 4. Stamp and build

Set `generated.at` (UTC now), `generated.commit` (`git rev-parse --short=12 HEAD`), `generated.by: "cartograph-refresh"`. Then:

```bash
python3 .cartographer/bin/check.py && python3 .cartographer/bin/build.py
```

## 5. Report

Three to six lines: commits covered, components updated, snippets re-pinned / re-extracted / retired, risks added or resolved, and anything you could not verify (added to `questions`).

## Scheduling

This skill is safe to run unattended (CI or a scheduled agent) because every edit is checked against the code. In unattended runs, do not delete components or risks — mark them in `questions` for a human instead.
