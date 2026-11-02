#!/usr/bin/env python3
"""Interactive C4 diagrams as one offline HTML file.

    c4.py MODEL.c4.json [-o OUT.html] [--open] [--check]       a hand-placed C4 model (explain/factory.c4.json)
    c4.py --from-map .cartographer/model/model.json [...]       the repository's codebase map, laid out by layer
    c4.py ... --fragment                                         the page without <html>/<head>, to publish as an artifact

The page has no network dependencies: data, styles and script are written into the file. Every element may cite the
files it lives in (`sources`); --check fails when a cited file is missing, an element is drawn twice or outside its
view, or a relationship or journey step names an element its view does not place.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "..", "viewer", "c4.html")
KINDS = {"person", "agent", "system", "container", "component", "external"}


def git(root, *args):
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True)
        return out.stdout.decode("utf-8", "replace").strip() if out.returncode == 0 else ""
    except OSError:
        return ""


def repo_root(path):
    start = os.path.dirname(os.path.abspath(path))
    return git(start, "rev-parse", "--show-toplevel") or os.getcwd()


def https_url(remote):
    """git@github.com:o/r.git or https://github.com/o/r.git -> https://github.com/o/r; anything else -> ''."""
    m = re.match(r"^(?:git@([^:]+):|https://([^/]+)/)([^/]+/[^/]+?)(?:\.git)?/?$", remote or "")
    return f"https://{m.group(1) or m.group(2)}/{m.group(3)}" if m else ""


# ---------- checks ----------

def check(model, root):
    """Problems that make the diagram wrong or unsupported, as sentences. Empty means it holds."""
    probs = []
    els = model.get("elements", {})
    views = {v["id"]: v for v in model.get("views", [])}
    for k, e in els.items():
        if e.get("kind") not in KINDS:
            probs.append(f"element {k}: kind {e.get('kind')!r} is not one of {', '.join(sorted(KINDS))}")
        if not e.get("name"):
            probs.append(f"element {k}: no name")
        if e.get("zoom") and e["zoom"] not in views:
            probs.append(f"element {k}: opens view {e['zoom']!r}, which does not exist")
        for src in e.get("sources", []):
            p = src if isinstance(src, str) else src.get("path", "")
            if not os.path.exists(os.path.join(root, p)):
                probs.append(f"element {k}: cites {p}, which does not exist")
    for v in model.get("views", []):
        vid = v.get("id")
        if v.get("kind") == "journey":
            base = views.get(v.get("base"))
            if not base or base.get("kind") != "static":
                probs.append(f"journey {vid}: base view {v.get('base')!r} is not a static view")
                continue
            for i, s in enumerate(v.get("steps", []), 1):
                if not s.get("text"):
                    probs.append(f"journey {vid} step {i}: no text")
                for key in ("from", "to", "at"):
                    if s.get(key) and s[key] not in base["positions"]:
                        probs.append(f"journey {vid} step {i}: {s[key]} is not drawn in {base['id']}")
                if not (s.get("at") or (s.get("from") and s.get("to"))):
                    probs.append(f"journey {vid} step {i}: needs from and to, or at")
            continue
        pos = v.get("positions", {})
        for k, p in pos.items():
            if k not in els:
                probs.append(f"view {vid}: places {k}, which is not an element")
            if len(p) != 4:
                probs.append(f"view {vid}: {k} needs [x, y, width, height]")
                continue
            x, y, w, h = p
            if x < 0 or y < 0 or x + w > v.get("width", 1e9) or y + h > v.get("height", 1e9):
                probs.append(f"view {vid}: {k} is outside the view")
        items = sorted(pos.items())
        for i, (a, pa) in enumerate(items):
            for b, pb in items[i + 1:]:
                if pa[0] < pb[0] + pb[2] and pb[0] < pa[0] + pa[2] and pa[1] < pb[1] + pb[3] and pb[1] < pa[1] + pa[3]:
                    probs.append(f"view {vid}: {a} and {b} overlap")
        bids = {q.get("id") for q in v.get("boundaries", []) if q.get("id")}
        for r in v.get("relationships", []):
            for key in ("from", "to"):
                if r.get(key) not in pos and r.get(key) not in bids:
                    probs.append(f"view {vid}: relationship {r.get('from')} -> {r.get('to')} names {r.get(key)}, not drawn here")
    return probs


# ---------- the codebase map as C4 ----------

def first_sentence(text, limit=90):
    s = re.split(r"(?<=[.!?])\s", (text or "").strip(), maxsplit=1)[0]
    if len(s) <= limit:
        return s.rstrip(".")
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def from_map(m, root):
    """Build a C4 model from .cartographer/model/model.json: a context view, the parts of the repository by layer,
    and each traced flow as a journey. Placement is computed; every element cites the paths the map gives it."""
    repo = m.get("repo", {})
    name = repo.get("name") or os.path.basename(root)
    layers = [l for l in m.get("layers", [])]
    comps = m.get("components", [])
    ext_ids = {l["id"] for l in layers if l["id"] in ("external",) or "extern" in l.get("name", "").lower()}
    externals = [c for c in comps if c.get("layer") in ext_ids]
    inner = [c for c in comps if c.get("layer") not in ext_ids]
    els = {}
    sid = "system"
    while sid in {c["id"] for c in comps}:
        sid += "_"
    entry = next((l for l in layers if l["id"] not in ext_ids), None)
    els["users"] = {
        "kind": "person", "name": "Users",
        "description": f"People who use {name}" + (f" through its {entry['name'].lower()} layer." if entry else "."),
        "short": f"Use {name}",
    }
    els[sid] = {"kind": "system", "name": name, "description": repo.get("summary", ""), "short": first_sentence(repo.get("summary", "")), "zoom": "parts"}
    for c in comps:
        e = {
            "kind": "external" if c in externals else "component",
            "name": c.get("name", c["id"]),
            "tech": c.get("kind") or "",
            "description": c.get("summary", ""),
            "short": first_sentence(c.get("summary", "")),
            "sources": [p for p in c.get("paths", []) if os.path.exists(os.path.join(root, p))],
        }
        resp = c.get("responsibilities") or []
        if resp:
            e["does"] = " ".join(r if r.endswith(".") else r + "." for r in resp[:4] if isinstance(r, str))
        els[c["id"]] = e

    # Context: users -> system -> each outside system the map connects to.
    W, BW, BH = 1100, 230, 120
    ctx_pos = {"users": [60, 60, BW, BH], sid: [W // 2 - 140, 60, 280, 150]}
    ctx_rel = [{"from": "users", "to": sid, "label": "uses"}]
    for i, c in enumerate(externals):
        ctx_pos[c["id"]] = [W - BW - 60, 60 + i * (BH + 40), BW, BH]
        labels = [e.get("label") or e.get("kind") or "" for e in m.get("edges", []) if e.get("to") == c["id"]]
        ctx_rel.append({"from": sid, "to": c["id"], "label": first_sentence(labels[0], 40) if labels else "uses"})
    ctx_h = max(260, 60 + max(1, len(externals)) * (BH + 40) + 20)

    # Parts: one band per layer, top to bottom, outside systems in a column on the right.
    bw, bh, gx, gy, per_row = 210, 106, 64, 30, 4
    bands, y, maxw = [], 30, 0
    for l in layers:
        if l["id"] in ext_ids:
            continue
        cs = [c for c in inner if c.get("layer") == l["id"]]
        if not cs:
            continue
        rows = [cs[i:i + per_row] for i in range(0, len(cs), per_row)]
        width = max(len(r) for r in rows) * (bw + gx) - gx + 40
        height = 36 + len(rows) * (bh + gy) - gy + 20
        bands.append((l, rows, y, height, width))
        maxw = max(maxw, width)
        y += height + 40
    unplaced = [c for c in inner if c.get("layer") not in {l["id"] for l in layers}]
    if unplaced:
        rows = [unplaced[i:i + per_row] for i in range(0, len(unplaced), per_row)]
        width = max(len(r) for r in rows) * (bw + gx) - gx + 40
        height = 36 + len(rows) * (bh + gy) - gy + 20
        bands.append(({"id": "other", "name": "Other"}, rows, y, height, width))
        maxw = max(maxw, width)
        y += height + 40
    x0 = 30
    pos, bnds = {}, []
    for l, rows, by, height, width in bands:
        bnds.append({"name": l["name"], "x": x0, "y": by, "w": maxw, "h": height})
        for ri, row in enumerate(rows):
            rw = len(row) * (bw + gx) - gx
            sx = x0 + (maxw - rw) / 2
            for ci, c in enumerate(row):
                pos[c["id"]] = [round(sx + ci * (bw + gx)), by + 36 + ri * (bh + gy), bw, bh]
    total_h = max(y + 8, 200)
    ex = x0 + maxw + 90
    for i, c in enumerate(externals):
        ey = round(total_h / 2 - (len(externals) * (bh + 30) - 30) / 2 + i * (bh + 30))
        pos[c["id"]] = [ex, max(30, ey), bw, bh]
    parts_w = (ex + bw + 30) if externals else (x0 + maxw + 30)
    seen, rels = set(), []
    for e in m.get("edges", []):
        k = (e.get("from"), e.get("to"))
        if k in seen or k[0] not in pos or k[1] not in pos or k[0] == k[1]:
            continue
        seen.add(k)
        rels.append({"from": k[0], "to": k[1], "label": first_sentence(e.get("label") or e.get("kind") or "", 36)})

    views = [
        {"id": "context", "kind": "static", "level": "System context", "title": f"{name} and what it works with",
         "description": first_sentence(repo.get("summary", ""), 400) + ".", "width": W, "height": ctx_h,
         "positions": ctx_pos, "relationships": ctx_rel},
        {"id": "parts", "kind": "static", "level": "Components", "title": f"The parts of {name}",
         "description": "Each part, grouped by layer from the codebase map, with the dependencies the map proves. Code depends downward.",
         "width": parts_w, "height": total_h, "boundaries": bnds, "positions": pos, "relationships": rels,
         "labels": "hover"},
    ]
    for f in m.get("flows", []):
        steps, prev = [], None
        for s in f.get("steps", []):
            c = s.get("component")
            if c not in pos:
                continue
            text = s.get("action") or ""
            steps.append({"from": prev, "to": c, "text": text} if prev and prev != c else {"at": c, "text": text})
            prev = c
        if steps:
            views.append({"id": "flow-" + re.sub(r"[^a-z0-9-]", "-", f["id"].lower()), "kind": "journey", "base": "parts",
                          "level": "Journey", "title": f.get("name", f["id"]), "description": f.get("summary") or f.get("trigger") or "", "steps": steps})
    gen = m.get("generated", {})
    return {
        "title": name, "summary": repo.get("summary", ""), "elements": els, "views": views, "ask": "/explain-repo",
        "meta": {"mapCommit": gen.get("commit", ""), "mapDate": (gen.get("at") or "")[:10], "repoUrl": repo.get("url", "")},
    }


# ---------- page ----------

def embed(model):
    return json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def page(model, fragment=False):
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    title = model.get("title", "Architecture")
    html = tpl.replace("__C4_TITLE__", title.replace("&", "&amp;").replace("<", "&lt;")).replace("__C4_DATA__", embed(model))
    if fragment:
        return html
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1">\n</head>\n<body>\n' + html + "\n</body>\n</html>\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    ap.add_argument("model", nargs="?", help="a C4 model JSON file")
    ap.add_argument("--from-map", metavar="MODEL_JSON", help="build the C4 model from a Cartographer codebase map")
    ap.add_argument("-o", "--out", help="output HTML (default .cache/explain/<name>.html under the repository)")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--check", action="store_true", help="only check the model; exit 1 on any problem")
    ap.add_argument("--fragment", action="store_true", help="write the page without <html>/<head>, for publishing")
    ap.add_argument("--repo-url", help="link cited files to this repository (default: the origin remote)")
    ap.add_argument("--ref", help="branch or commit for file links (default: the current commit)")
    ap.add_argument("--view", help="open at this view (adds #view to the address)")
    a = ap.parse_args(argv)
    if bool(a.model) == bool(a.from_map):
        ap.error("give a model file or --from-map, not both")
    src = a.model or a.from_map
    root = repo_root(src)
    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    model = from_map(data, root) if a.from_map else data
    probs = check(model, root)
    if probs:
        print("\n".join("problem: " + p for p in probs), file=sys.stderr)
        if a.check:
            return 1
    elif a.check:
        n = sum(1 for e in model["elements"].values() if e.get("sources"))
        print(f"ok: {len(model['elements'])} elements, {len(model['views'])} views; {n} cite files, all of which exist")
        return 0
    meta = model.setdefault("meta", {})
    rel = os.path.relpath(os.path.abspath(src), root).replace(os.sep, "/")
    meta["source"] = rel
    cited = sum(len(e.get("sources", [])) for e in model["elements"].values())
    if cited and not probs:
        meta["cited"] = f"{cited} cited files, all present"
    commit = git(root, "rev-parse", "--short", "HEAD")
    if a.from_map:
        meta["generated"] = "Map of " + (meta.get("mapCommit", "")[:7] or "unknown commit") + (", " + meta["mapDate"] if meta.get("mapDate") else "")
    else:
        meta["generated"] = "Built " + datetime.date.today().isoformat() + (f" from {commit}" if commit else "")
    url = a.repo_url if a.repo_url is not None else (meta.get("repoUrl") or https_url(git(root, "config", "--get", "remote.origin.url")))
    if url:
        meta["repoUrl"] = url
        meta["ref"] = a.ref or git(root, "rev-parse", "HEAD") or "main"
    out = a.out or os.path.join(root, ".cache", "explain", os.path.splitext(os.path.basename(src))[0].replace(".c4", "") + ".html")
    if a.from_map and not a.out:
        out = os.path.join(root, ".cache", "explain", "repo.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if not a.out and not os.path.exists(os.path.join(os.path.dirname(out), ".gitignore")):
        with open(os.path.join(os.path.dirname(out), ".gitignore"), "w") as f:
            f.write("*\n")  # a local page; never committed
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(page(model, fragment=a.fragment))
    print(out)
    if a.open:
        target = "file://" + os.path.abspath(out).replace(os.sep, "/") if os.sep == "/" else "file:///" + os.path.abspath(out).replace(os.sep, "/")
        if a.view:
            target += "#" + a.view
        if not webbrowser.open(target):
            print("could not open a browser here; open the file above", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
