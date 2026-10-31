#!/usr/bin/env python3
"""Read a "Rule decision" issue for the rule-decision workflow, and write the pull request's description.

  rule_decision.py read EVENT_JSON    print rule=, decision= and problem= lines for $GITHUB_OUTPUT
  rule_decision.py body EVENT_JSON    print the pull request description (markdown)

The issue comes from .github/ISSUE_TEMPLATE/rule-decision.yml, opened from the rules page. Its text is untrusted:
only a rule id that is in .cartographer/guardrails.proposed.json, and a decision of Accept or Reject, come out of
`read`; anything else becomes a problem the workflow reports on the issue without changing a file.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules_page import load, replay_text  # noqa: E402

HOME = os.path.join(os.getcwd(), ".cartographer")


def sections(body: str) -> dict:
    """An issue form's body: '### Label' headings, each followed by its answer."""
    out, key = {}, None
    for line in (body or "").splitlines():
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            key = m.group(1).strip().lower()
            out[key] = []
        elif key:
            out[key].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def decide(event: dict) -> dict:
    issue = event.get("issue") or {}
    form = sections(issue.get("body", ""))
    rule = form.get("rule", "").strip().strip("`")
    decision = form.get("decision", "").strip().lower()
    reason = form.get("why", "").strip()
    proposed = {r["id"]: r for r in load(os.path.join(HOME, "guardrails.proposed.json"), {"rules": []}).get("rules", [])}
    accepted = {r["id"] for r in load(os.path.join(HOME, "guardrails.json"), {"rules": []}).get("rules", [])}
    problem = ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", rule):
        problem = "the Rule field is not a rule id."
    elif rule in accepted:
        problem = f"`{rule}` is already in force."
    elif rule not in proposed:
        problem = f"`{rule}` is not a proposed rule in .cartographer/guardrails.proposed.json."
    elif decision not in ("accept", "reject"):
        problem = "the Decision field must be Accept or Reject."
    elif not reason or reason == "_No response_":
        problem = "the Why field is empty; a decision needs a reason."
    return {"rule": rule if not problem else "", "decision": decision if not problem else "", "reason": reason,
            "problem": problem, "number": issue.get("number"), "author": (issue.get("user") or {}).get("login", ""),
            "proposal": proposed.get(rule, {})}


def plain(html_text: str) -> str:
    return re.sub(r"</?code>", "`", re.sub(r"<(?!/?code)[^>]+>", "", html_text))


def body(d: dict) -> str:
    r = d["proposal"]
    lines = [f"Closes #{d['number']}.", "",
             f"**{d['decision'].capitalize()}** guard rule `{d['rule']}`, decided by @{d['author']} in #{d['number']}.", "",
             "### Why", "", d["reason"], "",
             "### The rule", "", r.get("description") or r.get("type", ""), ""]
    if r.get("_why"):
        lines += [f"Proposed because: {r['_why']}", ""]
    lines += [f"Evidence from history: {plain(replay_text(r))}", "",
              "Merging this pull request applies the decision. It changes only `.cartographer/guardrails.json` and "
              "`.cartographer/guardrails.proposed.json`, through `guard.py`."]
    return "\n".join(lines) + "\n"


def main(argv):
    if len(argv) != 3 or argv[1] not in ("read", "body"):
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[2]) as f:
        d = decide(json.load(f))
    if argv[1] == "read":
        # One line each: a newline in a value could add outputs, so values are single-line by construction.
        print(f"rule={d['rule']}")
        print(f"decision={d['decision']}")
        print("problem=" + d["problem"].replace("\n", " "))
    else:
        if d["problem"]:
            print(d["problem"], file=sys.stderr)
            return 1
        sys.stdout.write(body(d))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
