---
name: cartograph-changes
description: Explain what has changed in the repository and what is trending — activity by component, hotspots and defect hotspots, code age, change coupling and hidden dependencies, knowledge concentration and orphaned areas, AI-assisted share, PR flow — and what changed since a ref, date or release, grounded in computed git facts. Use when the user asks what changed, what's been happening, what's hot or risky to touch, what is coupled, who knows an area, how much AI is being used, or how PRs are flowing.
---

# Cartograph changes

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

Every number here is computed by the scripts and mapped onto the model's components. Never estimate counts. Definitions and caveats for every metric are in `.cartographer/docs/analytics.md` — read the section for any metric you cite.

## 1. Refresh the facts

```bash
python3 .cartographer/bin/collect.py      # history with fix / revert / AI / PR flags, file ages
python3 .cartographer/bin/graph.py        # import graph: needed to tell hidden coupling from real dependencies
python3 .cartographer/bin/prs.py          # PR flow (uses the GitHub API when gh is authenticated; git-only otherwise)
python3 .cartographer/bin/build.py
```

`build.py` writes everything to `.cartographer/dist/cartograph.json` (the same data the viewer shows). Load what you need:

```bash
python3 - <<'PY'
import json
f = json.load(open(".cartographer/dist/cartograph.json"))["facts"]
for k in ("repo", "coupling", "knowledge", "defects", "age", "ai", "prHistory"):
    print(k, json.dumps(f.get(k), indent=1)[:3000])
PY
```

| Key | Holds |
|---|---|
| `repo` | 90-day totals vs the prior 90, weekly series, fix share |
| `components[id]` | commits, prior, authors, weekly, owners (with `active`), busFactor, activeBusFactor, fixRatio, aiShare |
| `hotspots` | files by commits × log(size), with fixes and age |
| `defects` | fix-commit density by file over the history window |
| `age` | lines by time since last change; per component median age, stale and fresh shares |
| `coupling` | component and file pairs that change together; `hidden` = no import and no model edge |
| `knowledge` | orphaned share (changes by people inactive 90+ days), active bus factor |
| `ai` | AI-credited commits: share, tools, monthly series, follow-up-fix and revert rates vs other commits |
| `prHistory` | merged PRs, size, cycle/lead time, first review, stale PRs, review concentration, per component |
| `recent`, `sinceModel` | latest commits with components and flags; drift since the model's commit |

## 2. Ad-hoc windows and refs

For a range the windows don't cover, analyze it as a range — same signals as a PR:

```bash
python3 .cartographer/bin/pr.py --base v2.3 --head v2.4 --id release-2.4
```

or use git directly (`git log --numstat <from>..<to>`) and map paths to components with the model's `paths` (longest prefix wins).

## 3. Write the brief

Lead with the answer. For a senior audience:

1. **Headline** — where change concentrated and whether that is expected.
2. **By component** — the 3–6 busiest, each with commits and change vs prior, the theme of the work (read commit subjects; for the top ones read diffs), and known risks there.
3. **Where it hurts** — hotspots and defect hotspots that are also old (age) or orphaned (knowledge). Old, defect-dense and actively changing code is the strongest signal in this whole set.
4. **Hidden coupling** — `coupling.components` with `hidden: true`, and file pairs with no import between them. Open both sides and say what they share if you can find it (a table, a payload, copied logic).
5. **People and flow** — orphaned and single-owner areas that are changing; PR cycle time and review concentration if `prHistory` exists. Team level only — never rank individuals.
6. **AI-assisted work** — share, where it is concentrated, and the follow-up-fix comparison with its caveat (AI use clusters by area and period; a gap is a prompt to look, not proof).
7. **Model impact** — if `sinceModel.commits` > 0, which components' descriptions or snippets may be outdated; suggest `cartograph-refresh`.

Cite numbers exactly; use short SHAs and PR numbers when you quote work. Say "Unmapped" for paths outside any component.

## 4. Optional model updates (ask first if the user is present)

- A hidden dependency you confirmed in code → an `edge` with evidence (`cartograph-architecture`).
- An orphaned or single-owner area under change → a `knowledge` risk; `detail` states the computed share; `evidence: []`.
- A migration or rewrite visible in the history → a `decision` with `status: inferred` and an open question for the owner.

Then `check.py` and `build.py`.
