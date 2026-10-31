Review this diff for defects only. For each finding give: file and line, what goes wrong, a concrete input or
sequence that triggers it, and how sure you are.

Look for:
- logic that is wrong for some input (empty lists, missing optional fields, dates at boundaries, duplicate slugs)
- anything that could publish content no person approved, or drop a person's own words
- error handling that hides a failure (a catch that returns a default the caller treats as success)
- a change to one place that has a twin elsewhere that was not changed
- tests that would still pass if the change were reverted

Do not comment on style, naming or formatting. Do not repeat what these checks already enforce: <guard.py --explain output>
