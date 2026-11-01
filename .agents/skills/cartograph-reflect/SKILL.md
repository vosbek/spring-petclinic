---
name: cartograph-reflect
description: After a session or task, mine what went wrong or was learned (user corrections, guard blocks, map claims the code contradicted, skills that misfired or were missing) and route each lesson to the strongest home. In order, that's a guardrail proposal, a test, a model fix or a skill edit. Skill edits wait for approval. Use when the user says reflect or "learn from this session", at the end of a cartograph-factory task with corrections, or after a long autonomous run.
---

# Cartograph reflect

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

`/cartograph-learn` mines the repository's history. This skill mines one working session. Skip it when the session was trivial, or when nothing went wrong that a skill or rule should have prevented. One-offs aren't lessons.

## 1. Gather the session evidence

- **Transcript.**
  - Claude Code: the newest `.jsonl` in `~/.claude/projects/<workspace-path-with-/-and-.-as-->/`.
  - Cursor: the newest file in the workspace's `agent-transcripts/`.

  Read only this workspace's folder. If you can't find it, write a tight digest of the session from your context instead.
- **Guard log.** Entries since the session started in `.cartographer/log/guard.jsonl`: which rules blocked, how often, and what was suppressed.
- **Model check.** `python3 .cartographer/bin/check.py` and any drift the PR lens reported.

## 2. Extract findings

Look for:
1. **Corrections.** The user said "no", "don't", "that's wrong", "use X instead", or redid your work. For each: what you did, what was right, and what you would have needed to know.
2. **Late guard blocks.** A rule blocked at Stop or pre-commit for something you could have known at planning time. That means `--explain` or the skill didn't surface it early enough.
3. **Map errors.** A component, flow, claim, snippet annotation or glossary term the code contradicted.
4. **Skill gaps.** A step that was wrong, missing, ambiguous or out of order. A command that failed as written. A skill that should have triggered and didn't.

For long sessions, run three reviewers in parallel over the transcript, using the Agent tool with general-purpose agents:
- **Judgment.** Where did the agent decide wrongly, and why?
- **Tooling.** Which commands, scripts or hooks failed or misled?
- **Divergent.** What would a different approach have caught?

Then merge their findings yourself.

## 3. Route each finding to its strongest home

| Finding | Home | How |
|---|---|---|
| A mistake a diff can reveal | Guardrail proposal | Add a rule to `.cartographer/guardrails.proposed.json` with `evidence` pointing at this session's commit or diff. Prove it with `guard.py --replay --rule`. |
| Behaviour that broke and no test caught | Test | Write it. It fails before the fix and passes after. |
| The model was wrong | Model | Fix the claim, path, snippet or annotation with `/cartograph-refresh`. `check.py` ends at 0 errors. |
| A script misbehaved | Toolkit issue | Write a precise bug report (command, input, output, expected), or fix it in the Cartographer repo if you're working there. |
| A skill was wrong or missing a step | Skill edit | Draft it (next section). |
| A judgment call that can't be encoded | Agent instructions | One line plus the failure example, in CLAUDE.md / AGENTS.md. |

Before choosing "skill edit" or "agent instructions", ask whether a rule, test or script would enforce it more reliably. If yes, choose that. The instruction is the symptom.

## 4. Skill edits wait for approval

Skill changes affect every future agent in the repo. Show each proposed edit as a diff, with its destination (`.claude/skills/<skill>/SKILL.md` or `.github/skills/...`) and the evidence from this session. Apply only the edits the user approves. Keep edits small: a corrected command, a reordered step, a missing check. Anything over ~10 lines is a new section and needs the same approval.

## 5. Report

A short list with no preamble:
- **Proposed rules.** `id`: what it catches, with replay results. They wait for a person to decide them in the rules workbench (`guard.py --serve`).
- **Tests added.** Path, and what they pin.
- **Model fixes.** What changed and why.
- **Skill edits.** Applied and awaiting approval, one line each.
- **Dropped.** One line per finding you didn't route, and why.
