# Quality bar

The reader is a senior engineer or architect who will act on this model. Every rule below exists because the failure it prevents makes the model worse than no model.

## Grounding

1. **Every claim cites code.** A component, edge, risk or decision without `evidence` is a guess. If you cannot find evidence, lower `confidence` or turn the claim into an open question.
2. **Numbers come from scripts.** Commit counts, line counts, authors, churn, bus factor, coverage: `collect.py`/`build.py` compute them. Never estimate or restate them in prose fields — they go stale on the next commit.
3. **Snippets are verbatim.** Extract with `bin/snippet.py`. Never retype, abridge with `...`, or "clean up" code.
4. **Documented vs inferred.** A decision is `documented` only if a document or comment in the repo states it. Otherwise it is `inferred`, and that is fine — say so.
5. **Read before you write.** Do not describe a component from its directory name. Open the entry points and key files first.

## Writing

- Plain, specific, short. "Publishes PolicyIssued before the transaction commits" — not "robust event-driven integration layer".
- No adjectives that don't carry information: *robust, scalable, seamless, comprehensive, powerful, modern, clean, elegant*.
- Name the surprising thing. A summary that could describe any service is useless; say what is unusual about this one.
- Use the codebase's own vocabulary (class names, topic names, table names) so readers can grep for it.
- Summaries ≤ 2 sentences. Responsibilities ≤ 5 phrases. Annotations ≤ 1 sentence.

## Selection

- **Fewer, better.** 8–25 components, 5–20 snippets, 2–6 flows, 3–12 risks for most repos. A large monorepo scales components, not verbosity.
- A snippet earns its place if a senior engineer would be wrong about the system without having seen it: entry points, load-bearing logic, invariants, transaction/consistency boundaries, integration seams, and gotchas. Not getters, DTOs, boilerplate or generated code.
- A risk needs a failure mode and a consequence, not a code smell. "Long method" is not a risk; "SOAP call inside DB transaction exhausts the pool under PolicyMaster latency" is.

## Coverage

- Aim for ≥ 85 % of lines mapped to components (`build.py` reports it; the overview shows it). Unmapped code is either deliberately out of scope (say so in `questions`) or a missing component.
- Generated, vendored and test code may stay unmapped; configure `exclude` in `.cartographer/config.json` rather than inventing components for it.

## Before you finish

- `python3 .cartographer/bin/check.py` passes with **0 errors**. Warnings are allowed only with a reason.
- `python3 .cartographer/bin/build.py` succeeds, and you have stated the coverage %, what changed in the model, and what remains unknown.
- `generated.commit` equals the commit you verified against.
