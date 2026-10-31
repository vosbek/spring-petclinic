---
name: formal-models
description: "Model the part of this repository where a bug costs the most and tests reach the least (crash windows, retries, state machines, loops that commit or publish) as TLA+, check it with TLC, draw it with pstack's statechart in the reader's words, replay each bug path on the real code, and keep the model in verification/ so the fence checks it on every change."
argument-hint: "[area or bug]"
---

# Formal models

A test checks the cases someone wrote. A model checker checks every order of steps in a small model of the
design, including a process killed between any two writes. Arguments: `$ARGUMENTS`

## Steps

1. **Pick the target from the code**, or start from the area the user named: a promise the code makes,
   where a break harms users and tests rarely reach the case that breaks it. Writes to two places,
   moves between folders or states, retries, jobs that commit, push or publish.
2. **Write the model** in `verification/<area>/`: `SPEC.tla`, one `.cfg` per variant, `view.ts` for the
   chart, and `expect.json` naming which rules each variant breaks. Each action names the code it stands
   for in a comment. Write each rule a reader agreed to as an INVARIANT. Model the current code as one
   variant and at least one broken or older version as another, so the model is seen to fail.
3. **Check and draw it.** `python3 .cartographer/bin/formal.py check` runs every model; `formal.py chart
   <folder> <config>` prints the statechart report: each broken rule with its shortest path in plain words.
   The first run fetches pstack's statechart skill and TLC into `.cartographer/vendor/` (git-ignored).
4. **Replay every bug path on the real code**, one step per action, in a throwaway worktree. A path that does
   not reproduce means the model is wrong: fix the model, not the rule.
5. **Report findings as findings.** Do not weaken a rule or change product code unless the user asks. When a
   fix is agreed, add the fixed variant and show that each partial fix still breaks a rule if that is so.
6. **Leave a verify skill** for the area (pstack's create-formal-verification-skill describes what it must
   hold: coverage, the last commit the model was confirmed against, how to tell a real bug from a model error).
