---
name: cartograph-survey
description: Build a first mental model of a brownfield repository — components by layer, dependencies, key code snippets, flows, decisions, risks and a reading path — and render it as the Cartographer viewer. Use when the user asks to map, survey, onboard onto, or "understand" a codebase and no .cartographer/model/model.json exists yet (or they want it rebuilt from scratch).
---

# Cartograph survey

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

Produce `.cartographer/model/model.json` for this repository and build the viewer at `.cartographer/dist/cartograph.html`.

Every line-level evidence entry gets an `expect` string: a few characters the cited line must contain, such as a method name. `check.py` then catches line numbers that are off by a few. Take line numbers from `grep -n` or a single-file read, never from a multi-file `cat -n`.

Before writing anything, read `.cartographer/docs/quality-bar.md` and `.cartographer/docs/model-reference.md`. They are short and they are the contract. If `.cartographer/bin/` does not exist, stop and tell the user to run the Cartographer `install.sh` (or `install.ps1`) against this repo.

## 1. Collect facts (2 minutes)

```bash
python3 .cartographer/bin/collect.py      # files, lines by folder, large files, history with fix/AI/PR flags
python3 .cartographer/bin/graph.py        # import graph: real dependencies, interfaces, test reach
python3 .cartographer/bin/build.py        # first build on a provisional model: prints structure and knowledge
```

Read the printed summaries:
- **`collect.py`** gives lines by top-level folder and names every large file it counted. A multi-megabyte XML or SQL file is often the most important artifact in a legacy repo.
- **`build.py`** gives the dependency structure and the knowledge picture (single-owner and orphaned areas). You need that for risks in step 4.

Inventory rows are `[path, language, lines, created, modified]`. Don't read `history.json` by hand; `build.py` derives everything from it.

If the output says `TRUNCATED`, the repo is very active; that is fine.

## 2. Orient (read, don't write)

In this order, stopping when you have enough to name the parts:

1. `README*`, `docs/`, `adr/` or `docs/adr/` — note every documented decision.
2. Build and module files: `pom.xml`/`settings.gradle`, `package.json`/workspaces, `*.sln`/`*.csproj`, `go.mod`, `pyproject.toml`, `Cargo.toml`. Modules are the strongest hint for component boundaries.
3. Deploy/runtime config: `Dockerfile`, Helm/K8s manifests, `application*.yml`, `appsettings*.json`, `.env.example`, CI pipelines. These reveal external systems (databases, queues, APIs).
4. Entry points: `main`, controllers/routes, message listeners, scheduled jobs, CLI commands, UI routes.
5. For each candidate component, open 2–3 central files. Do not summarize a directory you have not opened.

## 3. Choose a strategy by size

| Lines (from collect) | Approach |
|---|---|
| < 50k | Single pass. Read most entry points yourself. |
| 50k – 500k | Map top-level modules first, then deepen the 5–8 that carry the business logic. |
| > 500k or > 20 modules | Partition by top-level module/service. If you can run sub-agents, give each one area with this instruction: "Return candidate components for `<paths>` as JSON matching model-reference.md Component, each with ≥ 1 evidence entry and confidence; plus edges you can prove; plus up to 3 snippet candidates (path, start, end, why). Read code; do not guess from names." Merge their results, dedupe, and reconcile edges yourself. |

Target 8–25 components regardless of size. In a monorepo, make each deployable service one component and represent shared libraries as a few `library` components; deepen later with `cartograph-architecture`.

## 4. Write the model

Create `.cartographer/model/model.json` in this order, saving as you go:

1. `repo` — name, 2-sentence summary (what it does + what is unusual), `blobUrl` from `git remote get-url origin`. Use the current remote even if the README says the repo will move. Record the move as an open question.
2. `layers` — start from the defaults; adapt names to the codebase.
3. `components` — `paths` must cover the code: every significant source directory belongs to exactly one component (longest prefix wins). External systems (databases, queues, third-party APIs, mainframes) are components in layer `external` with `paths: []`.
4. `edges` — only those you can show: DI wiring, imports across components, HTTP/SOAP/gRPC client config, topic names in producer/consumer config, table access. Evidence on the non-obvious ones.
5. `snippets` — 5–12 to start (the quality bar allows up to 20), following the `cartograph-snippets` skill. Always add them with `bin/snippet.py --write`. It rewrites `model.json`, so re-read the file before any hand edit that follows.
6. `flows` — 2–4 of the most important paths, starting with the one most changes touch (the quality bar allows up to 6). Each step references a snippet or evidence. Use `cartograph-architecture` for more.
7. `decisions` — every ADR you found (`documented`), plus ≤ 3 `inferred` ones that a newcomer would otherwise get wrong.
8. `risks` — 3–12 failure modes with evidence (see the quality bar). Knowledge-concentration risks come from the step 1 build output, not intuition. AI agent authors (for example a "Claude" author) are left out of ownership. A component with no human owner is itself worth a risk.
9. `glossary` — domain terms that appear in class/table names and would confuse a newcomer.
10. `questions` — what you could not determine. This is valuable; do not hide gaps.
11. `readingPath` — 4–7 stops in the order you would onboard a senior engineer.
12. `generated` — `at` (UTC now), `commit` (`git rev-parse --short=12 HEAD`), `branch`, `by: "cartograph-survey"`.

## 5. Verify and render

```bash
python3 .cartographer/bin/check.py                   # must end with 0 error(s)
python3 .cartographer/bin/check.py --show-evidence   # read every cited line once: does it say what the claim says?
python3 .cartographer/bin/build.py                   # coverage, structure vs the model, knowledge
```

Fix every error. `build.py` prints the share of lines mapped to components and the largest unmapped directories. If coverage is under 85 %, either add components for those directories or add them to `exclude` in `.cartographer/config.json` (generated, vendored, fixtures) and re-run `collect.py`.

## 6. Report

Tell the user, briefly: number of components/flows/snippets/risks, coverage %, the top 3 things a newcomer must know, the open questions, and where the viewer is (`.cartographer/dist/cartograph.html` — opens in any browser, no server). Suggest committing `.cartographer/model/` (the facts and dist folders are git-ignored and rebuilt). Then suggest `/cartograph-guard`. With a curated model it can enforce layering and pinned code, and it proposes rules from the repo's history.
