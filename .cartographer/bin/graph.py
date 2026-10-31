#!/usr/bin/env python3
"""Static structure for Cartographer: an import graph and an interface inventory.

Writes <repo>/.cartographer/facts/graph.json:

  fileEdges   [from, to, line]   resolved in-repo imports (Java/Kotlin/Groovy/Scala,
                                 Python, JavaScript/TypeScript, Go, C#)
  external    third-party packages/namespaces imported, with counts
  api         interface inventory: HTTP routes, MCP tools, topics/queues, scheduled jobs, tables
  tests       test files (used to compute which components tests reach)
  languages   per-language files, import statements and resolution rate

It is a heuristic, regex-based graph: no compiler, no dependencies, works on any
checkout in seconds. It under-reports dynamic and reflective wiring (DI by string,
HTTP between services) — those edges stay with the agents and their evidence.
For compiler-accurate graphs, a SCIP index can be converted to the same format.

Usage:
  python3 .cartographer/bin/graph.py [--repo .]
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import carto  # noqa: E402

EXT = {
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".groovy": "jvm", ".scala": "jvm",
    ".py": "py",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js", ".vue": "js", ".svelte": "js",
    ".go": "go",
    ".cs": "cs",
    ".sql": "sql",
}
JS_EXTS = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte", ".d.ts"]
MAX_BYTES = 1_500_000


def lineno(text, pos):
    return text.count("\n", 0, pos) + 1


def strip_json_comments(s):
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(^|[^:\"'])//.*$", r"\1", s, flags=re.M)


class Repo:
    def __init__(self, root, patterns):
        self.root = root
        raw = carto.git(root, "ls-files", "-z")
        self.files = [p for p in raw.split("\x00") if p and not carto.excluded(p, patterns)]
        self.fileset = set(self.files)
        self.dirs = defaultdict(list)
        for p in self.files:
            self.dirs[posixpath.dirname(p)].append(p)
        self.text_cache = {}

    def text(self, p):
        if p in self.text_cache:
            return self.text_cache[p]
        full = os.path.join(self.root, p)
        try:
            if os.path.getsize(full) > MAX_BYTES:
                t = ""
            else:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    t = fh.read()
        except OSError:
            t = ""
        self.text_cache[p] = t
        return t


# ------------------------------------------------------------------- JVM

def jvm_index(repo, files):
    fqn, pkg_files = {}, defaultdict(list)
    for p in files:
        m = re.search(r"^\s*package\s+([\w.]+)", repo.text(p), re.M)
        if not m:
            continue
        pkg = m.group(1)
        stem = posixpath.splitext(posixpath.basename(p))[0]
        fqn[pkg + "." + stem] = p
        pkg_files[pkg].append(p)
    return fqn, pkg_files


JVM_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+)(\.\*)?\s*;?", re.M)


def jvm_edges(repo, files, ext):
    fqn, pkg_files = jvm_index(repo, files)
    roots = {".".join(k.split(".")[:3]) for k in pkg_files}
    edges, stats = [], Counter()
    for p in files:
        t = repo.text(p)
        for m in JVM_IMPORT.finditer(t):
            name, star = m.group(2), m.group(3)
            stats["imports"] += 1
            targets = []
            if star and name in pkg_files:
                targets = pkg_files[name]
            else:
                parts = name.split(".")
                for cut in range(0, 3):
                    cand = ".".join(parts[:len(parts) - cut]) if cut else name
                    if cand in fqn:
                        targets = [fqn[cand]]
                        break
            if targets:
                stats["resolved"] += 1
                ln = lineno(t, m.start())
                for tgt in targets[:50]:
                    if tgt != p:
                        edges.append([p, tgt, ln])
            elif ".".join(name.split(".")[:3]) not in roots:
                ext[".".join(name.split(".")[:2])] += 1
                stats["external"] += 1
    edges.extend(same_scope_edges(repo, pkg_files))
    return edges, stats


def same_scope_edges(repo, scope_files):
    """Classes in the same package/namespace reference each other without imports.
    Add an edge when a file mentions another file's type name as a whole word."""
    out = []
    for _scope, fs in scope_files.items():
        if len(fs) < 2 or len(fs) > 400:
            continue
        stems = {posixpath.splitext(posixpath.basename(f))[0]: f for f in fs}
        rx = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(stems, key=len, reverse=True)) + r")\b")
        for f in fs:
            t = repo.text(f)
            body = re.sub(r"^\s*(package|import|using|namespace)\b.*$", "", t, flags=re.M)
            seen = set()
            for m in rx.finditer(body):
                tgt = stems[m.group(1)]
                if tgt != f and tgt not in seen:
                    seen.add(tgt)
                    out.append([f, tgt, lineno(body, m.start())])
    return out


# ------------------------------------------------------------------- Python

PY_IMPORT = re.compile(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*)", re.M)
PY_FROM = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import[ \t]+\(?([^\n)]*)", re.M)


def py_index(files):
    idx = defaultdict(set)
    for p in files:
        mod = p[:-3]
        if mod.endswith("/__init__"):
            mod = mod[:-9]
        parts = mod.split("/")
        for i in range(len(parts)):
            idx[".".join(parts[i:])].add(p)
    return idx


def py_resolve(idx, name, importer):
    cands = idx.get(name)
    if not cands:
        return None
    if len(cands) == 1:
        return next(iter(cands))
    # prefer the candidate sharing the longest directory prefix with the importer
    return max(cands, key=lambda c: (len(posixpath.commonprefix([c, importer])), -len(c)))


def py_edges(repo, files, ext):
    idx = py_index(files)
    edges, stats = [], Counter()
    for p in files:
        t = repo.text(p)
        pkg = posixpath.dirname(p)
        for m in PY_IMPORT.finditer(t):
            for name in [x.strip() for x in m.group(1).split(",")]:
                stats["imports"] += 1
                hit = None
                parts = name.split(".")
                for cut in range(len(parts), 0, -1):
                    hit = py_resolve(idx, ".".join(parts[:cut]), p)
                    if hit:
                        break
                if hit and hit != p:
                    stats["resolved"] += 1
                    edges.append([p, hit, lineno(t, m.start())])
                elif not hit:
                    ext[parts[0]] += 1
                    stats["external"] += 1
        for m in PY_FROM.finditer(t):
            dots, mod, names = m.group(1), m.group(2), m.group(3)
            stats["imports"] += 1
            ln = lineno(t, m.start())
            if dots:
                base = pkg
                for _ in range(len(dots) - 1):
                    base = posixpath.dirname(base)
                rel = (base + "/" + mod.replace(".", "/")).strip("/") if mod else base
                found = []
                for n in [x.strip().split(" as ")[0] for x in names.split(",") if x.strip() and x.strip() != "*"]:
                    for c in (rel + "/" + n + ".py", rel + "/" + n + "/__init__.py"):
                        if c in repo.fileset:
                            found.append(c)
                if not found:
                    for c in (rel + ".py", rel + "/__init__.py"):
                        if c in repo.fileset:
                            found.append(c)
                            break
            else:
                found = []
                for n in [x.strip().split(" as ")[0] for x in names.split(",") if x.strip() and x.strip() != "*"]:
                    sub = py_resolve(idx, mod + "." + n, p) if re.match(r"^\w+$", n) else None
                    if sub and sub.endswith(("/" + n + ".py", "/" + n + "/__init__.py")):
                        found.append(sub)
                if not found:
                    parts = mod.split(".")
                    for cut in range(len(parts), 0, -1):
                        hit = py_resolve(idx, ".".join(parts[:cut]), p)
                        if hit:
                            found.append(hit)
                            break
                if not found and mod.split(".")[0] not in idx:
                    ext[mod.split(".")[0]] += 1
                    stats["external"] += 1
            found = [f for f in dict.fromkeys(found) if f != p]
            if found:
                stats["resolved"] += 1
                edges.extend([p, f, ln] for f in found)
    return edges, stats


# ------------------------------------------------------------------- JS / TS

JS_SPEC = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|^\s*import\s+|\bexport\s+\*\s+from\s*)(['"`])([^'"`\n]+)\1""", re.M)


def js_config(repo):
    aliases = []  # (prefix, [target prefixes]) from tsconfig/jsconfig paths
    for p in repo.files:
        b = posixpath.basename(p)
        if b in ("tsconfig.json", "jsconfig.json") or (b.startswith("tsconfig.") and b.endswith(".json")):
            try:
                cfg = json.loads(strip_json_comments(repo.text(p)))
            except ValueError:
                continue
            co = cfg.get("compilerOptions") or {}
            base = posixpath.normpath(posixpath.join(posixpath.dirname(p), co.get("baseUrl", ".")))
            for k, vs in (co.get("paths") or {}).items():
                aliases.append((k.rstrip("*"), [posixpath.normpath(posixpath.join(base, v.rstrip("*"))) + ("/" if v.endswith("/*") else "") for v in vs]))
    packages = {}
    for p in repo.files:
        if posixpath.basename(p) == "package.json" and "node_modules" not in p:
            try:
                name = json.loads(repo.text(p)).get("name")
            except ValueError:
                continue
            if name and posixpath.dirname(p):
                packages[name] = posixpath.dirname(p)
    return aliases, packages


def js_try(repo, base):
    base = posixpath.normpath(base)
    if base in (".", ""):
        base = ""
    if base in repo.fileset:
        return base
    stem = re.sub(r"\.(js|mjs|cjs|jsx)$", "", base)  # TS ESM imports use .js for .ts sources
    for b in (base, stem):
        for e in JS_EXTS:
            if b and b + e in repo.fileset:
                return b + e
        for e in JS_EXTS:
            c = posixpath.join(b, "index" + e) if b else "index" + e
            if c in repo.fileset:
                return c
    if base and base + "/package.json" in repo.fileset:
        try:
            main = json.loads(repo.text(base + "/package.json")).get("main")
        except ValueError:
            main = None
        if main:
            return js_try(repo, posixpath.join(base, main)) if main.strip("./") else None
    return None


def js_edges(repo, files, ext):
    aliases, packages = js_config(repo)
    edges, stats = [], Counter()
    for p in files:
        t = repo.text(p)
        d = posixpath.dirname(p)
        for m in JS_SPEC.finditer(t):
            spec = m.group(2)
            if any(ch.isspace() for ch in spec) or "${" in spec:
                continue   # prose in a comment or string ("from "Get the file""), or a template, never a module
            stats["imports"] += 1
            hit = None
            if spec.startswith("."):
                hit = js_try(repo, posixpath.join(d, spec))
            else:
                for pre, targets in aliases:
                    if pre and spec.startswith(pre):
                        for tg in targets:
                            hit = js_try(repo, tg + spec[len(pre):]) if tg.endswith("/") else js_try(repo, tg)
                            if hit:
                                break
                    if hit:
                        break
                if not hit:
                    pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
                    if pkg in packages:
                        rest = spec[len(pkg):].lstrip("/")
                        root = packages[pkg]
                        hit = (js_try(repo, posixpath.join(root, rest)) or js_try(repo, posixpath.join(root, "src", rest))) if rest else \
                            (js_try(repo, root + "/src/index") or js_try(repo, root + "/index") or js_try(repo, root + "/src/main"))
                        if not hit:
                            hit = root + "/package.json" if root + "/package.json" in repo.fileset else None
                    elif not spec.startswith(("http", "data:", "node:")):
                        ext[pkg] += 1
                        stats["external"] += 1
                    elif spec.startswith("node:"):
                        ext["node:"] += 1
                        stats["external"] += 1
            if hit and hit != p:
                stats["resolved"] += 1
                edges.append([p, hit, lineno(t, m.start())])
    return edges, stats


# ------------------------------------------------------------------- Go

GO_IMPORTS = re.compile(r"^import\s*(?:\(\s*(.*?)\)|(?:[\w.]+\s+)?\"([^\"]+)\")", re.M | re.S)


def go_edges(repo, files, ext):
    modules = {}
    for p in repo.files:
        if posixpath.basename(p) == "go.mod":
            m = re.search(r"^module\s+(\S+)", repo.text(p), re.M)
            if m:
                modules[m.group(1)] = posixpath.dirname(p)
    edges, stats = [], Counter()
    for p in files:
        t = repo.text(p)
        for m in GO_IMPORTS.finditer(t):
            block = m.group(1) or ('"%s"' % m.group(2))
            for im in re.finditer(r"\"([^\"]+)\"", block):
                spec = im.group(1)
                stats["imports"] += 1
                hit = None
                for mod, root in modules.items():
                    if spec == mod or spec.startswith(mod + "/"):
                        d = posixpath.normpath(posixpath.join(root, spec[len(mod):].lstrip("/"))) if root else spec[len(mod):].lstrip("/")
                        goes = [f for f in repo.dirs.get(d, []) if f.endswith(".go") and not f.endswith("_test.go")]
                        hit = goes[0] if goes else None
                        break
                if hit and hit != p:
                    stats["resolved"] += 1
                    edges.append([p, hit, lineno(t, m.start() + im.start())])
                elif not hit:
                    if not any(spec == m or spec.startswith(m + "/") for m in modules):
                        ext["/".join(spec.split("/")[:3]) if "." in spec.split("/")[0] else "std:" + spec.split("/")[0]] += 1
                        stats["external"] += 1
    return edges, stats


# ------------------------------------------------------------------- C#

def cs_edges(repo, files, ext):
    ns_files = defaultdict(list)
    for p in files:
        for m in re.finditer(r"^\s*namespace\s+([\w.]+)", repo.text(p), re.M):
            ns_files[m.group(1)].append(p)
    edges, stats = [], Counter()
    for p in files:
        t = repo.text(p)
        for m in re.finditer(r"^\s*(?:global\s+)?using\s+(?!static\b)(?!\w+\s*=)([\w.]+)\s*;", t, re.M):
            ns = m.group(1)
            stats["imports"] += 1
            targets = ns_files.get(ns)
            if targets:
                stats["resolved"] += 1
                seen_dirs = set()
                for tg in targets:
                    dd = posixpath.dirname(tg)
                    if tg != p and dd not in seen_dirs:
                        seen_dirs.add(dd)
                        edges.append([p, tg, lineno(t, m.start())])
            elif not any(k.startswith(ns.split(".")[0] + ".") or k == ns.split(".")[0] for k in ns_files):
                ext[".".join(ns.split(".")[:2])] += 1
                stats["external"] += 1
    edges.extend(same_scope_edges(repo, ns_files))
    return edges, stats


# ------------------------------------------------------------------- interfaces

ROUTE_RULES = [
    # (language family, kind, regex, method group or fixed, name group)
    ("js", "http", re.compile(r"\b(?:app|router|server|api|routes?|fastify|r)\.(get|post|put|delete|patch|all|options|head)\(\s*(['\"`])([^'\"`]+)\2"), 1, 3),
    ("py", "http", re.compile(r"@\w+\.(get|post|put|delete|patch|route|api_route|websocket)\(\s*(['\"])([^'\"]*)\2"), 1, 3),
    ("go", "http", re.compile(r"\.(GET|POST|PUT|DELETE|PATCH|HandleFunc|Handle)\(\s*\"([^\"]+)\""), 1, 2),
    ("cs", "http", re.compile(r"\[Http(Get|Post|Put|Delete|Patch)(?:\(\s*\"([^\"]*)\"\s*\))?\]"), 1, 2),
    ("js", "mcp-tool", re.compile(r"\.(?:registerTool|tool)\(\s*(['\"`])([\w.\-/]+)\1"), None, 2),
    ("js", "mcp-resource", re.compile(r"\.(?:registerResource|resource)\(\s*(['\"`])([\w.\-/ ]+)\1"), None, 2),
    ("js", "mcp-prompt", re.compile(r"\.(?:registerPrompt|prompt)\(\s*(['\"`])([\w.\-/]+)\1"), None, 2),
    ("jvm", "event-in", re.compile(r"@KafkaListener\([^)]*?topics\s*=\s*\{?\s*\"([^\"]+)\""), None, 1),
    ("jvm", "event-in", re.compile(r"@(?:RabbitListener\([^)]*?queues|JmsListener\([^)]*?destination)\s*=\s*\{?\s*\"([^\"]+)\""), None, 1),
    ("jvm", "event-in", re.compile(r"@SqsListener\(\s*(?:value\s*=\s*)?\{?\s*\"([^\"]+)\""), None, 1),
    ("jvm", "event-out", re.compile(r"(?:kafkaTemplate|template|producer)\.send\(\s*\"([\w.\-]+)\""), None, 1),
    ("js", "event-out", re.compile(r"\.send\(\s*\{\s*topic:\s*['\"]([\w.\-]+)['\"]"), None, 1),
    ("jvm", "job", re.compile(r"@Scheduled\(([^)]*)\)"), None, 1),
    ("py", "job", re.compile(r"@(?:\w+\.)?(?:task|shared_task|periodic_task)\b"), None, None),
    ("jvm", "table", re.compile(r"@Table\(\s*name\s*=\s*\"([^\"]+)\""), None, 1),
    ("py", "table", re.compile(r"__tablename__\s*=\s*['\"]([^'\"]+)['\"]"), None, 1),
    ("sql", "table", re.compile(r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?([\w.]+)", re.I), None, 1),
]
SPRING_MAP = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b(?:\(\s*(?:value\s*=\s*|path\s*=\s*)?\{?\s*\"([^\"]*)\")?")
SPRING_METHOD = re.compile(r"method\s*=\s*(?:\{\s*)?RequestMethod\.(\w+)")
JAXRS_PATH = re.compile(r"@Path\(\s*\"([^\"]*)\"")
JAXRS_VERB = re.compile(r"@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
CS_ROUTE = re.compile(r"\[Route\(\s*\"([^\"]*)\"\s*\)\]")
PY_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)", re.M)


def join_route(a, b):
    a, b = (a or "").strip(), (b or "").strip()
    if not a:
        return b or "/"
    if not b:
        return a
    return a.rstrip("/") + "/" + b.lstrip("/")


def interfaces(repo, by_family):
    out = []
    for fam, files in by_family.items():
        for p in files:
            t = repo.text(p)
            if fam == "jvm":
                prefix, class_seen = "", False
                cls = re.search(r"\b(class|interface|object)\s+\w+", t)
                cls_pos = cls.start() if cls else len(t)
                for m in SPRING_MAP.finditer(t):
                    verb, path = m.group(1), m.group(2) or ""
                    if m.start() < cls_pos and verb == "Request":
                        prefix = path
                        continue
                    if verb == "Request":
                        mm = SPRING_METHOD.search(t, m.start(), m.start() + 300)
                        verb = mm.group(1).title() if mm else "Any"
                    out.append({"kind": "http", "method": verb.upper(), "name": join_route(prefix, path), "path": p, "line": lineno(t, m.start())})
                jprefix = ""
                for m in JAXRS_PATH.finditer(t):
                    if m.start() < cls_pos:
                        jprefix = m.group(1)
                        continue
                    v = JAXRS_VERB.search(t, max(0, m.start() - 200), m.start() + 200)
                    out.append({"kind": "http", "method": v.group(1) if v else "ANY", "name": join_route(jprefix, m.group(1)), "path": p, "line": lineno(t, m.start())})
            if fam == "cs":
                cr = CS_ROUTE.search(t)
                base_route = cr.group(1) if cr else ""
            for rfam, kind, rx, mg, ng in ROUTE_RULES:
                if rfam != fam:
                    continue
                for m in rx.finditer(t):
                    name = m.group(ng) if ng else None
                    if kind == "job" and fam == "py":
                        d = PY_DEF.search(t, m.end())
                        name = d.group(1) if d else "task"
                    method = (m.group(mg).upper() if mg else None)
                    if fam == "cs" and kind == "http":
                        name = join_route(base_route, name or "")
                    if method in ("HANDLEFUNC", "HANDLE"):
                        method = "ANY"
                    if kind == "job" and fam == "jvm":
                        name = re.sub(r"\s+", " ", name)[:80]
                    out.append({"kind": kind, "method": method, "name": name or "", "path": p, "line": lineno(t, m.start())})
    # Django urls.py
    for p in by_family.get("py", []):
        if posixpath.basename(p) == "urls.py":
            t = repo.text(p)
            for m in re.finditer(r"\b(?:re_)?path\(\s*r?['\"]([^'\"]*)['\"]", t):
                out.append({"kind": "http", "method": "ANY", "name": "/" + m.group(1).lstrip("^/"), "path": p, "line": lineno(t, m.start())})
    seen, uniq = set(), []
    for it in out:
        k = (it["kind"], it.get("method"), it["name"], it["path"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


def build_graph(root, patterns):
    repo = Repo(root, patterns)
    fam_files = defaultdict(list)
    for p in repo.files:
        ext = posixpath.splitext(p)[1].lower()
        if p.endswith(".d.ts"):
            continue
        if ext in EXT:
            fam_files[EXT[ext]].append(p)
    ext_pkgs = Counter()
    edges, langs = [], {}
    for fam, fn in (("jvm", jvm_edges), ("py", py_edges), ("js", js_edges), ("go", go_edges), ("cs", cs_edges)):
        if fam_files.get(fam):
            e, st = fn(repo, fam_files[fam], ext_pkgs)
            edges.extend(e)
            langs[fam] = {"files": len(fam_files[fam]), "imports": st["imports"], "external": st["external"], "resolved": st["resolved"]}
    seen, uniq = set(), []
    for e in edges:
        k = (e[0], e[1])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    fan_in, fan_out = Counter(), Counter()
    for a, b, _ in uniq:
        fan_out[a] += 1
        fan_in[b] += 1
    api = interfaces(repo, fam_files)
    tests = [p for p in repo.files if carto.is_test(p) and posixpath.splitext(p)[1].lower() in EXT]
    return {"fileEdges": uniq, "external": dict(ext_pkgs.most_common(60)), "api": api, "tests": tests,
            "languages": langs, "fanIn": fan_in.most_common(30), "fanOut": fan_out.most_common(30),
            "filesScanned": sum(len(v) for v in fam_files.values())}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    root = os.path.abspath(args.repo)
    cfg = carto.load_config(root)
    patterns = carto.DEFAULT_EXCLUDES + cfg.get("graphExclude", [])
    g = build_graph(root, patterns)
    g["commit"] = carto.git(root, "rev-parse", "--short=12", "HEAD").strip()
    out = args.out or os.path.join(carto.home(root), "facts", "graph.json")
    carto.save(out, g)
    names = {"jvm": "JVM", "py": "Python", "js": "JS/TS", "go": "Go", "cs": "C#"}
    for fam, st in g["languages"].items():
        internal = st["imports"] - st["external"]
        rate = 100.0 * st["resolved"] / internal if internal else 100.0
        print("%-7s %5d files, %6d imports (%d third-party), %5.1f%% of in-repo imports resolved" % (names[fam], st["files"], st["imports"], st["external"], rate))
    kinds = Counter(a["kind"] for a in g["api"])
    print("edges: %d file-to-file · interfaces: %s · tests: %d files" % (
        len(g["fileEdges"]), ", ".join("%d %s" % (n, k) for k, n in kinds.most_common()) or "none", len(g["tests"])))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
