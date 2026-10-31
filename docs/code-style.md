# Code style guide

Every rule here is checked by the fence (`node .fence/bin/fence.mjs`) on every pull request, outside any agent.

| Category | Rule | Check | The gardener fixes it |
|---|---|---|---|
| Tests | Main and test code compile (maven) | `compile` | no |
| Style | Java code is formatted (Spring Java Format, as the build configures it) | `format` | yes |
| Practices | Checkstyle passes with the repository's configuration | `checkstyle` | no |
| Tests | All tests pass (mvn test) | `unit` | no |
| Security | No secrets (keys, tokens, private keys, hard-coded credentials) in the change | `secrets` | no |
| Security | Changed workflows: least privilege, no pull_request_target, no untrusted text pasted into run:, actions pinned | `workflows` | no |
| Patterns | A new npm dependency is approved in .fence/dependencies.json with a reason and a person | `dependencies` | no |
| Patterns | A new code file belongs to a component of the codebase map | `map-coverage` | no |
| Structure | Guard rules: layering, no new cycles, pinned code, the repository's own rules | `guard` | some (guard:ratchet) |
| Tests | Formal models in verification/: the code's model holds, broken variants still fail | `formal` | no |
| Structure | No expired exceptions | `exceptions` | no |
| Patterns | No new copied block (jscpd, 50 tokens) | `duplicates` | no |

Add the repository's own conventions below, each with the check that enforces it, or "a person" when no
script can. When a rule needs a check that does not exist yet, propose it with `/tend-fence`.
