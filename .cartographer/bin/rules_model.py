#!/usr/bin/env python3
"""The "How it works" tab of the rules page: a mental model of the fence for this repository.

Four parts, each drawn from the repository's own files (no numbers are typed in):
  1. Where each check runs: the agent's machine, GitHub Actions, the private hidden-tests repository, a person;
     in the order a change meets them.
  2. What each part catches, by kind of problem, with the checks this repository actually runs.
  3. How a rule changes: proposed, replayed on history, decided, reviewed, in force; and how exceptions work.
  4. What nothing here checks.
The diagrams are inline SVG styled with Carbon's color tokens, so they follow the light and dark themes.
"""
from __future__ import annotations

import html

W = 1160


def e(x) -> str:
    return html.escape(str(x), quote=True)


def text(x, y, lines, cls="sf-d-t", first_cls="sf-d-tb", lh=17):
    out = []
    for i, line in enumerate(lines):
        c = first_cls if (i == 0 and first_cls) else cls
        out.append(f'<text class="{c}" x="{x}" y="{y + i * lh}">{e(line)}</text>')
    return "".join(out)


def box(x, y, w, h, lines, kind="box"):
    """A stage in the diagram. kind: box (a check), decide (where a script decides), person (where a person decides)."""
    cls = {"box": "sf-d-box", "decide": "sf-d-box sf-d-box--decide", "person": "sf-d-box sf-d-box--person"}[kind]
    tcls, bcls = ("sf-d-t sf-d-t--on", "sf-d-tb sf-d-t--on") if kind == "decide" else ("sf-d-t", "sf-d-tb")
    return f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}"/>' + text(x + 12, y + 22, lines, tcls, bcls)


def arrow(d, label=None, lx=0, ly=0, dashed=False):
    a = f'<path class="sf-d-arrow{" sf-d-arrow--dashed" if dashed else ""}" d="{d}" marker-end="url(#sf-ar)"/>'
    if label:
        a += f'<text class="sf-d-label" x="{lx}" y="{ly}">{e(label)}</text>'
    return a


def svg(h, body, label):
    return (f'<div class="sf-diagram" role="img" aria-label="{e(label)}"><svg viewBox="0 0 {W} {h}" width="{W}" height="{h}" '
            'xmlns="http://www.w3.org/2000/svg"><defs><marker id="sf-ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path class="sf-d-head" d="M0,0 L10,5 L0,10 z"/></marker></defs>' + body + "</svg></div>")


def where_it_runs(d):
    """Diagram 1: lanes (where) by columns (when)."""
    col_x = [186 + i * 196 for i in range(5)]
    cw = 160
    lanes = [("Agent's machine", "early feedback; trust does", "not depend on it", 50, 112),
             ("GitHub Actions", "required checks on the", "pull request decide", 170, 176),
             ("Private repository", "hidden tests the author", "cannot see", 354, 112),
             ("People", "own the rules; decide", "what no script can", 474, 112)]
    cols = ["1  While editing", "2  Before commit", "3  On the pull request", "4  When a check fails", "5  Merge"]
    B = []
    for i, c in enumerate(cols):
        B.append(f'<text class="sf-d-col" x="{col_x[i]}" y="26">{e(c)}</text>')
    for name, s1, s2, y, h in lanes:
        B.append(f'<rect class="sf-d-lane" x="0" y="{y}" width="{W}" height="{h}"/>')
        B.append(text(14, y + 26, [name, s1, s2], "sf-d-t sf-d-muted", "sf-d-tb"))
    rules_line = f"{d['n_rules']} in force" if d["n_rules"] else "(none accepted yet)"
    cats = d["categories"]
    cat_lines = [", ".join(cats[i:i + 2]) + ("," if i + 2 < len(cats) else "") for i in range(0, len(cats), 2)]
    x0, x1, x2, x3, x4 = col_x
    # Lane 1: agent's machine
    B.append(box(x0, 62, cw, 88, ["Edit hook", "refuses edits to the", "rule files and to", "protected paths"]))
    B.append(box(x1, 62, cw, 88, ["Stop hook, pre-commit", "checks the guard rules", rules_line]))
    # Lane 2: GitHub Actions
    B.append(box(x2, 182, cw, 96, [f"Fence: {d['n_checks']} checks", *cat_lines[:3]], "decide"))
    B.append(box(x2, 288, cw, 52, ["Guardrails", "guard rules, ratchet"], "decide"))
    B.append(box(x3, 182, cw, 96, ["Gardener", "runs the fixers,", "checks everything again,", "commits only if nothing", "that passed now fails"]))
    # Lane 3: private repository
    B.append(box(x2, 366, cw, 88, ["Hidden tests", "only requirement titles", "reach the pull request"], "decide"))
    B.append(box(x3, 366, cw, 88, ["Referee", "turns a failure into a", "hint by requirement,", "never the test itself"]))
    # Lane 4: people
    B.append(box(x3, 486, cw, 88, ["Decide what is left", "change the code, the", "request or the fence,", "or record an exception"], "person"))
    B.append(box(x4, 486, cw, 88, ["Review and merge", "the code owner reviews", "what no check covers"], "person"))
    # Arrows
    B.append(arrow(f"M{x0 + cw} 106 H{x1 - 4}"))
    B.append(arrow(f"M{x1 + cw} 106 H{x1 + cw + 18} V222 H{x2 - 4}"))
    B.append(arrow(f"M{x1 + cw + 18} 222 V406 H{x2 - 4}"))
    B.append(f'<text class="sf-d-label" x="{x1 + cw + 24}" y="166">push</text>')
    B.append(arrow(f"M{x2 + cw} 206 H{x3 - 4}"))
    B.append(f'<text class="sf-d-label" x="{x2 + cw + 4}" y="199">fails</text>')
    B.append(arrow(f"M{x3 + 24} 278 V283 H{x2 + cw + 4}"))
    B.append(f'<text class="sf-d-label" x="{x3 + 30}" y="300">fixed: every check runs again</text>')
    B.append(arrow(f"M{x3 + cw} 230 H{x3 + cw + 18} V530 H{x3 + cw + 4}"))
    B.append(f'<text class="sf-d-label" x="{x3 + cw + 24}" y="330">cannot</text><text class="sf-d-label" x="{x3 + cw + 24}" y="345">fix it</text>')
    B.append(arrow(f"M{x2 + cw} 410 H{x3 - 4}"))
    B.append(f'<text class="sf-d-label" x="{x2 + cw + 4}" y="403">fails</text>')
    B.append(arrow(f"M{x3 + 40} 454 V482"))
    B.append(arrow(f"M{x2 + cw / 2} 454 V470 H{x4 + cw / 2} V482"))
    B.append(f'<text class="sf-d-label" x="{x4 + 4}" y="464">when every check passes</text>')
    B.append(arrow(f"M{x3 + 12} 574 V592 H{x0 + cw / 2} V154", dashed=True))
    B.append(f'<text class="sf-d-label" x="{x0 + cw / 2 + 6}" y="606">back to the change</text>')
    legend = (f'<rect class="sf-d-box sf-d-box--decide" x="{x0}" y="618" width="14" height="14"/><text class="sf-d-label" x="{x0 + 20}" y="630">a script decides</text>'
              f'<rect class="sf-d-box sf-d-box--person" x="{x0 + 140}" y="618" width="14" height="14"/><text class="sf-d-label" x="{x0 + 160}" y="630">a person decides</text>'
              f'<rect class="sf-d-box" x="{x0 + 280}" y="618" width="14" height="14"/><text class="sf-d-label" x="{x0 + 300}" y="630">feedback or a fix, never the verdict</text>')
    return svg(642, "".join(B) + legend, "Where each check runs: edit and stop hooks on the agent's machine give early feedback; on the pull request the fence, "
               "guardrails and hidden tests decide; the gardener fixes what a script can and every check runs again; what is left goes to a person, "
               "who changes the code, the request or the fence, or records an exception; the code owner reviews and merges.")


def how_rules_change(d):
    """Diagram 2: the life of a rule."""
    xs = [0, 234, 468, 702, 936]
    w = 210
    commits = d["replay_commits"]
    stages = [(["Proposed", "by an agent, or guard.py", "--suggest from the map", "and history (nightly)"], "box"),
              (["Replayed on history", f"each proposal runs over the", f"last {commits} commits:" if commits else "repository's commits:", "would it have fired?"], "box"),
              (["Accept or Reject", "a person presses one on", "this page and says why", "(a GitHub issue)"], "person"),
              (["Pull request", "a workflow applies it;", "the code owner reviews", "and merges"], "person"),
              (["In force", "edit hook, Stop hook,", "pre-commit and CI all", "check it on every change"], "decide")]
    B = []
    for i, (lines, kind) in enumerate(stages):
        B.append(box(xs[i] + 4, 10, w, 96, lines, kind))
        if i:
            B.append(arrow(f"M{xs[i - 1] + 4 + w} 58 H{xs[i]}"))
    B.append(text(4, 140, ["Rejected rules are remembered, so they are not proposed again.",
                           "An exception lets one file break one rule for a while: it needs a reason, a named approver and an expiry date,",
                           "and the fence fails the day it expires. Agents cannot edit the rule files; CODEOWNERS sends every change to them to a person."],
                  "sf-d-t sf-d-muted", None, 20))
    return svg(210, "".join(B), "How a rule changes: proposed by an agent or guard.py, replayed on history, accepted or rejected by a person "
               "with a reason, applied in a pull request the code owner merges, then enforced by the hooks and CI.")


def catches(d, table, tag):
    """What each part catches, by kind of problem, with the checks this repository runs."""
    by_cat = {}
    for c in d["checks"]:
        by_cat.setdefault(c["category"], []).append(c)
    nice = {"style": "Formatting and style", "practices": "Code quality rules", "patterns": "Repeated code, new packages, unmapped code",
            "security": "Secrets and unsafe CI workflows", "performance": "Performance rules", "structure": "Architecture and process rules",
            "tests": "Code that does not compile, breaks a test, or breaks a formal model", "database": "Database changes"}
    rows = []
    for cat in d["categories"]:
        cs = by_cat.get(cat, [])
        names = ", ".join(f'<span class="sf-id">{e(c["id"])}</span>' for c in cs)
        fix = "the gardener fixes it" if any(c["fixer"] for c in cs) else "the author fixes it, or a person decides the rule is wrong"
        rows.append([e(nice.get(cat, cat)), names, "Fence", fix])
    rows.append(["Imports that break the layers, cycles, files that must change together",
                 f'guard rules: {d["n_rules"]} in force, {d["n_props"]} proposed; run by the fence\'s guard check and the guardrails job', "Guard rules", "the author fixes it, or a person decides the rule is wrong"])
    rows.append(["Behaviour that does not meet the requirements, even when every visible test passes",
                 "tests in a private repository", "Hidden tests", "the author fixes it from the requirement hint"])
    rows.append(["Whether it is the right change, design fit, cases no test covers",
                 f'{d["n_judgment"]} written rules for reviewers' if d["n_judgment"] else "the review contract on each pull request",
                 "A person", "the code owner"])
    return table("catches", "What each part catches",
                 "Each kind of problem has one owner. Scripts decide everything a script can decide, so the reviewer's time goes to the last row.",
                 ["Problem", "Checked by", "Part", "When it fails"], rows)


def not_covered(d):
    items = ["No check starts the application and uses it. Behaviour is covered by the repository's tests and the hidden tests only."]
    if not d["has_package_json"]:
        items.append("New dependencies are not checked here: the dependency check reads package.json, and this repository has none.")
    if not d["n_rules"]:
        items.append("No guard rule is in force yet, so architecture is not enforced: an import that breaks the layers passes the fence.")
    if not d["has_verification"]:
        items.append("No formal model: there is no verification/ folder.")
    if not d["hidden_workflow"]:
        items.append("No hidden tests workflow is installed.")
    else:
        items.append("Hidden tests run on a pull request only when the HIDDEN_TESTS_TOKEN secret is set; without it the job passes without running them.")
    return ('<div class="sf-block"><h2 class="sf-h2">What nothing here checks</h2><ul class="sf-list">'
            + "".join(f"<li>{e(i)}</li>" for i in items) + "</ul></div>")


def section(d, table, tag):
    return ('<div class="sf-block"><h2 class="sf-h2">Where each check runs</h2>'
            '<p class="sf-lede">A change meets these in order, left to right. Only the checks on the pull request decide whether it can merge; '
            'they run in GitHub Actions, outside any agent. Everything on the agent\'s machine is early feedback.</p>'
            + where_it_runs(d) + '<p class="sf-cap">The diagram scrolls sideways on a narrow screen.</p></div>'
            + catches(d, table, tag)
            + '<div class="sf-block"><h2 class="sf-h2">How a rule changes</h2>'
            '<p class="sf-lede">Scripts apply the rules; people own them. A rule reaches the scripts only through a pull request a code owner merges.</p>'
            + how_rules_change(d) + "</div>"
            + not_covered(d))


CSS = """
.sf-block{margin-bottom:2.5rem}.sf-h2{font-size:1.25rem;line-height:1.4;font-weight:400;margin:0 0 .5rem}
.sf-lede{font-size:.875rem;line-height:1.43;color:var(--cds-text-secondary);max-width:48rem;margin:0 0 1rem}
.sf-cap{font-size:.75rem;color:var(--cds-text-secondary);margin:.5rem 0 0}@media (min-width:72rem){.sf-cap{display:none}}
.sf-diagram{overflow-x:auto;background:var(--cds-layer-01);padding:1rem}.sf-diagram svg{display:block;max-width:none}
@media (min-width:72rem){.sf-diagram svg{width:100%;height:auto}}
.sf-d-lane{fill:var(--cds-layer-02)}.sf-d-col{font-size:13px;font-weight:600;fill:var(--cds-text-secondary)}
.sf-d-box{fill:var(--cds-layer-01);stroke:var(--cds-border-strong-01);stroke-width:1}
.sf-d-box--decide{fill:var(--cds-interactive);stroke:var(--cds-interactive)}
.sf-d-box--person{fill:var(--cds-layer-01);stroke:var(--cds-text-primary);stroke-width:2}
.sf-d-t{font-size:12.5px;fill:var(--cds-text-primary)}.sf-d-tb{font-size:13px;font-weight:600;fill:var(--cds-text-primary)}
.sf-d-t--on{fill:var(--cds-text-on-color)}.sf-d-muted{fill:var(--cds-text-secondary)}
.sf-d-label{font-size:12px;fill:var(--cds-text-secondary)}
.sf-d-arrow{fill:none;stroke:var(--cds-text-secondary);stroke-width:1.4}.sf-d-arrow--dashed{stroke-dasharray:5 4}.sf-d-head{fill:var(--cds-text-secondary)}
.sf-list{margin:0;padding-left:1.25rem;font-size:.875rem;line-height:1.6;max-width:56rem}.sf-list li{list-style:disc;margin-bottom:.25rem}
"""
