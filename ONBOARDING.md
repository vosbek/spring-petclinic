# Software factory onboarding

Installed by onboard.sh on 2026-10-31).
Finish with the `factory-onboard` skill. A person does the items marked (person).

- [ ] Map the codebase: `/cartograph-survey` (a large repository: the area you change first)
- [ ] Guardrails from the map: `/cartograph-guard`; a person accepts proposals with `guard.py --accept`
- [ ] Review `.fence/fence.json` and `docs/code-style.md`; `node .fence/bin/fence.mjs` passes on a clean branch
- [ ] Check the formal model: `python3 .cartographer/bin/formal.py check` (needs Java 11+)
- [ ] (person) Create a private hidden-tests repository from the template's `factory/hidden-tests/`
- [ ] (person) Secrets `HIDDEN_TESTS_TOKEN`, `HIDDEN_TESTS_REPORT_TOKEN`, `GARDENER_TOKEN`; variable `HIDDEN_TESTS_REPO`
- [ ] (person) Required checks: Fence, Hidden tests, guardrails, and the repository's own CI; code owner review on
