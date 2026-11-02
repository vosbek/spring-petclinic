# Software factory onboarding

Installed by onboard.sh on 2026-11-02.
Finish with the `factory-onboard` skill. A person does the items marked (person).

- [ ] Map the codebase: `/cartograph-survey` (a large repository: the area you change first)
- [ ] Guardrails from the map: `/cartograph-guard` proposes rules with their evidence from history
- [ ] Review `.fence/fence.json` and `docs/code-style.md`; `node .fence/bin/fence.mjs` passes on a clean branch
- [ ] Check the formal model: `python3 .cartographer/bin/formal.py check` (needs Java 11+)
- [ ] (person) Create a private hidden-tests repository from the template's `factory/hidden-tests/`
- [ ] (person) Secrets `HIDDEN_TESTS_TOKEN`, `HIDDEN_TESTS_REPORT_TOKEN`, `GARDENER_TOKEN`; variable `HIDDEN_TESTS_REPO`
- [ ] (person) Until then the Hidden tests check fails. To onboard first, set the variable `HIDDEN_TESTS_OPTIONAL` to `true`, and delete it once the token is set
- [ ] (person) The fence's guard check fails while no rule is accepted ("enforces nothing"). Accept rules first, or set `"guardMayBeEmpty": true` in `.fence/fence.json` and remove it after
- [ ] (person) Decide the proposed guard rules: `python3 .cartographer/bin/guard.py --serve` opens the rules workbench; Accept or Reject each with a reason, then open one pull request
- [ ] (person) Optional read-only copy on GitHub Pages: Settings > Pages > Source "GitHub Actions"
- [ ] (person) Required checks: Fence, Hidden tests, guardrails, and the repository's own CI; code owner review on; no bypass for administrators
