#!/usr/bin/env python3
"""Deterministic guardrails: rules a change must pass, checked from git, the import graph and the model.

  guard.py --staged                         what you are about to commit (git pre-commit hook)
  guard.py --worktree                       this branch plus uncommitted work vs the default branch (agent Stop hook)
  guard.py --base origin/main --head HEAD   a PR or any range (CI)
  guard.py --explain PATH [PATH ...]        which rules apply to these files, before you edit them
  guard.py --replay [--rule ID]             prove rules against history: the past commits each rule would have caught
  guard.py --suggest [--write]              propose rules from facts (layers, cycles, coupling, migrations, interfaces)
  guard.py --baseline [--force]             record ratchet metrics (only tightens unless --force)

Rules live in .cartographer/guardrails.json (see docs/guardrails.md). No rule uses an LLM: the same change
always gets the same verdict. Exit code 1 when a blocking rule fails, 0 otherwise.
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import posixpath
import re
import subprocess
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import carto  # noqa: E402

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
FOLLOW_UP_DAYS = 14
DEFAULT_SEVERITY = {"interface-change": "warn", "max-size": "warn", "tests-required": "warn"}
FILE_LEVEL = {"protected-paths", "co-change", "tests-required", "interface-change"}
REPLAYABLE = {"layering", "forbid-import", "forbid-pattern", "no-new-cycles", "co-change", "interface-change",
              "protected-paths", "tests-required", "max-size", "require-added-pattern", "path-convention", "same-value",
              "tree-pattern", "same-set"}
TREE_TYPES = {"tree-pattern", "same-set"}     # repository checks: judged on whole files at the head of the change, like a test
EDIT_ONLY_TYPES = {"forbid-command"}          # checked only when an agent acts (PreToolUse), never on a diff
EDIT_CAPABLE = {"protected-paths", "forbid-pattern", "forbid-command"}


def enforced_at(rule):
    """Where a rule runs: 'edit' = agent tool calls (hooks/protect.py), 'diff' = Stop hook, pre-commit and CI."""
    t = rule.get("type")
    if t in EDIT_ONLY_TYPES:
        return ["edit"]
    e = rule.get("enforce")
    if e:
        return [x for x in (e if isinstance(e, list) else [e]) if x in ("edit", "diff")]
    return ["edit", "diff"] if t == "protected-paths" else ["diff"]


def enforced_label(rule):
    at = enforced_at(rule)
    parts = []
    if "edit" in at:
        parts.append("agent edit hook" if rule.get("type") != "forbid-command" else "agent command hook")
    if "diff" in at:
        parts.append("CI" if rule.get("type") == "ratchet" else "Stop hook, pre-commit, CI")
    return ", ".join(parts)
METRICS = ["layerViolations", "cycles", "unmodeledDependencies", "hiddenDependencies", "untestedComponents", "checkErrors"]
METRIC_LABEL = {"layerViolations": "layer violations", "cycles": "dependency cycles", "unmodeledDependencies": "dependencies not in the model",
                "hiddenDependencies": "hidden dependencies", "untestedComponents": "components no test imports", "checkErrors": "model check errors"}
ALLOW = re.compile(r"cartographer-allow:\s*([\w*.-]+)\s+until\s+(\d{4}-\d{2}-\d{2})\s+by\s+(\S+?)\s*[:\-]\s*(\S.*)")
LOCKFILES = ["**/package-lock.json", "**/yarn.lock", "**/pnpm-lock.yaml", "**/poetry.lock", "**/Cargo.lock", "**/go.sum",
             "**/composer.lock", "**/Gemfile.lock", "**/*.min.js", "**/*.snap", "**/uv.lock"]


# ----------------------------------------------------------------------------- selectors

def glob_re(pat):
    """gitignore-style glob: ** spans directories, * stays inside one; a pattern without / matches a basename."""
    anchored = "/" in pat.strip("/") or pat.startswith("/")
    pat = pat.strip("/")
    out, i = [], 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    return re.compile(("^" if anchored else "^(?:.*/)?") + body + "(?:/.*)?$")


class Selector:
    """A list of globs, `component:<id>`, `layer:<name>`, `tests`, `code`; entries starting with ! exclude."""

    def __init__(self, spec, ctx):
        if spec is None:
            spec = []
        specs = [spec] if isinstance(spec, str) else list(spec)
        self.empty = not specs
        self.pos, self.neg = [], []
        for s in specs:
            (self.neg if s.startswith("!") else self.pos).append(self._one(s.lstrip("!"), ctx))

    @staticmethod
    def _one(s, ctx):
        if s.startswith("component:"):
            cid = s.split(":", 1)[1]
            return lambda p: ctx.mapper(p) == cid
        if s.startswith("layer:"):
            layer = s.split(":", 1)[1]
            return lambda p: ctx.comp.get(ctx.mapper(p) or "", {}).get("layer") == layer
        if s == "tests":
            return carto.is_test
        if s == "code":
            return lambda p: carto.category(p) == "code"
        rx = glob_re(s)
        return lambda p: bool(rx.match(p))

    def __call__(self, path):
        if path is None:
            return False
        ok = any(m(path) for m in self.pos) if self.pos else True
        return ok and not any(m(path) for m in self.neg)


# ----------------------------------------------------------------------------- change sets

class FileChange:
    __slots__ = ("path", "old", "status", "add", "dele", "added", "removed", "hunks", "binary")

    def __init__(self, path):
        self.path, self.old, self.status = path, None, "M"
        self.add = self.dele = 0
        self.added, self.removed, self.hunks = [], [], []   # added: [(new line no, text)], hunks: [(old start, old len)]
        self.binary = False


class ChangeSet:
    def __init__(self, files, label, sha=None, reader=None, tree=None):
        self.paths = {f.path for f in files} | {f.old for f in files if f.old}
        files = [f for f in files if not f.path.startswith(".cartographer/")]   # the toolkit and model are not product code
        self.files = files            # list[FileChange]
        self.label = label
        self.sha = sha
        self.reader = reader          # path -> list of lines at the head of the change (for inline exceptions)
        self.tree = tree              # Tree at the head of the change (repository checks)
        self._deps = None


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    files, cur, newno = [], None, 0
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileChange(m.group(2)) if m else None
            if cur:
                if m.group(1) != m.group(2):
                    cur.old, cur.status = m.group(1), "R"
                files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("new file mode"):
            cur.status = "A"
        elif line.startswith("deleted file mode"):
            cur.status = "D"
        elif line.startswith("rename from "):
            cur.old, cur.status = line[12:], "R"
        elif line.startswith("copy from "):
            cur.old, cur.status = line[10:], "A"
        elif line.startswith("Binary files "):
            cur.binary = True
        elif line.startswith("+++ ") or line.startswith("--- "):
            continue
        elif line.startswith("@@"):
            m = HUNK.match(line)
            if m:
                cur.hunks.append((int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)))
                newno = int(m.group(3))
        elif line.startswith("+"):
            cur.added.append((newno, line[1:]))
            cur.add += 1
            newno += 1
        elif line.startswith("-"):
            cur.removed.append(line[1:])
            cur.dele += 1
        elif line.startswith(" "):
            newno += 1
    return files


def git(repo, *a, check=True):
    return carto.git(repo, *a, check=check)


def default_branch(repo):
    ref = git(repo, "symbolic-ref", "-q", "refs/remotes/origin/HEAD", check=False)
    if ref and ref.strip():
        return ref.strip().replace("refs/remotes/", "")
    for b in ("origin/main", "origin/master", "main", "master"):
        if git(repo, "rev-parse", "-q", "--verify", b, check=False):
            return b
    return "HEAD"


def show_reader(repo, rev):
    cache = {}

    def read(path):
        if path not in cache:
            out = git(repo, "show", "%s:%s" % (rev, path), check=False) if rev is not None else None
            cache[path] = (out or "").split("\n")
        return cache[path]
    return read


def disk_reader(repo):
    cache = {}

    def read(path):
        if path not in cache:
            try:
                with open(os.path.join(repo, path), encoding="utf-8", errors="replace") as fh:
                    cache[path] = fh.read().split("\n")
            except OSError:
                cache[path] = []
        return cache[path]
    return read


class Tree:
    """The files at one revision (rev=sha), in the index (rev=""), or on disk (rev=None), read lazily.

    Blobs are read through one `git cat-file --batch` process per repository and cached by blob id, so replaying
    a check over many commits reads each version of a file once.
    """
    _procs = {}
    _blobs = {}

    def __init__(self, repo, rev):
        self.repo, self.rev = repo, rev
        self._ids = None

    def _list(self):
        if self._ids is None:
            ids = {}
            if self.rev is None:
                out = git(self.repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", check=False) or ""
                ids = {p: None for p in out.split("\x00") if p and os.path.isfile(os.path.join(self.repo, p))}
            elif self.rev == "":
                for row in (git(self.repo, "ls-files", "-s", "-z", check=False) or "").split("\x00"):
                    if "\t" in row:
                        meta, path = row.split("\t", 1)
                        ids[path] = meta.split()[1]
            else:
                for row in (git(self.repo, "ls-tree", "-r", "-z", self.rev, check=False) or "").split("\x00"):
                    if "\t" in row:
                        meta, path = row.split("\t", 1)
                        parts = meta.split()
                        if parts[1] == "blob":
                            ids[path] = parts[2]
            self._ids = ids
        return self._ids

    def paths(self):
        return sorted(self._list())

    def exists(self, path):
        return path in self._list()

    def read(self, path):
        ids = self._list()
        if path not in ids:
            return []
        if ids[path] is None:
            try:
                with open(os.path.join(self.repo, path), encoding="utf-8", errors="replace") as fh:
                    return fh.read().split("\n")
            except OSError:
                return []
        bid = ids[path]
        if bid not in Tree._blobs:
            Tree._blobs[bid] = self._cat(bid)
        return Tree._blobs[bid]

    @staticmethod
    def close():
        for proc in Tree._procs.values():
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
                proc.stdout.close()
            except Exception:
                pass
        Tree._procs.clear()

    def _cat(self, bid):
        proc = Tree._procs.get(self.repo)
        if proc is None or proc.poll() is not None:
            if not Tree._procs:
                atexit.register(Tree.close)
            proc = subprocess.Popen(["git", "-C", self.repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            Tree._procs[self.repo] = proc
        proc.stdin.write((bid + "\n").encode())
        proc.stdin.flush()
        header = proc.stdout.readline().decode().split()
        if len(header) < 3:
            return []
        data = proc.stdout.read(int(header[2]))
        proc.stdout.read(1)
        if len(data) > 2_000_000 or b"\x00" in data[:8000]:
            return []
        return data.decode("utf-8", "replace").split("\n")


def range_changes(repo, base, head):
    mb = (git(repo, "merge-base", base, head, check=False) or base).strip()
    files = parse_patch(git(repo, "diff", "-M", "-U0", "--no-color", "--no-ext-diff", mb, head))
    tree = Tree(repo, (git(repo, "rev-parse", head, check=False) or head).strip())
    return ChangeSet(files, "%s..%s" % (base, head), sha=head, reader=tree.read, tree=tree)


def staged_changes(repo):
    has_head = git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False)
    files = parse_patch(git(repo, "diff", "--cached", "-M", "-U0", "--no-color", "--no-ext-diff", "HEAD" if has_head else EMPTY_TREE))
    tree = Tree(repo, "")
    return ChangeSet(files, "staged", reader=tree.read, tree=tree)


def worktree_changes(repo, base=None):
    base = base or default_branch(repo)
    has_head = git(repo, "rev-parse", "-q", "--verify", "HEAD", check=False)
    mb = (git(repo, "merge-base", base, "HEAD", check=False) or "").strip() or ("HEAD" if has_head else EMPTY_TREE)
    files = parse_patch(git(repo, "diff", "-M", "-U0", "--no-color", "--no-ext-diff", mb))
    seen = {f.path for f in files}
    for p in (git(repo, "ls-files", "--others", "--exclude-standard", "-z", check=False) or "").split("\x00"):
        if not p or p in seen or (p.startswith(".cartographer/") and not p.startswith(".cartographer/model/")):
            continue
        fc = FileChange(p)
        fc.status = "A"
        full = os.path.join(repo, p)
        try:
            if os.path.getsize(full) <= 1_000_000:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    fc.added = list(enumerate(fh.read().split("\n"), start=1))
                    fc.add = len(fc.added)
        except OSError:
            pass
        files.append(fc)
    return ChangeSet(files, "worktree vs %s" % base, reader=disk_reader(repo), tree=Tree(repo, None))


def history_changes(repo, since_days=365, max_commits=500):
    """One ChangeSet per non-merge commit in the window, newest first, from a single git log call."""
    raw = git(repo, "log", "--no-merges", "-M", "-U0", "-p", "--no-color", "--no-ext-diff", "--since=%d.days" % since_days,
              "-n", str(max_commits), "--format=%x1e%H%x1f%aI%x1f%aN%x1f%aE%x1f%s%x1f%b%x1d", check=False) or ""
    out = []
    for block in raw.split("\x1e"):
        if "\x1d" not in block:
            continue
        head, patch = block.split("\x1d", 1)
        parts = head.split("\x1f")
        if len(parts) < 6:
            continue
        tree = Tree(repo, parts[0])
        cs = ChangeSet(parse_patch(patch), parts[0][:12], sha=parts[0], reader=tree.read, tree=tree)
        cs.date, cs.author, cs.subject = parts[1][:10], parts[2], parts[4]
        cs.iso = parts[1]
        cs.when = carto.parse_date(parts[1])
        cs.flags = carto.classify(parts[4], parts[5], parts[2], parts[3])
        out.append(cs)
    return out


# ----------------------------------------------------------------------------- context

class Context:
    def __init__(self, repo, model=None):
        self.repo = repo
        self.home = carto.home(repo)
        facts = os.path.join(self.home, "facts")
        self.inv = carto.load(os.path.join(facts, "inventory.json"))
        self.hist = carto.load(os.path.join(facts, "history.json"))
        self.graph = carto.load(os.path.join(facts, "graph.json"))
        self.curated = model is not None or os.path.exists(os.path.join(self.home, "model", "model.json"))
        if model is None:
            model = carto.load(os.path.join(self.home, "model", "model.json"))
        if model is None and self.inv:
            import build
            model = build.auto_model(self.inv, repo)
        self.model = model or {"components": [], "edges": []}
        self.comp = {c["id"]: c for c in self.model.get("components", [])}
        self.mapper = carto.Mapper(self.model.get("components", []))
        self.lidx = carto.layer_index(self.model)
        self.external = {c["id"] for c in self.model.get("components", []) if c.get("layer") == "external"}
        self.config = carto.load(os.path.join(self.home, "guardrails.json")) or {"rules": [], "exceptions": []}
        self.today = dt.date.today()
        self.agent = os.environ.get("CLAUDECODE") == "1" or bool(os.environ.get("CURSOR_AGENT"))   # running inside an agent's shell
        self._resolver = None
        self._static = None

    @property
    def rules(self):
        return [r for r in self.config.get("rules", []) if r.get("status", "active") == "active"]

    def resolver(self):
        if self._resolver is None:
            import pr
            self._resolver = pr.Resolver(self.repo)
        return self._resolver

    def static_edges(self):
        if self._static is None:
            self._static = defaultdict(set)
            if self.graph:
                for (a, b) in carto.component_graph(self.graph, self.mapper).keys():
                    self._static[a].add(b)
        return self._static

    def name(self, cid):
        return self.comp.get(cid, {}).get("name", cid)


IMPORT_LINE = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|using\s+[\w.]+\s*;|.*\brequire\(|.*\bfrom\s+['\"]|export\s+.*\bfrom\s+['\"])")


def added_deps(cs, ctx):
    """In-repo and external imports on added lines: (file change, line no, text, target file or None, from comp, to comp)."""
    if cs._deps is not None:
        return cs._deps
    def resolve(path, text):
        try:
            return ctx.resolver().resolve(path, "+" + text)
        except Exception:  # resolution is best effort; a parse failure must not fail the guard
            return None

    deps = []
    for f in cs.files:
        if f.status == "D" or carto.category(f.path) not in ("code", "test"):
            continue
        # An import the file already had is not new: a reformatted, re-wrapped or moved import line
        # shows as removed and added, but the dependency did not change.
        before = {t for t in (resolve(f.path, r) for r in f.removed if IMPORT_LINE.match(r)) if t}
        for n, text in f.added:
            if not IMPORT_LINE.match(text):
                continue
            tgt = resolve(f.path, text)
            if tgt and tgt in before:
                continue
            deps.append((f, n, text.strip(), tgt, ctx.mapper(f.path), ctx.mapper(tgt) if tgt else None))
    cs._deps = deps
    return deps


def V(rule, path, line, message, fix=None):
    return {"rule": rule["id"], "type": rule["type"], "severity": rule.get("severity") or DEFAULT_SEVERITY.get(rule["type"], "block"),
            "path": path, "line": line, "message": message, "fix": fix or rule.get("fix") or ""}


# ----------------------------------------------------------------------------- rule types

def r_layering(rule, cs, ctx):
    out = []
    scope = Selector(rule.get("paths"), ctx)
    for f, n, text, tgt, fc, tc in added_deps(cs, ctx):
        if not (fc and tc) or fc == tc or carto.is_test(f.path) or not scope(f.path):
            continue
        if fc in ctx.external or tc in ctx.external:
            continue
        if ctx.lidx.get(fc, 99) > ctx.lidx.get(tc, 99):
            lf, lt = ctx.comp[fc].get("layer", "?"), ctx.comp[tc].get("layer", "?")
            out.append(V(rule, f.path, n, "%s (%s layer) now imports %s (%s layer), which sits above it: %s" % (ctx.name(fc), lf, ctx.name(tc), lt, text),
                         rule.get("fix") or "Depend downward only. Move the shared type into the %s layer or below, or invert the call through an interface that %s owns." % (lf, ctx.name(fc))))
    return out


def r_forbid_import(rule, cs, ctx):
    out = []
    frm = Selector(rule.get("from"), ctx)
    to = Selector(rule.get("to"), ctx) if rule.get("to") else None
    mod = re.compile(rule["module"]) if rule.get("module") else None
    for f, n, text, tgt, fc, tc in added_deps(cs, ctx):
        if not frm(f.path):
            continue
        hit = (to is not None and tgt is not None and to(tgt)) or (mod is not None and mod.search(text))
        if hit:
            out.append(V(rule, f.path, n, "%s: %s" % (rule.get("description") or "forbidden import", text)))
    return out


def r_forbid_pattern(rule, cs, ctx):
    out = []
    paths = Selector(rule.get("paths"), ctx)
    rx = re.compile(rule["pattern"])
    for f in cs.files:
        if f.status == "D" or not paths(f.path) or (rule.get("ignoreTests", True) and carto.is_test(f.path)):
            continue
        for n, text in f.added:
            m = rx.search(text)
            if m:
                out.append(V(rule, f.path, n, "%s: `%s`" % (rule.get("description") or "forbidden pattern", m.group(0)[:80])))
    return out


def r_cycles(rule, cs, ctx):
    out = []
    edges = ctx.static_edges()
    new = defaultdict(set)
    for f, n, text, tgt, fc, tc in added_deps(cs, ctx):
        if fc and tc and fc != tc and not carto.is_test(f.path) and fc not in ctx.external and tc not in ctx.external:
            new[fc].add(tc)
    reported = set()
    for f, n, text, tgt, fc, tc in added_deps(cs, ctx):
        if not (fc and tc) or fc == tc or carto.is_test(f.path) or (fc, tc) in reported or fc in ctx.external or tc in ctx.external:
            continue
        # does tc reach fc through the graph plus this change's new edges?
        prev, stack, seen = {tc: None}, [tc], {tc}
        found = False
        while stack and not found:
            x = stack.pop()
            for y in edges.get(x, set()) | new.get(x, set()):
                if y in seen:
                    continue
                seen.add(y)
                prev[y] = x
                if y == fc:
                    found = True
                    break
                stack.append(y)
        if found:
            path, x = [fc], fc
            while x is not None and x != tc:
                x = prev[x]
                path.append(x)
            cyc = [ctx.name(fc)] + [ctx.name(c) for c in reversed(path[1:])] + [ctx.name(fc)]
            reported.add((fc, tc))
            out.append(V(rule, f.path, n, "import closes a dependency cycle: %s" % " → ".join(cyc),
                         rule.get("fix") or "Break the cycle: move the shared piece into a component both can depend on, or pass it in from the caller."))
    return out


def stem(path):
    b = posixpath.basename(path)
    return b.split(".", 1)[0]


def r_cochange(rule, cs, ctx):
    out = []
    when = Selector(rule.get("when"), ctx)
    req_spec = rule.get("require")
    changed = [f for f in cs.files if f.add + f.dele > 0 or f.status in ("A", "D", "R")]
    triggers = [f for f in changed if when(f.path)]
    if not triggers:
        return out
    if isinstance(req_spec, str) and "{stem}" in req_spec:
        for f in triggers:
            req = Selector(req_spec.replace("{stem}", stem(f.path)), ctx)
            if not any(req(p) for p in cs.paths):
                need = req_spec.replace("{stem}", stem(f.path))
                out.append(V(rule, f.path, None, "%s changed without %s" % (f.path, need),
                             rule.get("fix") or "Make the matching change in %s, or add an exception that says why it is not needed this time." % need))
        return out
    req = Selector(req_spec, ctx)
    if not any(req(p) for p in cs.paths if not when(p)):
        target = req_spec if isinstance(req_spec, str) else ", ".join(req_spec)
        out.append(V(rule, triggers[0].path, None, "%s changed without %s%s" % (
            triggers[0].path, target, (" (and %d more trigger file(s))" % (len(triggers) - 1)) if len(triggers) > 1 else ""),
            rule.get("fix") or "Make the matching change in %s, or add an exception that says why it is not needed this time." % target))
    return out


def r_interface(rule, cs, ctx):
    import pr
    out = []
    paths = Selector(rule.get("paths"), ctx)
    kinds = set(rule.get("kinds") or [])
    on = rule.get("on", "removed")
    req = Selector(rule.get("require"), ctx) if rule.get("require") else None
    if req is not None and any(req(p) for p in cs.paths):
        return out
    for f in cs.files:
        if not paths(f.path) or carto.is_test(f.path):
            continue
        lines = ["+" + t for _, t in f.added] + ["-" + t for t in f.removed]
        found = [i for i in pr.scan_interfaces(lines, f.path) if (not kinds or i["kind"] in kinds) and (on == "any" or i["sign"] == "-")]
        if f.status == "D" and not found:
            continue
        if found:
            desc = ", ".join("%s%s %s" % ("removed " if i["sign"] == "-" else "", i.get("method") or i["kind"], i["name"]) for i in found[:4])
            out.append(V(rule, f.path, None, "interface change: %s%s" % (desc, (" without a change to %s" % rule["require"]) if req is not None else "")))
    return out


def r_protected(rule, cs, ctx):
    out = []
    paths = Selector(rule.get("paths"), ctx)
    for f in cs.files:
        p = f.old if f.status == "R" else f.path
        if not paths(p) and not paths(f.path):
            continue
        if f.status == "A" and rule.get("allowAdd"):
            continue
        if f.status == "D" and rule.get("allowDelete"):
            continue
        verb = {"A": "adds", "D": "deletes", "R": "renames"}.get(f.status, "modifies")
        out.append(V(rule, f.path, f.added[0][0] if f.added else None, "%s protected file %s" % (verb, p)))
    return out


def r_tests(rule, cs, ctx):
    paths = Selector(rule.get("paths") or "code", ctx)
    tests = Selector(rule.get("tests"), ctx) if rule.get("tests") else carto.is_test
    code = [f for f in cs.files if f.status != "D" and paths(f.path) and not carto.is_test(f.path) and carto.category(f.path) == "code"]
    lines = sum(f.add + f.dele for f in code)
    if lines < int(rule.get("minLines", 20)):
        return []
    if any(tests(f.path) for f in cs.files if f.status != "D"):
        return []
    biggest = max(code, key=lambda f: f.add + f.dele)
    return [V(rule, biggest.path, None, "%d lines of code changed in %d file(s) with no test change" % (lines, len(code)),
              rule.get("fix") or "Add or update a test that fails without this change.")]


def r_snippets(rule, cs, ctx):
    """A change that leaves a pinned snippet's code no longer present in its file, without re-pinning the model."""
    out = []
    by_path = defaultdict(list)
    for sn in ctx.model.get("snippets", []):
        by_path[sn.get("path")].append(sn)
    norm = lambda xs: [x.rstrip() for x in xs]
    for f in cs.files:
        sns = by_path.get(f.old or f.path, []) + (by_path.get(f.path, []) if f.old else [])
        if not sns:
            continue
        lines = norm(cs.reader(f.path)) if (cs.reader and f.status != "D") else []
        text = "\n".join(" ".join(l.split()) for l in lines)
        for sn in sns:
            want = norm(sn.get("code", "").replace("\r\n", "\n").split("\n"))
            while want and not want[-1].strip():
                want.pop()
            if want and "\n".join(" ".join(l.split()) for l in want) in text:
                continue   # still there (possibly moved); check.py --update-snippets re-pins line numbers
            lo = next((st for st, ln in f.hunks if st <= int(sn.get("end", 0))), None)
            out.append(V(rule, f.path, lo, "%s pinned snippet \u201c%s\u201d (%s:%s\u2013%s) and the model still holds the old code" % (
                "deletes the file holding" if f.status == "D" else "changes", sn.get("title", sn["id"]), sn["path"], sn.get("start"), sn.get("end")),
                rule.get("fix") or "Re-read the snippet's annotations against the new code, re-pin it (snippet.py or /cartograph-refresh), and commit the model with this change."))
    return out


def r_size(rule, cs, ctx):
    skip = Selector(LOCKFILES + list(rule.get("exclude") or []), ctx)
    lines = sum(f.add + f.dele for f in cs.files if not skip(f.path) and not f.binary)
    mx = int(rule.get("max", 800))
    if lines > mx:
        return [V(rule, None, None, "%d changed lines (limit %d, lockfiles excluded)" % (lines, mx),
                  rule.get("fix") or "Split the change into smaller units that each end in a passing check.")]
    return []


def metrics(ctx):
    """Current values of the ratchet metrics, from the facts on disk."""
    import build
    if not (ctx.inv and ctx.hist):
        return {}
    d = build.derive(ctx.model, ctx.inv, ctx.hist, None, ctx.graph)
    m = {}
    st = d.get("structure")
    if st:
        m["layerViolations"] = sum(1 for e in st["compEdges"] if e.get("violation"))
        m["cycles"] = len(st["cycles"])
        m["untestedComponents"] = len(st["untested"])
        if ctx.curated:
            m["unmodeledDependencies"] = sum(1 for e in st["compEdges"] if not e.get("inModel"))
    if ctx.curated and d.get("coupling"):
        m["hiddenDependencies"] = sum(1 for p in d["coupling"]["components"] if p.get("hidden"))
    if ctx.curated:
        import check
        m["checkErrors"] = sum(1 for i in check.validate(ctx.model, ctx.repo) if i["level"] == "error")
    return m


def r_ratchet(rule, cs, ctx):
    base = carto.load(os.path.join(ctx.home, "baseline.json"))
    if not base:
        return [V(dict(rule, severity="warn"), None, None, "no baseline recorded", "Run `guard.py --baseline` and commit .cartographer/baseline.json.")]
    if ctx.graph and cs.sha and ctx.graph.get("commit"):
        head = (git(ctx.repo, "rev-parse", cs.sha, check=False) or "").strip()
        if head and not head.startswith(ctx.graph["commit"][:12]):
            return [V(dict(rule, severity="warn"), None, None, "facts are from %s, not %s; ratchet not evaluated" % (ctx.graph["commit"][:12], head[:12]),
                      "Run collect.py and graph.py on the head first (CI does this).")]
    if cs.sha is None and not getattr(ctx, "refreshed", False):
        return []
    cur = metrics(ctx)
    out = []
    for k in rule.get("metrics") or METRICS:
        if k in cur and k in base.get("metrics", {}) and cur[k] > base["metrics"][k]:
            out.append(V(rule, None, None, "%s rose from %d to %d" % (METRIC_LABEL.get(k, k), base["metrics"][k], cur[k]),
                         rule.get("fix") or ("Run `check.py` to see them; `check.py --fix-lines` follows evidence whose cited text only moved."
                                             if k == "checkErrors" else
                                             "Remove the new one, or if it is intended, record it with `guard.py --baseline --force` in a reviewed commit.")))
    return out


def r_require_added(rule, cs, ctx):
    """Files matching `paths` with a git status in `fileStatus` (default modified) must add a line matching `pattern`."""
    out = []
    paths = Selector(rule.get("paths"), ctx)
    statuses = set(rule.get("fileStatus") or ["M"])
    rx = re.compile(rule["pattern"])
    only = re.compile(rule["onlyIf"]) if rule.get("onlyIf") else None
    for f in cs.files:
        if only is not None:
            lines = [t for _, t in f.added] if f.status in ("A", "R") and not cs.reader else (cs.reader(f.path) if cs.reader else [t for _, t in f.added])
            if not any(only.search(t) for t in lines):
                continue
        if f.status in statuses and paths(f.path) and not any(rx.search(t) for _, t in f.added):
            out.append(V(rule, f.path, f.added[0][0] if f.added else None, "%s changed without adding %s" % (f.path, rule.get("needs") or "a line matching /%s/" % rule["pattern"])))
    return out


def r_path_convention(rule, cs, ctx):
    """New files under `paths` must have a basename matching `pattern`; names matching `ignore` are skipped."""
    out = []
    paths = Selector(rule.get("paths"), ctx)
    rx = re.compile(rule["pattern"])
    ign = re.compile(rule["ignore"]) if rule.get("ignore") else None
    for f in cs.files:
        if f.status not in ("A", "R") or not paths(f.path):
            continue
        b = posixpath.basename(f.path)
        if ign and ign.search(b):
            continue
        if not rx.search(b):
            out.append(V(rule, f.path, None, "%s does not follow the naming convention /%s/" % (b, rule["pattern"])))
    return out


def r_same_value(rule, cs, ctx):
    """Values captured by `sources[].pattern` (first group) in each file must be equal at the head of the change."""
    srcs = rule.get("sources") or []
    if not cs.reader or (not rule.get("always") and not any(sv.get("path") in cs.paths for sv in srcs)):
        return []
    tree = getattr(cs, "tree", None)
    if rule.get("skipMissing") and tree is not None and not all(tree.exists(sv["path"]) for sv in srcs):
        return []
    vals = []
    for sv in srcs:
        text = "\n".join(cs.reader(sv["path"]))
        m = re.search(sv["pattern"], text, re.M)
        vals.append((sv["path"], m.group(1) if m else None))
    if len({v for _, v in vals}) > 1:
        return [V(rule, vals[0][0], None, "values disagree: " + ", ".join("%s = %s" % (p, v if v is not None else "(not found)") for p, v in vals))]
    return []


def _touched(rule, cs, ctx):
    """Repository checks run on every change unless `when` limits them to changes touching those paths."""
    return not rule.get("when") or any(Selector(rule["when"], ctx)(p) for p in cs.paths)


def r_tree_pattern(rule, cs, ctx):
    """Whole files at the head of the change. mode forbid (default): each line matching `pattern` in files under
    `paths` fails. mode require: each such file (optionally only those with a line matching `onlyIf`) must contain a
    matching line. Unlike forbid-pattern, unchanged lines count: this is a test of the tree, not of the diff."""
    tree = getattr(cs, "tree", None)
    if tree is None or not _touched(rule, cs, ctx):
        return []
    paths = Selector(rule.get("paths"), ctx)
    rx = re.compile(rule["pattern"])
    only = re.compile(rule["onlyIf"]) if rule.get("onlyIf") else None
    require = rule.get("mode") == "require"
    out = []
    for p in tree.paths():
        if p.startswith(".cartographer/") or not paths(p):
            continue
        lines = tree.read(p)
        if only is not None and not any(only.search(t) for t in lines):
            continue
        if require:
            if not any(rx.search(t) for t in lines):
                out.append(V(rule, p, None, "%s has no %s" % (p, rule.get("needs") or "line matching /%s/" % rule["pattern"])))
            continue
        for n, t in enumerate(lines, start=1):
            m = rx.search(t)
            if m:
                hit = next((g for g in m.groups() if g), None) or m.group(0)
                out.append(V(rule, p, n, "%s `%s`" % (rule.get("message") or "found", hit.strip()[:80])))
    return out


def _extract(spec, tree, ctx):
    """Values from files at a tree. spec: {paths, within?, strip?, pattern, tokens?} or a list of specs (union).
    within: regex whose group 1 (or whole match) narrows the text; strip: regex removed first; pattern: findall,
    group 1; tokens: regex applied to each value to split it into several. Returns (values, files seen)."""
    specs = spec if isinstance(spec, list) else [spec]
    vals, seen = set(), 0
    for sp in specs:
        sel = Selector(sp.get("paths") or sp.get("path"), ctx)
        for p in tree.paths():
            if p.startswith(".cartographer/") or not sel(p):
                continue
            seen += 1
            text = "\n".join(tree.read(p))
            regions = [text]
            if sp.get("within"):
                regions = [m.group(1) if m.groups() else m.group(0) for m in re.finditer(sp["within"], text, re.S)]
            for reg in regions:
                if sp.get("strip"):
                    prev = None
                    while prev != reg:
                        prev, reg = reg, re.sub(sp["strip"], "", reg)
                for m in re.finditer(sp["pattern"], reg):
                    v = m.group(1) if m.groups() else m.group(0)
                    if sp.get("tokens"):
                        vals.update(re.findall(sp["tokens"], v or ""))
                    elif v is not None:
                        vals.add(v)
    return vals, seen


def r_same_set(rule, cs, ctx):
    """Two lists drawn from the code must agree: relation `equal` (default) or `subset` (every left value is on the
    right). Typical uses: a UI list against a schema enum, CSS classes against the markup that uses them.
    Skipped while either side matches no file (skipMissing, default true)."""
    tree = getattr(cs, "tree", None)
    if tree is None or not _touched(rule, cs, ctx):
        return []
    left, nl = _extract(rule["left"], tree, ctx)
    right, nr = _extract(rule["right"], tree, ctx)
    if rule.get("skipMissing", True) and (not nl or not nr):
        return []
    rel = rule.get("relation", "equal")
    missing = sorted(left - right)
    extra = sorted(right - left) if rel == "equal" else []
    if not missing and not extra:
        return []
    lname, rname = rule.get("leftName") or "left", rule.get("rightName") or "right"
    bits = []
    if missing:
        bits.append("in %s but not %s: %s" % (lname, rname, ", ".join(missing[:12])))
    if extra:
        bits.append("in %s but not %s: %s" % (rname, lname, ", ".join(extra[:12])))
    first = rule["left"][0] if isinstance(rule["left"], list) else rule["left"]
    path = first.get("path") or (first.get("paths") if isinstance(first.get("paths"), str) else None)
    return [V(rule, path, None, "; ".join(bits))]


def r_edit_only(rule, cs, ctx):
    return []


TYPES = {"layering": r_layering, "forbid-import": r_forbid_import, "forbid-pattern": r_forbid_pattern, "no-new-cycles": r_cycles,
         "co-change": r_cochange, "interface-change": r_interface, "protected-paths": r_protected, "tests-required": r_tests,
         "pinned-snippets": r_snippets, "max-size": r_size, "ratchet": r_ratchet,
         "require-added-pattern": r_require_added, "path-convention": r_path_convention, "same-value": r_same_value,
         "tree-pattern": r_tree_pattern, "same-set": r_same_set,
         "forbid-command": r_edit_only}


# ----------------------------------------------------------------------------- evaluation

def exception_state(ex, today):
    if not ex.get("reason") or not ex.get("approvedBy") or not ex.get("expires"):
        return "incomplete"
    try:
        exp = dt.date.fromisoformat(ex["expires"])
    except ValueError:
        return "incomplete"
    return "valid" if exp >= today else "expired"


def evaluate(cs, ctx, rules=None, use_exceptions=True):
    rules = ctx.rules if rules is None else rules
    raw, errors = [], []
    for r in rules:
        fn = TYPES.get(r.get("type"))
        if not fn:
            errors.append("rule %s: unknown type %r" % (r.get("id"), r.get("type")))
            continue
        if "diff" not in enforced_at(r):
            # An agent can write files through the shell, which the edit hook never sees. In an agent session the
            # content rules of the edit hook therefore also run on the diff (Stop hook, agent commits).
            if not (getattr(ctx, "agent", False) and r.get("type") == "forbid-pattern" and r.get("agentDiff", True)):
                continue
            try:
                raw.extend(dict(v, message=v["message"] + " (agent session: written outside the edit tools)") for v in fn(r, cs, ctx))
            except re.error as e:
                errors.append("rule %s: bad pattern (%s)" % (r.get("id"), e))
            continue
        try:
            raw.extend(fn(r, cs, ctx))
        except re.error as e:
            errors.append("rule %s: bad pattern (%s)" % (r.get("id"), e))
    if not use_exceptions:
        return raw, [], [], errors
    kept, suppressed, notes = [], [], []
    exceptions = []
    for ex in ctx.config.get("exceptions", []):
        state = exception_state(ex, ctx.today)
        exceptions.append((ex, state, Selector(ex.get("path") or "**", ctx)))
    by_path = {f.path: f for f in cs.files}
    for v in raw:
        why = None
        for ex, state, sel in exceptions:
            if ex.get("rule") in (v["rule"], "*") and (v["path"] is None or sel(v["path"])):
                if state == "valid":
                    why = "exception until %s by %s: %s" % (ex["expires"], ex["approvedBy"], ex["reason"])
                    break
                notes.append({"rule": "exceptions", "severity": "warn", "path": v["path"], "line": None, "type": "exception",
                              "message": "exception for %s on %s is %s%s" % (ex.get("rule"), ex.get("path") or "**", state,
                                                                              (" (expired %s)" % ex["expires"]) if state == "expired" else ""),
                              "fix": "Renew it with a new expiry and approver, or fix the violation."})
        if why is None and v["path"]:
            cands = []
            if v["line"] and cs.reader:
                lines = cs.reader(v["path"])
                cands = [(ln, lines[ln - 1]) for ln in (v["line"], v["line"] - 1) if 0 < ln <= len(lines)]
            if v["type"] in FILE_LEVEL and v["path"] in by_path:
                cands += by_path[v["path"]].added
            for ln, text in cands:
                m = ALLOW.search(text)
                if m and m.group(1) in (v["rule"], "*"):
                    try:
                        ok = dt.date.fromisoformat(m.group(2)) >= ctx.today
                    except ValueError:
                        ok = False
                    if ok:
                        why = "inline exception until %s by %s: %s" % (m.group(2), m.group(3), m.group(4).strip())
                    else:
                        notes.append({"rule": "exceptions", "severity": "warn", "path": v["path"], "line": ln, "type": "exception",
                                      "message": "inline exception for %s expired %s" % (v["rule"], m.group(2)), "fix": "Renew or fix."})
                    break
        if why:
            suppressed.append(dict(v, suppressedBy=why))
        else:
            kept.append(v)
    seen, uniq = set(), []
    for n in notes:
        k = (n["message"], n["path"])
        if k not in seen:
            seen.add(k)
            uniq.append(n)
    return kept, suppressed, uniq, errors


def report(cs, ctx, kept, suppressed, notes, errors, mode):
    block = [v for v in kept if v["severity"] == "block"]
    info = [v for v in kept if v["severity"] == "info"]       # context for the reviewer: PR comment only, never a nag
    warn = [v for v in kept if v["severity"] not in ("block", "info")] + notes
    return {"kind": "cartograph-guard", "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), "mode": mode,
            "label": cs.label, "files": len(cs.files), "rules": len(ctx.rules), "ok": not block,
            "blocking": block, "warnings": warn, "info": info, "suppressed": suppressed, "errors": errors,
            "autoModel": not ctx.curated}


def text_report(res, quiet=False):
    L = []
    for tag, items in (("BLOCK", res["blocking"]), ("WARN ", res["warnings"])):
        for v in items:
            loc = (v["path"] or "(change)") + (":%d" % v["line"] if v.get("line") else "")
            L.append("%s %s [%s] %s" % (tag, loc, v["rule"], v["message"]))
            if v.get("fix"):
                L.append("      fix: %s" % v["fix"])
    for e in res["errors"]:
        L.append("ERROR %s" % e)
    if not quiet:
        for v in res.get("info", []):
            L.append("INFO  %s [%s] %s" % ((v["path"] or "(change)"), v["rule"], v["message"]))
    if not quiet or L:
        L.append("guard: %d blocking, %d warning(s), %d note(s), %d suppressed by exceptions · %d rule(s) · %d file(s) · %s" % (
            len(res["blocking"]), len(res["warnings"]), len(res.get("info", [])), len(res["suppressed"]), res["rules"], res["files"], res["label"]))
    return "\n".join(L)


def markdown_report(res):
    L = ["<!-- cartographer-guard -->", "### Guardrails · %s" % ("passed" if res["ok"] else "%d blocking" % len(res["blocking"]))]
    rows = [("\U0001F7E5", v) for v in res["blocking"]] + [("\U0001F7E7", v) for v in res["warnings"]]
    if rows:
        L += ["", "| | Rule | Where | What | Fix |", "|---|---|---|---|---|"]
        for icon, v in rows[:20]:
            loc = "`%s%s`" % (v["path"], ":%d" % v["line"] if v.get("line") else "") if v.get("path") else ""
            L.append("| %s | `%s` | %s | %s | %s |" % (icon, v["rule"], loc, v["message"].replace("|", "\\|"), (v.get("fix") or "").replace("|", "\\|")))
    if res.get("info"):
        L += ["", "<details><summary>For the reviewer: %d note(s)</summary>\n" % len(res["info"])]
        for v in res["info"][:20]:
            L.append("- `%s` %s%s" % (v["rule"], ("`%s` " % v["path"]) if v.get("path") else "", v["message"]))
        L.append("\n</details>")
    if res["suppressed"]:
        L += ["", "<details><summary>%d suppressed by exceptions</summary>\n" % len(res["suppressed"])]
        for v in res["suppressed"]:
            L.append("- `%s` %s — %s" % (v["rule"], v.get("path") or "", v["suppressedBy"]))
        L.append("\n</details>")
    L += ["", "<sub>%d deterministic rule(s) from `.cartographer/guardrails.json`. Same change, same verdict.</sub>" % res["rules"]]
    return "\n".join(L)


def log_run(ctx, res, source=None, session=None, ms=None):
    first = (res["blocking"] or res["warnings"] or [None])[0]
    carto.log_event(ctx.home, {
        "at": res["at"], "source": source or carto.detect_source(), "kind": "run", "session": session, "mode": res["mode"],
        "label": res["label"], "files": res["files"], "checked": res["rules"],
        "decision": "pass" if res["ok"] and not res["warnings"] else ("warn" if res["ok"] else "block"),
        "rules": sorted({v["rule"] for v in res["blocking"]}), "warnings": sorted({v["rule"] for v in res["warnings"]}),
        "suppressed": sorted({v["rule"] for v in res["suppressed"]}),
        "message": ("%s%s: %s" % (first["path"] or "(change)", ":%d" % first["line"] if first.get("line") else "", first["message"]))[:300] if first else None,
        "ms": ms})


# ----------------------------------------------------------------------------- replay

def outcomes(history):
    """sha12 -> 'reverted' | 'fixed': later reverted, or followed by a fix to one of its files within 14 days."""
    out = {}
    reverted = set()
    fixes_by_file = defaultdict(list)
    for c in history:
        if c.flags.get("reverts"):
            reverted.add(c.flags["reverts"][:7])
        if c.flags.get("fix"):
            for f in c.files:
                fixes_by_file[f.path].append((c.when, c.sha))
    for c in history:
        if c.sha[:7] in reverted:
            out[c.sha[:12]] = "reverted"
            continue
        if c.flags.get("fix") or c.flags.get("bot"):
            continue
        for f in c.files:
            if any(0 < (fd - c.when).total_seconds() <= FOLLOW_UP_DAYS * 86400 for fd, fs in fixes_by_file.get(f.path, ()) if fs != c.sha):
                out[c.sha[:12]] = "fixed"
                break
    return out


def replay(ctx, rules, since_days=365, max_commits=500, history=None):
    history = history if history is not None else history_changes(ctx.repo, since_days, max_commits)
    outc = outcomes(history)
    eligible = [c for c in history if c.files and not c.flags.get("bot")]
    base_bad = sum(1 for c in eligible if outc.get(c.sha[:12]))
    res = []
    for r in rules:
        if r.get("type") not in REPLAYABLE or "diff" not in enforced_at(r):
            res.append({"rule": r["id"], "type": r.get("type"), "replayable": False})
            continue
        fired = []
        for c in eligible:
            vs, _, _, _ = evaluate(c, ctx, [r], use_exceptions=False)
            if vs:
                fired.append({"sha": c.sha[:12], "date": c.date, "subject": c.subject[:100], "author": c.author,
                              "where": (vs[0]["path"] or "") + (":%d" % vs[0]["line"] if vs[0].get("line") else ""),
                              "message": vs[0]["message"][:160], "outcome": outc.get(c.sha[:12])})
        bad = sum(1 for f in fired if f["outcome"])
        rate = (bad / len(fired)) if fired else None
        base = (base_bad / len(eligible)) if eligible else None
        fired.sort(key=lambda f: (f["outcome"] is None, f["date"]), reverse=False)
        res.append({"rule": r["id"], "type": r["type"], "replayable": True, "commits": len(eligible), "fired": len(fired),
                    "firedPct": round(100.0 * len(fired) / len(eligible), 1) if eligible else 0,
                    "followedByFixOrRevert": bad, "hitRatePct": round(100 * rate) if rate is not None else None,
                    "baseRatePct": round(100 * base) if base is not None else None,
                    "lift": round(rate / base, 2) if rate is not None and base else None,
                    "examples": fired[:6]})
    return {"kind": "cartograph-guard-replay", "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "window": {"days": since_days, "maxCommits": max_commits, "commits": len(eligible), "followedByFixOrRevert": base_bad},
            "rules": res,
            "note": "Rules are replayed as configured today against each past commit's diff; imports resolve against today's tree."}


# ----------------------------------------------------------------------------- history simulation

def simulate_history(ctx, since_days=365, max_commits=500):
    """Replay past commits through today's rules as if the guardrails had been live, dated at each commit.

    Diff rules run on each commit's diff (Stop hook / pre-commit / CI equivalent). Edit-time rules run on the
    lines each commit added, standing in for the agent hook. Command rules cannot be simulated (history has no
    commands). Results go to log/simulated.jsonl, never to the real event log.
    """
    history = history_changes(ctx.repo, since_days, max_commits)
    outc = outcomes(history)
    events = []
    ctx.agent = False
    rules = [r for r in ctx.rules if r.get("type") in REPLAYABLE]   # model- and facts-based rules cannot be judged in the past
    seen = {}                                        # repository checks: findings already present at the previous commit
    for c in reversed(history):                      # oldest first
        if not c.files or c.flags.get("bot"):
            continue
        vs, _, _, _ = evaluate(c, ctx, rules, use_exceptions=False)
        # A repository check judges the whole tree, so a finding persists until it is fixed. Had the check been live, only
        # the commit that introduced a finding would have been sent back; later commits carry it unchanged.
        now = {(v["rule"], v["path"], v["message"]) for v in vs if v["type"] in TREE_TYPES}
        vs = [v for v in vs if v["type"] not in TREE_TYPES or (v["rule"], v["path"], v["message"]) not in seen]
        seen = now
        block = [v for v in vs if v["severity"] == "block"]
        warn = [v for v in vs if v["severity"] == "warn"]
        first = (block or warn or [None])[0]
        base = {"at": c.iso, "simulated": True, "sha": c.sha[:12], "subject": c.subject[:120], "author": c.author,
                "outcome": outc.get(c.sha[:12])}
        events.append(dict(base, source="simulation", kind="run", label="commit %s" % c.sha[:9], files=len(c.files),
                           checked=sum(1 for r in rules if "diff" in enforced_at(r)),
                           decision="block" if block else ("warn" if warn else "pass"),
                           rules=sorted({v["rule"] for v in block}), warnings=sorted({v["rule"] for v in warn}),
                           notes=sorted({v["rule"] for v in vs if v["severity"] == "info"}),
                           message=("%s%s: %s" % (first["path"] or "(change)", ":%d" % first["line"] if first.get("line") else "", first["message"]))[:300] if first else None))
        per_rule = defaultdict(list)
        for f in c.files:
            for h in check_edit(ctx, f.path, f.status != "A", [t for _, t in f.added]):
                per_rule[h["rule"]].append((f.path, h["message"]))
        for rid, hits in sorted(per_rule.items()):
            events.append(dict(base, source="edit-hook", kind="tool", tool="Edit", decision="block", rules=[rid],
                               target=hits[0][0] + (" and %d more file(s)" % (len(hits) - 1) if len(hits) > 1 else ""),
                               files=len(hits), message=hits[0][1][:300]))
    path = os.path.join(ctx.home, "log", "simulated.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return events


# ----------------------------------------------------------------------------- suggest

VERSIONED = re.compile(r"(^V\d+(_\d+)*__|^\d{3,}[_-]|^\d{8,}|^R__)", re.I)


def suggest(ctx, since_days=365, max_commits=500):
    have = {r["id"] for r in ctx.config.get("rules", [])}
    have_types = {r.get("type") for r in ctx.config.get("rules", [])}
    cands = []

    def add(rule, why):
        if rule["id"] in have:
            return
        base_id, n = rule["id"], 2
        while rule["id"] in {c["rule"]["id"] for c in cands}:
            rule["id"] = "%s-%d" % (base_id, n)
            n += 1
        rule.setdefault("source", "suggested")
        rule.setdefault("added", ctx.today.isoformat())
        cands.append({"rule": rule, "why": why})

    layers = [(l.get("name") or l.get("id")) if isinstance(l, dict) else str(l) for l in ctx.model.get("layers", [])
              if (l.get("id") if isinstance(l, dict) else l) != "external"]
    if ctx.curated and len(layers) >= 2 and "layering" not in have_types:
        add({"id": "layering", "type": "layering", "severity": "block", "description": "Components depend downward through the model's layers: " + " → ".join(layers) + "."},
            "The model orders %d layers; this keeps new code from importing upward." % len(layers))
    if ctx.graph and "no-new-cycles" not in have_types:
        add({"id": "no-new-cycles", "type": "no-new-cycles", "severity": "block", "description": "No import may close a dependency cycle between components."},
            "Cycles make components impossible to change or release separately.")
    if ctx.model.get("snippets") and "pinned-snippets" not in have_types:
        add({"id": "pinned-snippets", "type": "pinned-snippets", "severity": "block", "description": "Code the model pins as key must not change without the model."},
            "%d snippets are pinned; changes to them silently invalidate the map." % len(ctx.model["snippets"]))

    # co-change rules from history, ranked by how often skipping the partner was followed by a fix to it
    if ctx.hist:
        coup = carto.coupling(ctx.hist)
        commits = list(carto.iter_commits(ctx.hist))
        fixes_by_file = defaultdict(list)
        for sha, d, _a, _s, changes, flags in commits:
            if flags.get("fix"):
                for p, _, _ in changes:
                    fixes_by_file[p].append(d)
        exists = {r[0] for r in (ctx.inv or {}).get("files", [])}
        skip_cat = {"docs", "build", "config"}
        pairs = []
        for p in coup["files"]:
            for a, b, prob in ((p["a"], p["b"], p["pBgivenA"]), (p["b"], p["a"], p["pAgivenB"])):
                if prob < 0.75 or p["shared"] < 5 or a not in exists or b not in exists:
                    continue
                if carto.category(a) in skip_cat or carto.category(b) in skip_cat or carto.is_test(a):
                    continue
                if carto.is_test(b) and stem(b).lower().startswith(stem(a).lower()):
                    continue  # implementation -> its own test is what tests-required covers
                missed_then_fixed = 0
                for sha, d, _a, _s, changes, flags in commits:
                    ps = {x for x, _, _ in changes}
                    if a in ps and b not in ps and not flags.get("fix"):
                        if any(0 < (fd - d).total_seconds() <= FOLLOW_UP_DAYS * 86400 for fd in fixes_by_file.get(b, ())):
                            missed_then_fixed += 1
                pairs.append((missed_then_fixed, prob, p["shared"], a, b))
        pairs.sort(key=lambda x: (-x[0], -x[1], -x[2]))
        for mtf, prob, shared, a, b in pairs[:6]:
            rid = "co-change-" + re.sub(r"[^a-z0-9]+", "-", stem(a).lower() + "-" + stem(b).lower()).strip("-")[:48]
            add({"id": rid, "type": "co-change", "severity": "warn" if mtf == 0 else "block", "when": a, "require": b,
                 "description": "%s changes with %s %d%% of the time (%d commits)." % (b, a, round(prob * 100), shared)},
                "%s followed %d of %d changes to %s; %d time(s) it was skipped and a fix to it followed within %d days." % (
                    posixpath.basename(b), shared, int(round(shared / prob)), posixpath.basename(a), mtf, FOLLOW_UP_DAYS))

    # versioned migrations: add new ones, never edit applied ones
    if ctx.inv and "protected-paths" not in have_types:
        mig_dirs = Counter()
        for row in ctx.inv["files"]:
            p = row[0]
            if carto.category(p) == "schema" and VERSIONED.search(posixpath.basename(p)):
                mig_dirs[posixpath.dirname(p)] += 1
        dirs = [d for d, n in mig_dirs.items() if n >= 3]
        if dirs:
            add({"id": "applied-migrations", "type": "protected-paths", "severity": "block", "allowAdd": True,
                 "paths": [d + "/**" for d in sorted(dirs)], "description": "Applied migrations are immutable; add a new migration instead.",
                 "fix": "Write a new versioned migration that makes the change."},
                "%d versioned migration folder(s): %s." % (len(dirs), ", ".join(sorted(dirs)[:3])))

    if ctx.graph and ctx.graph.get("api") and "interface-change" not in have_types:
        specs = [r[0] for r in (ctx.inv or {}).get("files", []) if re.search(r"(openapi|swagger)[^/]*\.(ya?ml|json)$", r[0], re.I)]
        rule = {"id": "interface-change", "type": "interface-change", "severity": "warn", "on": "removed",
                "description": "Removing or changing a declared interface (route, tool, topic) needs its contract updated."}
        if specs:
            rule["require"] = specs[:3]
            rule["severity"] = "block"
        add(rule, "%d interface declarations found%s." % (len(ctx.graph["api"]), (" and a spec at " + specs[0]) if specs else ""))

    if ctx.graph and ctx.graph.get("tests") and "tests-required" not in have_types:
        add({"id": "tests-required", "type": "tests-required", "severity": "warn", "minLines": 30,
             "description": "A code change of 30+ lines comes with a test change."},
            "%d test files import production code here, so the convention exists." % len(ctx.graph["tests"]))
    if "max-size" not in have_types:
        add({"id": "max-size", "type": "max-size", "severity": "warn", "max": 1000, "description": "Changes stay reviewable."},
            "Large changes get shallow reviews.")
    if "ratchet" not in have_types and ctx.graph:
        add({"id": "ratchet", "type": "ratchet", "severity": "block", "description": "Structural debt never grows: " + ", ".join(METRIC_LABEL[m] for m in METRICS) + "."},
            "Locks today's counts so they can only go down. Run --baseline after accepting.")

    rep = replay(ctx, [c["rule"] for c in cands], since_days, max_commits) if cands else {"rules": []}
    by = {r["rule"]: r for r in rep["rules"]}
    for c in cands:
        c["replay"] = by.get(c["rule"]["id"])
    return {"kind": "cartograph-guard-suggestions", "at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "candidates": cands, "window": rep.get("window")}


# ----------------------------------------------------------------------------- explain

GLOBAL_TYPES = ("layering", "no-new-cycles", "ratchet", "max-size", "tests-required")


def explain(ctx, paths):
    L = []
    coup = carto.coupling(ctx.hist) if ctx.hist else None
    glob = [r for r in ctx.rules if r.get("type") in GLOBAL_TYPES and not r.get("paths")]
    if glob:
        L.append("Every change:")
        for r in glob:
            L.append("  [%s] %s: %s" % (r.get("severity") or DEFAULT_SEVERITY.get(r["type"], "block"), r["id"], r.get("description") or r["type"]))
    cmds = [r for r in ctx.rules if r.get("type") == "forbid-command"]
    if cmds:
        L.append("Commands an agent may not run:")
        for r in cmds:
            L.append("  [block] %s: %s" % (r["id"], r.get("description") or r["pattern"]))
            glob.append(r)
    for p in paths:
        p = os.path.relpath(os.path.abspath(p), ctx.repo) if os.path.exists(p) else p.strip("/")
        cid = ctx.mapper(p)
        L.append(p)
        if cid:
            c = ctx.comp[cid]
            L.append("  component: %s (%s layer)" % (c.get("name", cid), c.get("layer", "?")))
            if ctx.curated and c.get("layer") not in (None, "external"):
                below = [x["name"] for x in ctx.model.get("components", []) if x["id"] != cid and (ctx.lidx.get(x["id"], 99) >= ctx.lidx.get(cid, 99) or x.get("layer") == "external")]
                if below:
                    L.append("  may import (same layer or below): %s" % ", ".join(below[:12]))
        hits = []
        for r in ctx.rules:
            if r in glob:
                continue
            t = r.get("type")
            if t == "pinned-snippets":
                applies = any(sn.get("path") == p for sn in ctx.model.get("snippets", []))
            else:
                applies = any(Selector(r.get(k), ctx)(p) for k in ("paths", "when", "from") if r.get(k) is not None)
            if applies:
                hits.append(r)
        for r in hits:
            sev = r.get("severity") or DEFAULT_SEVERITY.get(r["type"], "block")
            extra = ""
            if r["type"] == "co-change":
                extra = " -> also change %s" % (r["require"] if isinstance(r["require"], str) else ", ".join(r["require"]))
            if r["type"] == "pinned-snippets":
                extra = " -> " + ", ".join("%s (lines %s-%s)" % (sn.get("title"), sn.get("start"), sn.get("end")) for sn in ctx.model["snippets"] if sn.get("path") == p)
            if r["type"] == "protected-paths":
                extra = " (new files allowed)" if r.get("allowAdd") else ""
            L.append("  [%s] %s: %s%s" % (sev, r["id"], r.get("description") or r["type"], extra))
        if coup:
            ps = [x for x in carto.partners(coup, p, 0.5) if carto.category(x[0]) != "docs"][:4]
            if ps:
                L.append("  usually changes with: " + ", ".join("%s (%d%%)" % (x[0], round(x[1] * 100)) for x in ps))
        if not hits:
            L.append("  no file-specific rule")
    return "\n".join(L)


# ----------------------------------------------------------------------------- edit-time checks (agent hooks)

def check_edit(ctx, rel, exists, added_lines):
    """Violations for an agent writing `added_lines` into `rel` (None = path only). Used by hooks/protect.py."""
    out = []
    for r in ctx.rules:
        if "edit" not in enforced_at(r) or (r.get("severity") or DEFAULT_SEVERITY.get(r["type"], "block")) != "block":
            continue
        t = r["type"]
        if t == "protected-paths" and Selector(r.get("paths"), ctx)(rel) and not (not exists and r.get("allowAdd")):
            out.append(V(r, rel, None, "%s is protected" % rel))
        elif t == "forbid-pattern" and added_lines and Selector(r.get("paths"), ctx)(rel) and not (r.get("ignoreTests", True) and carto.is_test(rel)):
            rx = re.compile(r["pattern"])
            for ln in added_lines:
                m = rx.search(ln)
                if m:
                    out.append(V(r, rel, None, "%s: `%s`" % (r.get("description") or "forbidden pattern", m.group(0)[:80])))
                    break
    return out


SHELL_WRITE = re.compile(r"(>>?\s*|\btee\b\s+(-a\s+)?|\bsed\s+(-[a-zA-Z]*i|--in-place)\b[^|;&]*?|\b(cp|mv|rm|truncate|touch)\b[^|;&]*?|open\([^)]*['\"](w|a)['\"])")
PATHLIKE = re.compile(r"""[\w./@*{}-]*[/.][\w./@*{}-]+""")


def check_command(ctx, command):
    out = []
    command = command or ""
    for r in ctx.rules:
        if r.get("type") != "forbid-command":
            continue
        m = re.search(r["pattern"], command)
        if m:
            out.append(V(r, None, None, "%s: `%s`" % (r.get("description") or "forbidden command", m.group(0)[:100])))
    # writes through the shell to files an edit-time protected-paths rule covers (cat >, sed -i, tee, cp, mv, rm, open(..., 'w'))
    if SHELL_WRITE.search(command):
        for r in ctx.rules:
            if r.get("type") != "protected-paths" or "edit" not in enforced_at(r) or (r.get("severity") or "block") != "block":
                continue
            sel = Selector(r.get("paths"), ctx)
            for tok in PATHLIKE.findall(command):
                tok = tok.strip("'\"").lstrip("./")
                if tok and sel(tok):
                    out.append(V(r, tok, None, "shell command writes protected file %s" % tok,
                                 r.get("fix") or "Use the script that owns this file, or ask a person."))
                    break
    return out


def exempt(ctx, v, rel):
    for ex in ctx.config.get("exceptions", []):
        if ex.get("rule") in (v["rule"], "*") and exception_state(ex, ctx.today) == "valid" and (rel is None or Selector(ex.get("path") or "**", ctx)(rel)):
            return True
    return False


def format_event(e):
    t = (e.get("at") or "")[11:19]
    src = e.get("source", "?")
    dec = (e.get("decision") or "").upper()
    if e.get("kind") == "run":
        what = "%s · %s file(s) · %s rule(s)" % (e.get("label") or e.get("mode"), e.get("files", "?"), e.get("checked", "?"))
    else:
        what = "%s %s" % (e.get("tool") or "", e.get("target") or "")
    tail = ""
    if e.get("rules"):
        tail += "  [%s]" % ", ".join(e["rules"])
    if e.get("warnings"):
        tail += "  warn[%s]" % ", ".join(e["warnings"])
    if e.get("message") and dec != "ALLOW":
        tail += "  " + e["message"][:140]
    sess = (" " + e["session"][:8]) if e.get("session") else ""
    if e.get("simulated"):
        t = (e.get("at") or "")[:16].replace("T", " ")
        what = "%s %s" % (e.get("sha", "")[:9], (e.get("subject") or "")[:60]) + ((" -> " + e["target"]) if e.get("kind") == "tool" else "")
        src = "sim:" + src
    return "%s  %-12s %-5s %s%s%s" % (t, src, dec, what.strip()[:90], tail, sess)


# ----------------------------------------------------------------------------- rule table

def rule_table(ctx):
    L = ["<!-- cartographer-rules:start -->",
         "Guardrails (generated by `guard.py --table`; edit `.cartographer/guardrails.json`, not this table). "
         "Run `guard.py --explain <file>` before editing. Edit-hook rules stop an agent's edit or command before it runs; "
         "the rest run in the Stop hook, pre-commit and CI on every change.", "",
         "| Rule | What it requires | On failure | Enforced by | Evidence |", "|---|---|---|---|---|"]
    for r in ctx.rules:
        sev = r.get("severity") or DEFAULT_SEVERITY.get(r["type"], "block")
        ev = ", ".join(r.get("evidence", [])[:3]) or r.get("source", "")
        if sev == "info":
            failure, where = "A note for the reviewer on the pull request; never blocks.", "PR comment (CI)"
        else:
            failure, where = r.get("fix") or ("blocks" if sev == "block" else "warns"), enforced_label(r)
        L.append("| `%s` | %s | %s | %s | %s |" % (r["id"], (r.get("description") or r["type"]).replace("|", "\\|"),
                                                   failure.replace("|", "\\|"), where, ev))
    if ctx.config.get("judgment"):
        L += ["", "Not enforced by script (needs a person):", ""]
        for j in ctx.config["judgment"]:
            L.append("- %s%s" % (j.get("text"), (" (%s)" % j["why"]) if j.get("why") else ""))
    L.append("<!-- cartographer-rules:end -->")
    return "\n".join(L)


# ----------------------------------------------------------------------------- proposals

def propose(home, rules, by_id=None):
    """Add rules to guardrails.proposed.json (agents write here; guardrails.json is for humans to accept into)."""
    path = os.path.join(home, "guardrails.proposed.json")
    prop = carto.load(path) or {"schemaVersion": 1, "rules": []}
    have = {r["id"] for r in prop["rules"]} | {r["id"] for r in (carto.load(os.path.join(home, "guardrails.json")) or {}).get("rules", [])}
    have |= {x.get("id") for x in prop.get("rejected", [])}
    n = 0
    for r in rules:
        if r["id"] in have:
            continue
        r = dict(r)
        if by_id and by_id.get(r["id"]):
            r["_why"] = by_id[r["id"]].get("why")
            rp = by_id[r["id"]].get("replay")
            if rp:
                r["_replay"] = {k: rp.get(k) for k in ("replayable", "commits", "fired", "followedByFixOrRevert", "hitRatePct", "baseRatePct")}
                head = git(os.path.dirname(home), "rev-parse", "--short=12", "HEAD", check=False)
                if head:
                    r["_replay"]["head"] = head.strip()
                ex = [{k: x.get(k) for k in ("sha", "date", "subject", "outcome")} for x in (rp.get("examples") or [])[:5]]
                if ex:
                    r["_replay"]["examples"] = ex
        prop["rules"].append(r)
        n += 1
    carto.save(path, prop, pretty=True)
    return n


def accept(home, accept_ids, reject_ids, why=None, by=None):
    """Apply a person's decision. why and by are recorded with it: on the accepted rule as "decided", on the
    rejected entry beside its date, together with the replay evidence the person saw."""
    ppath, gpath = os.path.join(home, "guardrails.proposed.json"), os.path.join(home, "guardrails.json")
    prop = carto.load(ppath) or {"schemaVersion": 1, "rules": []}
    cfg = carto.load(gpath) or {"schemaVersion": 1, "rules": [], "exceptions": []}
    keep, moved = [], []
    for r in prop["rules"]:
        record = {k: v for k, v in (("by", by), ("on", dt.date.today().isoformat()), ("why", why), ("replay", r.get("_replay"))) if v}
        if r["id"] in accept_ids:
            evidence = r.get("_replay")
            r = {k: v for k, v in r.items() if not k.startswith("_")}
            r["status"] = "active"
            r.setdefault("added", dt.date.today().isoformat())
            if why or by:
                r["decided"] = record
            elif evidence:
                r["decided"] = {"on": record["on"], "replay": evidence}
            cfg["rules"] = [x for x in cfg["rules"] if x["id"] != r["id"]] + [r]
            moved.append(r["id"])
        elif r["id"] in reject_ids:
            # Remembered, so --suggest does not propose it again; the pull request that removed it is the decision record.
            prop.setdefault("rejected", [])
            entry = {"id": r["id"], "rejected": record["on"]}
            entry.update({k: v for k, v in record.items() if k in ("by", "why")})
            prop["rejected"] = [x for x in prop["rejected"] if x.get("id") != r["id"]] + [entry]
        else:
            keep.append(r)
    prop["rules"] = keep
    carto.save(gpath, cfg, pretty=True)
    carto.save(ppath, prop, pretty=True)
    missing = set(accept_ids) - set(moved)
    print("accepted: %s%s" % (", ".join(moved) or "none", ("; not found: " + ", ".join(sorted(missing))) if missing else ""))
    if reject_ids:
        print("rejected: " + ", ".join(reject_ids))
    return 0 if not missing else 1


# ----------------------------------------------------------------------------- main

def run(repo, mode, base=None, head=None, refresh=False, model=None, log=True, source=None, session=None, agent=None):
    import time
    t0 = time.time()
    ctx = Context(repo, model)
    if agent is not None:
        ctx.agent = agent
    elif source in ("stop-hook", "cursor-stop"):
        ctx.agent = True
    if mode == "staged":
        cs = staged_changes(repo)
    elif mode == "worktree":
        cs = worktree_changes(repo, base)
    else:
        cs = range_changes(repo, base or default_branch(repo), head or "HEAD")
    ctx.refreshed = refresh
    kept, suppressed, notes, errors = evaluate(cs, ctx)
    res = report(cs, ctx, kept, suppressed, notes, errors, mode)
    if log and ctx.rules:
        log_run(ctx, res, source, session, int((time.time() - t0) * 1000))
    return res


def main():
    # No abbreviations: --acc must not mean --accept, so a hook that refuses --accept cannot be talked around.
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("--repo", default=".")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--staged", action="store_true")
    g.add_argument("--worktree", action="store_true")
    g.add_argument("--head")
    g.add_argument("--explain", nargs="+", metavar="PATH")
    g.add_argument("--replay", action="store_true")
    g.add_argument("--suggest", action="store_true")
    g.add_argument("--baseline", action="store_true")
    g.add_argument("--proposals", action="store_true", help="list proposed rules waiting for a human")
    g.add_argument("--table", action="store_true", help="print the rule table (markdown) for CLAUDE.md / AGENTS.md")
    g.add_argument("--log", action="store_true", help="print the guardrail activity log as a timeline")
    g.add_argument("--simulate-history", action="store_true", help="replay past commits through today's rules, dated at each commit (log/simulated.jsonl)")
    g.add_argument("--export", metavar="HTML", help="write a static guardrails report: enforcement diagram, who checks what, every rule")
    g.add_argument("--story", metavar="HTML", help="write the fence-and-review page: workflow and improvement-loop diagrams, review topics, estimated time saved, real and replayed runs")
    g.add_argument("--outcomes", metavar="HTML", help="write the outcomes report: what was blocked, what got through and the check we would build, what only a person can judge")
    g.add_argument("--accept", nargs="+", metavar="ID", help="move proposed rules into guardrails.json (a human decision)")
    g.add_argument("--reject", nargs="+", metavar="ID", help="drop proposed rules")
    g.add_argument("--serve", action="store_true", help="open the rules workbench for this checkout (a person decides rules there)")
    ap.add_argument("--why", help="with --accept/--reject: the person's reason, recorded with the decision")
    ap.add_argument("--by", help="with --accept/--reject: who decided (recorded with the decision)")
    ap.add_argument("--port", type=int, default=4340, help="with --serve: the local port (default 4340)")
    ap.add_argument("--no-browser", action="store_true", help="with --serve: do not open a browser")
    ap.add_argument("--base", help="base ref (default: the default branch)")
    ap.add_argument("--rule", nargs="+", action="extend", help="limit --replay to these rule ids")
    ap.add_argument("--days", type=int, default=None, help="history window for --replay/--suggest (default historyDays or 365)")
    ap.add_argument("--max-commits", type=int, default=500)
    ap.add_argument("--write", action="store_true", help="with --suggest: write candidates to guardrails.proposed.json")
    ap.add_argument("--force", action="store_true", help="with --baseline: allow metrics to rise")
    ap.add_argument("--refresh", action="store_true", help="run collect.py and graph.py first so the ratchet sees this tree")
    ap.add_argument("--json", help="write the result as JSON to this path")
    ap.add_argument("--markdown", help="write a PR comment section to this path")
    ap.add_argument("--quiet", action="store_true", help="print nothing when everything passes")
    ap.add_argument("--no-log", action="store_true")
    ap.add_argument("--session", help="with --log: only this agent session")
    ap.add_argument("--simulated", action="store_true", help="with --log: show the history simulation instead of real activity")
    ap.add_argument("--last", type=int, default=50, help="with --log: number of events (default 50)")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    home = carto.home(repo)
    days = a.days or carto.load_config(repo).get("historyDays", 365)

    if a.refresh:
        for script in ("collect.py", "graph.py"):
            subprocess.run([sys.executable, os.path.join(HERE, script), "--repo", repo], check=False, stdout=subprocess.DEVNULL)

    if a.explain:
        print(explain(Context(repo), a.explain))
        return 0
    if a.replay:
        ctx = Context(repo)
        proposed = (carto.load(os.path.join(home, "guardrails.proposed.json")) or {}).get("rules", [])
        if a.rule:
            rules = [r for r in ctx.config.get("rules", []) + proposed if r["id"] in a.rule]
        else:
            rules = ctx.rules
        res = replay(ctx, rules, days, a.max_commits)
        carto.save(os.path.join(home, "facts", "guard-replay.json"), res)
        w = res["window"]
        print("replayed %d rule(s) over %d commits (%d days); %d of them (%d%%) were followed by a fix or revert" % (
            len(rules), w["commits"], w["days"], w["followedByFixOrRevert"], round(100.0 * w["followedByFixOrRevert"] / w["commits"]) if w["commits"] else 0))
        for r in res["rules"]:
            if not r.get("replayable"):
                why = "agent hook only" if r.get("type") == "forbid-command" else "checks today's model, facts or file contents"
                print("  %-34s not replayable (%s)" % (r["rule"], why))
                continue
            print("  %-34s fired on %3d commits (%4.1f%%)%s" % (r["rule"], r["fired"], r["firedPct"],
                  ("; %d followed by fix/revert (%s%% vs %s%% base)" % (r["followedByFixOrRevert"], r["hitRatePct"], r["baseRatePct"])) if r["fired"] else "; never fired"))
            for e in r["examples"][:3]:
                print("      %s %s %s%s" % (e["sha"][:9], e["date"], e["subject"][:60], "  [%s]" % e["outcome"] if e["outcome"] else ""))
        if a.json:
            carto.save(a.json, res, pretty=True)
        return 0
    if a.suggest:
        ctx = Context(repo)
        res = suggest(ctx, days, a.max_commits)
        carto.save(os.path.join(home, "facts", "guard-suggestions.json"), res)
        for c in res["candidates"]:
            r, rp = c["rule"], c.get("replay") or {}
            stat = ""
            if rp.get("replayable"):
                stat = " · replay: fired %d/%d commits%s" % (rp["fired"], rp["commits"], (", %d followed by fix/revert" % rp["followedByFixOrRevert"]) if rp["fired"] else "")
            print("%-36s %-16s %-5s %s%s" % (r["id"], r["type"], r["severity"], c["why"], stat))
        if a.write and res["candidates"]:
            n = propose(home, [c["rule"] for c in res["candidates"]], {c["rule"]["id"]: c for c in res["candidates"]})
            print("wrote %d proposal(s) to .cartographer/guardrails.proposed.json; a person decides them with guard.py --serve" % n)
        if a.json:
            carto.save(a.json, res, pretty=True)
        return 0
    if a.accept or a.reject:
        return accept(home, a.accept or [], a.reject or [], why=a.why, by=a.by)
    if a.serve:
        import rules_app
        return rules_app.serve(repo, port=a.port, open_browser=not a.no_browser)
    if a.table:
        print(rule_table(Context(repo)))
        return 0
    if a.simulate_history:
        ctx = Context(repo)
        evs = simulate_history(ctx, days, a.max_commits)
        runs = [e for e in evs if e["kind"] == "run"]
        fired = Counter(r for e in evs for r in e.get("rules", []) + e.get("warnings", []))
        print("simulated %d commits: %d would have been blocked, %d warned, %d agent edits refused (notes for reviewers are not counted)" % (
            len(runs), sum(1 for e in runs if e["decision"] == "block"), sum(1 for e in runs if e["decision"] == "warn"),
            sum(1 for e in evs if e["kind"] == "tool")))
        for r, n in fired.most_common():
            print("  %-40s %3d" % (r, n))
        print("wrote %s" % os.path.join(home, "log", "simulated.jsonl"))
        return 0
    if a.log:
        evs = carto.read_events(home, simulated=getattr(a, "simulated", False))
        if a.session:
            evs = [e for e in evs if (e.get("session") or "").startswith(a.session)]
        evs = evs[-a.last:]
        if a.json:
            carto.save(a.json, evs, pretty=True)
        for e in evs:
            print(format_event(e))
        if not evs:
            print("no guardrail activity logged yet (.cartographer/log/events.jsonl)")
        return 0
    if a.export:
        import guard_export
        res = guard_export.export(Context(repo), a.export)
        print("wrote %s (%d rules, %d human responsibilities, %d gaps)" % (a.export, res["rules"], res["human"], res["none"]))
        return 0
    if a.story:
        import guard_story
        res = guard_story.story(Context(repo), a.story)
        print("wrote %s: review questions %d -> %d left for a person; %d commits stopped before review" % (
            a.story, res["questionsBefore"], res["questionsAfter"], res["roundTrips"]))
        return 0
    if a.outcomes:
        import guard_outcomes
        res = guard_outcomes.outcomes_report(Context(repo), a.outcomes)
        print("wrote %s: %d of %d commits sent back; %d problems got through, %d of %d replayed checks fire at the cause, %d covered today; %d of %d agent probes refused" % (
            a.outcomes, res["blockedCommits"], res["commits"], res["missed"], res["proven"], res["replayed"], res["covered"], res["probesRefused"], res["probes"]))
        return 0
    if a.proposals:
        prop = carto.load(os.path.join(home, "guardrails.proposed.json")) or {"rules": []}
        for r in prop["rules"]:
            rp = r.get("_replay") or {}
            print("%-36s %-16s %-5s %s%s" % (r["id"], r["type"], r.get("severity", ""), r.get("description", ""),
                  (" · replay: fired %s, %s followed by fix/revert" % (rp.get("fired"), rp.get("followedByFixOrRevert"))) if rp.get("replayable") else ""))
        if not prop["rules"]:
            print("no proposals")
        return 0
    if a.baseline:
        ctx = Context(repo)
        cur = metrics(ctx)
        path = os.path.join(home, "baseline.json")
        old = (carto.load(path) or {}).get("metrics", {})
        new = {}
        for k, v in cur.items():
            new[k] = v if (a.force or k not in old) else min(v, old[k])
            if k in old and v > old[k] and not a.force:
                print("%s is %d, above the baseline %d; kept %d (use --force to accept)" % (k, v, old[k], old[k]))
        head = (git(repo, "rev-parse", "--short=12", "HEAD", check=False) or "").strip()
        carto.save(path, {"schemaVersion": 1, "at": ctx.today.isoformat(), "head": head, "metrics": new}, pretty=True)
        print("baseline: " + ", ".join("%s %d" % (METRIC_LABEL.get(k, k), v) for k, v in new.items()))
        return 0

    mode = "staged" if a.staged else "worktree" if a.worktree else "range"
    res = run(repo, mode, a.base, a.head, refresh=a.refresh, log=not a.no_log)
    if not res["rules"]:
        if not a.quiet:
            print("guard: no active rules in .cartographer/guardrails.json (try guard.py --suggest)")
        return 0
    carto.save(os.path.join(home, "facts", "guard", "last.json"), res)
    out = text_report(res, a.quiet)
    if out:
        print(out)
    if a.json:
        carto.save(a.json, res, pretty=True)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as fh:
            fh.write(markdown_report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
