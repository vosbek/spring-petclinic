---
name: factory-onboard
description: "Finish onboarding this repository to the software factory after onboard.sh ran: map the codebase, turn the map into guardrails, review the fence configuration the script detected, record what the repository already carries, seed hidden tests and a first formal model, and hand the person the settings only they can change. Use right after onboarding, or when ONBOARDING.md has unticked items."
argument-hint: "[area to map first, default the whole repository]"
---

# Finish onboarding

`onboard.sh` installed the toolkit, the fence, the workflows and the skills, and detected the repository's own
tools. This skill does the parts that need reading the code. Work through `ONBOARDING.md` and tick each item.
Arguments: `$ARGUMENTS`

## Steps

1. **Map the codebase.** Run `/cartograph-survey`. For a large repository, map the area named in the
   arguments first, at the depth a change there needs, and say which areas the map does not cover yet.
2. **Guardrails from the map.** Run `/cartograph-guard`: proposed rules go to
   `.cartographer/guardrails.proposed.json`, each replayed against history. A person decides them in the rules workbench (`python3 .cartographer/bin/guard.py --serve`, in their own terminal); the agent hooks refuse it from you.
3. **Review the fence.** Read `.fence/fence.json` and `docs/code-style.md`. Run
   `node .fence/bin/fence.mjs` on a clean branch: every check should pass or skip with a reason. A check
   that fails on an unchanged branch is configured wrong; fix the configuration, never the code, and
   tell the person.
4. **Seed hidden tests.** Copy the template's `hidden-tests/` into a new private repository (a person
   creates it) and write two or three tests of behaviour users depend on, titled as requirements.
5. **One formal model** where a bug costs the most, with the `formal-models` skill.
6. **Hand off the settings only a person can change**: required checks, code owner review, the secrets
   (`HIDDEN_TESTS_TOKEN`, `HIDDEN_TESTS_REPORT_TOKEN`, `GARDENER_TOKEN`) and `vars.HIDDEN_TESTS_REPO`.
