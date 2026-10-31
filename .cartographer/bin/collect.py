#!/usr/bin/env python3
"""Collect deterministic facts about a git repository for Cartographer.

Writes (never hand-edit; re-run instead):

  <out>/inventory.json  tracked files at HEAD: language, lines, created and last-modified dates
  <out>/history.json    commits in the window with per-file add/delete counts and flags
                        (fix, revert, AI-assisted tool, bot, PR number)

Standard library only. Python 3.8+.

Usage:
  python3 .cartographer/bin/collect.py                 # repo = cwd, 365-day window
  python3 .cartographer/bin/collect.py --days 180 --repo path/to/repo
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
from collections import Counter, defaultdict
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import carto  # noqa: E402

LANGUAGES = {
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".py": "Python", ".rb": "Ruby", ".php": "PHP", ".go": "Go", ".rs": "Rust",
    ".c": "C", ".h": "C", ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cxx": "C++",
    ".cs": "C#", ".vb": "Visual Basic", ".fs": "F#", ".swift": "Swift", ".m": "Objective-C",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".mts": "TypeScript", ".vue": "Vue", ".svelte": "Svelte",
    ".html": "HTML", ".htm": "HTML", ".jsp": "JSP", ".css": "CSS", ".scss": "SCSS", ".less": "Less",
    ".sql": "SQL", ".pls": "PL/SQL", ".pks": "PL/SQL", ".pkb": "PL/SQL",
    ".xml": "XML", ".xsd": "XML", ".wsdl": "XML", ".xsl": "XML", ".xslt": "XML",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML", ".ini": "INI", ".properties": "Properties",
    ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell", ".bat": "Batch", ".cmd": "Batch",
    ".tf": "Terraform", ".hcl": "HCL", ".gradle": "Gradle", ".cbl": "COBOL", ".cob": "COBOL", ".cpy": "COBOL",
    ".jcl": "JCL", ".pli": "PL/I", ".r": "R", ".dart": "Dart", ".lua": "Lua", ".pl": "Perl", ".ex": "Elixir", ".exs": "Elixir",
    ".md": "Markdown", ".mdx": "Markdown", ".rst": "reStructuredText", ".adoc": "AsciiDoc",
}
FILENAMES = {"Dockerfile": "Dockerfile", "Makefile": "Makefile", "Jenkinsfile": "Groovy", "pom.xml": "Maven POM"}
MAX_FILE_BYTES = 2_000_000


def language_of(path):
    base = os.path.basename(path)
    if base in FILENAMES:
        return FILENAMES[base]
    return LANGUAGES.get(os.path.splitext(base)[1].lower(), "Other")


def count_lines(full):
    """Line count, or None for binary/unreadable files. Large text files are streamed, never skipped."""
    try:
        size = os.path.getsize(full)
        with open(full, "rb") as fh:
            head = fh.read(8192)
            if b"\x00" in head:
                return None
            if size <= MAX_FILE_BYTES:
                data = head + fh.read()
                if not data:
                    return 0
                return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
            n, last = head.count(b"\n"), head[-1:]
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                n += chunk.count(b"\n")
                last = chunk[-1:]
            return n + (0 if last == b"\n" else 1)
    except OSError:
        return None


def file_ages(repo, max_commits):
    """path -> [created, lastModified] over ALL history (bounded). Cheap: names only, and --no-renames so a
    partial clone (--filter=blob:none) never fetches file contents to detect renames."""
    raw = carto.git(repo, "log", "--no-merges", "--format=@@c@@%aI", "--name-only", "--no-renames",
                   "--max-count=%d" % max_commits)
    ages = {}
    when = None
    for line in raw.splitlines():
        if line.startswith("@@c@@"):
            when = line[5:15]
        elif line and when:
            a = ages.get(line)
            if a is None:
                ages[line] = [when, when]  # newest first: first seen = last modified
            else:
                a[0] = when  # keeps moving back to the oldest = created
    return ages


def inventory(repo, patterns, max_commits):
    raw = carto.git(repo, "ls-files", "-z")
    ages = file_ages(repo, max_commits)
    files, langs, skipped = [], {}, 0
    excluded_n, binary, large = 0, [], []
    for path in filter(None, raw.split("\x00")):
        if carto.excluded(path, patterns):
            skipped += 1
            excluded_n += 1
            continue
        full = os.path.join(repo, path)
        loc = count_lines(full)
        if loc is None:
            skipped += 1
            binary.append(path)
            continue
        try:
            if os.path.getsize(full) > MAX_FILE_BYTES:
                large.append({"path": path, "bytes": os.path.getsize(full), "loc": loc})
        except OSError:
            pass
        lang = language_of(path)
        created, modified = ages.get(path, [None, None])
        files.append([path, lang, loc, created, modified])
        agg = langs.setdefault(lang, {"name": lang, "files": 0, "loc": 0})
        agg["files"] += 1
        agg["loc"] += loc
    total = sum(f[2] for f in files) or 1
    languages = sorted(langs.values(), key=lambda x: -x["loc"])
    for l in languages:
        l["pct"] = round(100.0 * l["loc"] / total, 1)
    return {"files": files, "languages": languages, "totalFiles": len(files),
            "totalLoc": sum(f[2] for f in files), "skipped": skipped, "excluded": excluded_n,
            "binary": len(binary), "large": sorted(large, key=lambda x: -x["bytes"])}


def history(repo, days, max_commits, patterns):
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%d")
    fmt = "%x1e%H%x1f%aI%x1f%aN%x1f%aE%x1f%s%x1f%b%x1d"
    raw = carto.git(repo, "log", "--no-merges", "-M", "--numstat", "--date=iso-strict",
                    "--since=" + since, "--max-count=%d" % max_commits, "--format=" + fmt)
    identity = resolve_identities(raw)
    authors, author_ix, paths, path_ix, commits = [], {}, [], {}, []
    for block in raw.split("\x1e"):
        if not block.strip():
            continue
        head, _, stats = block.partition("\x1d")
        parts = head.split("\x1f")
        if len(parts) < 6:
            continue
        sha, when, name, email, subject, body = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
        key = identity.get((name, email), name)   # one person, many spellings and addresses
        if key not in author_ix:
            author_ix[key] = len(authors)
            authors.append(key)
        name = authors[author_ix[key]]
        changes = []
        for line in stats.splitlines():
            cols = line.split("\t")
            if len(cols) != 3:
                continue
            add, dele, p = cols
            p = normalize_path(p)
            if carto.excluded(p, patterns):
                continue
            if p not in path_ix:
                path_ix[p] = len(paths)
                paths.append(p)
            changes.append([path_ix[p], int(add) if add.isdigit() else 0, int(dele) if dele.isdigit() else 0])
        row = [sha[:12], when, author_ix[key], subject[:200], changes]
        flags = carto.classify(subject, body, name, email)
        if flags:
            row.append(flags)
        commits.append(row)
    return {"since": since, "days": days, "truncated": len(commits) >= max_commits,
            "authors": authors, "paths": paths, "commits": commits,
            "added": lifecycle(repo, since, "A", patterns), "deleted": lifecycle(repo, since, "D", patterns)}


GENERIC_EMAIL = re.compile(r"^(noreply|no-reply|actions|bot|build|ci|jenkins|root|admin)@", re.I)


def name_key(name):
    n = " ".join(name.split())
    if "," in n:                                   # "Hairston, Matthew S" -> "Matthew S Hairston"
        last, _, first = n.partition(",")
        n = (first.strip() + " " + last.strip()).strip()
    return n.lower()


def email_key(email):
    e = email.strip().lower()
    local, _, domain = e.partition("@")
    if not domain or GENERIC_EMAIL.match(e):
        return None
    return local.split("+", 1)[0] + "@" + domain    # drop +tags: a+work@x == a@x


def resolve_identities(raw):
    """Map (name, email) -> canonical display name. Same email (ignoring case and +tags) or same name
    (ignoring case, spacing and "Last, First" order) is the same person; .mailmap still wins because git applies it to %aN/%aE."""
    pairs = Counter()
    for block in raw.split("\x1e"):
        parts = block.partition("\x1d")[0].split("\x1f")
        if len(parts) >= 4:
            pairs[(parts[2], parts[3])] += 1
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for (name, email), _ in pairs.items():
        n = "n:" + name_key(name)
        find(n)
        ek = email_key(email)
        if ek:
            union(n, "e:" + ek)
    groups = defaultdict(Counter)
    def natural(name):
        n = " ".join(name.split())
        if "," in n:
            last, _, first = n.partition(",")
            n = (first.strip() + " " + last.strip()).strip()
        return n
    for (name, email), c in pairs.items():
        groups[find("n:" + name_key(name))][natural(name)] += c
    display = {}
    for root, names in groups.items():
        display[root] = sorted(names.items(), key=lambda x: (len(x[0].split()) < 2, -x[1]))[0][0]   # a full name, then the most used
    return {(name, email): display[find("n:" + name_key(name))] for (name, email) in pairs}


def normalize_path(p):
    import re
    m = re.match(r"^(.*)\{(.*) => (.*)\}(.*)$", p)
    if m:
        pre, _old, new, post = m.groups()
        return re.sub(r"/+", "/", pre + new + post).strip("/")
    if " => " in p:
        return p.split(" => ", 1)[1]
    return p


def lifecycle(repo, since, kind, patterns):
    raw = carto.git(repo, "log", "--no-merges", "--diff-filter=" + kind, "--name-only", "--no-renames",
                    "--since=" + since, "--format=@@c@@%aI")
    seen, out, when = set(), [], None
    for line in raw.splitlines():
        if line.startswith("@@c@@"):
            when = line[5:]
        elif line.strip() and when and line not in seen and not carto.excluded(line, patterns):
            seen.add(line)
            out.append([line, when])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default=None, help="output dir (default: <repo>/.cartographer/facts)")
    ap.add_argument("--days", type=int, default=None, help="history window in days (default 365)")
    ap.add_argument("--max-commits", type=int, default=None, help="cap on commits read (default 50000)")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    cfg = carto.load_config(repo)
    days = args.days or cfg.get("historyDays", 365)
    max_commits = args.max_commits or cfg.get("maxCommits", 50000)
    patterns = carto.DEFAULT_EXCLUDES + cfg.get("exclude", [])
    out = args.out or os.path.join(carto.home(repo), "facts")
    os.makedirs(out, exist_ok=True)

    head = carto.git(repo, "rev-parse", "HEAD").strip()
    branch = carto.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    stamp = {"commit": head[:12], "branch": branch, "commitDate": carto.git(repo, "log", "-1", "--format=%cI").strip(),
             "collectedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}
    remote = carto.git(repo, "remote", "get-url", "origin", check=False)
    if remote:
        stamp["remote"] = remote.strip()

    inv = inventory(repo, patterns, max(max_commits, 200000))
    inv.update(stamp)
    hist = history(repo, days, max_commits, patterns)
    hist.update(stamp)
    carto.save(os.path.join(out, "inventory.json"), inv)
    carto.save(os.path.join(out, "history.json"), hist)

    flags = [c[5] for c in hist["commits"] if len(c) > 5]
    print("inventory: %d files, %d lines; not counted: %d excluded by config, %d binary" % (inv["totalFiles"], inv["totalLoc"], inv["excluded"], inv["binary"]))
    for lg in inv["large"][:5]:
        print("  large file counted: %s (%.1f MB, %d lines)" % (lg["path"], lg["bytes"] / 1e6, lg["loc"]))
    dirs = {}
    for row in inv["files"]:
        top = row[0].split("/", 1)[0] if "/" in row[0] else "(root)"
        d = dirs.setdefault(top, [0, 0])
        d[0] += 1
        d[1] += row[2]
    print("by folder: " + ", ".join("%s %d files/%d lines" % (k, v[0], v[1]) for k, v in sorted(dirs.items(), key=lambda x: -x[1][1])[:10]))
    print("history:   %d commits by %d authors since %s%s" % (len(hist["commits"]), len(hist["authors"]), hist["since"],
                                                              " (TRUNCATED)" if hist["truncated"] else ""))
    print("           %d fix, %d revert, %d AI-assisted, %d bot, %d with PR numbers" % (
        sum(1 for f in flags if f.get("fix")), sum(1 for f in flags if f.get("revert")),
        sum(1 for f in flags if f.get("ai")), sum(1 for f in flags if f.get("bot")), sum(1 for f in flags if f.get("pr"))))
    print("top languages: " + ", ".join("%s %.1f%%" % (l["name"], l["pct"]) for l in inv["languages"][:5]))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
