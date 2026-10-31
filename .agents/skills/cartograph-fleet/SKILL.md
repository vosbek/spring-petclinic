---
name: cartograph-fleet
description: Build and interpret a portfolio view across many repositories — activity, AI-assisted share, knowledge risk, hidden dependencies, structure problems, PR flow and model freshness per repo — as one shareable page. Use when the user asks about many repos at once, an org or team portfolio, which repos need attention, where AI tools are being used, or wants a leadership-level codebase overview.
---

# Cartograph fleet

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

`fleet.py` works on local clones. Each repo gets its facts from the same scripts as a single-repo build; repos without a curated model get a provisional one (components = directories), clearly labelled.

## 1. Get the clones

Put the repositories under one folder (for example `~/fleet/`). Full history is needed for trends, coupling and knowledge — do not use `--depth 1`. For a large org, clone with `--filter=blob:none` to keep it fast; history is still complete.

## 2. Build

```bash
python3 <toolkit>/bin/fleet.py --root ~/fleet --collect --out ~/fleet/fleet.html --title "Payments platform repos"
```

- `--collect` runs `collect.py`, `graph.py` and `prs.py --no-api` for repos that have no facts yet; `--refresh` re-runs them for all.
- For PR cycle time and review data, run `prs.py` (with the GitHub API) in each repo first; `fleet.py` picks it up.
- The toolkit only needs to be installed in repos that will get curated models; `fleet.py` can run from the Cartographer checkout against plain clones.

Output: `fleet.html` (open in a browser) and `fleet.json` (the same data for further analysis).

## 3. Interpret

Read `fleet.json` and write a brief for the audience the user named. Structure:

1. **Portfolio in one paragraph** — how many repos are active, total size, the AI-assisted share and where it is concentrated, how many repos have curated models.
2. **Needs attention** — repos with signals, worst first, each with the specific signal and what it means:
   - orphaned areas (most changes by people inactive 90+ days) → knowledge loss risk
   - single-owner areas under active change → key-person risk (ignored in repos with ≤ 2 authors)
   - hidden dependencies (co-change with no import or model edge) → coupling that will surprise people
   - dependency cycles / layer violations → change and release friction
   - stale PRs → review bottleneck
   - model check errors or a model far behind → map no longer trustworthy
3. **AI adoption** — by repo, with the caveat that only credited AI use is visible (trailers, "Generated with" footers, AI bot authors).
4. **Next actions** — 3–5, specific: which repo to survey next, which area needs a second owner, which PR queue to unblock.

Keep it at repo and team level. Do not rank individual people.
