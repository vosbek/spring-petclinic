---
name: cartograph-snippets
description: Select, extract and annotate the key code snippets in the Cartographer model — entry points, load-bearing logic, invariants, transaction boundaries, integration seams and gotchas — pinned verbatim to a commit. Use when the user asks for the important code in an area, wants a snippet added or re-extracted, says "show me where X happens", or when a flow step needs code.
---

# Cartograph snippets

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

Snippets are the most-read part of the viewer. They must be verbatim, short, and worth a senior engineer's attention. Read `.cartographer/docs/quality-bar.md` (Selection) first.

## What earns a snippet

Pick code that a capable engineer would get **wrong** about the system without having seen it:

| Kind | Looks like |
|---|---|
| `entry-point` | the route/listener/job definition where work enters — shows the contract |
| `core-logic` | the calculation, state machine or orchestration everything depends on |
| `invariant` | validation or ordering that must not change (regulatory, financial, security) |
| `gotcha` | swallowed errors, side effects inside transactions, hidden retries, static state, surprising defaults |
| `integration` | where the system talks to another one: timeouts, mapping, error handling |
| `pattern` | the house pattern new code is expected to copy |
| `config` | a configuration block that changes behaviour materially (pool sizes, schedules, feature flags) |

Never: getters/setters, DTOs, generated code, imports, logging boilerplate, tests (unless the test is the only specification of a behaviour).

## Extract — always with the script

1. Find the exact range with line numbers (`grep -n`, or read the file). Aim for 5–40 lines: the method signature through the decisive lines. Cut the method short rather than including unrelated tails.
2. Write it:

```bash
python3 .cartographer/bin/snippet.py <path> <start> <end> \
  --id <kebab-id> --title "<what it is, ≤ 6 words>" --kind <kind> --component <component-id> \
  --why "<1–2 sentences: why this matters / what breaks if misunderstood>" \
  --note <line> "<one sentence>" --note <line> "<one sentence>" \
  --write
```

The script copies the code verbatim, pins it to HEAD with a hash, links it to the component, and refuses annotations outside the range. Never hand-edit `code`.

## Annotate

- ≤ 4 notes, absolute line numbers, one sentence each.
- Annotate the line where the surprise *is*, and say the consequence: "Event is sent before commit. If the commit fails, consumers still see PolicyIssued." — not "publishes an event".
- Reference incidents, tickets or ADRs when you found them in code or docs (`INC-4471`, `ADR 0007`). Do not invent them.

## Re-extracting drifted snippets

`check.py` reports `moved` (same code, new lines — `check.py --update-snippets` fixes it) or `stale` (code changed). For stale snippets: re-read the new code; if the snippet still matters, re-run `snippet.py` with the same `--id` (existing annotations are kept and shifted when possible — re-verify each one); if it no longer matters, remove it from `snippets` and from any `components[].snippets` and `flows[].steps[].snippet`.

## Finish

`python3 .cartographer/bin/check.py && python3 .cartographer/bin/build.py`, then tell the user which snippets you added and why, in one line each.
