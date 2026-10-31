---
name: cartograph-pr
description: Explain a pull request, commit or release range against the Cartographer model — what it touches, what it could break, what is missing, who should review it — and write a review guide or PR comment. Use when the user asks to review, summarize or assess a PR, asks what changed between two refs or in a release, wants a reviewer suggestion, or asks whether a change is risky.
---

# Cartograph PR

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

The numbers and signals come from `pr.py`; your job is to read the code behind the strongest signals and say what they mean for this change. Read `.cartographer/docs/quality-bar.md` once if you have not this session.

## 1. Compute

Make sure facts exist and are current (cheap):

```bash
python3 .cartographer/bin/collect.py
python3 .cartographer/bin/graph.py        # blast radius, test reach and new-dependency detection need it
```

Then one of:

```bash
python3 .cartographer/bin/pr.py --pr 123                       # open PR (fetches refs/pull/123/head)
python3 .cartographer/bin/pr.py --merged 123                   # merged PR found in history
python3 .cartographer/bin/pr.py --commit <sha>                 # one commit
python3 .cartographer/bin/pr.py --base v2.3 --head v2.4 --id release-2.4   # a release or any range
python3 .cartographer/bin/pr.py --base main --head HEAD --id my-branch     # your current branch
```

Add `--markdown pr-comment.md` to also produce a PR comment. Then `python3 .cartographer/bin/build.py` so the analysis appears under **Pull requests** in the viewer (`#/pr/<id>`).

The script prints the signals. The full result is `.cartographer/facts/prs/<id>.json`:
`touched`, `groups` (review order), `files` (category, hotspot rank, fixes, interfaces, new dependencies, pinned snippets and cited lines hit), `keyHunks`, `signals`, `missedCoChanges`, `reviewers`, `blast` (importers, inbound model edges, flows, tests), `modelDrift`.

## 2. Investigate the signals that matter

Work through `signals` in order (high → medium → info). For each high or medium one, open the code and decide whether it is a real problem:

| Signal | What to check |
|---|---|
| model-drift (pinned snippet) | Does the change alter the behaviour the snippet's annotations describe? If yes, that is the headline for reviewers. |
| new-dependency | Read the new import and its call site. Is the coupling intended? Is it against layering? Does it add a synchronous call, a transaction scope, a cycle? |
| missed-co-change | Open the partner file. Does this PR genuinely need a matching change there (mapper, view, schema, test), or is the historical coupling incidental? Say which. |
| interface (removed/changed) | Find consumers (`grep` the route/topic/tool name across the repo and, if you can, other repos). Is it backward compatible? |
| schema | Is the migration reversible? Is the application code compatible with both old and new schema during deploy? |
| knowledge | If the author is also the main owner, recommend a second reviewer from `reviewers`. |
| tests | Is the changed behaviour covered? Name the test that should exist if none does. |

Do not restate info signals unless they change the advice.

## 3. Write the result

For a review or summary in chat, lead with a verdict in one sentence, then:

1. **What it does** — 2–3 sentences in system terms (components, flows), not file names.
2. **Look closely at** — the 1–4 things a reviewer must check, each with `path:line`, from step 2.
3. **Probably missing** — confirmed missed co-changes, tests or consumer updates.
4. **Reviewers** — from `reviewers`, with the reason.
5. **Model impact** — snippets or cited lines that change; suggest `/cartograph-refresh` after merge.

For a PR comment, use the generated `--markdown` file as the base and add a short "Reviewer notes" section with your findings from step 2 above the signal table. Post it only if the user asked you to (for example `gh pr comment 123 --body-file pr-comment.md`).

For a release range, write a release brief instead: changes grouped by component and flow, interface and schema changes first, then notable fixes, then risks introduced or resolved.

## Rules

- Never claim a file "should" change without having opened it.
- Cite numbers exactly as computed (probability of co-change, hotspot rank, counts).
- Keep reviewer suggestions about knowledge of the code, never about people's speed or quality.
