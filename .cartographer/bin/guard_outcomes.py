#!/usr/bin/env python3
"""Outcomes report: what the guardrails take off a reviewer's desk.

  python3 .cartographer/bin/guard.py --outcomes outcomes.html

Three sections, each answering a reviewer's question:
  1. Blocked: what a reviewer no longer has to look for. From the history simulation (log/simulated.jsonl),
     live events (log/events.jsonl) and probes run now against the agent hooks.
  2. Got through: problems that reached the default branch and were caught by a person, each with the check we
     would build. Rule checks are replayed over every commit in the window: does the check fire at the commit
     that introduced the problem, is it quiet after the fix, is the tree clean today.
  3. Never blockable: what review is for.

Inputs: .cartographer/outcomes.json (the analysis, written by cartograph-learn and reviewed by a person),
guardrails.json, guardrails.proposed.json (rules carry `addresses: <item id>`), the logs and git history.
No scripts in the output.
"""
from __future__ import annotations

import datetime as dt
import html
import os

import carto
import guard
import guard_export

E = html.escape

CSS = guard_export.CSS + """
.desk{display:flex;flex-direction:column;gap:10px}
.desk svg{display:block;width:100%;height:auto;max-width:860px}
.sec{display:flex;flex-direction:column;gap:18px}
.sec>.lead{display:flex;flex-direction:column;gap:8px;max-width:860px}
.tag{font-size:11px;font-weight:600;border-radius:4px;padding:1px 7px;white-space:nowrap;background:var(--gray-fill);color:var(--ink2)}
.tag.block{background:var(--ink);color:var(--bg)}
.tag.ok{background:var(--blue);color:var(--bg)}
.tag.line{background:transparent;border:1px solid var(--line);color:var(--muted)}
.item{border-top:1px solid var(--line);padding:18px 0;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px 28px}
.item>.l,.item>.r{display:flex;flex-direction:column;gap:8px;min-width:0}
.item h3{font-size:16px;font-family:var(--serif);font-weight:600}
.item .when{font-size:12.5px;color:var(--muted)}
.item .k{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.verdict{display:flex;flex-direction:column;gap:4px;font-size:13.5px;margin:0;padding:0;list-style:none}
.verdict li{display:block;position:relative;padding-left:22px}
.verdict li::before{position:absolute;left:2px;top:0;font-weight:700}
.verdict .y::before{content:"\\2713";color:var(--blue)}
.verdict .n::before{content:"\\2715";color:var(--ink)}
.verdict .q::before{content:"\\2013";color:var(--muted)}
.strip{display:flex;flex-direction:column;gap:4px}
.strip svg{display:block;width:100%;height:auto}
.strip .cap{font-size:11.5px;color:var(--muted)}
.found{font-size:12.5px;color:var(--muted);background:var(--subtle);border-radius:4px;padding:6px 10px;font-family:var(--mono);word-break:break-word}
.ex{border-top:1px solid var(--line);padding:16px 0;display:flex;flex-direction:column;gap:12px}
.ex-hd{display:flex;flex-direction:column;gap:2px}
.ex-hd h3{font-family:var(--serif);font-size:16px}
.ex .when{font-size:12.5px;color:var(--muted)}
.ex-cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:14px 28px}
.ex-cols>div{display:flex;flex-direction:column;gap:6px;min-width:0}
.ex .k{font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.ex .k2{font-size:13px;font-weight:600;color:var(--ink)}
.plain{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:6px;font-size:13.5px}
.plain li{border-left:3px solid var(--ink);padding-left:10px}
@media (max-width:760px){.ex-cols{grid-template-columns:1fr}}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px 28px}
@media (max-width:760px){.item{grid-template-columns:1fr}}
"""

STATE_LABEL = {"active": "in force", "covered": "in force", "proposed": "proposed", "declined": "not adopted", "built": "built"}
KIND_LABEL = {"rule": "Guardrail", "build-check": "Build check", "test": "Test", "ci-command": "CI step", "probe": "Existing guardrail"}


def _n(k, word):
    return "%d %s%s" % (k, word, "" if k == 1 else "s")


def _short(sha):
    return (sha or "")[:7]


class ProbeTree:
    """A tree with a few files replaced, for proving a rule on a change that has not happened."""

    def __init__(self, base, files):
        self.base, self.files = base, files

    def paths(self):
        return sorted(set(self.base.paths()) | set(self.files))

    def exists(self, path):
        return path in self.files or self.base.exists(path)

    def read(self, path):
        return list(self.files[path]) if path in self.files else self.base.read(path)


def compute(ctx):
    home = ctx.home
    oc = carto.load(os.path.join(home, "outcomes.json")) or {}
    proposed = {r["id"]: r for r in (carto.load(os.path.join(home, "guardrails.proposed.json")) or {}).get("rules", [])}
    active = {r["id"]: r for r in ctx.config.get("rules", [])}
    by_addr = {r.get("addresses"): r for r in proposed.values() if r.get("addresses")}
    ctx.agent = False

    history = guard.history_changes(ctx.repo, 365, 500)
    commits = [c for c in reversed(history) if c.files and not c.flags.get("bot")]      # oldest first
    head_sha = (guard.git(ctx.repo, "rev-parse", "HEAD", check=False) or "").strip()
    head_tree = guard.Tree(ctx.repo, head_sha) if head_sha else None

    def idx(sha):
        for i, c in enumerate(commits):
            if sha and c.sha.startswith(sha):
                return i
        return None

    def today(rule):
        if head_tree is None or rule["type"] not in guard.TREE_TYPES | {"same-value"}:
            return None
        r = dict(rule, always=True)
        r.pop("when", None)
        cs = guard.ChangeSet([], "HEAD", sha=head_sha, reader=head_tree.read, tree=head_tree)
        return guard.evaluate(cs, ctx, [r], use_exceptions=False)[0]

    def probe_rule(rule, p):
        overlay = ProbeTree(head_tree, {p["path"]: p["lines"]})
        fc = guard.FileChange(p["path"])
        fc.status = "M" if head_tree.exists(p["path"]) else "A"
        fc.added = list(enumerate(p["lines"], start=1))
        fc.add = len(fc.added)
        cs = guard.ChangeSet([fc], "probe", reader=overlay.read, tree=overlay)
        vs = guard.evaluate(cs, ctx, [rule], use_exceptions=False)[0]
        return {"label": p["label"], "expect": p.get("expect"), "decision": "block" if vs else "pass",
                "message": vs[0]["message"] if vs else None}

    probes = []
    for p in oc.get("probes", []):
        if p["kind"] == "command":
            hits = guard.check_command(ctx, p["command"])
            probes.append({"label": p["label"], "action": p["command"], "decision": "block" if hits else "allow",
                           "rules": sorted({h["rule"] for h in hits}), "message": hits[0]["message"] if hits else None})
        else:
            exists = head_tree.exists(p["path"]) if head_tree else os.path.exists(os.path.join(ctx.repo, p["path"]))
            hits = guard.check_edit(ctx, p["path"], exists, p.get("lines") or [])
            probes.append({"label": p["label"], "action": "Edit %s" % p["path"], "decision": "block" if hits else "allow",
                           "rules": sorted({h["rule"] for h in hits}), "message": hits[0]["message"] if hits else None})
    probe_by_label = {p["label"]: p for p in probes}

    def assess(item):
        chk = dict(item.get("check") or {})
        rid = chk.get("rule")
        rule = proposed.get(rid) or active.get(rid) or by_addr.get(item["id"]) or chk.get("ruleDef")
        out = dict(item, check=chk, rule=rule, replay=None, probes=[])
        if chk.get("kind") == "probe" and chk.get("probe") in probe_by_label:
            out["probes"] = [probe_by_label[chk["probe"]]]
        if rule is None:
            return out
        out["ruleState"] = "active" if rule["id"] in active else ("declined" if item.get("status") == "declined" else "proposed")
        for p in chk.get("probes") or []:
            out["probes"].append(probe_rule(rule, p))
        if rule["type"] in guard.REPLAYABLE and "diff" in guard.enforced_at(rule) and item.get("introduced"):
            fired, first, keys = [], None, {}
            for i, c in enumerate(commits):
                vs = guard.evaluate(c, ctx, [rule], use_exceptions=False)[0]
                if vs:
                    fired.append(i)
                    keys[i] = {(v.get("path"), v["message"]) for v in vs}
            intro = [i for i in (idx(s) for s in item.get("introduced") or []) if i is not None]
            fix = idx(item.get("fixed"))
            start = min(intro) if intro else 0
            at_intro = any(i in fired for i in intro)
            if at_intro:
                c = commits[intro[0]]
                vs = guard.evaluate(c, ctx, [rule], use_exceptions=False)[0]
                first = vs[0]["message"] if vs else None
            after = [i for i in fired if fix is not None and i >= fix]
            between = [i for i in fired if start <= i and (fix is None or i < fix)]
            # after the fix: findings the fix left in place, and new findings that appeared later
            before = keys.get(fix - 1, set()) if fix else set()
            left = set().union(*(keys[i] for i in after)) & before if after else set()
            new_from = next((i for i in after if keys[i] - before), None)
            out["replay"] = {"fired": fired, "intro": intro, "fix": fix, "start": start, "atIntro": at_intro, "first": first,
                             "left": len(left), "newFrom": new_from, "newSha": commits[new_from].sha[:7] if new_from is not None else None,
                             "newSubject": commits[new_from].subject if new_from is not None else None,
                             "afterFix": after, "between": between, "today": today(rule),
                             "tree": rule["type"] in guard.TREE_TYPES or rule["type"] == "same-value"}
        elif chk.get("probes"):
            out["replay"] = {"today": today(rule)}
        return out

    missed = [assess(m) for m in oc.get("missed", [])]
    gaps = [assess(g) for g in oc.get("gaps", [])]

    sim = carto.read_events(home, simulated=True)
    live = carto.read_events(home)
    return {"oc": oc, "commits": commits, "missed": missed, "gaps": gaps, "probes": probes, "sim": sim, "live": live,
            "active": active, "proposed": proposed, "head": head_sha[:12]}


# ----------------------------------------------------------------------------- drawing

def strip_svg(n, rep, w=420):
    """One cell per commit, oldest left. Filled: the check fires. Blue: the problem's own commits (introduced to
    fixed). Grey hatch: before the check could exist."""
    if not n:
        return ""
    gap = 1.5
    cw = max(2.0, (w - gap * (n - 1)) / n)
    w = cw * n + gap * (n - 1)
    h = 26
    fired = set(rep.get("fired") or [])
    intro = set(rep.get("intro") or [])
    fix = rep.get("fix")
    start = rep.get("start") or 0
    parts = ['<svg viewBox="0 0 %.1f %d" role="img" aria-label="%s">' % (w, h + 14, E(
        "%d commits; the check fires on %d; introduced at commit %s, fixed at commit %s" % (
            n, len(fired), ", ".join(str(i + 1) for i in sorted(intro)) or "unknown", (fix + 1) if fix is not None else "unknown")))]
    for i in range(n):
        x = i * (cw + gap)
        if i < start and rep.get("tree"):
            parts.append('<rect x="%.1f" y="4" width="%.1f" height="%d" fill="var(--subtle)"/>' % (x, cw, h - 8))
            continue
        in_window = fix is not None and start <= i < fix
        if i in fired:
            fill = "var(--blue)" if in_window or i in intro else "var(--ink)"
            parts.append('<rect x="%.1f" y="4" width="%.1f" height="%d" fill="%s"/>' % (x, cw, h - 8, fill))
        else:
            parts.append('<rect x="%.1f" y="4.5" width="%.1f" height="%d" fill="none" stroke="var(--line)" stroke-width="1"/>' % (x, max(cw - 0.5, 1), h - 9))
    for i in sorted(intro):
        x = i * (cw + gap) + cw / 2
        parts.append('<path d="M%.1f %d l-4 -6 h8 z" fill="var(--ink)"/>' % (x, h + 8))
    if fix is not None:
        x = fix * (cw + gap) + cw / 2
        parts.append('<line x1="%.1f" y1="0" x2="%.1f" y2="%d" stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="2 2"/>' % (x, x, h))
    parts.append("</svg>")
    return "".join(parts)


def desk_svg(scripted, proposed, person):
    total = max(scripted + proposed + person, 1)
    W, H = 860, 92
    x1 = W * scripted / total
    x2 = x1 + W * proposed / total
    lab = lambda x, w, big, small, fg: (
        '<text x="%.1f" y="34" font-size="22" font-weight="600" fill="%s" font-family="var(--sans)">%s</text>'
        '<text x="%.1f" y="54" font-size="12" fill="%s" font-family="var(--sans)">%s</text>' % (x + 12, fg, big, x + 12, fg, small)) if w > 120 else ""
    return ('<svg viewBox="0 0 %d %d" role="img" aria-label="%s">' % (W, H, E(
        "%d review concerns: %d checked by script today, %d with a proposed check, %d need a person" % (total, scripted, proposed, person))) +
        '<rect x="0" y="6" width="%.1f" height="60" fill="var(--blue)"/>' % x1 +
        '<rect x="%.1f" y="6" width="%.1f" height="60" fill="var(--blue-tint)" stroke="var(--blue)" stroke-dasharray="4 3"/>' % (x1, x2 - x1) +
        '<rect x="%.1f" y="6" width="%.1f" height="60" fill="var(--ink)"/>' % (x2, W - x2) +
        lab(0, x1, scripted, "checked by script today", "var(--bg)") +
        lab(x1, x2 - x1, proposed, "check proposed below", "var(--ink)") +
        lab(x2, W - x2, person, "only a person can judge", "var(--bg)") +
        '<text x="0" y="86" font-size="12" fill="var(--muted)" font-family="var(--sans)">Each unit is one thing a reviewer would otherwise check by eye on every pull request.</text>'
        "</svg>")


def applies(rule, cs, ctx):
    """Whether a reviewer would have to ask this rule's question about this change (independent of the answer)."""
    t = rule.get("type")
    paths = [f.path for f in cs.files]
    if not paths:
        return False
    sel = lambda spec: guard.Selector(spec, ctx)
    code = [p for p in paths if carto.category(p) == "code" and not carto.is_test(p)]
    if rule.get("when") and t != "co-change":
        if not any(sel(rule["when"])(p) for p in paths):
            return False
    if t in ("layering", "no-new-cycles", "forbid-import", "ratchet", "max-size"):
        return bool(code)
    if t == "pinned-snippets":
        pinned = {sn.get("path") for sn in ctx.model.get("snippets", [])}
        return any(p in pinned for p in paths)
    if t == "same-value":
        return any(sv.get("path") in paths for sv in rule.get("sources", []))
    if t == "same-set":
        left = rule["left"] if isinstance(rule["left"], list) else [rule["left"]]
        return any(sel(sp.get("paths") or sp.get("path"))(p) for sp in left for p in paths)
    if t == "co-change":
        return any(sel(rule.get("when"))(p) for p in paths)
    if t == "forbid-command":
        return False
    if t == "tests-required":
        return any(sel(rule.get("paths"))(p) for p in code) if rule.get("paths") else bool(code)
    spec = rule.get("paths")
    return any(sel(spec)(p) for p in paths) if spec else True


def review_load(ctx, changes, rules, cannot, skip_spec, checks=None):
    """Per change: the questions a reviewer would ask with no guardrails (rules that apply plus judgment items that
    apply), the ones a check answers, the ones left for a person, and lines a reviewer can skip outright."""
    skip = guard.Selector(skip_spec, ctx) if skip_spec else None
    person_sel = [(x, guard.Selector(x["appliesTo"], ctx)) for x in cannot if x.get("appliesTo")]
    # Checks that are not rules (the build leak check, site verification, tests) answer review questions too.
    check_sel = [({"id": c["id"], "type": "check", "description": c.get("label", c["id"]), "severity": "block"}, guard.Selector(c["appliesTo"], ctx))
                 for c in (checks or []) if c.get("appliesTo")]
    out = []
    for c in changes:
        auto = [r for r in rules if guard_export._sev(r) != "info" and applies(r, c, ctx)]
        auto += [pc for pc, sel in check_sel if any(sel(f.path) for f in c.files)]
        notes = [r for r in rules if guard_export._sev(r) == "info" and applies(r, c, ctx)]
        person = [x for x, sel in person_sel if any(sel(f.path) for f in c.files)]
        lines = sum(f.add + f.dele for f in c.files)
        skipped = sum(f.add + f.dele for f in c.files if skip and skip(f.path))
        out.append({"change": c, "auto": auto, "notes": notes, "person": person, "lines": lines, "skip": skipped})
    return out


def load_svg(rows, w=860):
    """One column per commit, oldest left: the full column is what a reviewer checked by eye before; the black part
    is what is left with the guardrails."""
    n = len(rows)
    if not n:
        return ""
    mx = max([len(r["auto"]) + len(r["person"]) for r in rows] + [1])
    gap = 2.0
    cw = max(3.0, (w - gap * (n - 1)) / n)
    w = cw * n + gap * (n - 1)
    H = 150
    parts = ['<svg viewBox="0 0 %.1f %d" role="img" aria-label="%s">' % (w, H + 22, E(
        "Review questions per commit: blue answered by checks, black left for a person, across %d commits" % n))]
    for k in (5, 10, 15, 20):
        if k <= mx:
            y = H - H * k / mx
            parts.append('<line x1="0" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--line)" stroke-width="1"/>' % (y, w, y))
            parts.append('<text x="%.1f" y="%.1f" font-size="10" fill="var(--muted)" text-anchor="end" font-family="var(--sans)">%d</text>' % (w, y - 3, k))
    for i, r in enumerate(rows):
        x = i * (cw + gap)
        a, p = len(r["auto"]), len(r["person"])
        hp = H * p / mx
        ha = H * a / mx
        if hp:
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="var(--ink)"><title>%s: %d left for a person</title></rect>' % (
                x, H - hp, cw, hp, E(r["change"].subject[:60]), p))
        if ha:
            parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="var(--blue)"><title>%s: %d answered by checks</title></rect>' % (
                x, H - hp - ha, cw, ha, E(r["change"].subject[:60]), a))
    parts.append('<line x1="0" y1="%d" x2="%.1f" y2="%d" stroke="var(--ink)" stroke-width="1"/>' % (H, w, H))
    parts.append('<text x="0" y="%d" font-size="11" fill="var(--muted)" font-family="var(--sans)">oldest commit</text>' % (H + 16))
    parts.append('<text x="%.1f" y="%d" font-size="11" fill="var(--muted)" text-anchor="end" font-family="var(--sans)">newest</text>' % (w, H + 16))
    parts.append("</svg>")
    return "".join(parts)


def workflow_svg(rows):
    """Where the guardrails sit, from idea to merged PR. rows: (stage, sub, guard, guard_sub, kind) with kind
    in machine | person | info."""
    W, bw, gx, gw, rh, gap, top = 860, 250, 300, 560, 54, 14, 26
    H = top + len(rows) * (rh + gap) + 52 + 30
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Where the guardrails sit in the workflow, from idea to merged pull request">' % (W, H),
           '<defs><marker id="wfa" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M2 1L8 5L2 9" fill="none" stroke="var(--muted)" stroke-width="1.5"/></marker></defs>',
           '<text x="%d" y="14" font-size="11" font-weight="600" letter-spacing=".06em" fill="var(--muted)" font-family="var(--sans)">WHAT YOU DO</text>' % 0,
           '<text x="%d" y="14" font-size="11" font-weight="600" letter-spacing=".06em" fill="var(--muted)" font-family="var(--sans)">WHAT GUARDS THAT STEP</text>' % gx]
    for i, (stage, sub, g, gsub, kind) in enumerate(rows):
        y = top + i * (rh + gap)
        out.append('<rect x="0.5" y="%d" width="%d" height="%d" rx="6" fill="var(--subtle)" stroke="var(--line)"/>' % (y, bw, rh))
        out.append('<text x="14" y="%d" font-size="14" font-weight="600" fill="var(--ink)" font-family="var(--sans)">%s</text>' % (y + 22, E(stage)))
        out.append('<text x="14" y="%d" font-size="12" fill="var(--muted)" font-family="var(--sans)">%s</text>' % (y + 40, E(sub)))
        if i < len(rows) - 1:
            out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="var(--muted)" stroke-width="1" marker-end="url(#wfa)"/>' % (bw // 2, y + rh + 1, bw // 2, y + rh + gap - 2))
        out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="var(--line)" stroke-width="1" stroke-dasharray="3 3"/>' % (bw + 1, y + rh // 2, gx - 1, y + rh // 2))
        if kind == "machine":
            box, fg, fg2 = 'fill="var(--blue)" stroke="var(--blue)"', "var(--bg)", "var(--bg)"
        elif kind == "verify":
            box, fg, fg2 = 'fill="var(--blue-tint)" stroke="var(--blue)" stroke-width="1.5"', "var(--ink)", "var(--ink2)"
        elif kind == "person":
            box, fg, fg2 = 'fill="var(--bg)" stroke="var(--ink)" stroke-dasharray="5 3"', "var(--ink)", "var(--ink2)"
        else:
            box, fg, fg2 = 'fill="var(--gray-fill)" stroke="var(--line)"', "var(--ink)", "var(--ink2)"
        out.append('<rect x="%d.5" y="%d" width="%d" height="%d" rx="6" %s/>' % (gx, y, gw - 1, rh, box))
        sub_lines = gsub.split("\n")
        ty = y + (18 if len(sub_lines) > 1 else 22)
        out.append('<text x="%d" y="%d" font-size="14" font-weight="600" fill="%s" font-family="var(--sans)">%s</text>' % (gx + 14, ty, fg, E(g)))
        for j, line in enumerate(sub_lines[:2]):
            out.append('<text x="%d" y="%d" font-size="12" fill="%s" font-family="var(--sans)" opacity=".92">%s</text>' % (gx + 14, (y + 35 + j * 14) if len(sub_lines) > 1 else y + 40, fg2, E(line)))
    y = top + len(rows) * (rh + gap)
    out.append('<rect x="0.5" y="%d" width="%d" height="40" rx="6" fill="var(--gray-fill)" stroke="var(--line)"/>' % (y, W - 1))
    out.append('<text x="%d" y="%d" font-size="13" fill="var(--ink)" text-anchor="middle" font-family="var(--sans)">&#8635; Nightly: replay history and propose rules. Nothing is enforced until a person accepts it.</text>' % (W // 2, y + 25))
    ly = y + 64
    out.append('<rect x="0" y="%d" width="14" height="14" rx="3" fill="var(--blue)"/><text x="22" y="%d" font-size="12" fill="var(--muted)" font-family="var(--sans)">Blocks automatically</text>' % (ly - 11, ly))
    out.append('<rect x="190.5" y="%d" width="13" height="13" rx="3" fill="none" stroke="var(--ink)" stroke-dasharray="3 2"/><text x="212" y="%d" font-size="12" fill="var(--muted)" font-family="var(--sans)">Needs a person</text>' % (ly - 11, ly))
    out.append('<rect x="340" y="%d" width="14" height="14" rx="3" fill="var(--gray-fill)" stroke="var(--line)"/><text x="362" y="%d" font-size="12" fill="var(--muted)" font-family="var(--sans)">Information, never blocks</text>' % (ly - 11, ly))
    if any(r[4] == "verify" for r in rows):
        out.append('<rect x="540" y="%d" width="14" height="14" rx="3" fill="var(--blue-tint)" stroke="var(--blue)" stroke-width="1.5"/><text x="562" y="%d" font-size="12" fill="var(--muted)" font-family="var(--sans)">The agent proves its own work</text>' % (ly - 11, ly))
    out.append("</svg>")
    return "".join(out)


def _ids(rs, k=4):
    names = [r["id"] for r in rs]
    return ", ".join(names[:k]) + (" and %d more" % (len(names) - k) if len(names) > k else "")


# ----------------------------------------------------------------------------- report

def outcomes_report(ctx, out_path):
    d = compute(ctx)
    oc, commits, missed, gaps = d["oc"], d["commits"], d["missed"], d["gaps"]
    n = len(commits)
    repo = (ctx.model.get("repo") or {}).get("name") or os.path.basename(ctx.repo.rstrip("/"))
    rq = oc.get("reviewerQuestions") or {}
    rules = ctx.rules
    cannot = oc.get("cannotBlock") or []
    judgment = ctx.config.get("judgment", [])

    # history simulation: per rule, commits sent back (block) and agent edits refused
    sim = d["sim"]
    runs = [e for e in sim if e.get("kind") == "run"]
    edits = [e for e in sim if e.get("source") == "edit-hook"]
    blocked_commits = {e["sha"] for e in runs if e.get("decision") == "block"}
    blocked_commits |= {e["sha"] for e in edits}
    per_rule = {}
    for e in runs:
        for rid in e.get("rules") or []:
            per_rule.setdefault(rid, {"block": [], "edit": [], "warn": []})["block"].append(e)
        for rid in (e.get("warnings") or []) + (e.get("notes") or []):
            per_rule.setdefault(rid, {"block": [], "edit": [], "warn": []})["warn"].append(e)
    for e in edits:
        for rid in e.get("rules") or []:
            per_rule.setdefault(rid, {"block": [], "edit": [], "warn": []})["edit"].append(e)
    later_fixed = {e["sha"] for e in runs if e.get("decision") == "block" and e.get("outcome")}
    sim_dates = sorted(e.get("at", "")[:10] for e in runs)
    live = d["live"]
    live_blocks = [e for e in live if e.get("decision") == "block"]
    probes = d["probes"]
    probe_blocks = [p for p in probes if p["decision"] == "block"]

    replayed = [m for m in missed if (m.get("replay") or {}).get("intro")]
    proven = [m for m in replayed if m["replay"]["atIntro"]]
    def in_force(m):
        return m.get("ruleState") == "active" or m.get("status") == "built" or (
            m.get("status") == "covered" and any(p["decision"] == "block" for p in m.get("probes", [])))
    covered = [m for m in missed if in_force(m)]
    declined = [m for m in missed if m.get("status") == "declined"]
    open_items = [m for m in missed if m not in covered and m not in declined]
    proposed_checks = [x for x in missed + gaps if x.get("status") == "proposed"]
    built_checks = [x for x in missed + gaps if x.get("status") == "built"]
    today_hits = [(m, m["replay"]["today"]) for m in missed + gaps if (m.get("replay") or {}).get("today") and m.get("ruleState") != "declined"]
    scripted = [r for r in rules if guard_export._sev(r) != "info"]

    def cell_commit(e):
        return "<code>%s</code> %s" % (E(_short(e.get("sha"))), E((e.get("subject") or "")[:70]))

    # review load: questions a reviewer asks per change, with and without the guardrails
    tm_checks = (oc.get("timeModel") or {}).get("checks")
    load = review_load(ctx, commits, rules, cannot, oc.get("skipPaths"), tm_checks)
    active_load = [r for r in load if r["auto"] or r["person"]]
    q_before = sum(len(r["auto"]) + len(r["person"]) for r in active_load)
    q_after = sum(len(r["person"]) for r in active_load)
    q_pct = round(100 * (1 - q_after / q_before)) if q_before else 0
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0
    med_before = med([len(r["auto"]) + len(r["person"]) for r in active_load])
    med_after = med([len(r["person"]) for r in active_load])
    only_green = [r for r in active_load if not r["person"]]
    lines_all = sum(r["lines"] for r in load)
    lines_skip = sum(r["skip"] for r in load)

    # ---------------------------------------------------------------- summary
    kpis = [
        ("%d%%" % q_pct, "fewer review questions for a person: %d of the %d questions these %d commits raised are answered by a check" % (
            q_before - q_after, q_before, len(active_load))),
        (len(blocked_commits), "of %d past commits stopped before review: %d by the commit checks, %d at the agent's keyboard" % (
            n, len({e["sha"] for e in runs if e.get("decision") == "block"}), len({e["sha"] for e in edits}))),
        (len(missed), "problems reached main and were caught by a person"),
        ("%d of %d" % (len(proven), len(replayed)), "of those with a scripted check fire at the commit that caused the problem"),
        (len(cannot), "things only a person can judge: what review is for"),
    ]
    S = []
    S.append('<header class="title"><span class="num">Guardrail outcomes</span><h1>What the guardrails take off a reviewer\'s desk</h1>'
             '<p>%s</p>'
             '<div class="meta"><span>Repository <b>%s</b></span><span>%d commits, %s to %s</span><span>Head <code>%s</code></span><span>Generated %s</span></div></header>' % (
                 "Every check that fails sends a change back before anyone reviews it. This report shows what the checks already stop, the problems that "
                 "got past people and the check that would catch each one next time, and the short list that is left for review.", E(repo), n, E(sim_dates[0] if sim_dates else "?"),
                 E(sim_dates[-1] if sim_dates else "?"), E(d["head"]), dt.date.today().isoformat()))
    S.append('<section class="col"><div class="kpis">%s</div></section>' % "".join(
        '<div class="kpi"><span class="v">%s</span><span class="k">%s</span></div>' % (E(str(v)), E(k)) for v, k in kpis))

    # ---------------------------------------------------------------- review load
    def example_html(ex):
        cs = None
        if ex.get("sha"):
            cs = next((c for c in commits if c.sha.startswith(ex["sha"])), None)
        elif ex.get("base") and ex.get("head"):
            try:
                cs = guard.range_changes(ctx.repo, ex["base"], ex["head"])
                cs.subject = ex.get("subject") or ex["head"]
                cs.sha = (guard.git(ctx.repo, "rev-parse", ex["head"], check=False) or "").strip()
            except SystemExit:
                cs = None
        if cs is None or not cs.files:
            return ""
        r = review_load(ctx, [cs], rules, cannot, oc.get("skipPaths"), tm_checks)[0]
        failed, live_only = {}, set()
        for rule in r["auto"] + r["notes"]:
            if "diff" in guard.enforced_at(rule) and rule["type"] not in guard.REPLAYABLE:
                live_only.add(rule["id"])           # judged on today's facts and model: no verdict for a past commit
                continue
            if "diff" in guard.enforced_at(rule):
                vs = guard.evaluate(cs, ctx, [rule], use_exceptions=False)[0]
            else:
                vs = [h for f in cs.files for h in guard.check_edit(ctx, f.path, f.status != "A", [t for _, t in f.added]) if h["rule"] == rule["id"]]
            if vs:
                failed[rule["id"]] = vs[0]["message"]
        auto_li = "".join('<li class="%s"><span>%s%s</span></li>' % (
            "n" if a["id"] in failed else ("q" if a["id"] in live_only else "y"), E(rq.get(a["id"]) or a.get("description", "")),
            ' <span class="tag block">sent back</span>' if a["id"] in failed else (' <span class="tag line">checked in CI</span>' if a["id"] in live_only else ""))
            for a in r["auto"])
        note_li = "".join('<li class="q"><span>%s <span class="tag line">note</span></span></li>' % E(rq.get(a["id"]) or a.get("description", ""))
                          for a in r["notes"] if a["id"] in failed)
        person_li = "".join("<li><span><b>%s.</b> %s</span></li>" % (E(x["text"]), E(x.get("review", ""))) for x in r["person"])
        total = len(r["auto"]) + len(r["person"])
        head = '<div class="ex-hd"><h3>%s</h3><span class="when"><code>%s</code> %s &middot; %d files, %d lines changed%s</span></div>' % (
            E(ex.get("label", "")), E(_short(cs.sha) if cs.sha else ""), E((getattr(cs, "subject", "") or "")[:80]), len(cs.files), r["lines"],
            (" &middot; %d of them generated, safe to skip" % r["skip"]) if r["skip"] else "")
        return ('<article class="ex">%s<div class="ex-cols"><div><span class="k">Without guardrails, you check all %d</span>'
                '<span class="k2">Answered by checks: %d</span><ul class="verdict">%s%s</ul></div>'
                '<div><span class="k">With guardrails, you check %d</span><span class="k2">Left for you</span><ul class="plain">%s</ul></div></div>%s</article>') % (
            head, total, len(r["auto"]), auto_li or '<li class="q"><span>No rule applies to these files</span></li>', note_li, len(r["person"]),
            person_li or "<li><span>Nothing to judge: confirm the checks are green.</span></li>",
            ('<p class="caption">%s</p>' % E(ex["note"])) if ex.get("note") else "")

    examples = "".join(example_html(ex) for ex in oc.get("examples", []))
    S.append('<section class="sec"><div class="lead"><span class="num">How much less review</span><h2>The questions a reviewer still has to answer</h2>'
             '<p>Every pull request raises questions: does this import point the right way, did an agent mark this published, is this story true. '
             'For each of the %d commits, the chart counts the questions that apply to the files it changed. Blue ones are answered by a check before anyone is asked to review; '
             'black ones are left for a person. Without the guardrails, the reviewer answers the whole column by reading.</p></div>'
             '<div class="pair"><p><b>%d%% fewer</b> questions for a person: %d of %d.</p><p><b>Median per commit:</b> %d questions before, %d after.</p>'
             '<p><b>%d commits</b> leave nothing to judge: the reviewer only confirms the checks are green.</p>%s</div>'
             '<div class="scroll">%s</div>'
             '<p class="caption">A question is counted when its rule or judgment item applies to a changed file, whatever the answer. The black questions are the heavy ones: '
             'accuracy, judgment, logic and looks take longer than the mechanical questions the checks remove, so time saved is smaller than the count saved. '
             'What the checks buy is that a reviewer can spend all of their time on the black part.</p>'
             '<div class="lead"><h3>Three real changes, before and after</h3><p>Each check below was run on that commit as it was. A check that fails sends the change back before review. Checks that read the import graph or the structural counts give no verdict for a past commit; they run in CI on every new change.</p></div>%s</section>' % (
                 len(active_load), q_pct, q_before - q_after, q_before, med_before, med_after, len(only_green),
                 ("<p><b>%d of %d changed lines</b> are generated files a reviewer can skip, because a guardrail stops hand edits to them.</p>" % (lines_skip, lines_all)) if lines_skip else "",
                 load_svg(active_load), examples))

    # ---------------------------------------------------------------- workflow
    wf = oc.get("workflow") or {}
    edit_rules = [r for r in rules if "edit" in guard.enforced_at(r) and r["type"] != "forbid-command"]
    cmd_rules = [r for r in rules if r["type"] == "forbid-command"]
    diff_rules = [r for r in rules if "diff" in guard.enforced_at(r) and r["type"] != "ratchet" and guard_export._sev(r) != "info"]
    info_rules = [r for r in rules if guard_export._sev(r) == "info"]
    wrows = [
        ("I have an idea", "Plan the change with the map", "Map and rule lookup", "guard.py --explain lists the rules for the files you will touch", "info"),
        ("Agent edits a file", "Edit or Write tool", "Edit hook, before anything is written", wf.get("edit") or "%d rules refuse the edit before it exists" % len(edit_rules), "machine"),
        ("Agent runs a command", "Shell", "Command hook", wf.get("command") or "%d rule(s), plus shell writes to protected files" % len(cmd_rules), "machine"),
        ("Agent says it is done", "End of its turn", "Stop hook", "%d rules on the whole diff; the agent keeps working until they pass" % len(diff_rules), "machine"),
        ("git commit", "You or the agent", "Pre-commit", "The same %d rules on the staged files" % len(diff_rules), "machine"),
        ("Push and open a PR", "GitHub", wf.get("ciTitle") or "CI: the guardrails job", wf.get("ci") or "Every rule again, plus the ratchet; %d notes go to the reviewer" % len(info_rules), "machine"),
        ("Review", "A person reads it", "A person judges", "%d things no script can decide (section 3)" % len(cannot), "person"),
        ("PR merged", wf.get("mergeSub") or "Merged to the default branch", "Required checks on the default branch", "A GitHub setting a person turns on once", "person"),
    ]
    S.append('<section class="sec"><div class="lead"><span class="num">The workflow</span><h2>Where the guardrails sit, from idea to merged PR</h2>'
             '<p>Most checks act before a reviewer is involved. The edit and command hooks stop an agent before a change exists; the Stop hook and pre-commit run the diff rules; '
             'CI runs everything again and is the one checkpoint nobody can skip. Review and merge are the person\'s.</p></div>'
             '<div class="figure"><div class="scroll">%s</div></div></section>' % workflow_svg(wrows))

    # ---------------------------------------------------------------- 1. blocked
    rows = []
    for cat in guard_export.CATEGORY_ORDER + ["Other"]:
        rs = [r for r in rules if (guard_export.category(r) if cat != "Other" else None) == cat or (cat == "Other" and guard_export.category(r) not in guard_export.CATEGORY_ORDER)]
        if not rs:
            continue
        rows.append('<tr class="grp"><td colspan="4">%s</td></tr>' % E(cat))
        for r in rs:
            pr = per_rule.get(r["id"], {"block": [], "edit": [], "warn": []})
            nb = len({e["sha"] for e in pr["block"]})
            ne = len({e["sha"] for e in pr["edit"]})
            nw = len({e["sha"] for e in pr["warn"]})
            ex = (pr["block"] or pr["edit"] or pr["warn"] or [None])[-1]
            sev = guard_export._sev(r)
            if nb or ne:
                count = "%d commit%s%s" % (nb + ne, "" if nb + ne == 1 else "s", " (agent edit refused)" if ne and not nb else "")
            elif nw and sev == "info":
                count = "noted on %d" % nw
            elif nw:
                count = "warned on %d" % nw
            elif r["type"] in guard.REPLAYABLE or "edit" in guard.enforced_at(r):
                count = '<span class="tag line">never hit</span>'
            else:
                count = '<span class="tag line">live only</span>'
            rows.append("<tr><td>%s<br><code>%s</code> <span class=\"tag%s\">%s</span></td><td>%s</td><td class=\"r\">%s</td><td>%s</td></tr>" % (
                E(rq.get(r["id"]) or r.get("description", "")), E(r["id"]), " block" if sev == "block" else "", guard_export.SEV_LABEL.get(sev, sev),
                E(guard.enforced_label(r)), count, cell_commit(ex) if ex else '<span class="caption">%s</span>' % E(
                    "Replay needs the facts of the day" if r["type"] not in guard.REPLAYABLE and "edit" not in guard.enforced_at(r) else "No past commit broke it")))
    prow = "".join("<tr><td>%s<br><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (
        E(p["label"]), E(p["action"][:110]), '<span class="tag block">refused</span>' if p["decision"] == "block" else '<span class="tag line">allowed</span>',
        ", ".join("<code>%s</code>" % E(x) for x in p["rules"]) or "") for p in probes)
    S.append('<section class="sec"><div class="lead"><span class="num">1 &middot; Blocked</span><h2>What a reviewer no longer looks for</h2>'
             '<p>Each row is a reviewer question that a check now answers. The counts replay the %d commits of this repository through today\'s rules, '
             'as if the checks had been live: <b>%d commits</b> would have been sent back, %d of them commits that later needed a fix, and '
             '<b>%d agent edits</b> would have been refused at the keyboard. Rules that read the import graph or the model are judged live only. '
             'Past commits are judged by today\'s conventions, so some counts show where work would have been routed differently rather than a mistake.%s</p></div>'
             '<div class="scroll"><table><thead><tr><th>Reviewer question it retires</th><th>Where it runs</th><th class="r">Would have stopped</th><th>Latest example</th></tr></thead>'
             '<tbody>%s</tbody></table></div>'
             '<div class="lead"><h3>The agent checks, tried now</h3><p>%d of %d actions an agent should never take are refused by the hooks as configured today. '
             'These run against the real hook code, not a simulation.%s</p></div>'
             '<div class="scroll"><table><thead><tr><th>Action</th><th>Result</th><th>Rule</th></tr></thead><tbody>%s</tbody></table></div></section>' % (
                 n, len(blocked_commits), len(later_fixed & blocked_commits), len(edits), (" " + E(oc["blockedNote"])) if oc.get("blockedNote") else "", "".join(rows), len(probe_blocks), len(probes),
                 (" In live sessions so far the hooks recorded %d blocks." % len(live_blocks)) if live else "", prow))

    # ---------------------------------------------------------------- 2. got through
    def item_html(m, gap=False):
        chk = m.get("check") or {}
        rule = m.get("rule")
        rep = m.get("replay") or {}
        kind = KIND_LABEL.get(chk.get("kind"), chk.get("kind", ""))
        L = ['<div class="l"><h3>%s</h3>' % E(m["title"])]
        if not gap:
            intro = ", ".join("<code>%s</code>" % E(_short(s)) for s in m.get("introduced") or [])
            L.append('<span class="when">Introduced %s &middot; fixed <code>%s</code> &middot; found by: %s</span>' % (
                intro or "unknown", E(_short(m.get("fixed"))), E(m.get("foundBy", ""))))
        L.append("<p>%s</p></div>" % E(m.get("what", "")))
        live = m.get("ruleState") == "active" or m.get("status") in ("built", "covered")
        R = ['<div class="r"><span class="k">%s</span>' % ("The check" if live else "The check we would build")]
        if rule is not None:
            R.append('<p><span class="tag">%s</span> <code>%s</code> <span class="tag%s">%s</span>%s</p><p>%s</p>' % (
                E(kind if chk.get("kind") != "probe" else "Existing guardrail"), E(rule["id"]),
                " block" if guard_export._sev(rule) == "block" else "", guard_export.SEV_LABEL.get(guard_export._sev(rule), "warns"),
                " <span class=\"tag line\">%s</span>" % STATE_LABEL.get(m.get("ruleState"), "proposed"),
                E(chk.get("description") or rule.get("description", ""))))
        else:
            R.append('<p><span class="tag">%s</span> <span class="tag line">%s</span></p><p>%s</p>' % (
                E(kind), "built" if m.get("status") == "built" else STATE_LABEL.get(m.get("status"), "proposed"), E(chk.get("description", ""))))
        if chk.get("note"):
            R.append('<p class="caption">%s</p>' % E(chk["note"]))
        if m.get("decision"):
            R.append('<p class="caption"><b>Decision:</b> %s</p>' % E(m["decision"]))
        if rep.get("intro"):
            v = []
            v.append('<li class="%s">%s</li>' % ("y" if rep["atIntro"] else "n", "Fires at the commit that introduced it" if rep["atIntro"] else "Does not fire at the commit that introduced it"))
            if rep.get("tree"):
                if not rep["afterFix"]:
                    v.append('<li class="y">Quiet after the fix</li>')
                else:
                    if rep.get("left"):
                        v.append('<li class="n">The fix was partial: %s it left in place still fire%s</li>' % (_n(rep["left"], "finding"), "s" if rep["left"] == 1 else ""))
                    else:
                        v.append('<li class="y">Quiet after the fix</li>')
                    if rep.get("newSha"):
                        v.append('<li class="n">Fires again from <code>%s</code> (%s) on a new finding</li>' % (E(rep["newSha"]), E(rep["newSubject"][:60])))
            else:
                other = [i for i in rep["fired"] if i not in rep["intro"]]
                v.append('<li class="%s">%s</li>' % ("y" if not other else "q", "Fires on no other commit" if not other else
                         "Also fires on %d other commit(s) of %d" % (len(other), n)))
            t = rep.get("today") if m.get("ruleState") != "declined" else None
            if t is not None:
                v.append('<li class="%s">%s</li>' % ("y" if not t else "n", "Clean today" if not t else "Fires today: %s to fix before the rule is turned on" % _n(len(t), "finding")))
            R.append('<ul class="verdict">%s</ul>' % "".join(v))
            R.append('<div class="strip">%s<span class="cap">%d commits, oldest left. Filled: the check fires. Blue: between the cause (&#9650;) and the fix (dashed). Black: elsewhere.%s</span></div>' % (
                strip_svg(n, rep), n, " Grey: before the cause." if rep.get("tree") and rep.get("start") else ""))
            if rep.get("first"):
                R.append('<div class="found">%s</div>' % E(rep["first"][:220]))
            for f in (t or [])[:3]:
                R.append('<div class="found">today: %s%s %s</div>' % (E(f.get("path") or ""), (":%d" % f["line"]) if f.get("line") else "", E(f["message"][:160])))
        for p in m.get("probes") or []:
            ok = p.get("expect") is None or p["decision"] == p["expect"] or (p.get("expect") == "block" and p["decision"] == "block")
            R.append('<ul class="verdict"><li class="%s"><span>%s: <b>%s</b>%s</span></li></ul>' % (
                "y" if ok else "n", E(p["label"]), "refused" if p["decision"] == "block" else ("passes" if p["decision"] == "pass" else "allowed"),
                (" (expected %s)" % p["expect"]) if p.get("expect") and not ok else ""))
        if m.get("evidence"):
            R.append('<p class="caption">%s</p>' % E(m["evidence"]))
        R.append("</div>")
        return '<article class="item">%s%s</article>' % ("".join(L), "".join(R))

    S.append('<section class="sec"><div class="lead"><span class="num">2 &middot; Got through</span><h2>What people caught, and the check that would catch it next time</h2>'
             '<p>These problems reached the main branch and a person found them later, most in the final review before the 2.0 release. For each one, the check we would have built. '
             'Where the check is a rule, it was replayed over all %d commits: a good check fires at the commit that caused the problem and goes quiet once it is fixed.</p>'
             '<div class="pair"><p><b>%d of %d</b> scripted checks fire at the commit that caused the problem.</p><p><b>%d of %d</b> are now caught by a rule in force or a check that is built.</p>'
             '<p><b>%d</b> not adopted, each with the reason; <b>%d</b> still open.</p></div></div>%s'
             '<div class="lead"><h3>Not yet happened</h3><p>Risks the codebase map found with no check in place. Each is proven on a made-up change instead of history.</p></div>%s</section>' % (
                 n, len(proven), len(replayed), len(covered), len(missed), len(declined), len(open_items), "".join(item_html(m) for m in missed), "".join(item_html(g, gap=True) for g in gaps)))

    # ---------------------------------------------------------------- 3. never
    crow = "".join("<tr><th>%s</th><td>%s</td><td>%s</td></tr>" % (E(c["text"]), E(c.get("why", "")), E(c.get("review", ""))) for c in cannot)
    jrow = "".join("<tr><th>%s</th><td>%s</td><td class=\"caption\">%s</td></tr>" % (E(j.get("text", "")), E(j.get("why", "")), E(j.get("source", ""))) for j in judgment)
    S.append('<section class="sec"><div class="lead"><span class="num">3 &middot; Never blockable</span><h2>What review is for</h2>'
             '<p>No script can decide these. With the checks above in place, this is the whole of the reviewer\'s job on a pull request.</p></div>'
             '<div class="scroll"><table><thead><tr><th>Only a person can judge</th><th>Why no script can</th><th>What the reviewer does</th></tr></thead><tbody>%s</tbody></table></div>'
             '%s</section>' % (crow, ('<div class="lead"><h3>Rules in the project instructions that need a person</h3></div><div class="scroll"><table><thead><tr><th>Rule</th><th>Why it is not scripted</th><th>Source</th></tr></thead><tbody>%s</tbody></table></div>' % jrow) if judgment else ""))

    # ---------------------------------------------------------------- decisions
    dec = []
    for m, t in today_hits:
        dec.append("<li><code>%s</code> fires today on %s: %s. Fix them%s.</li>" % (
            E(m["rule"]["id"]), _n(len(t), "finding"), "; ".join("%s%s" % (E(f.get("path") or ""), (":%d" % f["line"]) if f.get("line") else "") for f in t[:4]),
            ": the rule is in force and blocks every change until then" if m.get("ruleState") == "active" else ", then accept the rule"))
    pending = [x for x in missed + gaps if x.get("status") == "proposed" and x.get("rule") is not None and x.get("ruleState") == "proposed"]
    if pending:
        dec.append("<li>Accept the proposed rules: <code>python3 .cartographer/bin/guard.py --accept %s</code></li>" % E(" ".join(x["rule"]["id"] for x in pending)))
    other = [x for x in missed + gaps if x.get("status") == "proposed" and x.get("rule") is None]
    if other:
        dec.append("<li>Build the %d checks that are not rules: %s.</li>" % (len(other), "; ".join(E(x["title"].lower()) for x in other)))
    dec.append("<li>Make the guardrails and Checks jobs required on the default branch, so the local hooks are a convenience and CI is the gate.</li>")
    S.append('<section class="col"><span class="num">Decisions</span><h2>What it takes to finish</h2><ol>%s</ol></section>' % "".join(dec))
    S.append('<footer>Generated by <code>guard.py --outcomes</code> from outcomes.json, guardrails.json, guardrails.proposed.json, the event logs and git history. '
             'Replays judge each past commit with today\'s rules; imports resolve against today\'s tree.</footer>')

    page = ('<title>Guardrail Outcomes</title><meta name="claude:viewport-fit-sides" content="cover">'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600&display=swap">'
            "<style>%s</style><main class=\"doc\">%s</main>" % (CSS, "".join(S)))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">" + page + "</html>\n")
    return {"path": out_path, "missed": len(missed), "proven": len(proven), "replayed": len(replayed), "covered": len(covered),
            "blockedCommits": len(blocked_commits), "commits": n, "probesRefused": len(probe_blocks), "probes": len(probes)}
