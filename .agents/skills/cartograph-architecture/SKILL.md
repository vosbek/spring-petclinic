---
name: cartograph-architecture
description: Deepen the Cartographer model's architecture — split or merge components, prove dependency edges, map external systems, and trace end-to-end flows (request paths, jobs, event handlers) step by step with code references. Use when the user asks how something works end to end, wants a flow traced, says the architecture view is wrong or too coarse, or asks to add a component, edge or flow.
---

# Cartograph architecture

> Commands below use `python3`; on Windows use `py -3` or `python` if `python3` is not on PATH.

Works on an existing `.cartographer/model/model.json`. If there is none, run `cartograph-survey` first. Read `.cartographer/docs/quality-bar.md` before editing.

## Tracing a flow

1. **Pick the trigger** concretely: an HTTP route, a scheduled job, a consumed topic, a CLI command, a UI action. Find its handler by searching for the route string, cron expression, topic name or command name.
2. **Follow the call path** through the code, one hop at a time. At each hop record: which component it is in (by path → component mapping), what it does in one line, and `path:line` of the call site or method.
3. **Stop at the boundary** of the system (a datastore write, an outbound call, a published message, a response) — that external system is the last step.
4. **Collapse** to 3–8 steps. Consecutive hops inside one component become one step. Keep every hop that crosses a component boundary, opens/closes a transaction, retries, or swallows an error.
5. **Promote** the 1–2 most important steps to snippets (`cartograph-snippets`); the rest carry `evidence`.
6. Write the flow with `id` (kebab-case), `name` (verb phrase: "Issue a policy"), `trigger`, a 1–2 sentence `summary` that states the surprising part (e.g. "…all inside one database transaction"), and `steps`.
7. Add any edge the trace proved that the model lacked, with the call site as evidence.

## Editing components

- **Split** when a component has two unrelated responsibilities or two owners (check the build's ownership data on the component's History tab or in `facts`). Give the new component a narrower `paths` entry — longest prefix wins, so a nested path carves out of its parent without editing the parent.
- **Merge** when two components are always changed together and have no independent consumers.
- **Rename** only `name`; never change an `id` once published — reading paths, links and people's bookmarks use it. If an id is truly wrong, keep the old one.
- **External systems**: one component per real system (database schema, queue cluster, third-party API, mainframe), `layer: external`, `paths: []`, evidence = the config that points at it.

## Use the static graph

Run `python3 .cartographer/bin/graph.py` and `build.py`, then open the viewer's **Structure** page (or `facts.structure` in `.cartographer/dist/cartograph.json`):

- **In code, not in the model** — import dependencies between components that the model lacks. Add the real ones as edges (with the example import as evidence); a large count usually means a component boundary is wrong.
- **Model edges unseen in imports** — fine for runtime wiring (DI by name, HTTP, messaging); otherwise re-check the edge.
- **Cycles and layer violations** — record the ones that matter as risks, with the importing line as evidence.
- **Interfaces** — routes, MCP tools, topics, jobs and tables found in code; use them for `components[].interfaces` and flow triggers.

The graph is evidence, not the model: it cannot see reflection, DI by string or calls between services.

## Proving edges

Acceptable evidence, strongest first: an injected dependency or constructor parameter; a client call site; producer/consumer config naming a topic/queue; SQL or ORM mapping naming a table; an import across component boundaries. A shared library being on the classpath is not an edge by itself.

Use `kind` precisely: `calls` (sync request), `publishes`/`consumes` (async), `reads`/`writes` (data), `depends` (compile-time/library). Put the specific operation in `label`.

## Finish

```bash
python3 .cartographer/bin/check.py && python3 .cartographer/bin/build.py
```

Report what you added or changed in two or three lines, and any open question the trace raised (also add it to `questions`). Update `generated.commit`/`at` only if you verified the whole model against the new HEAD; otherwise leave them.
