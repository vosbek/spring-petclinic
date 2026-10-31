#!/usr/bin/env python3
"""Agent stop hook: an agent cannot finish while its change breaks a blocking guardrail.

Claude Code (Stop event):   exit 2 + stderr keeps the agent working and shows it the violations.
Cursor (stop event):        pass --cursor; prints {"followup_message": ...} so Cursor sends the violations back.

Runs guard.py in worktree mode (this branch plus uncommitted work against the default branch).
After three blocked stops in a row with the same violations it lets the agent stop, so a rule the agent
cannot satisfy ends in a question to the human instead of a loop.
"""
import json
import os
import sys

MAX_REPEATS = 3


def main():
    cursor = "--cursor" in sys.argv
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("CURSOR_PROJECT_DIR") or (data.get("workspace_roots") or [None])[0] or data.get("cwd") or os.getcwd()
    if cursor and (data.get("status") not in (None, "completed") or int(data.get("loop_count") or 0) >= MAX_REPEATS):
        print("{}")
        return 0
    home = os.path.join(root, ".cartographer")
    if not os.path.exists(os.path.join(home, "guardrails.json")):
        print("{}") if cursor else None
        return 0
    sys.path.insert(0, os.path.join(home, "bin"))
    import guard
    res = guard.run(root, "worktree", source="cursor-stop" if cursor else "stop-hook",
                    session=data.get("session_id") or data.get("conversation_id"))
    if res["ok"]:
        print("{}") if cursor else None
        return 0
    key = sorted("%s|%s|%s" % (v["rule"], v["path"], v.get("line")) for v in res["blocking"])
    state_path = os.path.join(home, "log", "stop-hook.json")
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    repeats = state.get("repeats", 0) + 1 if state.get("key") == key else 1
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump({"key": key, "repeats": repeats}, fh)
    if repeats > MAX_REPEATS:
        try:
            import carto
            carto.log_event(home, {"source": "cursor-stop" if cursor else "stop-hook", "kind": "loop-breaker", "session": data.get("session_id"),
                                   "decision": "release", "rules": sorted({v["rule"] for v in res["blocking"]}),
                                   "message": "same violations %d times in a row; agent allowed to stop and ask a person" % repeats})
        except Exception:
            pass
        print("{}") if cursor else None
        return 0
    msg = ("Guardrails failed, so the work is not done yet:\n" + guard.text_report(res) +
           "\n\nFix each blocking item (each one names its fix). If a rule is wrong for this change, stop and ask the user for an "
           "exception with a reason, an expiry date and an approver. Do not edit .cartographer/guardrails.json or baseline.json.")
    if cursor:
        print(json.dumps({"followup_message": msg}))
        return 0
    sys.stderr.write(msg + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
