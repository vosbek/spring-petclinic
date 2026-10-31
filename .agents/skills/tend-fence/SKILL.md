---
name: tend-fence
description: "Decide what to do with each fence finding the gardener could not fix, one at a time - change the code, change the request, change the fence, or record an exception - with evidence for each choice and nothing applied until a person picks. Use when a pull request has the 'fence: needs a person' label, when the fence fails and the fix is not obvious, or when someone thinks a rule is wrong."
argument-hint: "[base ref, default origin/main]"
---

# Tend the fence

The fence (`node .fence/bin/fence.mjs`) decides whether a change is inside our code and process rules. The gardener fixes
what a script can fix safely. What is left comes to a person, and this skill helps them decide. It never
weakens the fence on its own: it gathers evidence and writes proposals. Arguments: `$ARGUMENTS`

## Steps

1. **Get the findings.** Run `node .fence/bin/fence.mjs --base origin/main` (or the base in the arguments) and read
   `.cache/fence/report.md`. On a pull request, the Fence comment has the same report. Hidden test failures
   show as requirement titles in the Hidden tests comment; their details are private, so treat each title
   as the behaviour that broke.
2. **For each finding, gather evidence before deciding.**
   - What the rule protects: the rule's comment in `eslint.config.mjs`, its section in `docs/fence.md` or
     `docs/code-style.md`, or `python3 .cartographer/bin/guard.py --explain <file>` for structure rules.
   - Whether the rule has been right before: for guard rules, `python3 .cartographer/bin/guard.py --replay --rule <id>`;
     for lint, duplicate and pattern rules, the baseline in `.fence/baselines/` shows how much of it the
     repository already carries and whether it has been going down. In a large repository, lint-style
     checks compare each changed file with the merge base, so a finding means this change added it.
   - What was asked: the pull request description or the task the agent was given.
3. **Put each finding in exactly one of four outcomes** (the shape is `templates/decision.md`):
   - **Change the code.** The rule is right and the code can meet it. Say how: the helper to reuse, the
     function to extract, the test to add. An agent can then make the fix.
   - **Change the request.** The rule is right, and the task as asked cannot be done inside it (a new
     package when a helper already does the job, a page that fetches from the network, a hand edit to a
     generated file). Reword the task so it can be done inside the fence and tell whoever asked.
   - **Change the fence.** The rule is wrong here: it flags correct code, it is broader than what it
     protects, or the thing it protects is gone. Write `templates/fence-proposal.md` with the evidence and a
     test that shows the new boundary (a unit test, or a hidden test the referee adds). Guard rules go to `.cartographer/guardrails.proposed.json`; fence configuration changes go in
     their own pull request, which CODEOWNERS routes to a person.
   - **Exception.** The rule is right in general and this case is a legitimate, temporary exception. Fill
     in `templates/exception.md`. The person adds it to `.fence/exceptions.json` with their own name as
     the approver and an expiry no more than 90 days out; the fence fails when it expires.
4. **Show the decisions as one table and stop.** Nothing is applied until the person picks an outcome for
   each finding. Default to "change the code" when the evidence is mixed: a fence that bends for one pull
   request bends for every agent after it.
5. **Close the loop.**
   - A class of problem that reached review or production without a check is a missing rule: propose one
     (`templates/fence-proposal.md`) and ask for a hidden test in the private test repository.
   - The same rule proposed for change twice in a month means the rule or its message is unclear: say so.

## Never

- Edit `.fence/`, the linter and formatter configuration, the fence, gardener or hidden test workflows,
  `.cartographer/guardrails.json` or `.cartographer/baseline.json`. The edit hook refuses,
  and CODEOWNERS requires a person on all of them.
- Write the approver's name on an exception, or pick an outcome for the person.
- Silence a finding in the code: `eslint-disable`, `@ts-ignore`, `@ts-expect-error`, `@ts-nocheck` and
  `prettier-ignore` comments are blocked by the `no-suppression-comments` rule.
