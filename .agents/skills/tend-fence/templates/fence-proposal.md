# Proposal: change the fence rule <check or rule id>

## What it flagged
<file:line and the finding, from .cache/fence/report.md>

## Why the rule is wrong here
<one of: flags correct code (show the code and why it is correct); broader than what it protects (name what
it protects and what it also catches); what it protected is gone (show where)>

## Evidence
- Replay or baseline: <guard.py --replay --rule <id> output, or the baseline trend in .fence/baselines/>
- Other places the rule fires today: <count and two examples>

## The change
<the exact narrower pattern, path scope, threshold or category; the file it lives in>

## The test that shows the new boundary
<a unit test in tests/fence.test.ts, or a hidden test for the referee: one case the rule must still catch,
one case it must now allow>

## Decision
Waiting on a code owner. An agent does not edit the fence configuration.
