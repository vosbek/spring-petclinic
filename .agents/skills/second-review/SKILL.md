---
name: second-review
description: Get an independent review of a change from a model of a different family than the one that wrote it, then sort its findings into act on, consider, noted and dismissed. Nothing is applied automatically. Use for code changes before asking a person to review, or when the user asks for a second opinion.
argument-hint: "[base ref, default origin/main]"
---

# Second review from a different model

The author model shares its own blind spots. A model from a different family reads the same diff against the
same rubric. Arguments: `$ARGUMENTS`

## Steps

1. **Collect the change.** `git diff --stat origin/main...HEAD` and `git diff origin/main...HEAD` (or the base
   named in the arguments). Leave out generated files and data.
2. **Ask a reviewer from another family.** If Claude wrote the change, use GitHub Copilot CLI with a model from a
   different family (pick it with `/model`). If a Copilot model wrote it, use Claude Code. Give it the diff, the
   rubric in `templates/rubric.md`, and `python3 .cartographer/bin/guard.py --explain <changed files>` so it knows
   the rules that already apply. Ask for findings only, each with a file and line.
3. **Sort every finding** into the shape of `templates/findings.md`:
   - **Act on:** a real defect, with a concrete input that breaks it.
   - **Consider:** a reasonable improvement that is not a defect.
   - **Noted:** true, but out of scope for this change.
   - **Dismissed:** wrong, or already covered by a check (name the check).
4. **Do not apply anything yet.** Show the sorted list to the user. Fix "act on" items only once they agree. A
   finding a check could have caught becomes a proposed rule in `.cartographer/guardrails.proposed.json`,
   proven with `python3 .cartographer/bin/guard.py --replay --rule <id>`.
