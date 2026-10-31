#!/usr/bin/env python3
"""Formal models next to the code: TLA+ specs checked by TLC and drawn with pstack's statechart.

  formal.py setup                 fetch the pinned pstack statechart skill and TLC into .cartographer/vendor/ (git-ignored)
  formal.py check [DIR ...]       check every model folder (default: every verification/*/expect.json); exit 1 on a surprise
  formal.py chart DIR CFG [OUT]   run statechart for one config and keep its report and chart

A model folder holds SPEC.tla, one .cfg per variant, an optional view.ts for statechart, and expect.json:
  {"spec": "StoreSave.tla", "view": "view.ts", "runs": {"StoreSave.cfg": {"breaks": ["StoreMatchesFiles"]},
                                                     "StoreSaveFixed.cfg": {"breaks": []}}}
A variant that models the current code must match the code; a variant that models a broken or older
version must keep failing, which proves the model can fail. Needs Java 11+, git and network for setup.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

PSTACK_REPO = "https://github.com/cursor/plugins"
PSTACK_COMMIT = "4dd1d05818d19792e7cd6e818f1667f95f7e1dbe"  # pstack 0.15.36
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
TLC_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"  # the checksum pstack pins


def repo_root():
    return subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()


ROOT = repo_root()
VENDOR = os.path.join(ROOT, ".cartographer", "vendor")
PSTACK = os.path.join(VENDOR, "pstack")
JAR = os.path.join(VENDOR, "tla2tools.jar")
STATECHART = os.path.join(PSTACK, "pstack", "skills", "statechart", "scripts", "statechart.ts")


def setup():
    os.makedirs(VENDOR, exist_ok=True)
    if not os.path.exists(STATECHART):
        shutil.rmtree(PSTACK, ignore_errors=True)
        run = lambda *a: subprocess.run(a, cwd=PSTACK, check=True, capture_output=True)
        os.makedirs(PSTACK)
        run("git", "init", "-q")
        run("git", "remote", "add", "origin", PSTACK_REPO)
        run("git", "sparse-checkout", "set", "pstack/skills/statechart")
        run("git", "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", PSTACK_COMMIT)
        run("git", "checkout", "-q", "FETCH_HEAD")
    if not (os.path.exists(JAR) and sha256(JAR) == TLC_SHA256):
        tmp = JAR + ".part"
        urllib.request.urlretrieve(TLC_URL, tmp)
        if sha256(tmp) != TLC_SHA256:
            os.remove(tmp)
            sys.exit("tla2tools.jar does not match its pinned checksum; not using it.")
        os.replace(tmp, JAR)
    print("pstack statechart at %s and TLC 1.7.4 in %s" % (PSTACK_COMMIT[:12], os.path.relpath(VENDOR, ROOT)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def chart(folder, cfg, out=None):
    """Run statechart for one config; return (exit code, rules that break, report text, out dir)."""
    if not os.path.exists(STATECHART) or not os.path.exists(JAR):
        setup()
    spec = json.load(open(os.path.join(folder, "expect.json")))
    out = out or tempfile.mkdtemp(prefix="formal-")
    if os.path.isdir(out) and os.listdir(out):
        shutil.rmtree(out)
    args = ["node", STATECHART, spec["spec"], "--config", cfg, "--out", out]
    if spec.get("view"):
        args += ["--view", spec["view"]]
    r = subprocess.run(args, cwd=folder, capture_output=True, text=True, env={**os.environ, "TLA2TOOLS": JAR}, timeout=900)
    report = open(os.path.join(out, "report.md")).read() if os.path.exists(os.path.join(out, "report.md")) else r.stdout + r.stderr
    breaks = sorted(re.findall(r"^## (\S+) breaks", report, re.M))
    return r.returncode, breaks, report, out


def check(folders):
    surprises = 0
    for folder in folders:
        spec = json.load(open(os.path.join(folder, "expect.json")))
        for cfg, want in spec["runs"].items():
            code, breaks, report, _ = chart(folder, cfg)
            ok = code in (0, 1) and breaks == sorted(want["breaks"])
            surprises += not ok
            print("%s %s/%s: breaks %s%s" % ("ok  " if ok else "FAIL", os.path.relpath(folder, ROOT), cfg,
                                              ", ".join(breaks) or "nothing", "" if ok else " (expected %s)" % (", ".join(want["breaks"]) or "nothing")))
            if not ok and code not in (0, 1):
                print(report[-1500:])
    return 1 if surprises else 0


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "setup":
        setup()
        return 0
    if args[0] == "check":
        folders = [os.path.abspath(a) for a in args[1:]] or sorted(
            os.path.join(ROOT, "verification", d) for d in os.listdir(os.path.join(ROOT, "verification"))
            if os.path.exists(os.path.join(ROOT, "verification", d, "expect.json")))
        return check(folders)
    if args[0] == "chart" and len(args) >= 3:
        code, breaks, report, out = chart(os.path.abspath(args[1]), args[2], os.path.abspath(args[3]) if len(args) > 3 else None)
        print(report)
        print("files in %s" % out)
        return code
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
