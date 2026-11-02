---
name: capture-intent
description: Interview a business owner directly and turn what they want into a checked intent record - the outcome, the rules, a worked example for each rule, what is out of scope, and the questions only they can answer. Use when a business person brings a request ("I want to sell a new product", "let staff cancel visits"), when someone types /capture-intent, or before any feature reaches the factory without approved examples.
argument-hint: "<the request, in the owner's words>"
---

# Capture business intent

You are talking to the person who owns the decision, usually not an engineer. Your job is to make what they want
complete and testable without deciding anything for them. The record you build is what every later step works from:
the architect routes it to systems, the hidden-test author turns its examples into tests, the factory builds against
it, and the owner accepts the result against the same examples.

The tool: `python3 .cartographer/bin/intent.py` (`python` on Windows). In this repository it is `toolkit/bin/intent.py`.

## 1. Start the record with their words

```bash
python3 .cartographer/bin/intent.py new intent/<short-id> --request "<their request, verbatim>" --owner "<Name>, <role>"
```

Keep `request` exactly as they said it. Everything else goes in `intent/<short-id>/intent.json`, which you edit.

## 2. Find out what exists today

Before asking anything, read the codebase map of the systems this probably touches (`.cartographer/model/model.json`,
or `/explain-repo`) and the code it cites. You are looking for the facts that turn into good questions: what the
records involved store and how precisely, what happens to them when they are removed, who can see and do what, and
everywhere else the same data appears. Record a fact from the code as `"source": "code"` with `"ref": "path:line"`. A fact from the code is never
the owner's decision.

## 3. Interview: outcome, rules, examples

Ask in plain words, three questions at a time at most.

- **Outcome:** what is different for the business when this is done? Who notices?
- **Rules:** what must be true? One rule per sentence, in their words.
- **A worked example for every rule:** real-looking names, dates and amounts. *Given* the situation, *when* someone
  does something, *then* exactly what they see or what changes. If they say "it should handle it properly", ask what
  they would see.
- **Out of scope:** what this is not, so nobody builds it.

Then go through the edges, because that is where agents otherwise decide silently. For each rule, ask the kinds that
apply:

| Edge | For example, in a policy system |
|---|---|
| Boundaries | Does the rate change on the 25th birthday or the day after? Is exactly two cars "multi-car"? |
| Existing data | When a quote expires, is it removed or kept? Can it be brought back? |
| Mistakes | What happens if the same endorsement is submitted twice? Can it be undone, and by whom? |
| Who may | Can a service rep do this, or only a licensed agent? What does everyone else see? |
| Other places | Does the same thing show on the customer portal, in documents, in another system's feed? |
| What tells people | Does the policyholder get a letter or an email? Does anyone else need to know? |

Ask each kind against the system in front of you: the examples are only there to show what the kind means.

## 4. Record, never decide

- Every rule, example and out-of-scope item has a `source`: `owner` (they said it), `code` (seen in the code, with
  `ref`), or `assumption`. An assumption blocks approval until the owner confirms it or it becomes a question.
- Anything you cannot get from them goes in `questions` with `why` (what depends on the answer). A question about how
  the system should behave technically rather than what the business wants (two people saving at once, a database
  constraint) gets `"ask": "architect"`, so the owner is not handed it. Do not answer your
  own questions. When they answer, fill `answer` and `answeredBy` with their name.
- No vague results in `then`: the check refuses "appropriately", "correctly", "as expected", "handled" and similar.

## 5. Check, show, repeat

```bash
python3 .cartographer/bin/intent.py check intent/<short-id>       # what blocks it, and who acts next
python3 .cartographer/bin/intent.py render intent/<short-id>      # the page you show the owner
```

Show them the rendered page, not the JSON. Go round until the `capture` section of the check is empty.

## 6. Hand over

- The owner approves this version in their own terminal: `intent.py approve intent/<short-id> --role owner --by "<Name>"`.
  You never approve and never write `approvals.json`; the hooks refuse both. If they are not at a terminal, the person
  running the session does it with them.
- Then `/find-impact` places each example in the systems that change, and the architect confirms.
- If the record changes after approval, the approval goes stale and they approve again. Say so when it happens.
