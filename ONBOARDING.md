# Software factory onboarding

Installed by onboard.sh on 2026-10-31.
Finish with the `factory-onboard` skill. A person does the items marked (person).

- [ ] Map the codebase: `/cartograph-survey` (a large repository: the area you change first)
- [x] Guardrails from the map: `/cartograph-guard` proposed seven rules with their evidence from history
- [ ] Review `.fence/fence.json` and `docs/code-style.md`; `node .fence/bin/fence.mjs` passes on a clean branch
- [ ] Check the formal model: `python3 .cartographer/bin/formal.py check` (needs Java 11+)
- [x] (person) Create a private hidden-tests repository from the template's `factory/hidden-tests/` (vosbek/ci-verification)
- [ ] (person) Secrets `HIDDEN_TESTS_TOKEN`, `HIDDEN_TESTS_REPORT_TOKEN`, `GARDENER_TOKEN`; variable `HIDDEN_TESTS_REPO`
- [ ] (person) Decide the proposed guard rules: `python3 .cartographer/bin/guard.py --serve` opens the rules workbench; Accept or Reject each with a reason, then open one pull request
- [x] (person) Read-only rules page on GitHub Pages: Settings > Pages > Source "GitHub Actions"
- [ ] (person) Required checks: Fence, Hidden tests, guardrails, and the repository's own CI; code owner review on
