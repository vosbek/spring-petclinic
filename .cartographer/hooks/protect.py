#!/usr/bin/env python3
"""Claude Code PreToolUse hook: stop an agent's edit or command before it runs.

Edit/Write/MultiEdit/NotebookEdit are refused when
  - the target is covered by an active `protected-paths` rule that is enforced at edit time
    (new files are allowed when the rule says allowAdd)
  - the new text adds a line matching a `forbid-pattern` rule enforced at edit time
    (for example: an agent typing `reviewedBy:` by hand, where only a person publishes)
  - the target is the guardrail configuration itself (.cartographer/guardrails.json, baseline.json).
    Agents propose rules in guardrails.proposed.json; a human accepts them.
    Set CARTOGRAPHER_ALLOW_RULE_EDITS=1 for a session where a human is editing rules on purpose.
Bash is refused when the command matches a `forbid-command` rule (for example running a publish
command that only the curator may run), and when it would decide a guard rule: guard.py --accept, --reject or
--serve, rules_app.py serve, or a call to the rules workbench's API. Deciding rules is a person's job.

Valid exceptions in guardrails.json (reason, expiry, approver) let the call through.
Exit 2 with a message on stderr blocks the tool call and shows the message to the agent.
"""
import json
import os
import re
import sys
import time

T0 = time.time()

SELF = {".cartographer/guardrails.json", ".cartographer/baseline.json"}
# Commands that apply a decision on a guard rule, open the place where a person makes one, or write the rule files.
# This stops an agent that follows its hooks; what binds is CODEOWNERS with code owner review required (see README).
DECIDE = re.compile(
    r"guard\.py\b[^\n;&|]*?\s['\"]?--(?:acc|rej|ser)"                  # --accept, --reject, --serve, quoted or not
    r"|rules_app\b[^\n;&|]*\bserve\b"
    r"|\bimport\s+(?:rules_app|guard)\b|\bfrom\s+(?:rules_app|guard)\s+import\b|__import__\(\s*['\"](?:rules_app|guard)['\"]"
    r"|/api/(?:decide|undo|discard|pr|branch)\b"
    r"|\b(?:guardrails|baseline)\.json\b",                                # any shell command naming the rule files
    re.I)


def added_lines(tool, ti):
    if tool == "Write":
        return (ti.get("content") or "").split("\n")
    if tool == "Edit":
        old = set((ti.get("old_string") or "").split("\n"))
        return [l for l in (ti.get("new_string") or "").split("\n") if l not in old]
    if tool == "MultiEdit":
        out = []
        for e in ti.get("edits") or []:
            old = set((e.get("old_string") or "").split("\n"))
            out += [l for l in (e.get("new_string") or "").split("\n") if l not in old]
        return out
    if tool == "NotebookEdit":
        return (ti.get("new_source") or "").split("\n")
    return []


def block(msg):
    sys.stderr.write(msg.rstrip() + "\n")
    return 2


def log(root, data, source, tool, target, decision, rules=None, message=None, checked=None):
    try:
        import carto
        carto.log_event(os.path.join(root, ".cartographer"), {
            "source": source, "kind": "tool", "agent": "claude-code", "session": data.get("session_id"), "tool": tool,
            "target": (target or "")[:200], "decision": decision, "rules": rules or [], "message": (message or "")[:300] or None,
            "checked": checked, "ms": int((time.time() - T0) * 1000)})
    except Exception:  # logging never blocks or breaks the hook
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    if not os.path.exists(os.path.join(root, ".cartographer", "guardrails.json")):
        return 0
    sys.path.insert(0, os.path.join(root, ".cartographer", "bin"))
    try:
        import guard
    except ImportError:
        return 0

    if tool == "Bash":
        cmd = ti.get("command") or ""
        if DECIDE.search(cmd) and os.environ.get("CARTOGRAPHER_ALLOW_RULE_EDITS") != "1":
            log(root, data, "command-hook", tool, cmd, "block", ["rule-decision"], "deciding a guard rule is a person's job")
            return block("Blocked: accepting or rejecting a guard rule is a person's decision, and the rule files change only through it. "
                         "Tell the user which proposals are waiting and ask them to decide in the rules workbench: "
                         "python3 .cartographer/bin/guard.py --serve (in their own terminal). Read the rule files with the Read tool.")
        ctx = guard.Context(root)
        n = sum(1 for r in ctx.rules if r.get("type") == "forbid-command")
        hits = [v for v in guard.check_command(ctx, cmd) if not guard.exempt(ctx, v, None)]
        if hits:
            v = hits[0]
            log(root, data, "command-hook", tool, cmd, "block", [h["rule"] for h in hits], v["message"], n)
            return block("Blocked by guardrail %s: %s. %s" % (v["rule"], v["message"], v["fix"] or "Ask the user to run it."))
        if n:
            log(root, data, "command-hook", tool, cmd, "allow", checked=n)
        return 0

    target = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
    if not target:
        return 0
    full = target if os.path.isabs(target) else os.path.join(data.get("cwd") or root, target)
    rel = os.path.relpath(os.path.realpath(full), os.path.realpath(root)).replace(os.sep, "/")
    if rel.startswith("../"):
        return 0
    if rel in SELF and os.environ.get("CARTOGRAPHER_ALLOW_RULE_EDITS") != "1":
        log(root, data, "edit-hook", tool, rel, "block", ["guardrail-config"], "the guardrail configuration is changed only by a person")
        return block("Blocked: %s is the guardrail configuration and only a human changes it. "
                     "To add a rule, write it to .cartographer/guardrails.proposed.json (or run guard.py --suggest --write / learn.py --propose) "
                     "and ask the user to decide it in the rules workbench (guard.py --serve). To get past a rule for one change, "
                     "ask the user for an exception (reason, expiry, approver)." % rel)
    ctx = guard.Context(root)
    n = sum(1 for r in ctx.rules if "edit" in guard.enforced_at(r) and r.get("type") != "forbid-command")
    hits = [v for v in guard.check_edit(ctx, rel, os.path.exists(full), added_lines(tool, ti)) if not guard.exempt(ctx, v, rel)]
    if not hits:
        log(root, data, "edit-hook", tool, rel, "allow", checked=n)
    if hits:
        v = hits[0]
        log(root, data, "edit-hook", tool, rel, "block", [h["rule"] for h in hits], v["message"], n)
        return block("Blocked by guardrail %s: %s. %s %s" % (v["rule"], v["message"], next((r.get("description", "") for r in ctx.rules if r["id"] == v["rule"]), ""),
                                                          v["fix"] or "Ask the user before changing it."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
