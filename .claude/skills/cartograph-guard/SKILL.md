---
name: cartograph-guard
description: Turn the codebase map into deterministic guardrails (layering, no new cycles, co-change pairs, protected migrations, interface contracts, pinned code, test and size rules, a structural ratchet), each proven against the repo's own history, then enforce them in agent hooks, pre-commit and CI. Use when the user asks to set up guardrails, make agents safe in this repo, enforce architecture, stop a recurring mistake mechanically, or check why a guard blocked a change.
---

# Cartograph guard

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

A guardrail is a rule a script checks against a diff. It returns the same verdict for the same change every time, and its error names the fix. Rules are only worth having when they would have caught something real, so every rule is replayed against history before it's accepted. Read `.cartographer/docs/guardrails.md` once for the rule types and fields.

**You propose; a person decides.** You never edit `.cartographer/guardrails.json` or `baseline.json`, and you never run `guard.py --accept`, `--reject` or `--serve`; the agent hooks refuse all of them. Proposals go to `.cartographer/guardrails.proposed.json`. The person decides in the rules workbench (`python3 .cartographer/bin/guard.py --serve`): Accept or Reject each with a reason, then one pull request committed as them.

## 1. Facts

```bash
python3 .cartographer/bin/collect.py
python3 .cartographer/bin/graph.py
```

A curated model (`/cartograph-survey`) makes layering and pinned-snippet rules possible. Without one, the guard still runs co-change, protected-path, interface, test, size and ratchet rules on a provisional model.

## 2. Suggested rules, with proof

```bash
python3 .cartographer/bin/guard.py --suggest --write
python3 .cartographer/bin/guard.py --proposals
```

Each candidate comes with its reason and a replay over the history window. The replay gives:
- how many past commits it would have fired on
- how many of those were followed by a fix to the same files within 14 days, or reverted
- the base rate for all commits

Judge each candidate:

| Keep it when | Drop or narrow it when |
|---|---|
| It fired on commits that were then fixed or reverted, above the base rate. Open two examples with `git show <sha>` and confirm the fix was about what the rule checks. | It fires on more than ~10% of commits (noise). |
| It encodes a decision in the model that is cheap to break by accident: layering, applied migrations, a pinned invariant. | Its fires look like normal work when you read them. |
| It never fired but guards something irreversible, such as applied migrations or the public API. | It duplicates a check the repo already has (its own linter, type checker or tests). |

For co-change candidates, open both files. A co-change rule is right when the second file must follow the first, as with a client and its API, a mapper and its schema, or a fixture and its generator. It's wrong when they just tend to change in the same feature.

## 3. Rules from the model

Read the model's decisions and risks. Each one that a script can check becomes a rule proposal, written to `guardrails.proposed.json`:

| Model says | Rule |
|---|---|
| "Controllers never touch the database" | `forbid-import` from `layer:entry`, module matching the data-access package |
| "Applied migrations are immutable" | `protected-paths` with `allowAdd: true` |
| "All money is `Money`, never `double`" | `forbid-pattern` on `\bdouble\s+\w*(amount\|premium\|price)` in the domain paths |
| "The PolicyMaster client is the only SOAP caller" | `forbid-import` of the SOAP library from everywhere except the adapter |
| A `gotcha` or `invariant` snippet | Covered by `pinned-snippets` |

Every rule needs:
- `description`: what it requires
- `fix`: what to do instead, naming the file, type or function to use
- `evidence`: the decision id, or the commit SHAs it would have caught

Prove each one:

```bash
python3 .cartographer/bin/guard.py --replay --rule <id>     # works on proposed and active rules
```

A rule that never fires and encodes no decision isn't worth its maintenance. Leave it out.

**Go through the agent instructions rule by rule.** Read CLAUDE.md / AGENTS.md (and CONTRIBUTING) for every "never", "only", "always" and "must". Each one goes into the config, in one of three forms:

| The instruction is about | Encode it as |
|---|---|
| What a change contains, such as a file pair, a naming convention, a value that must match, or an edit that needs a correction note | A diff rule: `co-change`, `path-convention`, `same-value`, `require-added-pattern`, `forbid-pattern` or `forbid-import` |
| How a change is made: an agent must not hand-edit a generated file, type `reviewedBy:`, or run a publish command, even though a person or a script legitimately produces the same change | An edit-time rule: `protected-paths` or `forbid-pattern` with `"enforce": "edit"`, or `forbid-command` |
| Judgment, such as "never fabricate a source" | An entry in `judgment` (`text`, `why`, `source`), so the boundary between enforced and not enforced is explicit |

When the user asks for the full rules, every instruction lands in one of the three.

## 4. Accept, baseline, enforce

Show the user a table with one row per proposal: id, what it requires, replay result, and your recommendation. Then hand over the decision:

> Open the rules workbench in your own terminal: `python3 .cartographer/bin/guard.py --serve`. Accept or reject each rule with a reason, then press "Open pull request". The rules are in force once the code owner merges it.

After that pull request merges:

```bash
python3 .cartographer/bin/guard.py --baseline            # locks today's structural counts; they can only go down
python3 .cartographer/bin/guard.py --table               # paste between the markers in CLAUDE.md / AGENTS.md
```

Enforcement (once per repo):
- `./install.sh <repo> --hooks` installs three hooks:
  - **Claude Code `PreToolUse`:** refuses edits to protected files and to the guardrails themselves.
  - **`Stop` hook (Claude Code and Cursor):** the agent can't finish with blocking violations.
  - **Git `pre-commit`:** checks staged changes.
- `./install.sh <repo> --ci` adds the `guardrails` job; make it a required status check on the default branch.

## 5. When the guard blocks

```bash
python3 .cartographer/bin/guard.py --worktree          # or --staged, or --base origin/main --head HEAD
```

Each line shows `BLOCK path:line [rule] message`, followed by `fix:`. Do what the fix says. If the rule is wrong for this one change, ask the user for an exception. There are two ways to record one:
- **Inline**, on the offending line or the line above:
  `cartographer-allow: <rule-id> until YYYY-MM-DD by <approver>: <reason>`
- **In `guardrails.json` → `exceptions`** (the user edits it):
  `{"rule", "path", "reason", "expires", "approvedBy"}`

Expired or incomplete exceptions stop suppressing and are reported. If the rule is wrong in general, that's a lesson. Run `/cartograph-learn`.

## Rules

- Never weaken or delete a rule to get a change through. That's the user's call, made in `guardrails.json`.
- Never fabricate evidence. Every SHA you cite comes from replay output or `git log`.
- Prefer one precise rule over several broad ones. A guard that cries wolf gets bypassed with `--no-verify`.
