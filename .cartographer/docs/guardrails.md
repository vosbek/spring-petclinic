# Guardrails reference

`guard.py` checks a change against the rules in `.cartographer/guardrails.json`. Every rule is a script over the diff, the model and the import graph. No rule calls an LLM, so the same change always gets the same verdict.

| Where it runs | Command | What it sees |
|---|---|---|
| Agent edit and command hook (Claude Code `PreToolUse`) | `hooks/protect.py` | The tool call before it runs. For a file edit it sees the target and the lines being added: it enforces `protected-paths` and edit-time `forbid-pattern` rules, and refuses edits to `guardrails.json` and `baseline.json`. For a shell command it enforces `forbid-command`. |
| Agent stop hook (Claude Code `Stop`, Cursor `stop`) | `hooks/stop_guard.py` | This branch plus uncommitted work, compared with the default branch. |
| Git pre-commit | `hooks/pre-commit` | Staged changes. |
| CI (`cartographer-pr.yml`, job `guardrails`) | `guard.py --base origin/<base> --head HEAD` | The PR. Facts are recomputed on the head, so the ratchet is evaluated. |
| You, before editing | `guard.py --explain <files>` | The rules that apply, the components you may import, and the files that usually change together. |

Exit code 1 means a blocking rule failed. Warnings never fail.

## File format

```json
{
  "schemaVersion": 1,
  "rules": [
    {
      "id": "applied-migrations",
      "type": "protected-paths",
      "severity": "block",
      "paths": ["db/migrations/**"],
      "allowAdd": true,
      "description": "Applied migrations are immutable.",
      "fix": "Write a new versioned migration that makes the change.",
      "evidence": ["3f2a91c0d4e1", "decision:migrations-forward-only"],
      "source": "learned",
      "added": "2026-10-29",
      "status": "active"
    }
  ],
  "exceptions": [
    { "rule": "co-change-mapper-schema", "path": "core/mapper/LegacyMapper.java",
      "reason": "Legacy mapper is frozen until the Q1 cutover", "expires": "2027-01-31", "approvedBy": "a.lead" }
  ]
}
```

Every rule has these fields:

| Field | Meaning |
|---|---|
| `id`, `type` | Required. `id` is what shows in every error message. |
| `severity` | `block` (the default for most types) or `warn`. |
| `description` | What the rule requires. It appears in `--explain` and in the rule table. |
| `fix` | What to do instead. The error message prints it, so name the file, type or function. |
| `evidence` | The commits, PRs or model decisions that justify the rule. |
| `source` | `suggested`, `learned` or `manual`. |
| `status` | `active`. Anything else means the rule is not enforced. |
| `enforce` | Where the rule runs. `"diff"` means the Stop hook, pre-commit and CI; `"edit"` means the agent's tool call, before it runs; or a list of both. Defaults: `protected-paths` runs at both, `forbid-command` at edit only, and everything else at diff only. Use `"edit"` for rules about how a change is made rather than what it contains. Examples: an agent must not type `reviewedBy:` by hand, while the curator's publish script writes the same line legitimately; or generated files must not be hand-edited, while their sync script rewrites them. |

The `judgment` list in `guardrails.json` holds rules that need a person: `{ "text", "why", "source" }`. They are not enforced, but they are listed next to the enforced ones in the viewer and in `--table`, so the boundary is visible.

**Selectors** (the `paths`, `when`, `require`, `from` and `to` fields) take one entry or a list of them:
- **Globs, gitignore-style.** `**` spans directories. A pattern with no `/` matches a basename anywhere.
- **`component:<id>` and `layer:<id>`.** Match files by the model.
- **`tests` and `code`.** Match by file category.
- **`!<entry>`.** Excludes whatever the entry matches.

## Rule types

| Type | Fires when | Fields | Default |
|---|---|---|---|
| `layering` | An added import makes a component depend on one in an earlier layer, against the model's layer order. Tests are exempt. | `paths` (optional scope) | block |
| `no-new-cycles` | An added import closes a cycle between components, counting the current graph plus this change. | | block |
| `forbid-import` | A file matching `from` adds an import that resolves to a file matching `to`, or whose text matches the `module` regex. | `from`, `to` and/or `module` | block |
| `forbid-pattern` | An added line in `paths` matches `pattern` (a regex). Tests are skipped unless `ignoreTests: false`. | `paths`, `pattern` | block |
| `co-change` | A file matching `when` changes and nothing matching `require` does. `{stem}` in `require` stands for each trigger file's name without its extension, for example `src/**/{stem}.test.ts`. | `when`, `require` | block |
| `protected-paths` | A matching file is modified, deleted or renamed. Additions are allowed with `allowAdd`, deletions with `allowDelete`. | `paths` | block |
| `interface-change` | A declared interface (route, MCP tool, topic listener) is removed (`on: "removed"`) or touched (`on: "any"`), and nothing matching `require` changed. | `paths`, `kinds`, `on`, `require` | warn |
| `tests-required` | At least `minLines` lines of code change in `paths` with no test file changed. | `paths`, `minLines`, `tests` | warn |
| `pinned-snippets` | A changed file no longer contains the code of a snippet the model pins to it, and the model wasn't re-pinned. Moved code is fine. | | block |
| `max-size` | Changed lines exceed `max`. Lockfiles, snapshots and minified files don't count. | `max`, `exclude` | warn |
| `require-added-pattern` | A file matching `paths` with a git status in `fileStatus` (default `["M"]`) changes without adding a line matching `pattern`. An example is a published brief edited without a dated correction note. | `paths`, `pattern`, `fileStatus`, `needs` | block |
| `path-convention` | A new or renamed file under `paths` has a name that doesn't match `pattern`. Names matching `ignore` are skipped. | `paths`, `pattern`, `ignore` | block |
| `same-value` | The first capture group of each `sources[].pattern`, read from each file at the head of the change, isn't equal across files. An example is package.json's version against the newest changelog entry. It's checked when one of the files changes, or on every change when `always` is set. `skipMissing` skips it while a source file doesn't exist. | `sources`, `always`, `skipMissing` | block |
| `tree-pattern` | A repository check, judged on whole files at the head of the change, like a test. With `mode: forbid` (the default) each line in `paths` matching `pattern` fails, changed or not. With `mode: require`, each file in `paths` (only those with a line matching `onlyIf`, if set) must contain a matching line. It runs on every change unless `when` limits it. Example: page copy must not name a retired feature. | `paths`, `pattern`, `mode`, `onlyIf`, `when`, `message`, `needs` | block |
| `same-set` | A repository check: two lists drawn from the code disagree. Each side (`left`, `right`) is a spec or a list of specs: `paths`, an optional `within` regex that narrows the text (group 1), an optional `strip` regex removed first, a `pattern` whose group 1 is collected, and optional `tokens` to split each value. `relation` is `equal` (default) or `subset`. It's skipped while a side matches no file. Examples: a legend against a schema enum, print CSS classes against the markup. | `left`, `right`, `relation`, `leftName`, `rightName`, `when` | block |
| `forbid-command` | An agent's shell command matches `pattern`. It runs at edit time only. | `pattern` | block |
| `ratchet` | A structural count rises above `.cartographer/baseline.json`. The counts are layer violations, cycles, dependencies not in the model, hidden dependencies, untested components and model check errors. It is evaluated only when facts match the head (CI, or `--refresh`). | `metrics` | block |

Imports are resolved by the same resolvers as `graph.py` (JVM, Python, JS/TS, Go, C#). An import that doesn't resolve to a repo file can only be matched by `forbid-import`'s `module` regex.

## Exceptions

There are two ways to make an exception. Both need a reason, an expiry date and an approver:
- **Inline**, on the offending line or the line above it:
  `cartographer-allow: <rule-id> until 2027-01-31 by a.lead: <reason>`
  An inline marker counts only once it is on the base branch: one added in the same change is reported ("does not count") and suppresses nothing, because nobody checks the name it gives. Land it in its own reviewed change first, or use the config. A repository can opt back in with `"inlineAllowInChange": true` in `guardrails.json`.
- **In config**, as `exceptions[]` in `guardrails.json`, with `path` as a selector.

An expired or incomplete exception stops suppressing anything and is reported as a warning.

In CI the rules, exceptions and baseline come from the base branch: `guard.py --rules-from <base checkout>/.cartographer` (the fence passes it). A change cannot loosen the rules that judge it. With no active rules, `--json` still writes a result (`"rules": 0`) so a caller can tell "nothing enforced" from "never ran"; a rule that cannot be evaluated is an error and makes `ok` false. Suppressed violations stay visible in the report and in the PR comment.

## Proving rules: replay

```bash
guard.py --replay [--rule ID ...]
```

Replay runs each rule against every non-merge commit in the history window and reports:
- how many commits it fired on
- how many of those were reverted, or followed by a fix to one of the same files within 14 days
- the same rate across all commits (the base rate)

A rule worth keeping fires rarely and catches commits that needed fixing more often than the base rate. That ratio is `lift`. The comparison is correlational. Big or risky changes attract fixes anyway, so read the examples before you trust the number.

Replay applies today's rules, model and import resolution to old diffs. Repository checks (`tree-pattern`, `same-set`, `same-value`) read the files as they were at each commit. `pinned-snippets` and `ratchet` aren't replayable.

## Outcomes report

```bash
guard.py --simulate-history
guard.py --outcomes outcomes.html
```

The outcomes report answers what the guardrails take off a reviewer's desk. It opens with **how much less review**: for every commit, the questions a reviewer would ask (rules and judgment items that apply to the changed files), split into those a check answers and those left for a person, with worked examples. Then **the workflow** from idea to merged PR with each checkpoint, and three sections:

1. **Blocked.** Each rule as the reviewer question it retires, with the past commits it would have sent back (from the history simulation). It also runs `probes` now: commands and edits an agent should never make, checked against the real hook code.
2. **Got through.** Problems that reached the default branch and were caught by a person, each with the check that would catch it next time. A rule check is replayed over every commit. It should fire at the commit that introduced the problem, go quiet after the fix, and be clean today. A strip of cells shows each commit. Checks that aren't rules (a build check, a render test, a CI step) are listed as not replayable. Risks with no incident yet are proven on a made-up change.
3. **Never blockable.** What only a person can judge, with what the reviewer does. This list plus the `judgment` entries is the whole of review.

The analysis lives in `.cartographer/outcomes.json`, written by the `cartograph-learn` skill from the history and reviewed by a person:

| Key | Content |
|---|---|
| `reviewerQuestions` | rule id → the question a reviewer used to answer by eye |
| `probes` | `{label, kind: "command", command}` or `{label, kind: "edit", path, lines}` |
| `missed[]` | `{id, title, what, introduced: [sha], fixed: sha, foundBy, check: {kind: rule\|build-check\|test\|ci-command\|probe, rule?, description?, note?, probe?}, evidence?, status: proposed\|covered}` |
| `gaps[]` | Like `missed`, without commits. `check.probes[]` holds `{label, path, lines, expect}` made-up changes. |
| `cannotBlock[]` | `{text, why, review}` |
| `blockedNote` | A sentence on how today's conventions differ from the past ones the simulation judges |
| `cannotBlock[].appliesTo` | Globs: the judgment item applies to a change that touches these files (for the review-load count) |
| `skipPaths` | Generated files a guardrail protects, so a reviewer can skip their lines |
| `examples[]` | `{label, sha}` or `{label, base, head, subject}`: real changes shown before and after, every check run on them |
| `workflow` | Optional wording for the idea-to-merge diagram: `edit`, `command`, `ciTitle`, `ci`, `mergeSub` |

Proposed rules in `guardrails.proposed.json` link to their item with `addresses: <item id>`, or by `check.rule`.

## Fence and review page

```bash
guard.py --story fence.html
```

This is one page for a team, in two parts.

**How it works:**
- The idea-to-merged-PR workflow with every guardrail and verification point, drawn.
- The improvement loop, drawn.
- The turns of that loop the repository has taken (`loopTurns`).

**What it does to review:**
- Review topics in three groups: don't review, review less (a check points the way), still review.
- Estimated reviewer minutes per commit before and after, from `timeModel`.
- Run cards. Each card is labelled **real** (an agent session's hook log, the live hook probes, a saved verifier report, a CI comment with screenshots) or **replayed** (a past commit judged by today's checks).

| Key in outcomes.json | Content |
|---|---|
| `timeModel.rules` | Rule id → minutes a reviewer spends on that question by eye |
| `timeModel.checks[]` | Checks that are not rules (build leak check, site verification, tests): `{id, label, appliesTo, minutes, by}` |
| `timeModel.person` | Judgment item text → `{minutes, withHelp, help}` |
| `timeModel.reReviewMinutes` | Minutes saved for each change sent back before review |
| `loopTurns[]` | `{when, what, by, change}`: problems found and what changed in the fence |
| `runs[]` | `{label, real, note}`, plus one source: `log`/`task`/`answer` (an agent session), `source: "probes"`, `report` (a verifier report.json), `comment` + `images` (a CI comment and screenshots), or `sha` (replay) |

Save run evidence under `.cartographer/runs/` so the page can be rebuilt. The time figures are estimates. The page says so, and its assumptions table lists every number.

## Suggesting, proposing and accepting

| Command | What it does |
|---|---|
| `guard.py --suggest [--write]` | Derives candidates from the facts and replays each one. Candidates come from layers, cycles, pinned snippets, strong co-change pairs (ranked by how often skipping the partner was followed by a fix to it), versioned migration folders, interface declarations, the repo's test convention, PR size and today's structural counts. |
| `learn.py --propose` | Proposes rules for mistake classes that no current rule covers. |
| `guard.py --proposals` | Lists `guardrails.proposed.json`. |
| `guard.py --serve` | The rules workbench, for a person: every proposal with its replay evidence, Accept and Reject with a reason, then "Open pull request" (only the two rule files, committed as the person, opened with their `gh` login). Runs on 127.0.0.1 and refuses to start without a terminal. The agent hooks refuse it, `--accept` and `--reject`. `rules_app.py export OUT` writes the read-only copy CI publishes. |
| `guard.py --accept ID ... --why TEXT` and `--reject ID ... --why TEXT` | The same decision from the command line, for a person. The reason, who decided (`--by`) and the replay evidence are recorded with the rule. |
| `guard.py --baseline [--force]` | Records the ratchet counts. Without `--force` a count can only go down. |
| `guard.py --table` | Prints the rule table for CLAUDE.md / AGENTS.md. |

## Rule health

`learn.py` flags rules for review in these cases:
- **Never fired** in the window. Keep the rule only if it guards something irreversible or encodes a decision.
- **Fires on 25% or more of commits.** It's noise.
- **No lift.** It fires on commits that are fixed less often than average.
- **Exceptions.** They are expired, incomplete or expiring within 14 days.
- **Repeats in the guard log.** Agents hit the same rule in two or more runs, so it surfaces too late.

## Design notes

- **Agents can't edit the rules.** Agents propose and humans accept. The edit hook enforces this, and CI is the backstop, because a hook can be bypassed locally (`--no-verify`, a different editor).
- **A rule is a lever.** If a mistake keeps happening, it's cheaper to make it fail than to write it down again. Follow the order in `/cartograph-learn`: architecture, then guardrail or type, then test, then docs.
- **Determinism is the point.** If a rule needs judgment to apply, it isn't a guardrail. Put it in the model's decisions or the agent instructions.
