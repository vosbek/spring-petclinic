#!/usr/bin/env python3
"""Claude Code PostToolUse hook: verification right after an agent writes a file, not only at the pull request.

For the file just written:
  - an intent record (intent/<id>/intent.json): intent.py check, so the agent sees what blocks the record and who acts
    next while it is still writing it (a rule with no worked example, an open question, a system not on the map)
  - the first time a file is touched in a session: guard.py --explain, so the agent sees the file's component, what it
    may depend on and the rules that apply before it goes further
  - always, when the repository has the fence: node .fence/bin/fence.mjs --phase edit --files <file> (the checks fast
    enough for every edit: secrets, and formatters and linters that run on changed files)

A failing edit check goes back to the agent (exit 2 with the findings on stderr). The intent and rule notes are
context, not failures. The pull request's fence still decides; this moves the feedback to the moment of the edit.
"""
import json
import os
import shutil
import subprocess
import sys

TIMEOUT = 100


def rel(root, path):
    p = os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")
    return None if p.startswith("..") else p


def run(argv, root, timeout=TIMEOUT):
    try:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)


def first_touch(root, session, file):
    """True the first time this session writes this file."""
    path = os.path.join(root, ".cache", "fence", "phase", "touched.json")
    try:
        with open(path, encoding="utf-8") as fh:
            seen = json.load(fh)
    except (OSError, ValueError):
        seen = {}
    files = seen.setdefault(session or "-", [])
    if file in files:
        return False
    files.append(file)
    seen = {k: v for k, v in list(seen.items())[-20:]}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)
    return True


def edit_findings(root, file):
    """Failing findings from the fence's edit phase on one file, or [] when it passes or there is no fence."""
    fence = os.path.join(root, ".fence", "bin", "fence.mjs")
    if not os.path.exists(fence) or not shutil.which("node"):
        return []
    code, out = run(["node", fence, "--phase", "edit", "--files", file], root)
    if code == 0:
        return []
    if code is None:
        return ["The fence's edit checks did not finish (%s); they run again when you stop." % out.strip()[:200]]
    try:
        with open(os.path.join(root, ".cache", "fence", "phase", "edit", "report.json"), encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return ["The fence's edit checks failed without a report: " + out.strip()[-600:]]
    lines = []
    for c in report.get("results", []):
        if c.get("status") != "fail":
            continue
        for f in c.get("findings", [])[:6]:
            where = "%s%s: " % (f["file"], ":%s" % f["line"] if f.get("line") else "") if f.get("file") else ""
            lines.append("%s (%s): %s%s%s" % (c.get("title"), c.get("id"), where, f.get("message", "").strip()[:400], ("\n    fix: " + f["fix"]) if f.get("fix") else ""))
    return lines


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not path:
        return 0
    file = rel(root, path)
    if not file or file.startswith((".git/", ".cache/")) or not os.path.exists(os.path.join(root, file)):
        return 0
    notes = []
    home = os.path.join(root, ".cartographer")
    if file.endswith("/intent.json") and "intent/" in file:
        tool = os.path.join(home, "bin", "intent.py")
        if os.path.exists(tool):
            _, out = run(["python3", tool, "check", os.path.dirname(file)], root)
            notes.append("Intent record check (python3 .cartographer/bin/intent.py check %s):\n%s" % (os.path.dirname(file), out.strip()[-2500:]))
    elif os.path.exists(os.path.join(home, "bin", "guard.py")) and first_touch(root, data.get("session_id"), file):
        code, out = run(["python3", os.path.join(home, "bin", "guard.py"), "--explain", file], root, 30)
        if code == 0 and out.strip():
            notes.append("Rules for %s (guard.py --explain):\n%s" % (file, out.strip()[-1500:]))
    failures = edit_findings(root, file)
    if failures:
        sys.stderr.write("The fence's edit checks failed on %s. Fix this before going on:\n- %s\n%s" % (
            file, "\n- ".join(failures), ("\n" + "\n\n".join(notes)) if notes else ""))
        return 2
    if notes:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(notes)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
