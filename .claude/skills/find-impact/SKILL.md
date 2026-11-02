---
name: find-impact
description: Work out which systems and which parts of them a business intent record changes, from the codebase maps and the code, and record each with evidence for the architect to confirm. Use after /capture-intent, when someone asks which systems or teams a request affects, or types /find-impact with an intent record.
argument-hint: "<intent/short-id> [system=path/to/model.json ...]"
---

# Find which systems change

Today a solution architect and product architects work this out from memory, and it takes weeks. You draft it from the
codebase maps and the code, with a citation for every claim, and the architect confirms or corrects it.

## 1. Gather the maps

Each candidate system needs its codebase map: `<repo>/.cartographer/model/model.json`. Use the repositories the person
names, or the ones checked out next to this one. A system without a map cannot be placed with evidence; say which ones
are missing and offer `/cartograph-survey` on them.

## 2. Place every example

For each worked example in `intent/<id>/intent.json`:

- Find the components whose responsibilities, flows or glossary cover it. Read the files the map cites, not only the
  map, before you claim a component is affected.
- Add or extend an entry in `impact`: `system` (the repository), `components` (ids from its map), `examples` (the
  example ids it covers), `why` (one sentence), `evidence` (`[{"path": "...", "line": n}]`, paths relative to that
  repository).
- If an example lands in no system, it is new capability: add a question for the architect ("No system shows visit
  reminders today; which team should own them?") rather than picking one.
- Where the map shows something the owner did not mention but the change must touch (a second screen showing the same
  data, a report, a translation file), add it as a question for the owner, not a silent addition.

Never fill `confirmedBy`. The architect of each system writes their name there, and the architect's approval in
`approvals.json` is what binds.

## 3. Check

```bash
python3 .cartographer/bin/intent.py check intent/<id> --map <system>=<repo>/.cartographer/model/model.json [--map ...]
```

The check fails on an example placed nowhere, a component not in the system's map, a cited file that does not exist,
or a system nobody has confirmed. Show the architect `intent.py render intent/<id>`.

## 4. After both approvals: one contract per system

```bash
python3 .cartographer/bin/intent.py contract intent/<id> --system <system> -o <repo>/intent/<id>.contract.json
```

The contract carries the rules as requirement titles (public: they appear in the pull request's check log), the worked
examples under each (for the hidden-test author only), the owner's decisions and what is out of scope. The hidden-test
author writes tests from the examples; the factory builds in that repository against the contract; the owner accepts
in dev against the same examples. A change to the record after approval voids the contracts until it is approved again.
