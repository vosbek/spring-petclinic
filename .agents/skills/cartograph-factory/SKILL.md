---
name: cartograph-factory
description: Do engineering work in this repository the factory way. Read the map before the code, plan against the guardrails, build in small units that each pass a deterministic check, review with the PR lens, keep the model true, and turn every correction into a structural lesson. Use for any non-trivial change (a feature, bug fix, refactor or migration), when the user says "factory mode" or "use the map", or when an agent will work unattended.
---

# Cartograph factory

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

The factory runs as a loop: **map → plan → build → guard → review → learn**. Each stage ends in a check that a script runs, not in an opinion. The guardrails in `.cartographer/guardrails.json` are enforced outside your judgment: the Stop hook, the git pre-commit hook and CI all run them. Your job is to pass them honestly. A rule you think is wrong goes to the user as a question.

Open a task list whose first items are the steps of the matching playbook below, copied verbatim. A step you skip stays in the list as `skip: <reason>`.

## Non-negotiables

- **Restate the goal first.** In your first message, say in your own words the user's goal, the problem behind it, and the smallest change you expect to meet it. Don't wait for confirmation.
- **Map before code.** Read the component, flows and pinned snippets for the area (`/cartograph-explore`), then run `guard.py --explain` on every file you expect to touch. Write down what it says you may import and what usually changes together.
- **Never edit `.cartographer/guardrails.json` or `baseline.json`.** The edit hook refuses it. New rules go to `guardrails.proposed.json`. Exceptions need a reason, an expiry and a human approver.
- **Evidence or label.** Every claim in your report carries its evidence (`path:line`, a command and its output) or is labelled inferred or guess.
- **Prefer deletion.** Make the smallest change that meets the goal. Delete what the change makes dead in the same unit.

## The loop

### 1. Map

```bash
python3 .cartographer/bin/collect.py && python3 .cartographer/bin/graph.py    # cheap; keeps facts current
python3 .cartographer/bin/guard.py --explain path/to/file.ext other/file.ext
```

From the model, note the component, its layer and the flows that pass through the area. From `--explain`, note:
- which rules apply
- which components you may import (same layer or below)
- which files usually change with these
- which pinned snippets you are about to touch

### 2. Plan

Write a short plan before code:
- **Data shape.** The types or records the change is built around.
- **Touched components and interfaces.** Routes, tools, topics and schemas that change.
- **Rules that apply.** From `--explain`, with how you will satisfy each one.
- **Units.** Ordered so each one ends in a passing check: tests, then `guard.py --worktree`.
- **Verification.** What proves the change works: the command, test or user path, and what you expect to see.

### 3. Build in verifiable units

After each unit:

```bash
<the repo's test command for the touched area>
python3 .cartographer/bin/guard.py --worktree
```

Fix blocking items before the next unit. Each one names its fix. If a co-change rule fires, open the partner file and decide. Make the matching change, or ask the user for an exception that says why it isn't needed. Never satisfy a rule with a no-op edit.

### 4. Review with the lens

```bash
python3 .cartographer/bin/pr.py --base <default-branch> --head HEAD --id <branch-name>
```

Work through every high and medium signal the way `/cartograph-pr` step 2 describes:
- **Missed co-changes.** Open the partner file.
- **New dependencies.** Check them against layering.
- **Untested changed files.** Add the test.
- **Interface removals.** Find the consumers.

The lens is advisory and the guard is binding. Do both.

### 5. Keep the map true

If the lens or guard reports model drift (a pinned snippet or a cited line changed):
1. Re-read the annotations against the new code.
2. Re-pin with `/cartograph-refresh` or `check.py --update-snippets`.
3. Commit the model with the change.

`check.py` must end with 0 errors.

### 6. Gate

Done means all of these are true, and you ran each one:
- the repo's tests pass
- `guard.py --worktree` passes, or every remaining item has a human-approved exception
- `check.py` passes

The Stop hook re-runs the guard and keeps you working until it passes. After three identical failures it lets you stop so you can ask the user.

### 7. Learn

Before the final report, ask yourself:
- Did the user correct me?
- Did a guard rule block something I should have known from the map?
- Did the model say something the code contradicted?

If yes to any, run `/cartograph-reflect`. It routes each lesson to a rule, a test, a model fix or a skill edit, in that order of preference.

### 8. Report

1. **Goal.** One sentence.
2. **What changed, in system terms.** Components and flows, not file lists.
3. **Evidence.** Commands run and their results: tests, guard, check, lens signals you resolved.
4. **Rules.** Rules that fired and how each was satisfied, plus exceptions requested and who must approve them.
5. **Lessons.** Proposals written to `guardrails.proposed.json` or skill edits awaiting approval.

## Playbooks

| Playbook | When | Extra steps |
|---|---|---|
| **Feature** | New or changed behaviour | Plan names the data shape first. Write the test that shows the new behaviour before the code that makes it pass. Add new interfaces to the model's component in step 5. |
| **Bug fix** | A reported defect | Reproduce first, with a failing test on the same surface the user hit. Root-cause to the first place the system went wrong and fix it there. Run `learn.py` and check whether this file is a follow-up-fix class. If it is, the test you wrote is the lesson; say so. |
| **Refactor** | Behaviour-preserving change | Tests must pass unchanged. Run `guard.py --worktree --refresh` so the ratchet compares structure: layer violations, cycles and unmodeled dependencies must not rise. If the refactor removes debt, run `guard.py --baseline` to tighten. |
| **Migration** | Many call sites, old API to new | Migrate the callers, then delete the old API in the same wave. Propose a `forbid-import` or `forbid-pattern` rule for the old API so it can't come back, and prove it with `guard.py --replay --rule <id>`. |
| **Investigation** | A read-only question | Steps 1 and 8 only. Every answer cites `path:line`. |
| **Review** | Someone else's PR | Hand off to `/cartograph-pr`. |

## Working unattended

When the user is away, take the most reasonable reading of the request and state it at the top of the report. Keep going through reversible work. Stop and leave a clear question only for:
- an exception to a blocking rule
- an irreversible action: a migration on shared data, a force-push, a deploy
- a product decision no test can settle
