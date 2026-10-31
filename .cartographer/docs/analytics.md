# Analytics reference

Every number in the viewer is computed by the toolkit from git, the import graph and (optionally) the GitHub API. This page defines each one and says what it cannot tell you. Agents cite these definitions; they never estimate the numbers.

Windows are anchored on the **HEAD commit date**, not "now", so a build is reproducible. Defaults: history window 365 days (`historyDays`), activity window 90 days compared with the 90 before.

## Activity

| Metric | Definition | Caveats |
|---|---|---|
| Commits · 90d | Non-merge commits in the last 90 days. Bot commits included in totals, excluded from people metrics. | Squash vs merge workflows change commit counts; compare a repo with itself over time, not across repos. |
| Change vs prior | (last 90 − prior 90) ÷ prior 90. Shown neutral when fewer than 6 commits in total. | Needs ≥ 180 days of history. |
| Component commits | Commits touching ≥ 1 file under the component's paths. | A commit touching two components counts for both. |

## Hotspots, defects and age

| Metric | Definition | Caveats |
|---|---|---|
| Hotspot score | commits in 90 days × log2(lines + 1), normalised to the top file = 100. Files with < 2 commits excluded. | Size is lines, not complexity. Generated files should be excluded in `config.json`. |
| Fix commit | Subject matches fix / bugfix / hotfix / bug / defect / regression / crash / incident / patch (or a `fix:` conventional-commit prefix), excluding typo / lint / format / docs. | Heuristic. Teams that don't say "fix" are under-counted; "fix typo"-style chores are filtered but not perfectly. |
| Defect hotspot | fix commits touching the file over the history window × log2(lines + 1). Files with < 2 fixes excluded. | Measures where fixes land, not where bugs were introduced. |
| Code age | Days between HEAD and the file's last change (all history, bounded by `maxCommits`). Component median is over files; shares are by lines. | Formatting-only commits reset age. |

## Coupling

| Metric | Definition | Caveats |
|---|---|---|
| Shared commits | Commits touching both files (or both components). | Commits with > 40 files and bot commits are ignored; docs are ignored; component coupling uses code, schema and interface files only. |
| Degree | shared ÷ mean(commits of A, commits of B) × 100 (Tornhill / code-maat). | Needs ≥ 3 shared and ≥ 4 commits per file. |
| P(B\|A) | shared ÷ commits of A — how often B changes when A does. Used for "missed co-change" in the PR lens (threshold 50%). | Historical habit, not necessity. The PR lens asks a human to judge. |
| Hidden dependency | Component pair with degree ≥ 20 and ≥ 5 shared commits, no edge in the model, and no import in either direction. | Requires `graph.py`. Common legitimate causes: shared database tables, message formats, copied rules, release choreography. |

File pairs where one is the other's test, and pairs of build/config/docs files, are left out of the file-pair list because they are expected.

## Knowledge

| Metric | Definition | Caveats |
|---|---|---|
| Owners | Authors by share of the component's commits over the history window (bots excluded). Case variants of a name are merged; use `.mailmap` for the rest. | Commit count, not lines or difficulty. Pairing and review knowledge are invisible. |
| Inactive | No commit anywhere in the repo in the last 90 days (relative to HEAD). | People who moved to another repo look inactive here. |
| Orphaned share | Share of the component's commits made by people now inactive. ≥ 50% = orphaned. | |
| Bus factor | Smallest number of authors covering more than half of the component's commits. *Active* bus factor counts active authors only. | Single-owner signals are suppressed in repos with ≤ 2 authors, where they are meaningless. |

This is a map of where knowledge sits, for planning reviews, pairing and handovers. It is not a performance measure and should not be used as one.

## AI-assisted work

| Metric | Definition | Caveats |
|---|---|---|
| AI-assisted commit | A `Co-authored-by:` trailer naming an AI tool (Claude, Copilot, Devin, Cursor, Codex, Gemini, Amazon Q, Windsurf, Aider, Cline), a "Generated with/by …" footer, or an AI bot author (for example `copilot-swe-agent`, `devin-ai-integration`). | Only credited use is visible. IDE completions and uncredited chat help are not. Treat shares as a floor. |
| Followed by a fix within 14 days | Share of non-fix commits for which a fix commit touched one of the same files within the next 14 days. Compared between AI-assisted and other commits. | Correlational. AI use clusters in particular areas and periods (rewrites, new code) that have different fix rates anyway. |
| Reverted | Share of commits later named in a "This reverts commit …" message. | Squash-merged reverts that don't use git revert are missed. |

## Static structure (`graph.py`)

| Metric | Definition | Caveats |
|---|---|---|
| Import edge | An in-repo import resolved to a file: Java/Kotlin/Groovy/Scala (imports plus same-package type references), Python (absolute and relative), JS/TS (relative, tsconfig paths, workspace packages), Go (module paths), C# (using + same namespace). | Regex-based. Reflection, DI by name, string-based wiring and HTTP between services are invisible. Resolution rate is printed per language. |
| Component dependency | Import edges aggregated by component (tests excluded). | |
| Model edge "verified" | The model's edge has at least one import in the same direction. "Not seen in imports" is expected for runtime wiring and messaging. | |
| Layer violation | A dependency from a component in a later layer to one in an earlier layer (layers are ordered as listed in the model; external excluded). | Only as good as the model's layer order. |
| Cycle | Strongly connected components of the component dependency graph. | |
| Interfaces | Declarations found by pattern: Spring/JAX-RS/Express/FastAPI/Flask/Django/ASP.NET/Go routes, MCP tools/resources/prompts, Kafka/Rabbit/JMS/SQS listeners and Kafka sends, scheduled jobs, JPA/SQLAlchemy/SQL tables. | Pattern-based; framework coverage is listed here, nothing else is detected. |
| Test reach | Test files importing at least one file of the component. | Black-box and end-to-end tests that call over HTTP don't import, so reach is a lower bound. |

## Pull requests (`prs.py`, `pr.py`)

| Metric | Definition | Caveats |
|---|---|---|
| Merged PRs | Git: squash commits ending "(#N)" and merge commits "Merge pull request #N". GitHub API adds open PRs. | Rebase-merged PRs without a number in the subject are invisible to git-only analysis. |
| Cycle time | PR created → merged (API). | |
| Lead time | First commit on the branch → merge (git, merge commits only). | |
| First review | PR created → first review by someone other than the author (API). | |
| Stale PR | Open ≥ 7 days, not draft, no review (API). | |
| Review concentration | Share of reviews given by the three busiest reviewers. | A distribution for spotting bottlenecks, not a leaderboard. |
| PR size | Lines added + deleted: XS < 10, S < 100, M < 400, L < 1000, XL ≥ 1000. | |

The PR lens (`pr.py`) signals are defined in `skills/cartograph-pr/SKILL.md`; every signal names the files and numbers behind it.
