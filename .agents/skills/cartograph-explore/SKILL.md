---
name: cartograph-explore
description: Answer questions about the codebase using the Cartographer model as the map and the code as the source of truth — where something lives, how it works, what would break if X changed, where to make a change, who knows an area — and fold durable findings back into the model. Use for any "how does…", "where is…", "what happens when…", "what's the impact of…", or onboarding question in a repo that has .cartographer/model/model.json.
---

# Cartograph explore

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

The model tells you where to look; the code tells you what is true. Use both, in that order.

## Answering

1. **Start from the map.** Read `.cartographer/model/model.json` (it is small). Find the components, flows, snippets, risks and glossary terms that match the question. This usually narrows the search to a handful of files.
2. **Verify in code.** Open the referenced files at the referenced lines. If the model is wrong or stale, trust the code and note the discrepancy.
3. **Answer like a senior colleague.** Lead with the answer; then the path through the code as `path:line` references (in order of execution for "how" questions); then caveats — known risks from the model and anything you could not confirm.
4. **Impact questions** ("what breaks if I change X?"): find X's component, its inbound edges (`edges` where `to` is X) and every flow step that passes through it; check those call sites in code; list affected flows and consumers, plus any `invariant`/`gotcha` snippet on the path. Use computed facts (`cartograph-changes`) for who has changed the area recently.
5. **"Where would I change…"**: name the component, the key file, and the existing `pattern` snippet to copy if there is one.
6. **Use the computed facts** in `.cartographer/dist/cartograph.json` (`facts`): the import graph for impact (`structure.compEdges`, importers), coupling for "what usually changes with this" (`coupling`), knowledge for "who knows this" (`components[id].owners` with `active`), and interfaces for "where is this route/topic/tool declared" (`structure.api`). Definitions: `.cartographer/docs/analytics.md`.

Point the user at the viewer where it helps: `.cartographer/dist/cartograph.html#/component/<id>`, `#/flows/<id>`, `#/code/<snippet-id>`, `#/risks/<id>`.

## Folding knowledge back

When the investigation produced something durable — a gotcha, an undocumented decision, a domain term, an answered open question, a missing edge — add it to the model so the next person does not repeat the work:

- gotcha / important code → `cartograph-snippets` (`snippet.py --write`)
- missing edge or flow → `cartograph-architecture`
- decision → `decisions` with `status: inferred` and evidence
- term → `glossary` with a code reference
- answered question → remove it from `questions`, put the answer where it belongs

If the user is present, list the proposed additions in one short block and ask before writing; if they asked you to keep the map current, just do it. Always finish edits with `check.py` and `build.py`.

Do not add speculation. If you could not confirm it in code, it belongs in `questions`, not in the model.
