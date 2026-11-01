---
name: cartograph-learn
description: Find the mistakes this repository keeps repeating (reverts, changes that needed a fix within two weeks, skipped co-change partners, edited migrations, recurring review comments, guard rules agents keep hitting) and make each one structurally impossible or caught, choosing architecture, then a guardrail, then a test, with docs last. Each fix is proven against the real past mistakes. Use for "learn from history", "why do we keep breaking X", "make this stop happening", after an incident, or on a regular cadence.
---

# Cartograph learn

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

Assume every contributor is an agent that sees only the files it opened, copies the nearest example, and takes the shortest path that compiles. Change the repo so that a change that looks right from one file is right for the whole repo. A lesson written only as prose is the weakest kind. Nothing fails when the next agent skips it.

## 1. Harvest

```bash
python3 .cartographer/bin/collect.py && python3 .cartographer/bin/graph.py
python3 .cartographer/bin/learn.py --api          # --api adds PR review comments when gh is authenticated
```

`learn.py` prints classes and writes `.cartographer/facts/lessons.json`:
- **Classes.** Each has its kind, key (file, file pair or folder), occurrences, examples (SHA, subject and what followed), and coverage by today's rules. Coverage is "gap", "partly covered" or "covered", decided by replay.
- **`ruleHealth`.** Rules that never fire, noisy rules, rules with no lift, and exceptions that are expired or expiring.
- **`guardLog.repeats`.** Rules agents keep tripping.
- **`reviewComments`.** Recent human review comments and the most-discussed files.

Definitions are in `.cartographer/docs/analytics.md` and the header of `learn.py`. A follow-up fix is correlation. Read the commits before calling anything a mistake.

## 2. Name the real classes

Work top-down through the gap and partly-covered classes with 2 or more occurrences:

1. Open two or three examples and the commits that fixed them (`git show <sha>`).
2. Write down the mistake in one sentence, in system terms. For example: "UI routes add a field without adding it to the API client", or "parsers change without the round-trip fixture".
3. Merge classes that are the same mistake, and drop ones that turn out to be ordinary iteration.

Review comments count when the same point recurs on 2 or more PRs.

## 3. Fix each class at the highest level that works

1. **Architecture: make it impossible.** Give each piece of state one owner and each task one supported way. Replace hand-synced lists with one source of truth, for example by generating the client from the API. Delete the old path an agent would copy.
2. **Guardrail or the repo's own type/lint: make it fail with the fix named.** Pick the rule type that matches the mistake:
   - skipped partner: `co-change`
   - forbidden dependency: `forbid-import`
   - forbidden construct in new lines: `forbid-pattern`
   - forbidden content anywhere in a file, such as a retired name: `tree-pattern`
   - two lists that must agree, such as a UI list and a schema enum, or CSS and markup: `same-set`
   - two values that must agree, such as a UI limit and a schema limit: `same-value`
   - edited immutable file: `protected-paths`
   If the pattern is already common, the ratchet or a rule scoped to new lines keeps it from growing.
3. **Test: pin the behaviour.** Write the test the fixes show was missing. Use `tests-required` scoped to the file only if the file keeps getting fixed and has no test importing it.
4. **Docs or agent instructions: judgment calls only, written last.** Include the failure example. Nothing fails when an agent skips them.

## 4. Prove it

- **Guardrail.** `learn.py --propose` writes candidates for gap classes. For hand-written rules, add them to `.cartographer/guardrails.proposed.json`. Then replay it (proposed rules replay too):
  ```bash
  python3 .cartographer/bin/guard.py --replay --rule <id>
  ```
  It must fire on the class's example commits and on few others: under ~5% of commits, unless the rule protects something irreversible.
- **Test.** It must fail on the commit before the fix and pass after:
  ```bash
  git stash && git checkout <fix-sha>^ -- <file>
  ```
  Restore afterwards.
- **Architecture.** Show the old path no longer exists (`grep`), and add a `forbid-import` so it can't come back.

## 5. Write the outcomes

Record the analysis in `.cartographer/outcomes.json`; `toolkit/docs/guardrails.md` (Outcomes report) has the format.
- **`missed`:** one item per problem that reached the default branch. Give the commit that introduced it, the commit that fixed it, who found it, and the check. Read the fix commit's diff, not only its subject. Find the introducing commit with `git log -S'<the line the fix changed>'`.
- **`gaps`:** risks with no incident yet, each with a made-up change that proves the rule.
- **`cannotBlock`:** what only a person can judge, each with what the reviewer does. Cite a past commit where one exists.
- **`reviewerQuestions`** and **`probes`:** for the Blocked section.

Every claim cites a commit, and nothing is unverified. If a check can't be replayed (a build or render test), say so in `evidence`.
Then render it:
```bash
python3 .cartographer/bin/guard.py --simulate-history
python3 .cartographer/bin/guard.py --outcomes outcomes.html
```
A proposed check that fires today must have its findings fixed before it is accepted. The report lists them under Decisions.

## 6. Hand over

Present one row per class:
- the class, with its evidence (SHAs)
- the level you chose, and why a higher level didn't work
- the proof (replay counts, or test fail-then-pass)
- what is waiting for approval

The person decides in the rules workbench (`python3 .cartographer/bin/guard.py --serve`, in their own terminal; the agent hooks refuse it from you). After their pull request merges:
```bash
python3 .cartographer/bin/guard.py --table
```
Update the rule table in CLAUDE.md / AGENTS.md from the `--table` output.

## 7. Rule health

Act on `ruleHealth`:
- **Never fired.** Propose removal unless the rule protects something irreversible or encodes a model decision. Drop a rule once its mistake can't happen any more.
- **Noisy (≥ 25% of commits).** Narrow its paths.
- **No lift.** It fires on commits that are fixed less often than average, so it isn't catching mistakes. Narrow it or drop it.
- **Exceptions.** Expiring ones go back to their approver. Expired ones mean the violation is live again.
- **Repeats in the guard log.** Agents keep hitting the rule, so they find out too late. Make sure `guard.py --explain` shows it for those paths. Mention it in the area's skill or the model's component notes.

## Cadence

The nightly workflow runs `learn.py --api` and `guard.py --suggest` and uploads the results. Run this skill weekly, after an incident, or whenever the user corrects an agent for the same thing twice.
