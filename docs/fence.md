# The fence, the gardener and hidden tests

The fence is every check that decides whether a change is inside this repository's code and process rules.
The gardener fixes what a script can fix safely and brings the rest to a person. Hidden tests check behaviour
with tests the author of a change cannot see. All three run in GitHub Actions, outside any agent. Agent hooks
(`.cartographer/hooks/`) give earlier feedback; trust in a merged change does not depend on them.

## The loop

```
change (person or agent)
  -> agent hooks and pre-commit (early feedback, local)
  -> pull request:  Fence · Hidden tests · the repository's own CI · Cartographer guardrails     (required)
  -> fence fails?   Gardener: fixers, every check again, commit only what a fixer changed and only if
                    nothing that passed now fails; otherwise label "fence: needs a person"
  -> a person runs /tend-fence: change the code | change the request | change the fence | exception
  -> merge
nightly: the gardener on the default branch, as a pull request
```

## Commands

```bash
node .fence/bin/fence.mjs                   # every check, against the merge base with origin's default branch
node .fence/bin/fence.mjs --only lint       # some checks
node .fence/bin/fence.mjs --fix             # what the gardener does
python3 .cartographer/bin/formal.py check   # the formal models (also the fence's formal check)
```

The report is `.cache/fence/report.md` and `report.json`. Exit 1 means a check failed.

## Configuration (`.fence/fence.json`, people own it)

- `builtins`: checks that need no setup: `secrets`, `workflows`, `dependencies`, `map-coverage`, `guard`,
  `formal`, `exceptions`, `duplicates`.
- `checks`: the repository's own tools. Each has `id`, `category`, `run` (an argument array, no shell),
  `parse` (`exit`, `lines` or `eslint-json`), `files` (`all`, or `changed` to append the changed files that
  match `glob`), and for `eslint-json`, `ratchet: "diff"`: the same command runs on the merge base for the same
  files, and only counts that went up are findings. That is how a large repository adopts strict rules
  without fixing everything first: existing findings never block, and no change may add one.
- `fixers`: commands per check id (or `guard:<rule>`); `{changed}` stands for the changed files.
- `lintCategories`: which fence category a lint rule belongs to; a `[category]` tag in a rule's message wins.

## Exceptions and approvals

`.fence/exceptions.json`: check id, file or folder, reason, the approving person's name, an expiry date. The
fence fails when one expires. `.fence/dependencies.json`: a new package's reason and approver.

## Who changes the fence

`.fence/`, the workflows and the guardrail files are owned by a person in `.github/CODEOWNERS`; with "Require
review from Code Owners" on, GitHub enforces it. Agents propose changes through `/tend-fence`.

## Hidden tests

A private repository, checked out in CI with `HIDDEN_TESTS_TOKEN`. The log shows counts and the titles of
failing tests, which are written as requirements. Full failure messages go to an issue in the private
repository; a referee agent that works only there posts a requirement-level hint on the pull request.
