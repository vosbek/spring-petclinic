#!/usr/bin/env python3
"""Agent stop hook: an agent cannot finish while its change breaks a blocking guardrail.

Claude Code (Stop event):   exit 2 + stderr keeps the agent working and shows it the violations.
Cursor (stop event):        pass --cursor; prints {"followup_message": ...} so Cursor sends the violations back.

The end of every agent loop is where the gardener runs, not only nightly or on the pull request:
  1. verify: guard.py in worktree mode, then the fence's stop phase
  2. garden: the fence's safe fixers (formatter, autofixable lint), then every check again (--fix)
  3. block: what is left goes back to the agent, which keeps working
  4. improve the fence: once a loop that failed along the way passes, the agent is asked once to run /cartograph-reflect,
     which turns what went wrong into proposed rules or tests (guardrails.proposed.json) for a person to accept.

Runs guard.py in worktree mode (this branch plus uncommitted work against the default branch), then, when the
repository has the fence, the fence's "stop" phase: compile, the tests, tests kept, the gate left alone and the other
checks that are not too slow to run every time an agent finishes (node .fence/bin/fence.mjs --phase stop).
After three blocked stops in a row with the same violations it lets the agent stop, so a rule the agent
cannot satisfy ends in a question to the human instead of a loop.
"""
import json
import os
import shutil
import subprocess
import sys

MAX_REPEATS = 3
FENCE_TIMEOUT = 840  # seconds; the hook's own timeout in .claude/settings.json is 900


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
    problems, keys, rules = [], [], set()
    if os.path.exists(os.path.join(home, "guardrails.json")):
        sys.path.insert(0, os.path.join(home, "bin"))
        import guard
        res = guard.run(root, "worktree", source="cursor-stop" if cursor else "stop-hook",
                        session=data.get("session_id") or data.get("conversation_id"))
        if not res["ok"]:
            problems.append("Guardrails failed:\n" + guard.text_report(res) +
                            "\nIf a rule is wrong for this change, stop and ask the user for an exception with a reason, an expiry date "
                            "and an approver. Do not edit .cartographer/guardrails.json or baseline.json.")
            keys += ["%s|%s|%s" % (v["rule"], v["path"], v.get("line")) for v in res["blocking"]]
            rules |= {v["rule"] for v in res["blocking"]}
    failed = fence_stop(root)
    if failed:
        problems.append(failed["text"])
        keys += failed["keys"]
        rules |= {"fence:" + k.split("|")[0] for k in failed["keys"]}
    state_path = os.path.join(home if os.path.isdir(home) else os.path.join(root, ".cache"), "log", "stop-hook.json")
    try:
        with open(state_path) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    session = data.get("session_id") or data.get("conversation_id") or ""
    if not problems:
        # The loop is clean. If it failed along the way, ask once for the lesson before the agent finishes.
        failed_before = session and session in state.get("failed_sessions", [])
        reflected = session in state.get("reflected_sessions", [])
        if failed_before and not reflected and not data.get("stop_hook_active"):
            state.setdefault("reflected_sessions", []).append(session)
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            with open(state_path, "w") as fh:
                json.dump(state, fh)
            msg = ("The checks pass now, but this loop failed along the way. Before finishing, run /cartograph-reflect: turn what "
                   "went wrong into a proposed rule or test in .cartographer/guardrails.proposed.json (never guardrails.json), so the "
                   "fence catches it earlier next time. Then tell the user what you proposed.")
            if cursor:
                print(json.dumps({"followup_message": msg}))
                return 0
            print(json.dumps({"decision": "block", "reason": msg}))
            return 0
        print("{}") if cursor else None
        return 0
    if session and session not in state.get("failed_sessions", []):
        state.setdefault("failed_sessions", []).append(session)
    key = sorted(keys)
    repeats = state.get("repeats", 0) + 1 if state.get("key") == key else 1
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump({**state, "key": key, "repeats": repeats, "failed_sessions": state.get("failed_sessions", [])[-50:],
                   "reflected_sessions": state.get("reflected_sessions", [])[-50:]}, fh)
    if repeats > MAX_REPEATS:
        try:
            sys.path.insert(0, os.path.join(home, "bin"))
            import carto
            carto.log_event(home, {"source": "cursor-stop" if cursor else "stop-hook", "kind": "loop-breaker", "session": data.get("session_id"),
                                   "decision": "release", "rules": sorted(rules),
                                   "message": "same failures %d times in a row; agent allowed to stop and ask a person" % repeats})
        except Exception:
            pass
        print("{}") if cursor else None
        return 0
    msg = ("The work is not done yet:\n\n" + "\n\n".join(problems) +
           "\n\nFix each item (each one names its fix). If a check is wrong, say so to the user and run /tend-fence; "
           "do not edit .fence/ or the workflows.")
    if cursor:
        print(json.dumps({"followup_message": msg}))
        return 0
    sys.stderr.write(msg + "\n")
    return 2


def fence_stop(root):
    """The fence's stop phase on the working copy. None when it passes or the repository has no fence."""
    fence = os.path.join(root, ".fence", "bin", "fence.mjs")
    if not os.path.exists(fence) or not shutil.which("node"):
        return None
    try:
        r = subprocess.run(["node", fence, "--phase", "stop", "--fix"], cwd=root, capture_output=True, text=True, timeout=FENCE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"text": "The fence's stop phase took longer than %d seconds, so it was not checked. Say so to the user." % FENCE_TIMEOUT, "keys": ["timeout|"]}
    if r.returncode == 0:
        return None
    try:
        with open(os.path.join(root, ".cache", "fence", "phase", "stop", "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return {"text": "The fence's stop phase failed without a report:\n" + (r.stderr or r.stdout)[-1500:], "keys": ["no-report|"]}
    fixed = report.get("fixed") or []
    lines, keys = ["Fence checks failed (node .fence/bin/fence.mjs --phase stop --fix):"], []
    if fixed:
        lines.append("The gardener ran the fixers for %s and checked everything again; review its changes with git diff." % ", ".join(fixed))
    if report.get("regressions"):
        lines.append("A fixer broke %s, so revert its changes (git checkout -p) and fix by hand." % ", ".join(report["regressions"]))
    for c in report.get("results", []):
        if c.get("status") != "fail":
            continue
        lines.append("- %s (%s)" % (c.get("title"), c.get("id")))
        for f in c.get("findings", [])[:8]:
            where = ("%s%s: " % (f["file"], ":%s" % f["line"] if f.get("line") else "")) if f.get("file") else ""
            lines.append("    %s%s%s" % (where, f.get("message", "").strip()[:600], ("\n      fix: " + f["fix"]) if f.get("fix") else ""))
            keys.append("%s|%s|%s" % (c.get("id"), f.get("file"), f.get("message", "")[:80]))
    return {"text": "\n".join(lines), "keys": keys}


if __name__ == "__main__":
    sys.exit(main())
