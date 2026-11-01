#!/usr/bin/env python3
"""Guardrails report: a static, self-contained HTML document for senior engineers and architects.

  python3 .cartographer/bin/guard.py --export guardrails.html

Sections: summary, how enforcement works (diagram), responsibility matrix (each rule against each checkpoint),
what reviewers must check, gaps with no guardrail, evidence behind each rule, and an appendix with every rule's
full definition. No scripts. Built only from guardrails.json, the model, baseline and facts.
"""
from __future__ import annotations

import html
import os

import carto
import guard

E = html.escape

CSS = """
:root{
  /* Report layout: one 860px reading column; diagram and matrix break out to 1080px. Nationwide palette: white, blue, gray, black. */
  --bg:#FFFFFF; --ink:#0F1419; --ink2:#2B323C; --muted:#5A6472; --line:#D9DEE5; --subtle:#F4F6F9;
  --blue:#0047BB; --blue-tint:#E6EDFA; --gray-fill:#EEF1F5; --mark:#0F1419;
  --serif:'IBM Plex Serif',Georgia,serif; --sans:'IBM Plex Sans','Segoe UI',system-ui,sans-serif; --mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0E1116; --ink:#E8ECF1; --ink2:#C9D1DB; --muted:#9AA4B2; --line:#2A313B; --subtle:#151A21;
  --blue:#86ABF0; --blue-tint:#16233B; --gray-fill:#1C222B; --mark:#E8ECF1; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#0E1116; --ink:#E8ECF1; --ink2:#C9D1DB; --muted:#9AA4B2; --line:#2A313B; --subtle:#151A21;
  --blue:#86ABF0; --blue-tint:#16233B; --gray-fill:#1C222B; --mark:#E8ECF1; color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
.doc{max-width:1080px;margin:0 auto;padding-block:40px 72px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:flex;flex-direction:column;gap:44px}
.col{max-width:860px;display:flex;flex-direction:column;gap:14px}
h1,h2,h3{margin:0;font-weight:600;text-wrap:balance;color:var(--ink)}
h1{font-family:var(--serif);font-size:34px;line-height:1.15;letter-spacing:-.01em}
h2{font-family:var(--serif);font-size:23px;line-height:1.25}
h3{font-size:15px}
p{margin:0;color:var(--ink2);max-width:72ch}
ul,ol{margin:0;padding-left:20px;color:var(--ink2);display:flex;flex-direction:column;gap:6px;max-width:76ch}
code{font-family:var(--mono);font-size:12.5px;word-break:break-word}
.num{font-family:var(--sans);font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--blue)}
.meta{display:flex;flex-wrap:wrap;gap:4px 20px;color:var(--muted);font-size:13px}
.title{display:flex;flex-direction:column;gap:12px;border-bottom:2px solid var(--ink);padding-bottom:24px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.kpi{padding:14px 16px 14px 0;display:flex;flex-direction:column;gap:2px}
.kpi .v{font-size:28px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1.1}
.kpi .k{font-size:12px;color:var(--muted)}
.callout{border-left:3px solid var(--blue);padding:4px 0 4px 16px;display:flex;flex-direction:column;gap:8px}
.figure{display:flex;flex-direction:column;gap:10px}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.figure svg{display:block;min-width:900px;width:100%;height:auto}
.caption{font-size:13px;color:var(--muted);max-width:86ch}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
thead th{font-size:12px;color:var(--muted);font-weight:600;border-bottom:2px solid var(--ink);white-space:nowrap}
tbody th{font-weight:500}
th.c,td.c{text-align:center;width:84px}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--blue);vertical-align:-1px}
.dot.warn{background:transparent;border:2px solid var(--blue)}
.dot.person{background:var(--mark);border-radius:2px}
.grp td{background:var(--subtle);font-weight:600;color:var(--ink);font-size:12px;letter-spacing:.04em;text-transform:uppercase}
.sev{font-size:11px;font-weight:600;border-radius:4px;padding:1px 7px;white-space:nowrap;background:var(--gray-fill);color:var(--ink2)}
.sev.high{background:var(--ink);color:var(--bg)}
.legend{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12.5px;color:var(--muted)}
.check{list-style:none;padding:0;gap:10px}
.check li{display:grid;grid-template-columns:20px minmax(0,1fr);gap:10px}
.check li::before{content:"";width:14px;height:14px;border:1.5px solid var(--ink);border-radius:3px;margin-top:4px}
.rule{border-top:1px solid var(--line);padding:16px 0;display:flex;flex-direction:column;gap:8px}
.rule .hd{display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline}
.rule .hd code{font-size:14px;font-weight:600;color:var(--ink)}
.defs{display:grid;grid-template-columns:minmax(120px,180px) minmax(0,1fr);gap:4px 16px;font-size:13.5px}
.defs .k{color:var(--muted);font-size:12.5px}
footer{color:var(--muted);font-size:12.5px;border-top:1px solid var(--line);padding-top:16px}
@media (max-width:640px){h1{font-size:28px}.defs{grid-template-columns:1fr}}
"""

FIELD_LABEL = {"why": "Why this severity", "left": "List", "right": "Must agree with", "relation": "Relation", "mode": "Mode",
               "message": "Reported as", "skipMissing": "Skipped while a file is missing", "leftName": "List name", "rightName": "Other list name",
               "paths": "Applies to", "when": "When these change", "require": "Must also change", "from": "Importing from",
               "to": "May not import", "module": "Import text matching", "pattern": "Pattern", "needs": "Must add",
               "fileStatus": "Git status", "onlyIf": "Only files containing", "ignore": "Names ignored", "sources": "Values that must match", "kinds": "Interface kinds",
               "on": "Interface change", "allowAdd": "New files allowed", "allowDelete": "Deletes allowed", "minLines": "Minimum lines",
               "max": "Maximum lines", "exclude": "Excluded", "metrics": "Counts", "tests": "Test files", "ignoreTests": "Skip tests",
               "always": "Check on every change"}
SKIP = {"id", "type", "description", "fix", "evidence", "severity", "source", "added", "status", "enforce", "category"}
CATEGORY_ORDER = ["Architecture", "Agent permissions", "Publishing gate", "Change conventions", "Change hygiene"]


def category(r):
    if r.get("category"):
        return r["category"]
    t = r.get("type")
    if t in ("layering", "no-new-cycles", "forbid-import", "ratchet", "pinned-snippets", "interface-change"):
        return "Architecture"
    if t == "forbid-command" or "diff" not in guard.enforced_at(r):
        return "Agent permissions"
    if t in ("tests-required", "max-size"):
        return "Change hygiene"
    return "Change conventions"


def _val(k, v):
    if k == "sources":
        return "<br>".join("<code>%s</code> <code>/%s/</code>" % (E(s.get("path", "")), E(s.get("pattern", ""))) for s in v)
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if k in ("pattern", "module", "ignore"):
        return "<code>/%s/</code>" % E(str(v))
    vals = v if isinstance(v, list) else [v]
    return ", ".join("<code>%s</code>" % E(str(x)) for x in vals)


SEV_LABEL = {"block": "blocks", "warn": "warns", "info": "PR note"}


def _sev(r):
    return r.get("severity") or guard.DEFAULT_SEVERITY.get(r.get("type"), "block")


def _covers(r, path, ctx):
    for k in ("paths", "when", "from", "require", "to"):
        if r.get(k) is not None and guard.Selector(r.get(k), ctx)(path):
            return True
    return False


def diagram(n_edit, n_cmd, n_local, n_ci):
    """Swimlane SVG: agent, automated checkpoints, person. Colours from the page tokens so it works in both themes."""
    W, H = 1000, 330
    lanes = [("Agent", 18), ("Automated checkpoints", 118), ("Person", 238)]
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-labelledby="dg-t dg-d" xmlns="http://www.w3.org/2000/svg">' % (W, H),
           '<title id="dg-t">How a change is checked</title>',
           '<desc id="dg-d">An agent action passes the agent hook, the Stop hook, pre-commit and the CI guardrails job before a person reviews and merges. Each checkpoint can send the change back to the agent with the fix named.</desc>',
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0 0L10 5L0 10z" fill="var(--ink2)"/></marker>'
           '<marker id="ahb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0 0L10 5L0 10z" fill="var(--blue)"/></marker></defs>']
    for i, (name, y) in enumerate(lanes):
        out.append('<rect x="0" y="%d" width="%d" height="%d" fill="%s"/>' % (y - 18, W, 100 if i < 2 else 92, "var(--subtle)" if i == 1 else "var(--bg)"))
        out.append('<text x="12" y="%d" font-size="11" font-weight="600" letter-spacing="1" fill="var(--muted)">%s</text>' % (y - 4, E(name.upper())))
    out.append('<line x1="0" y1="%d" x2="%d" y2="%d" stroke="var(--line)"/>' % (100, W, 100))
    out.append('<line x1="0" y1="%d" x2="%d" y2="%d" stroke="var(--line)"/>' % (220, W, 220))

    def box(x, y, w, h, title, sub, kind):
        fill = {"gate": "var(--blue-tint)", "person": "var(--bg)", "plain": "var(--bg)"}[kind]
        stroke = {"gate": "var(--blue)", "person": "var(--mark)", "plain": "var(--line)"}[kind]
        sw = 2 if kind == "person" else 1.25
        tc = "var(--blue)" if kind == "gate" else "var(--ink)"
        s = ['<rect x="%d" y="%d" width="%d" height="%d" rx="6" fill="%s" stroke="%s" stroke-width="%s"/>' % (x, y, w, h, fill, stroke, sw),
             '<text x="%d" y="%d" font-size="13.5" font-weight="600" fill="%s">%s</text>' % (x + 12, y + 22, tc, E(title))]
        for j, line in enumerate(sub):
            s.append('<text x="%d" y="%d" font-size="12" fill="var(--ink2)">%s</text>' % (x + 12, y + 40 + j * 15, E(line)))
        return "".join(s)

    # agent lane
    out.append(box(20, 30, 150, 56, "Agent acts", ["Edits a file or runs", "a command"], "plain"))
    # checkpoints
    gx = [190, 380, 560, 740]
    gates = [("Agent hook", ["%d rules, before the" % (n_edit + n_cmd), "edit or command runs"]),
             ("Stop hook", ["%d rules, before the" % n_local, "agent may finish"]),
             ("Pre-commit", ["%d rules on staged" % n_local, "changes (skippable)"]),
             ("CI guardrails job", ["%d rules on the PR;" % n_ci, "required check"])]
    for x, (t, sub) in zip(gx, gates):
        out.append(box(x, 120, 160, 64, t, sub, "gate"))
    # person lane
    out.append(box(740, 240, 160, 64, "Review and merge", ["Warnings, judgment,", "approval"], "person"))
    out.append(box(380, 240, 340, 64, "Owns the rules", ["Accepts proposed rules, grants exceptions", "with reason, expiry and approver"], "person"))
    # forward arrows
    out.append('<path d="M170 58 H270 V116" fill="none" stroke="var(--ink2)" stroke-width="1.5" marker-end="url(#ah)"/>')
    for a, b in zip(gx, gx[1:]):
        out.append('<path d="M%d 152 H%d" stroke="var(--ink2)" stroke-width="1.5" marker-end="url(#ah)"/>' % (a + 160, b - 4))
    out.append('<path d="M820 184 V236" stroke="var(--ink2)" stroke-width="1.5" marker-end="url(#ah)"/>')
    out.append('<path d="M900 272 H960" stroke="var(--ink2)" stroke-width="1.5" marker-end="url(#ah)"/>')
    out.append('<text x="928" y="262" font-size="12" font-weight="600" fill="var(--ink)" text-anchor="middle">main</text>')
    # blocked returns: a riser from each checkpoint joins one dashed line back to the agent
    for x in gx:
        out.append('<path d="M%d 120 V74" fill="none" stroke="var(--blue)" stroke-width="1.25" stroke-dasharray="4 3"/>' % (x + 120))
    out.append('<path d="M%d 74 H174" fill="none" stroke="var(--blue)" stroke-width="1.25" stroke-dasharray="4 3" marker-end="url(#ahb)"/>' % (gx[-1] + 120))
    out.append('<text x="440" y="66" font-size="12" fill="var(--blue)">Blocked: the message names the fix and the agent keeps working</text>')
    out.append('<path d="M550 240 V188" stroke="var(--mark)" stroke-width="1.25" stroke-dasharray="2 3" marker-end="url(#ah)"/>')
    out.append('<text x="558" y="216" font-size="11.5" fill="var(--muted)">configures every checkpoint</text>')
    out.append("</svg>")
    return "".join(out)


def export(ctx, out_path):
    rules = ctx.rules
    judgment = ctx.config.get("judgment", [])
    exceptions = ctx.config.get("exceptions", [])
    proposed = (carto.load(os.path.join(ctx.home, "guardrails.proposed.json")) or {}).get("rules", [])
    replay = carto.load(os.path.join(ctx.home, "facts", "guard-replay.json")) or {}
    rp = {r["rule"]: r for r in replay.get("rules", [])}
    base = (carto.load(os.path.join(ctx.home, "baseline.json")) or {}).get("metrics", {})
    try:
        cur = guard.metrics(ctx)
    except Exception:  # a report must not fail because facts are missing
        cur = {}
    repo = (ctx.model.get("repo") or {}).get("name") or os.path.basename(ctx.repo.rstrip("/"))
    head = (guard.git(ctx.repo, "rev-parse", "--short=12", "HEAD", check=False) or "").strip()
    branch = (guard.git(ctx.repo, "rev-parse", "--abbrev-ref", "HEAD", check=False) or "").strip()

    at = {r["id"]: guard.enforced_at(r) for r in rules}
    edit_rules = [r for r in rules if "edit" in at[r["id"]]]
    cmd_rules = [r for r in edit_rules if r["type"] == "forbid-command"]
    diff_rules = [r for r in rules if "diff" in at[r["id"]]]
    local_diff = [r for r in diff_rules if r["type"] != "ratchet"]
    warn_rules = [r for r in rules if _sev(r) == "warn"]
    info_rules = [r for r in rules if _sev(r) == "info"]
    block_rules = [r for r in rules if _sev(r) == "block"]

    specific = [r for r in rules if any(r.get(k) is not None for k in ("paths", "when", "from", "require", "to"))
                and r["type"] not in ("tests-required", "max-size")]
    files = [row[0] for row in (ctx.inv or {}).get("files", [])]
    uncovered_comps = []
    for c in ctx.model.get("components", []):
        if c.get("layer") == "external" or not c.get("paths"):
            continue
        cf = [p for p in files if ctx.mapper(p) == c["id"]]
        if cf and not any(_covers(r, p, ctx) for r in specific for p in cf[:400]):
            uncovered_comps.append(c)
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    uncovered_risks = sorted([rk for rk in ctx.model.get("risks", [])
                              if not any(_covers(r, e.get("path"), ctx) for r in specific for e in rk.get("evidence", []) if e.get("path"))],
                             key=lambda x: sev_rank.get(x.get("severity"), 3))

    window = replay.get("window") or {}
    fixed_base = window.get("followedByFixOrRevert", 0)
    proven = [r for r in rules if (rp.get(r["id"]) or {}).get("followedByFixOrRevert")]
    fired = [r for r in rules if (rp.get(r["id"]) or {}).get("fired")]

    # ---------------------------------------------------------------- 1. summary
    summary_points = [
        "<b>%d rules are enforced by script</b>: %d block a change, %d warn and %d only add a note for the reviewer on the pull request. They run at up to four checkpoints, and the CI job is the one that cannot be skipped." % (len(rules), len(block_rules), len(warn_rules), len(info_rules)),
        "<b>%d rules act on the agent before the change exists</b>. They refuse an edit or command, for example %s, while a person can still do the same thing." % (
            len(edit_rules), ", ".join("<code>%s</code>" % E(r["id"]) for r in edit_rules[:3]) or "none"),
        "<b>%d rules need judgment</b> and are listed for reviewers instead of being scripted." % len(judgment),
        "<b>%d gaps</b>: %d components get only the repo-wide rules, and %d of the model's risks have no rule on the code they cite." % (
            len(uncovered_comps) + len(uncovered_risks), len(uncovered_comps), len(uncovered_risks)),
    ]
    if window.get("commits"):
        if fixed_base:
            summary_points.append("<b>Evidence</b>: replayed against %d commits, %d rules fired on past commits and %d of those fired on commits that later needed a fix or were reverted." % (window["commits"], len(fired), len(proven)))
        else:
            summary_points.append("<b>Evidence is thin</b>: none of the %d replayed commits was later fixed or reverted, so history cannot yet show which rules catch real mistakes. The rules rest on the documented decisions they cite." % window["commits"])
    over = [k for k in cur if k in base and cur[k] > base[k]]
    decisions = []
    if over:
        decisions.append("Structural counts above the baseline: %s." % ", ".join(guard.METRIC_LABEL.get(k, k) for k in over))
    if proposed:
        decisions.append("%d proposed rule(s) waiting for acceptance." % len(proposed))
    for x in exceptions:
        st = guard.exception_state(x, ctx.today)
        if st != "valid":
            decisions.append("Exception for <code>%s</code> is %s." % (E(x.get("rule", "")), st))
    for rk in uncovered_risks:
        if rk.get("severity") == "high":
            decisions.append("High risk with no guardrail: %s." % E(rk.get("title", "")))
    if not decisions:
        decisions.append("None. Merge the rules and make the guardrails check required on the default branch.")

    # ---------------------------------------------------------------- 3. matrix
    def mark(r, point):
        a, sev = at[r["id"]], _sev(r)
        on = {"hook": "edit" in a, "stop": "diff" in a and r["type"] != "ratchet", "pre": "diff" in a and r["type"] != "ratchet", "ci": "diff" in a}[point]
        if not on:
            return '<td class="c"></td>'
        return '<td class="c"><span class="dot%s" title="%s"></span></td>' % ("" if sev == "block" else " warn", "Blocks" if sev == "block" else "Warns")

    by_cat = {}
    for r in rules:
        by_cat.setdefault(category(r), []).append(r)
    cats = [c for c in CATEGORY_ORDER if c in by_cat] + sorted(c for c in by_cat if c not in CATEGORY_ORDER)
    mrows = ""
    for c in cats:
        mrows += '<tr class="grp"><td colspan="6">%s</td></tr>' % E(c)
        for r in by_cat[c]:
            mrows += '<tr><th scope="row"><code>%s</code><br><span style="color:var(--muted);font-size:12.5px;font-weight:400">%s</span></th>%s%s%s%s<td class="c"></td></tr>' % (
                E(r["id"]), E(r.get("description", "")), mark(r, "hook"), mark(r, "stop"), mark(r, "pre"), mark(r, "ci"))
    if judgment:
        mrows += '<tr class="grp"><td colspan="6">Judgment</td></tr>'
        for j in judgment:
            mrows += '<tr><th scope="row">%s<br><span style="color:var(--muted);font-size:12.5px;font-weight:400">%s</span></th><td class="c"></td><td class="c"></td><td class="c"></td><td class="c"></td><td class="c"><span class="dot person" title="Person"></span></td></tr>' % (
                E(j.get("text", "")), E(j.get("why", "")))

    # ---------------------------------------------------------------- 4. reviewer checklist
    checklist = [
        "Read every guardrail warning in the PR comment and decide whether it applies: %s." % (", ".join("<code>%s</code>" % E(r["id"]) for r in warn_rules) or "none configured"),
    ]
    for j in judgment:
        checklist.append(E(j.get("text", "")) + (" <span style='color:var(--muted)'>(%s)</span>" % E(j["why"]) if j.get("why") else ""))
    for r in edit_rules:
        if r["type"] in ("forbid-command", "forbid-pattern"):
            checklist.append("Confirm a person, not an agent, did what <code>%s</code> reserves: %s" % (E(r["id"]), E(r.get("description", ""))))
    checklist.append("For changes to the components below that only the repo-wide rules cover, read the diff against the model's description of the component.")
    checklist.append("Treat any edit to <code>.cartographer/guardrails.json</code>, <code>baseline.json</code> or an exception as a policy change that needs its own approval.")

    # ---------------------------------------------------------------- 5. gaps
    gap_rows = ""
    for rk in uncovered_risks:
        gap_rows += '<tr><td><span class="sev %s">%s</span></td><td><b>%s</b><br><span style="color:var(--ink2)">%s</span></td><td>%s</td></tr>' % (
            "high" if rk.get("severity") == "high" else "", E((rk.get("severity") or "").title()), E(rk.get("title", "")), E(rk.get("detail", "")),
            "<br>".join("<code>%s%s</code>" % (E(e.get("path", "")), (":%s" % e["line"]) if e.get("line") else "") for e in rk.get("evidence", [])[:3]))
    comp_list = ", ".join(E(c.get("name", c["id"])) for c in uncovered_comps) or "none"

    # ---------------------------------------------------------------- 6. evidence
    ev_rows = ""
    for r in rules:
        q = rp.get(r["id"]) or {}
        if not q.get("replayable"):
            res = "Agent hook only" if "diff" not in at[r["id"]] else "Checks today's model or file contents"
            ev_rows += "<tr><td><code>%s</code></td><td class=r>—</td><td class=r>—</td><td>%s</td></tr>" % (E(r["id"]), res)
        else:
            res = ("Later fixed or reverted %s%% of the time, against %s%% for all commits" % (q["hitRatePct"], q["baseRatePct"])) if q.get("fired") and fixed_base else ("Fired on past commits; no fixes in history to compare" if q.get("fired") else "Never fired")
            ev_rows += "<tr><td><code>%s</code></td><td class=r>%d</td><td class=r>%s</td><td>%s</td></tr>" % (E(r["id"]), q.get("fired", 0), q.get("followedByFixOrRevert", 0), res)

    ratchet_rows = ""
    for k in guard.METRICS:
        if k in cur or k in base:
            b, c = base.get(k), cur.get(k)
            ratchet_rows += "<tr><td>%s</td><td class=r>%s</td><td class=r>%s</td><td>%s</td></tr>" % (
                E(guard.METRIC_LABEL.get(k, k).capitalize()), "—" if b is None else b, "—" if c is None else c,
                "Above baseline" if (b is not None and c is not None and c > b) else "Holding" if b is not None else "No baseline")

    # ---------------------------------------------------------------- appendix
    def rule_def(r):
        q = rp.get(r["id"]) or {}
        rows = []
        for k, v in r.items():
            if k in SKIP or k.startswith("_"):
                continue
            rows.append('<span class="k">%s</span><div>%s</div>' % (E(FIELD_LABEL.get(k, k)), _val(k, v)))
        rows.append('<span class="k">Enforced at</span><div>%s</div>' % E(guard.enforced_label(r)))
        if r.get("fix"):
            rows.append('<span class="k">Fix the message names</span><div>%s</div>' % E(r["fix"]))
        if r.get("evidence"):
            rows.append('<span class="k">Basis</span><div>%s</div>' % ", ".join("<code>%s</code>" % E(e) for e in r["evidence"]))
        if q.get("replayable"):
            rows.append('<span class="k">Replay</span><div>%s</div>' % ("Fired on %d of %d commits" % (q["fired"], q["commits"]) if q.get("fired") else "Never fired in %d commits" % q.get("commits", 0)))
        rows.append('<span class="k">Source</span><div>%s, added %s</div>' % (E(r.get("source", "manual")), E(r.get("added", "—"))))
        sev = _sev(r)
        return ('<div class="rule"><div class="hd"><code>%s</code><span class="sev">%s</span><span style="color:var(--muted);font-size:12.5px">%s · %s</span></div>'
                '<p>%s</p><div class="defs">%s</div></div>') % (E(r["id"]), "Blocks" if sev == "block" else "Warns", E(r["type"]), E(category(r)),
                                                                E(r.get("description", "")), "".join(rows))
    appendix = "".join('<h3 style="margin-top:12px">%s</h3>%s' % (E(c), "".join(rule_def(r) for r in by_cat[c])) for c in cats)

    exc_html = ""
    if exceptions:
        exc_html = "<table><thead><tr><th>Rule</th><th>Where</th><th>Reason</th><th>Approved by</th><th>Expires</th></tr></thead><tbody>%s</tbody></table>" % "".join(
            "<tr><td><code>%s</code></td><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s, %s</td></tr>" % (
                E(x.get("rule", "")), E(x.get("path") or "**"), E(x.get("reason", "")), E(x.get("approvedBy", "")), E(x.get("expires", "")), guard.exception_state(x, ctx.today))
            for x in exceptions)
    else:
        exc_html = "<p>No exceptions are recorded.</p>"

    events = carto.read_events(ctx.home)
    SRC = {"edit-hook": "Agent edit hook", "command-hook": "Agent command hook", "stop-hook": "Stop hook", "cursor-stop": "Cursor stop hook",
           "pre-commit": "Pre-commit", "ci": "CI", "cli": "Command line"}
    if events:
        tally = {}
        for e in events:
            d = {"pass": "allow"}.get(e.get("decision"), e.get("decision"))
            row = tally.setdefault(e.get("source", "?"), {"allow": 0, "warn": 0, "block": 0, "release": 0})
            if d in row:
                row[d] += 1
        trows = "".join("<tr><td>%s</td><td class=r>%d</td><td class=r>%d</td><td class=r>%d</td></tr>" % (
            E(SRC.get(k, k)), v["allow"], v["warn"], v["block"] + v["release"]) for k, v in tally.items())
        erows = ""
        for e in events[-25:][::-1]:
            what = ("%s, %s files" % (e.get("label") or e.get("mode") or "", e.get("files", "?"))) if e.get("kind") == "run" else "%s %s" % (e.get("tool") or "", e.get("target") or "")
            erows += "<tr><td class=r style='white-space:nowrap'>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td><td>%s%s</td></tr>" % (
                E((e.get("at") or "")[:16].replace("T", " ")), E(SRC.get(e.get("source"), e.get("source") or "")), E((e.get("decision") or "").capitalize()),
                E(what[:120]), " ".join("<code>%s</code>" % E(r) for r in (e.get("rules") or []) + (e.get("warnings") or [])),
                ("<br><span style='color:var(--muted)'>%s</span>" % E(e["message"][:200])) if e.get("message") and e.get("decision") != "allow" else "")
        activity = ('<section class="col"><span class="num">7 · Activity</span><h2>What the guardrails have done</h2>'
                    '<p>From <code>.cartographer/log/events.jsonl</code>: %d logged decisions since %s (UTC). Print the full log with <code>guard.py --log</code>.</p>'
                    '<table><thead><tr><th>Checkpoint</th><th class="r">Allowed or passed</th><th class="r">Warned</th><th class="r">Blocked</th></tr></thead><tbody>%s</tbody></table>'
                    '<h3>Latest decisions</h3><div class="scroll"><table style="min-width:720px"><thead><tr><th class="r">Time (UTC)</th><th>Where</th><th>Decision</th><th>What</th><th>Rules</th></tr></thead><tbody>%s</tbody></table></div></section>') % (
                        len(events), E((events[0].get("at") or "")[:10]), trows, erows)
    else:
        activity = ""

    sim = carto.read_events(ctx.home, simulated=True)
    simsec = ""
    if sim:
        import datetime as _dt
        from collections import Counter as _C
        weeks = {}
        for e in sim:
            try:
                d = _dt.date.fromisoformat((e.get("at") or "")[:10])
            except ValueError:
                continue
            k = (d - _dt.timedelta(days=d.weekday())).isoformat()
            w = weeks.setdefault(k, {"block": 0, "warn": 0})
            if e.get("decision") in w:
                w[e["decision"]] += 1
        wk = sorted(weeks)
        mx = max([weeks[k]["block"] + weeks[k]["warn"] for k in wk] + [1])
        bw = max(18, min(60, int(860 / max(len(wk), 1)) - 8))
        W = len(wk) * (bw + 8) + 40
        bars = ['<svg viewBox="0 0 %d 190" role="img" aria-label="Simulated blocks and warnings by week" xmlns="http://www.w3.org/2000/svg" style="min-width:%dpx">' % (W, min(W, 900))]
        bars.append('<line x1="20" y1="150" x2="%d" y2="150" stroke="var(--line)"/>' % (W - 10))
        for i, k in enumerate(wk):
            x = 24 + i * (bw + 8)
            b, wn = weeks[k]["block"], weeks[k]["warn"]
            hb, hw = round(b / mx * 120), round(wn / mx * 120)
            bars.append('<rect x="%d" y="%d" width="%d" height="%d" fill="var(--blue)"/>' % (x, 150 - hb, bw, hb))
            bars.append('<rect x="%d" y="%d" width="%d" height="%d" fill="var(--gray-fill)" stroke="var(--blue)" stroke-width="1"/>' % (x, 150 - hb - hw, bw, hw))
            if b + wn:
                bars.append('<text x="%d" y="%d" font-size="11" text-anchor="middle" fill="var(--ink2)">%d</text>' % (x + bw / 2, 150 - hb - hw - 5, b + wn))
            bars.append('<text x="%d" y="168" font-size="10.5" text-anchor="middle" fill="var(--muted)">%s</text>' % (x + bw / 2, E(k[5:])))
        bars.append("</svg>")
        per = _C(r for e in sim for r in (e.get("rules") or []) + (e.get("warnings") or []))
        runs = [e for e in sim if e.get("kind") == "run"]
        prows = "".join("<tr><td><code>%s</code></td><td class=r>%d</td></tr>" % (E(r), n) for r, n in per.most_common())
        ex = [e for e in sim if e.get("decision") == "block"][-12:][::-1]
        exrows = "".join("<tr><td class=r style='white-space:nowrap'>%s</td><td><code>%s</code> %s<br><span style='color:var(--muted)'>%s</span></td><td>%s<br><span style='color:var(--muted)'>%s</span></td></tr>" % (
            E((e.get("at") or "")[:10]), E((e.get("sha") or "")[:9]), E(e.get("subject") or ""), E(e.get("author") or ""),
            " ".join("<code>%s</code>" % E(r) for r in e.get("rules") or []), E((e.get("message") or "")[:160])) for e in ex)
        simsec = ('<section class="figure"><div class="col"><span class="num">8 · Simulated history</span><h2>If these guardrails had been live</h2>'
                  '<p>Every past commit replayed through today\'s rules, dated at the commit: %d commits, %d would have been blocked and %d warned; '
                  'agent-hook rules would have refused %d edits (judged on what each commit added, as if an agent had typed it). '
                  'This is a simulation for discussion: nothing was blocked at the time, some of these changes were made by people or scripts that the rules allow, '
                  'and rules that read today\'s model or facts (ratchet, pinned snippets) cannot be judged in the past.</p></div>'
                  '<div class="scroll">%s</div><p class="caption">Solid blue: would block. Outlined: would warn. Weeks start Monday.</p>'
                  '<div class="col"><h3>Rules that would have fired</h3><table><thead><tr><th>Rule</th><th class="r">Times</th></tr></thead><tbody>%s</tbody></table>'
                  '<h3>Latest commits that would have been blocked</h3><div class="scroll"><table style="min-width:640px"><thead><tr><th class="r">Date</th><th>Commit</th><th>Rule</th></tr></thead><tbody>%s</tbody></table></div></div></section>') % (
                      len(runs), sum(1 for e in runs if e.get("decision") == "block"), sum(1 for e in runs if e.get("decision") == "warn"),
                      sum(1 for e in sim if e.get("kind") == "tool"), "".join(bars), prows, exrows)

    kpis = "".join('<div class="kpi"><span class="v">%s</span><span class="k">%s</span></div>' % (E(str(v)), E(k)) for v, k in [
        (len(rules), "rules enforced by script"), (len(block_rules), "block a change"), (len(edit_rules), "act before the change exists"),
        (len(judgment), "left to judgment"), (len(uncovered_comps) + len(uncovered_risks), "gaps with no guardrail")])

    page = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@500;600&display=swap" rel="stylesheet">
<style>{css}</style></head><body><main class="doc">

<div class="title col"><span class="num">Guardrails report</span><h1>{repo}</h1>
<p>What every change to this repository must pass, how each rule is enforced, what reviewers are responsible for, and where nothing checks. For senior engineers and architects reviewing or adopting the rules.</p>
<div class="meta"><span>{branch} at {head}</span><span>Generated {today}</span><span>Source of truth: <code>.cartographer/guardrails.json</code></span></div></div>

<section class="col"><span class="num">1 · Summary</span><h2>The short version</h2>
<div class="kpis">{kpis}</div>
<ul>{summary}</ul>
<div class="callout"><h3>Decisions needed</h3><ul>{decisions}</ul></div></section>

<section class="figure"><div class="col"><span class="num">2 · Enforcement</span><h2>How a change is checked</h2>
<p>Rules are Python checks over the diff, the import graph and the architecture model. No language model is involved, so the same change always gets the same verdict. A change meets up to four checkpoints. The local ones stop an agent early and cheaply; the CI job is the one that cannot be bypassed, which is why it should be a required check.</p></div>
<div class="scroll">{svg}</div>
<p class="caption">Blue boxes are automated checkpoints, with the number of rules each runs. Black boxes are decisions only a person makes. Dashed blue lines are a blocked change returning to the agent with the fix named in the message; an agent that cannot satisfy a rule after three attempts stops and asks.</p></section>

<section class="figure"><div class="col"><span class="num">3 · Responsibility matrix</span><h2>Which checkpoint enforces each rule</h2>
<p>Read across a row to see where a rule runs. Rules in the agent hook judge how a change is made, so they do not run on the diff: a person or a person's script can still make the same change.</p></div>
<div class="scroll"><table style="min-width:760px"><thead><tr><th>Rule</th><th class="c">Agent hook</th><th class="c">Stop hook</th><th class="c">Pre-commit</th><th class="c">CI</th><th class="c">Person</th></tr></thead><tbody>{matrix}</tbody></table></div>
<div class="legend"><span><span class="dot"></span> Blocks</span><span><span class="dot warn"></span> Warns only</span><span><span class="dot person"></span> Needs a person</span></div></section>

<section class="col"><span class="num">4 · Review</span><h2>What a reviewer is expected to check</h2>
<p>The guardrails remove whole classes of mistake from review. These are what remain with the person approving the pull request.</p>
<ul class="check">{checklist}</ul></section>

<section class="col"><span class="num">5 · Gaps</span><h2>Where there is no guardrail</h2>
<p>Derived from the rules' own selectors, not written by hand. A gap is not necessarily a problem; it is where review carries the whole load.</p>
<h3>Components covered only by repo-wide rules</h3><p>{comps}</p>
<h3>Risks in the model with no rule on the code they cite</h3>
<div class="scroll"><table style="min-width:640px"><thead><tr><th>Severity</th><th>Risk</th><th>Cited code</th></tr></thead><tbody>{gaps}</tbody></table></div>
<h3>Blind spots of the approach</h3>
<ul><li>Dependencies an import scan cannot see: runtime wiring, subprocess calls, HTTP between services and dynamic imports are invisible to the layering and cycle rules.</li>
<li>Correctness. The guardrails check structure and process; behaviour is checked by the repository's own tests and by the reviewer.</li>
<li>Local checkpoints can be skipped (<code>--no-verify</code>, another editor). Only the CI job binds.</li></ul></section>

<section class="col"><span class="num">6 · Evidence</span><h2>What stands behind each rule</h2>
<p>Each rule was replayed against {commits} commits of history. A rule earns its place when the commits it fires on were later fixed or reverted more often than average ({basepct}). Rules that encode a documented decision may never have fired; their basis is listed in the appendix.</p>
<div class="scroll"><table style="min-width:620px"><thead><tr><th>Rule</th><th class="r">Fired</th><th class="r">Later fixed</th><th>Result</th></tr></thead><tbody>{evidence}</tbody></table></div>
<h3>Ratchet</h3><p>Structural counts that may only go down. Raising one takes <code>guard.py --baseline --force</code> in a reviewed commit.</p>
<table><thead><tr><th>Count</th><th class="r">Baseline</th><th class="r">Now</th><th></th></tr></thead><tbody>{ratchet}</tbody></table>
<h3>Exceptions</h3>{exc}</section>

{activity}
{simsec}
<section class="col"><span class="num">Appendix</span><h2>Rule definitions</h2>
<p>Each rule exactly as enforced. Change rules in <code>.cartographer/guardrails.json</code>; agents propose changes in <code>guardrails.proposed.json</code> and a person decides them in the rules workbench, <code>guard.py --serve</code>.</p>
{appendix}</section>

<footer>Generated by Codebase Cartographer with <code>guard.py --export</code>. Regenerate after any rule change; the nightly workflow publishes a fresh copy.</footer>
</main></body></html>""".format(
        title=E("%s Guardrails Report" % repo), css=CSS, repo=E(repo), branch=E(branch), head=E(head), today=ctx.today.isoformat(),
        kpis=kpis, summary="".join("<li>%s</li>" % s for s in summary_points), decisions="".join("<li>%s</li>" % d for d in decisions),
        svg=diagram(len(edit_rules) - len(cmd_rules), len(cmd_rules), len(local_diff), len(diff_rules)),
        matrix=mrows, checklist="".join("<li><span>%s</span></li>" % c for c in checklist),
        comps=comp_list, gaps=gap_rows or '<tr><td colspan="3">Every risk in the model has at least one rule on the code it cites.</td></tr>',
        commits=window.get("commits", 0), basepct=("%d%% here" % round(100.0 * fixed_base / window["commits"])) if window.get("commits") else "no history replayed",
        activity=activity, simsec=simsec, evidence=ev_rows, ratchet=ratchet_rows or '<tr><td colspan="4">No baseline recorded.</td></tr>', exc=exc_html, appendix=appendix)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(page)
    return {"rules": len(rules), "human": len(checklist), "none": len(uncovered_comps) + len(uncovered_risks)}
