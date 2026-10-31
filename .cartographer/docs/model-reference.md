# Model reference

`.cartographer/model/model.json` is the authored half of the mental model. Agents write it; `bin/check.py` validates it; `bin/build.py` joins it with computed git facts and renders the viewer. JSON Schema: `.cartographer/schema/model.schema.json`.

Two kinds of content, never mixed:

| | Where it lives | Who produces it | Example |
|---|---|---|---|
| **Facts** | `facts/*.json` (generated) | `collect.py` + `build.py` | 47 commits in 90 days, bus factor 1, 3,410 lines |
| **Claims** | `model/model.json` (authored) | agents, reviewed by humans | "Events are published before commit" + `PolicyService.java:61` |

Never write a number into the model that a script can compute. Never write a claim without evidence.

## Top level

```jsonc
{
  "schemaVersion": 1,
  "repo":        { "name", "summary", "purpose"?, "url"?, "blobUrl"? },
  "generated":   { "at", "commit", "branch", "by" },
  "readingPath": [ ReadingStop ],
  "layers":      [ Layer ],
  "components":  [ Component ],
  "edges":       [ Edge ],
  "flows":       [ Flow ],
  "snippets":    [ Snippet ],
  "decisions":   [ Decision ],
  "risks":       [ Risk ],
  "glossary":    [ Term ],
  "questions":   [ { "q", "context"? } ]
}
```

`repo.blobUrl` turns every file reference into a link. Placeholders: `{commit}`, `{path}`, `{line}`.

- GitHub / GHE: `https://github.com/org/repo/blob/{commit}/{path}#L{line}`
- Azure DevOps: `https://dev.azure.com/org/project/_git/repo?path=/{path}&version=GC{commit}&line={line}`
- Bitbucket Server: `https://host/projects/P/repos/r/browse/{path}?at={commit}#{line}`

Use the remote the team actually works in for `blobUrl`. If the README names a future home (say a move to an org repo), keep the current remote and add an open question.

`generated.commit` is the short HEAD the model was verified against. The viewer counts commits since then and `cartograph-refresh` uses it as the diff base.

## Evidence

```json
{ "path": "core/src/main/java/com/acme/PolicyService.java", "line": 61, "end": 64, "expect": "registerPolicy(", "note": "optional" }
```

The path is relative to the repo root. `line` and `end` are optional, because a directory or a whole file is enough for structural claims. `check.py` fails the build in two cases: the path is gone, or a line is past the end of the file.

**`expect` makes a line reference verifiable.** It's a short piece of text the cited line (or `line`..`end`) must contain, such as a method name or an annotation. Without it, an off-by-N line number passes every check while pointing at the wrong code. With it, `check.py` reports the mismatch and the line where the text really is. Add `expect` to every line-level citation.

Run `check.py --show-evidence` to print each cited line next to the claim, so you can read them all in one pass. Line numbers taken from `cat -n` output of several files at once are a common source of errors. Take them from one file at a time, or from `grep -n`.

## Layer

**Order matters.** List layers from the top (entry points, the things that call others) to the bottom (data, then `external` last). A component may depend on components in its own layer or any layer listed after it. A dependency on an earlier layer is a layer violation, and the `layering` guardrail blocks new ones. `build.py` prints each violation, so if you see many, check the order first.

`{ "id", "name", "description" }`. Default set: `entry`, `application`, `domain`, `integration`, `data`, `external`. Adapt to the codebase (e.g. `ui`, `batch`, `shared`) but keep 4–7 layers and keep `external` last — the viewer renders it as "outside this repo".

## Component

| Field | Notes |
|---|---|
| `id` | kebab-case, stable forever (links and history depend on it) |
| `name` | what engineers call it in conversation |
| `layer` | a layer id |
| `kind` | `service` `module` `library` `job` `ui` `adapter` `datastore` `external` `config` `test` |
| `paths` | repo-relative directories or files it owns. **Longest prefix wins** when mapping files → components, so a nested component can carve out of a parent. External systems have `[]`. |
| `summary` | 1–2 sentences. What it does and the one thing that's surprising about it. |
| `responsibilities` | 2–5 short phrases |
| `tech` | frameworks/libraries actually used (from build files, not guesses) |
| `keyFiles` | 2–5 `{ path, role }` — where a reader should start |
| `interfaces` | `{ kind: http|soap|grpc|event|batch|cli|ui, name, ref }` |
| `snippets`, `risks` | ids |
| `confidence` | `high` (read the code + evidence), `medium` (read entry points, inferred the rest), `low` (named from structure only) |
| `evidence` | at least one for non-external components |

Granularity: 8–25 components for most repos. A component is a unit someone would own, deploy, or reason about as one thing — never one per package.

## Edge

`{ "from", "to", "kind", "label"?, "evidence"? }` — `kind` is `calls` `reads` `writes` `publishes` `consumes` `depends`. Direction is "from depends on to". Label with the *specific* interaction ("registerPolicy · sync, in tx"), not the kind restated.

## Flow

```json
{ "id", "name", "trigger", "summary",
  "steps": [ { "component", "action", "snippet"?, "evidence"? } ] }
```

3–8 steps. Each step needs a snippet or evidence. A step's `evidence` is one evidence object, or a list of them; the viewer shows the first. Trigger is concrete: `POST /policies`, `Quartz · 01:30 daily`, `consume padm.policy.issued`.

## Snippet

Always created with `bin/snippet.py` so the code is verbatim and pinned.

| Field | Notes |
|---|---|
| `kind` | `entry-point` `core-logic` `invariant` `gotcha` `pattern` `integration` `config` |
| `path`, `start`, `end` | 5–40 lines |
| `code` | verbatim; never edited by hand |
| `why` | one or two sentences: why a senior engineer must have seen this |
| `annotations` | ≤ 4 `{ line, note }`, absolute line numbers, one sentence each |
| `pin` | `{ commit, sha256 }` set by the script |

## Decision

`{ "id", "title", "status": "documented" | "inferred", "context", "decision", "consequences", "evidence" }`. Documented = an ADR, design doc or explicit code comment in the repo. Inferred = reconstructed from code; say so, and add an open question if it matters.

## Risk

`{ "id", "title", "severity": "high"|"medium"|"low", "category", "detail", "components", "evidence" }`. Categories: `reliability` `security` `data` `performance` `maintainability` `knowledge` `compliance`. High = can lose data, money or availability today.

## Term (glossary)

`{ "term", "definition", "refs": [ Evidence ] }`. Glossary entries use `refs`, not `evidence`. Define domain words as the code uses them (`ChangeType`, `in force`, `pro-rata`), and point `refs` at the type or table that embodies each term.

## Question

`{ "q", "context"? }`. Questions are open items the code can't answer, such as which of two policies applies, whether a branch rule is on, or who owns an external system. One line each. Put the evidence that raised the question in `context`.

## Reading path

`{ "title", "target", "note" }`, 4–7 stops, in the order a new senior engineer should read them. `target` is `component:<id>`, `flow:<id>`, `snippet:<id>`, `risk:<id>`, `decision:<id>`, `file:<path>` or `view:<overview|architecture|flows|code|changes|risks>`.
