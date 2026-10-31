"""Shared helpers for the Cartographer toolkit (standard library only).

Everything here is deterministic: given the same git history and model, the
same numbers come out. Agents never compute these by hand.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import math
import os
import signal
import re
import subprocess
from collections import Counter, defaultdict

# --------------------------------------------------------------------------- io

if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)   # quiet exit when output is piped to head

def load(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def save(path, data, pretty=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if pretty:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        else:
            json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)


def git(repo, *args, check=True):
    out = subprocess.run(["git", "-C", repo, *args], capture_output=True)
    if out.returncode != 0:
        if check:
            raise SystemExit("git %s failed: %s" % (" ".join(args[:3]), out.stderr.decode("utf-8", "replace").strip()))
        return None
    return out.stdout.decode("utf-8", "replace")


def home(repo):
    return os.path.join(repo, ".cartographer")


def load_config(repo):
    return load(os.path.join(home(repo), "config.json")) or {}


def parse_date(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def week_start(d):
    return (d - dt.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- files

DEFAULT_EXCLUDES = [
    "node_modules/*", "*/node_modules/*", "vendor/*", "*/vendor/*", "dist/*", "*/dist/*", "build/*", "*/build/*",
    "target/*", "*/target/*", "*.min.js", "*.min.css", "*.map", "*.lock", "package-lock.json", "yarn.lock",
    "pnpm-lock.yaml", "*.svg", ".cartographer/*", "*/generated/*", "*/__generated__/*", "*.snap",
]

TEST_PATTERNS = [
    "*/test/*", "test/*", "*/tests/*", "tests/*", "*/__tests__/*", "*.test.*", "*.spec.*", "*_test.go",
    "*_test.py", "test_*.py", "*/test_*.py", "*Test.java", "*Tests.java", "*IT.java", "*Test.kt", "*Tests.cs", "*.Tests/*",
    "*/spec/*", "spec/*", "e2e/*", "*/e2e/*",
]


def excluded(path, patterns):
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def is_test(path):
    return any(fnmatch.fnmatch(path, p) for p in TEST_PATTERNS)


def category(path):
    """Coarse file category used by the PR lens."""
    p = path.lower()
    base = p.rsplit("/", 1)[-1]
    if is_test(path):
        return "test"
    if re.search(r"(^|/)(db/)?(migrations?|changelog|flyway|liquibase|alembic|schema)(/|$)", p) or re.match(r"v\d+(_\d+)*__.*\.sql$", base) or base.endswith(".sql"):
        return "schema"
    if base.endswith((".proto", ".avsc", ".graphql", ".gql", ".wsdl", ".xsd")) or re.search(r"(openapi|swagger|asyncapi)[^/]*\.(ya?ml|json)$", base):
        return "api"
    if base in ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "package.json", "go.mod", "go.sum",
                "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "cargo.toml", "gemfile", "pipfile") or base.endswith((".csproj", ".sln")):
        return "build"
    if base.endswith((".md", ".rst", ".adoc", ".txt")) or p.startswith("docs/"):
        return "docs"
    if base.endswith((".yml", ".yaml", ".properties", ".toml", ".ini", ".conf", ".env", ".json", ".xml", ".tf", ".hcl")) or base in ("dockerfile", "makefile", "jenkinsfile") or p.startswith((".github/", ".gitlab", "deploy/", "k8s/", "helm/", "charts/")):
        return "config"
    return "code"


# --------------------------------------------------------------------------- commits

AI_PATTERNS = [
    ("Claude", re.compile(r"(claude|anthropic)", re.I)),
    ("Copilot", re.compile(r"copilot", re.I)),
    ("Devin", re.compile(r"devin", re.I)),
    ("Cursor", re.compile(r"cursor(agent|\.com|\s*agent)?", re.I)),
    ("Codex", re.compile(r"(codex|openai|chatgpt)", re.I)),
    ("Gemini", re.compile(r"gemini|jules", re.I)),
    ("Amazon Q", re.compile(r"(amazon q|codewhisperer|kiro)", re.I)),
    ("Windsurf", re.compile(r"(windsurf|codeium)", re.I)),
    ("Aider", re.compile(r"aider", re.I)),
    ("Cline", re.compile(r"\bcline\b", re.I)),
]
COAUTHOR = re.compile(r"^co-authored-by:\s*(.+)$", re.I | re.M)
GENERATED_WITH = re.compile(r"generated (with|by) \[?(claude code|copilot|cursor|devin|codex|aider)", re.I)
FIX_SUBJECT = re.compile(r"(^(fix|bugfix|hotfix)(\(.+?\))?!?:)|\b(fix(es|ed|ing)?|bug(s|fix)?|hotfix|defect|regression|crash|incident|patch(ed)?)\b|^(restore[sd]?|repair(s|ed)?|correct(s|ed)?|resolve[sd]?|revert(s|ed)?)\b", re.I)
NOT_FIX = re.compile(r"\b(prefix|suffix|fixtures?|typos?|format(ting)?|whitespace)\b|^docs?:|^(fix(es)?|fixed)\s+(the\s+)?(lint|formatting|typo)s?\s*$", re.I)
REVERT_BODY = re.compile(r"this reverts commit ([0-9a-f]{7,40})", re.I)
PR_SQUASH = re.compile(r"\(#(\d+)\)\s*$")
PR_MERGE = re.compile(r"^Merge pull request #(\d+)")
AGENT_AUTHOR = re.compile(r"(^(claude|claude code|copilot|github copilot|devin|cursor agent|cursoragent|codex|jules|amazon q)\s*<)|<noreply@anthropic\.com>|copilot-swe-agent|devin-ai-integration|cursoragent@|claude\[bot\]|codex\[bot\]", re.I)
BOT = re.compile(r"(\[bot\]|dependabot|renovate|github-actions|snyk-bot|greenkeeper)", re.I)


def ai_tool(author, email, body):
    """Which AI tool, if any, the commit credits. Conservative: trailers, 'Generated with', or an AI bot author."""
    hay = []
    for m in COAUTHOR.finditer(body or ""):
        hay.append(m.group(1))
    g = GENERATED_WITH.search(body or "")
    if g:
        hay.append(g.group(2))
    if BOT.search(author or "") or BOT.search(email or ""):
        hay.append(author + " " + email)
    if re.search(r"(copilot-swe-agent|devin-ai-integration|claude\[bot\]|cursoragent|codex\[bot\])", (author or "") + " " + (email or ""), re.I):
        hay.append(author + " " + email)
    for h in hay:
        for name, rx in AI_PATTERNS:
            if rx.search(h):
                return name
    return None


def classify(subject, body, author, email):
    flags = {}
    if subject.startswith("Revert ") or REVERT_BODY.search(body or ""):
        flags["revert"] = 1
        m = REVERT_BODY.search(body or "")
        if m:
            flags["reverts"] = m.group(1)[:12]
    if FIX_SUBJECT.search(subject) and not NOT_FIX.search(subject):
        flags["fix"] = 1
    tool = ai_tool(author, email, body)
    if tool:
        flags["ai"] = tool
    agent = AGENT_AUTHOR.search((author or "") + " <" + (email or "") + ">")
    if agent:
        flags["agent"] = 1
        if not tool:
            flags["ai"] = next((n for n, rx in AI_PATTERNS if rx.search(agent.group(0))), "AI agent")
    if BOT.search(author or "") or BOT.search(email or ""):
        if not tool:
            flags["bot"] = 1
    m = PR_SQUASH.search(subject) or PR_MERGE.search(subject)
    if m:
        flags["pr"] = int(m.group(1))
    return flags


# --------------------------------------------------------------------------- model mapping

class Mapper:
    """File path -> component id (longest matching prefix in components[].paths wins)."""

    def __init__(self, components):
        self.rules = []
        for c in components or []:
            for p in c.get("paths", []):
                p = p.strip("/")
                if p:
                    self.rules.append((p, c["id"]))
        self.rules.sort(key=lambda r: -len(r[0]))
        self.cache = {}

    def __call__(self, path):
        if path in self.cache:
            return self.cache[path]
        hit = None
        for prefix, cid in self.rules:
            if path == prefix or path.startswith(prefix + "/"):
                hit = cid
                break
        self.cache[path] = hit
        return hit


def layer_index(model):
    order = {l["id"]: i for i, l in enumerate(model.get("layers", []))}
    return {c["id"]: order.get(c.get("layer"), 99) for c in model.get("components", [])}


# --------------------------------------------------------------------------- history analytics

MAX_FILES_PER_COMMIT = 40   # larger commits are refactors/merges; they swamp coupling


def iter_commits(hist):
    """Yield (sha, date, author, subject, [(path, add, del)], flags) newest first."""
    authors, paths = hist["authors"], hist["paths"]
    for c in hist["commits"]:
        sha, when, a_ix, subject, changes = c[:5]
        flags = c[5] if len(c) > 5 else {}
        yield sha, parse_date(when), authors[a_ix], subject, [(paths[p], a, d) for p, a, d in changes], flags


def coupling(hist, mapper=None, min_shared=3, min_revs=4, exclude=None):
    # Component-level coupling only counts code-like files: build files, config and
    # docs change together with everything and would make every pair look coupled.
    """Logical coupling from co-change. Returns file pairs and component pairs.

    For files a, b: shared = commits touching both; P(b|a) = shared / revs(a).
    degree = shared / mean(revs(a), revs(b)) (Tornhill / code-maat definition).
    """
    revs = Counter()
    pair = Counter()
    comp_revs = Counter()
    comp_pair = Counter()
    exclude = exclude or set()
    for _sha, _d, _a, _s, changes, flags in iter_commits(hist):
        if flags.get("bot"):
            continue
        files = sorted({p for p, _, _ in changes if p not in exclude and category(p) != "docs"})
        if not files or len(files) > MAX_FILES_PER_COMMIT:
            continue
        for f in files:
            revs[f] += 1
        for i in range(len(files)):
            for j in range(i + 1, len(files)):
                pair[(files[i], files[j])] += 1
        if mapper:
            comps = sorted({mapper(f) for f in files if mapper(f) and category(f) in ("code", "schema", "api")})
            for c in comps:
                comp_revs[c] += 1
            for i in range(len(comps)):
                for j in range(i + 1, len(comps)):
                    comp_pair[(comps[i], comps[j])] += 1
    files_out = []
    for (a, b), n in pair.items():
        if n < min_shared or revs[a] < min_revs or revs[b] < min_revs:
            continue
        files_out.append({"a": a, "b": b, "shared": n, "revsA": revs[a], "revsB": revs[b],
                          "pBgivenA": round(n / revs[a], 2), "pAgivenB": round(n / revs[b], 2),
                          "degree": round(100.0 * n / ((revs[a] + revs[b]) / 2.0))})
    files_out.sort(key=lambda x: (-x["degree"], -x["shared"]))
    comps_out = []
    for (a, b), n in comp_pair.items():
        if n < min_shared:
            continue
        comps_out.append({"a": a, "b": b, "shared": n, "revsA": comp_revs[a], "revsB": comp_revs[b],
                          "degree": round(100.0 * n / ((comp_revs[a] + comp_revs[b]) / 2.0))})
    comps_out.sort(key=lambda x: (-x["degree"], -x["shared"]))
    return {"files": files_out, "components": comps_out, "revs": dict(revs)}


def partners(coup, path, threshold=0.5):
    """Files that change with `path` at least `threshold` of the time path changes."""
    out = []
    for p in coup["files"]:
        if p["a"] == path and p["pBgivenA"] >= threshold:
            out.append((p["b"], p["pBgivenA"], p["shared"], p["revsA"]))
        elif p["b"] == path and p["pAgivenB"] >= threshold:
            out.append((p["a"], p["pAgivenB"], p["shared"], p["revsB"]))
    out.sort(key=lambda x: -x[1])
    return out


def author_activity(hist):
    """author -> last commit datetime in the window (bots and AI agent authors excluded)."""
    last = {}
    for _sha, d, a, _s, _c, flags in iter_commits(hist):
        if flags.get("bot") or flags.get("agent"):
            continue
        if a not in last:
            last[a] = d
    return last


def bus_factor(counter):
    total = sum(counter.values())
    if not total:
        return 0
    acc = n = 0
    for _, v in counter.most_common():
        acc += v
        n += 1
        if acc * 2 > total:
            return n
    return n


def hotspot_score(commits, loc):
    return commits * math.log2((loc or 0) + 1)


def size_class(lines):
    if lines < 10:
        return "XS"
    if lines < 100:
        return "S"
    if lines < 400:
        return "M"
    if lines < 1000:
        return "L"
    return "XL"


# --------------------------------------------------------------------------- static graph -> components

def component_graph(graph, mapper, include_tests=False):
    """Aggregate file import edges to component edges: {(from, to): {count, example}}."""
    out = {}
    tests = set(graph.get("tests", [])) if graph else set()
    for a, b, line in (graph or {}).get("fileEdges", []):
        if not include_tests and (a in tests or is_test(a)):
            continue
        ca, cb = mapper(a), mapper(b)
        if not ca or not cb or ca == cb:
            continue
        e = out.setdefault((ca, cb), {"count": 0, "example": {"path": a, "line": line, "to": b}})
        e["count"] += 1
    return out


def sccs(nodes, edges):
    """Tarjan strongly connected components; returns lists with >1 node (cycles)."""
    index, low, on, stack, res, counter = {}, {}, set(), [], [], [0]
    adj = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in adj[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                res.append(sorted(comp))

    import sys as _s
    _s.setrecursionlimit(max(10000, _s.getrecursionlimit()))
    for n in nodes:
        if n not in index:
            visit(n)
    return res


# --------------------------------------------------------------------------- event log

EVENT_LOG = os.path.join("log", "events.jsonl")


def detect_source(default="cli"):
    """Who is running a check: set by hooks and CI, so the log says where each decision was made."""
    if os.environ.get("CARTOGRAPHER_SOURCE"):
        return os.environ["CARTOGRAPHER_SOURCE"]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "ci"
    return default


def log_event(home_dir, event):
    """Append one guardrail decision to .cartographer/log/events.jsonl (never raises)."""
    try:
        event = dict(event)
        event.setdefault("at", dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
        if os.environ.get("GITHUB_RUN_ID"):
            event.setdefault("ciRun", os.environ["GITHUB_RUN_ID"])
        path = os.path.join(home_dir, EVENT_LOG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(home_dir, limit=None, simulated=False):
    out = []
    names = (os.path.join("log", "simulated.jsonl"),) if simulated else (EVENT_LOG, os.path.join("log", "guard.jsonl"))
    for name in names:
        path = os.path.join(home_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if name.endswith("guard.jsonl"):          # older format: guard runs only
                        e = {"at": e.get("at"), "source": "cli", "kind": "run", "mode": e.get("mode"), "label": e.get("label"),
                             "decision": "pass" if e.get("ok") else "block", "rules": e.get("blocking", []), "warnings": e.get("warnings", [])}
                    out.append(e)
        except OSError:
            continue
        if out and name in (EVENT_LOG, os.path.join("log", "simulated.jsonl")):
            break
    out.sort(key=lambda e: e.get("at") or "")
    return out[-limit:] if limit else out
