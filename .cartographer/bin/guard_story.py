#!/usr/bin/env python3
"""Fence and review: one page that shows how changes are kept safe and what is left for a person to review.

  python3 .cartographer/bin/guard.py --story fence.html

Part 1, how it works: where every guardrail and verification sits from idea to merged pull request, and the
loop that improves them, both drawn; then the turns of that loop this repository has taken.
Part 2, what it does to review: what a reviewer no longer reviews, reviews less, and still reviews, with the
questions answered by checks against those left for a person, the rework avoided, and runs that show it: real ones (live hooks, the
verifier, an agent's pull request, CI) and ones replayed on the repository's history, each labelled.

Inputs: .cartographer/outcomes.json (timeModel, runs, cannotBlock, missed), guardrails.json, the logs, the
history and saved run evidence under .cartographer/runs/. No hours are shown until review time is measured.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os

import carto
import guard
import guard_export
import guard_outcomes as go

E = html.escape

CSS = go.CSS + """
.part{border-top:2px solid var(--ink);padding-top:18px;display:flex;flex-direction:column;gap:6px}
.part .num{font-size:12px}
.part h2{font-size:28px}
.loop{display:flex;flex-direction:column;gap:10px}
.run{border:1px solid var(--line);border-radius:6px;padding:16px;display:flex;flex-direction:column;gap:10px}
.run .hd{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:baseline}
.run h3{font-family:var(--serif);font-size:17px}
.tag.real{background:var(--blue);color:var(--bg)}
.tag.replay{background:transparent;border:1px dashed var(--ink);color:var(--ink)}
pre.log{margin:0;background:var(--subtle);border-radius:4px;padding:10px 12px;font:12px/1.55 var(--mono);color:var(--ink2);overflow-x:auto;white-space:pre}
.shots{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.shots figure{margin:0;display:flex;flex-direction:column;gap:6px}
.shots img{width:100%;height:auto;border:1px solid var(--line);border-radius:4px;display:block}
.shots figcaption{font-size:12px;color:var(--muted)}
.bucket{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}
.bucket>div{border-top:4px solid var(--blue);padding-top:10px;display:flex;flex-direction:column;gap:8px;min-width:0}
.bucket>div.less{border-top-color:var(--ink2)}
.bucket>div.still{border-top-color:var(--ink)}
.bucket h3{font-size:15px}
.bucket ul{font-size:13.5px;gap:4px}
.big{font-size:32px;font-weight:600;line-height:1;font-variant-numeric:tabular-nums}
.vs{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:16px 0}
.vs>div{display:flex;flex-direction:column;gap:4px}
.vs .k{font-size:12.5px;color:var(--muted)}
.vs .arrow{font-size:20px;color:var(--muted)}
details summary{cursor:pointer;font-size:13px;color:var(--muted)}
@media (max-width:760px){.bucket,.shots{grid-template-columns:1fr}}
"""


def _png(path):
    try:
        with open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    except OSError:
        return None


def _h(minutes):
    return "%.1f h" % (minutes / 60.0) if minutes >= 90 else "%d min" % round(minutes)


def loop_svg(steps, ret):
    """The improvement loop as a row of stations with a return arrow underneath (never a ring)."""
    W, n = 860, len(steps)
    gap = 14
    bw = (W - gap * (n - 1)) / n
    H = 178
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s">' % (W, H, E("The improvement loop: " + " then ".join(s[0] for s in steps) + ", then back to the start")),
           '<defs><marker id="lpa" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M2 1L8 5L2 9" fill="none" stroke="var(--ink)" stroke-width="1.5"/></marker></defs>']
    for i, (title, lines, kind) in enumerate(steps):
        x = i * (bw + gap)
        if kind == "person":
            box, fg = 'fill="var(--bg)" stroke="var(--ink)" stroke-dasharray="5 3"', "var(--ink)"
        elif kind == "machine":
            box, fg = 'fill="var(--blue)" stroke="var(--blue)"', "var(--bg)"
        else:
            box, fg = 'fill="var(--subtle)" stroke="var(--line)"', "var(--ink)"
        out.append('<rect x="%.1f" y="8" width="%.1f" height="96" rx="6" %s/>' % (x + 0.5, bw - 1, box))
        out.append('<text x="%.1f" y="32" font-size="13.5" font-weight="600" fill="%s" font-family="var(--sans)">%s</text>' % (x + 10, fg, E(title)))
        for j, ln in enumerate(lines):
            out.append('<text x="%.1f" y="%d" font-size="11.5" fill="%s" opacity=".9" font-family="var(--sans)">%s</text>' % (x + 10, 52 + j * 15, fg, E(ln)))
        if i < n - 1:
            out.append('<line x1="%.1f" y1="56" x2="%.1f" y2="56" stroke="var(--ink)" stroke-width="1.2" marker-end="url(#lpa)"/>' % (x + bw + 1, x + bw + gap - 1))
    last = (n - 1) * (bw + gap) + bw / 2
    first = bw / 2
    out.append('<path d="M%.1f 104 V140 H%.1f V108" fill="none" stroke="var(--ink)" stroke-width="1.2" marker-end="url(#lpa)"/>' % (last, first))
    out.append('<text x="%.1f" y="160" font-size="12.5" fill="var(--ink2)" text-anchor="middle" font-family="var(--sans)">%s</text>' % (W / 2, E(ret)))
    out.append("</svg>")
    return "".join(out)


def minutes_svg(rows, w=860):
    """Per commit, oldest left: the whole column is the estimated review time by eye; black is what is left."""
    n = len(rows)
    if not n:
        return ""
    mx = max([r["before"] for r in rows] + [1])
    gap, gutter = 2.0, 52
    cw = max(3.0, (w - gutter - gap * (n - 1)) / n)
    plot = cw * n + gap * (n - 1)
    w = plot + gutter
    H = 160
    out = ['<svg viewBox="0 0 %.1f %d" role="img" aria-label="Estimated review minutes per commit, before and after the fence">' % (w, H + 22)]
    for k in (30, 60, 90, 120, 180):
        if k <= mx:
            y = H - H * k / mx
            out.append('<line x1="0" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--line)" stroke-width="1"/><text x="%.1f" y="%.1f" font-size="10.5" fill="var(--muted)" font-family="var(--sans)">%d min</text>' % (y, plot + 4, y, plot + 8, y + 3.5, k))
    for i, r in enumerate(rows):
        x = i * (cw + gap)
        ha, hb = H * r["after"] / mx, H * r["before"] / mx
        if hb:
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="var(--blue)"><title>%s: %s by eye</title></rect>' % (x, H - hb, cw, hb, E(r["subject"][:60]), _h(r["before"])))
        if ha:
            out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="var(--ink)"><title>%s: %s left for a person</title></rect>' % (x, H - ha, cw, ha, E(r["subject"][:60]), _h(r["after"])))
    out.append('<line x1="0" y1="%d" x2="%.1f" y2="%d" stroke="var(--ink)" stroke-width="1"/>' % (H, plot, H))
    out.append('<text x="0" y="%d" font-size="11" fill="var(--muted)" font-family="var(--sans)">oldest commit</text><text x="%.1f" y="%d" font-size="11" fill="var(--muted)" text-anchor="end" font-family="var(--sans)">newest</text>' % (H + 16, plot, H + 16))
    out.append("</svg>")
    return "".join(out)


def story(ctx, out_path):
    d = go.compute(ctx)
    oc, commits = d["oc"], d["commits"]
    tm = oc.get("timeModel") or {}
    rules = ctx.rules
    rq = oc.get("reviewerQuestions") or {}
    cannot = oc.get("cannotBlock") or []
    person_tm = tm.get("person") or {}
    rule_min = tm.get("rules") or {}
    checks = [(c, guard.Selector(c["appliesTo"], ctx)) for c in tm.get("checks", [])]
    repo = (ctx.model.get("repo") or {}).get("name") or os.path.basename(ctx.repo.rstrip("/"))
    sim = d["sim"]
    runs_sim = [e for e in sim if e.get("kind") == "run"]
    edits_sim = [e for e in sim if e.get("source") == "edit-hook"]
    sent_back = {e["sha"] for e in runs_sim if e.get("decision") == "block"} | {e["sha"] for e in edits_sim}

    # ------------------------------------------------------------------ the numbers
    def assess(cs):
        base = go.review_load(ctx, [cs], rules, cannot, oc.get("skipPaths"))[0]
        chk = [c for c, sel in checks if any(sel(f.path) for f in cs.files)]
        before = sum(rule_min.get(r["id"], 2) for r in base["auto"]) + sum(c.get("minutes", 3) for c in chk) + \
            sum((person_tm.get(x["text"]) or {}).get("minutes", 5) for x in base["person"])
        after = sum((person_tm.get(x["text"]) or {}).get("withHelp", (person_tm.get(x["text"]) or {}).get("minutes", 5)) for x in base["person"])
        return dict(base, checks=chk, before=before, after=after, subject=getattr(cs, "subject", "") or "", sha=(cs.sha or "")[:7])

    rows = [assess(c) for c in commits]
    active = [r for r in rows if r["auto"] or r["checks"] or r["person"]]
    q_before = sum(len(r["auto"]) + len(r["checks"]) + len(r["person"]) for r in active)
    q_after = sum(len(r["person"]) for r in active)
    m_before = sum(r["before"] for r in active)
    m_after = sum(r["after"] for r in active)
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0
    re_min = tm.get("reReviewMinutes", 15)
    round_trips = len(sent_back)
    saved = m_before - m_after + round_trips * re_min

    # per-topic tallies for the review topics section
    topic_rule = {}
    for r in active:
        for x in r["auto"]:
            topic_rule.setdefault(x["id"], [x, 0])[1] += 1
    topic_check = {}
    for r in active:
        for c in r["checks"]:
            topic_check.setdefault(c["id"], [c, 0])[1] += 1
    topic_person = {}
    for r in active:
        for x in r["person"]:
            topic_person.setdefault(x["text"], [x, 0])[1] += 1

    S = []
    S.append('<header class="title"><span class="num">%s &middot; fence and review</span><h1>Safe changes, short reviews</h1>'
             '<p>How every change to %s is checked from the first idea to the merged pull request, how those checks get better, '
             'and what that leaves for a person to review. Runs marked <span class="tag real">real</span> happened; runs marked '
             '<span class="tag replay">replayed</span> are the repository\'s own history judged by today\'s checks.</p>'
             '<div class="meta"><span>%d commits in history, %s to %s</span><span>Head <code>%s</code></span><span>Generated %s</span></div></header>' % (
                 E(repo), E(repo), len(commits), E(commits[0].date if commits else "?"), E(commits[-1].date if commits else "?"), E(d["head"]), dt.date.today().isoformat()))
    # rework: fix commits for problems a check now catches
    covered = [m for m in d["missed"] if m.get("ruleState") == "active" or m.get("status") in ("built", "covered")]
    fix_shas = sorted({m["fixed"] for m in covered if m.get("fixed")})
    fix_files = fix_lines = 0
    for sha in fix_shas:
        c = next((c for c in commits if c.sha.startswith(sha)), None)
        if c is not None:
            fix_files += len(c.files)
            fix_lines += sum(f.add + f.dele for f in c.files)
    first_verify = next((r for r in oc.get("runs", []) if r.get("report")), None)
    vrep = carto.load(os.path.join(ctx.repo, first_verify["report"])) if first_verify else None
    dead = {(r["path"], p["detail"]) for r in (vrep or {}).get("results", []) for p in r.get("problems", [])}

    kpis = [("%d%%" % round(100 * (1 - q_after / q_before)) if q_before else "0", "of review questions answered by a check before anyone reviews (%d of %d)" % (q_before - q_after, q_before)),
            (str(round_trips), "of %d past commits stopped before review" % len(commits)),
            ("%d of %d" % (len(covered), len(d["missed"])), "problems that got past review are now caught by a check")]
    if dead:
        kpis.append((str(len(dead)), "broken links on %d pages found by the verifier's first run" % len({p for p, _ in dead})))
    S.append('<section class="col"><div class="kpis">%s</div></section>' % "".join(
        '<div class="kpi"><span class="v">%s</span><span class="k">%s</span></div>' % (E(v), E(k)) for v, k in kpis))

    # ------------------------------------------------------------------ part 1
    S.append('<section class="part"><span class="num">Part 1</span><h2>How it works</h2></section>')
    rules_by = lambda pred: [r for r in rules if pred(r)]
    edit_rules = rules_by(lambda r: "edit" in guard.enforced_at(r) and r["type"] != "forbid-command")
    cmd_rules = rules_by(lambda r: r["type"] == "forbid-command")
    diff_rules = rules_by(lambda r: "diff" in guard.enforced_at(r) and r["type"] != "ratchet" and guard_export._sev(r) != "info")
    ci_rules = rules_by(lambda r: r["type"] == "ratchet")
    info_rules = rules_by(lambda r: guard_export._sev(r) == "info")
    q = lambda r: rq.get(r["id"]) or r.get("description", "")
    groups = [
        ("Before the change exists: the agent's edit and command hooks", [(r["id"], q(r)) for r in edit_rules + cmd_rules]),
        ("On every change: Stop hook, pre-commit and CI", [(r["id"], q(r)) for r in diff_rules]),
        ("On every pull request, in CI", [(r["id"], q(r)) for r in ci_rules] + [(c.get("by", c["id"]), c["label"] + "?") for c, _ in checks]),
        ("Notes for the reviewer, never a block", [(r["id"], q(r)) for r in info_rules] + list((oc.get("notes") or {}).items())),
        ("The agent proves its own work before it says done", list((oc.get("agentChecks") or {}).items())),
    ]
    n_checks = sum(len(g[1]) for g in groups)
    S.append('<section class="sec"><div class="lead"><span class="num">1.1</span><h2>The fence: %d checks</h2>'
             '<p>Every check, grouped by when it runs, with the question it answers so a reviewer does not have to.</p></div>'
             '<div class="scroll"><table><thead><tr><th>Check</th><th>The question it answers</th></tr></thead><tbody>%s</tbody></table></div></section>' % (
                 n_checks, "".join('<tr class="grp"><td colspan="2">%s (%d)</td></tr>%s' % (E(g), len(items), "".join(
                     "<tr><td><code>%s</code></td><td>%s</td></tr>" % (E(a), E(b)) for a, b in items)) for g, items in groups if items)))

    wf = oc.get("workflow") or {}
    wrows = [
        ("I have an idea", "Plan it with the codebase map", "Map and rule lookup", "guard.py --explain lists the rules for the files you will touch", "info"),
        ("Agent edits a file", "Edit or Write", "Edit hook, before it is written", wf.get("edit") or "%d rules refuse the edit" % len(edit_rules), "machine"),
        ("Agent runs a command", "Shell", "Command hook", wf.get("command") or "%d rule(s), and shell writes to protected files" % len(cmd_rules), "machine"),
        ("Agent finishes a step", "End of a turn", "Stop hook", "%d rules on the whole diff; the agent keeps working until they pass" % len(diff_rules), "machine"),
        ("Agent proves it works", "Before it says done", "/verify-site", "Builds the site, opens the pages in a browser, hands back screenshots", "verify"),
        ("Agent changed code", "Scripts, libraries, app", "/second-review", "A model of another family reviews the diff; nothing applied automatically", "verify"),
        ("git commit", "You or the agent", "Pre-commit", "The same %d rules on the staged files" % len(diff_rules), "machine"),
        ("Push and open a PR", "GitHub", "CI: guardrails, Checks and verify jobs", "Every rule, the ratchet, linter, tests, types, build leak check, draft rules;\npages in a browser, screenshots against the base, cited links, claims note", "machine"),
        ("Review", "A person reads it", "A person judges", "%d things no script decides, with notes pointing the way" % len(cannot), "person"),
        ("PR merged", wf.get("mergeSub") or "Deploys to readers", "Required checks on main", "A GitHub setting a person turns on once", "person"),
    ]
    S.append('<section class="sec"><div class="lead"><span class="num">1.2</span><h2>From idea to merged pull request</h2>'
             '<p>Blue boxes stop a change on their own. Outlined boxes are the agent proving its own work. Only the dashed boxes need a person.</p></div>'
             '<div class="figure"><div class="scroll">%s</div></div></section>' % go.workflow_svg(wrows))

    loop_steps = [
        ("Something slips", ["A reviewer, the verifier,", "an agent that keeps", "hitting a rule, or the", "nightly replay finds it"], "info"),
        ("Find the cause", ["cartograph-learn finds", "the commit that", "caused it"], "info"),
        ("Write the check", ["A rule, a test or a", "verifier check, at the", "highest level that", "works"], "info"),
        ("Prove it", ["Fires at the cause,", "quiet after the fix,", "clean today, rare", "elsewhere"], "machine"),
        ("A person decides", ["guard.py --accept or", "--reject; exceptions", "need a reason, expiry", "and approver"], "person"),
        ("In force", ["Runs in the hooks,", "pre-commit and CI", "on every change"], "machine"),
    ]
    S.append('<section class="sec"><div class="lead"><span class="num">1.3</span><h2>How the checks get better</h2>'
             '<p>A check goes into force only after it is proven on the real mistake and a person accepts it. One that misfires is narrowed, turned into a note, or removed.</p></div>'
             '<div class="figure"><div class="scroll">%s</div></div>' % loop_svg(loop_steps, "Measure and prune: noisy checks become notes or go. Then the next problem."))
    turns = oc.get("loopTurns") or []
    if turns:
        S.append('<details><summary>Turns of the loop in this repository (%d)</summary><div class="scroll"><table><thead><tr><th>When</th><th>What slipped or misfired</th><th>Found by</th><th>What changed</th></tr></thead><tbody>%s</tbody></table></div></details>' % (len(turns), "".join(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (E(t["when"]), E(t["what"]), E(t["by"]), E(t["change"])) for t in turns)))
    S.append("</section>")

    # ------------------------------------------------------------------ part 2
    S.append('<section class="part"><span class="num">Part 2</span><h2>What it does to review</h2></section>')
    dont = sorted([(rq.get(k) or v[0].get("description", ""), v[1]) for k, v in topic_rule.items()] +
                  [(v[0]["label"], v[1]) for v in topic_check.values()], key=lambda t: -t[1])
    helped = lambda t: (person_tm.get(t) or {}).get("help")
    less = [(t, v[1], person_tm.get(t) or {}) for t, v in topic_person.items() if helped(t)]
    still = [(t, v[1], v[0]) for t, v in topic_person.items() if not helped(t)]
    S.append('<section class="sec"><div class="lead"><span class="num">2.1</span><h2>Review topics</h2>'
             '<p>Each topic with the number of the %d commits it applied to.</p></div>'
             '<div class="bucket"><div><h3>Don\'t review: a check answers it</h3><ul>%s</ul>%s</div>'
             '<div class="less"><h3>Review less: a check points the way</h3><ul>%s</ul></div>'
             '<div class="still"><h3>Still review</h3><ul>%s</ul></div></div></section>' % (
                 len(active),
                 "".join("<li>%s <span class=\"caption\">%d</span></li>" % (E(t), c) for t, c in dont[:10]),
                 ('<details><summary>%d more</summary><ul>%s</ul></details>' % (len(dont) - 10, "".join("<li>%s <span class=\"caption\">%d</span></li>" % (E(t), c) for t, c in dont[10:]))) if len(dont) > 10 else "",
                 "".join("<li>%s <span class=\"caption\">%d; helped by %s</span></li>" % (E(t), c, E(p.get("help", ""))) for t, c, p in less),
                 "".join("<li>%s <span class=\"caption\">%d</span></li>" % (E(t), c) for t, c, x in still)))

    qrows = [{"auto": r["auto"] + r["checks"], "person": r["person"], "change": type("C", (), {"subject": r["subject"]})()} for r in active]
    S.append('<section class="sec"><div class="lead"><span class="num">2.2</span><h2>Effort</h2>'
             '<p>Nobody reads every one of these questions on every commit. The history shows what happened instead: most were never asked, and the '
             'problems that slipped through cost a separate review and rework later. The checks ask all of them, every time, before a person looks.</p></div>'
             '<div class="vs"><div><span class="k">Questions answered by a check</span><span class="big">%d of %d</span></div>'
             '<div><span class="k">Left for a person, median commit</span><span class="big">%d of %d</span></div>'
             '<div><span class="k">Commits stopped before review</span><span class="big">%d</span></div>'
             '<div><span class="k">Rework now avoided</span><span class="big">%d files</span><span class="k">in %d fix commits (%s) for problems a check now catches</span></div></div>'
             '<div class="scroll">%s</div><p class="caption">Questions per commit, oldest left. Blue: answered by a check. Black: left for a person.</p>'
             '<p class="caption">Review time is not measured yet, so this page shows no hours. %s</p></section>' % (
                 q_before - q_after, q_before, med([len(r["person"]) for r in active]), med([len(r["auto"]) + len(r["checks"]) + len(r["person"]) for r in active]),
                 round_trips, fix_files, len(fix_shas), ", ".join(fix_shas), go.load_svg(qrows),
                 E(oc.get("timeNote") or "Once pull requests run through the fence, GitHub's review timestamps give the real before and after.")))

    # ------------------------------------------------------------------ runs
    def run_card(run):
        tag = '<span class="tag real">real</span>' if run.get("real") else '<span class="tag replay">replayed</span>'
        body = []
        if run.get("source") == "events":
            if run.get("task"):
                try:
                    with open(os.path.join(ctx.repo, run["task"]), encoding="utf-8") as fh:
                        body.append('<p><b>Asked:</b> %s</p>' % E(fh.read().strip()[:700]))
                except OSError:
                    pass
            evs = []
            if run.get("log"):
                try:
                    with open(os.path.join(ctx.repo, run["log"]), encoding="utf-8") as fh:
                        evs = [json.loads(l) for l in fh if l.strip()]
                except (OSError, ValueError):
                    evs = []
            else:
                evs = carto.read_events(ctx.home)
            lines = []
            for e in evs[-24:]:
                ln = guard.format_event(e).split("\n")[0]
                if e.get("session"):
                    ln = ln.rsplit(" ", 1)[0] if ln.endswith(str(e["session"])[:8]) else ln
                lines.append(ln[:150])
            body.append('<p class="caption">The hook log of that session, every line as recorded:</p><pre class="log">%s</pre>' % E("\n".join(lines)))
            if run.get("answer"):
                try:
                    with open(os.path.join(ctx.repo, run["answer"]), encoding="utf-8") as fh:
                        ans = [l for l in fh.read().split("\n") if l.strip()][:7]
                    body.append('<p class="caption">What the agent reported back, opening lines:</p><pre class="log" style="white-space:pre-wrap">%s</pre>' % E("\n".join(ans)))
                except OSError:
                    pass
        elif run.get("source") == "probes":
            body.append('<pre class="log">%s</pre>' % E("\n".join("%-8s %-28s %s" % ("REFUSED" if p["decision"] == "block" else "allowed", ",".join(p["rules"])[:28], p["action"][:90]) for p in d["probes"])))
        elif run.get("report"):
            rep = carto.load(os.path.join(ctx.repo, run["report"])) or {}
            lines = ["verdict %s at %s, %d page checks" % (rep.get("verdict"), rep.get("sha"), len(rep.get("results", [])))]
            seen = set()
            for r in rep.get("results", []):
                for p in r.get("problems", []):
                    key = (r["path"], p["detail"])
                    if key in seen:
                        continue
                    seen.add(key)
                    lines.append("FAIL  %-14s %-24s %s" % (r["path"], p["check"], p["detail"]))
            body.append('<pre class="log">%s</pre>' % E("\n".join(lines[:16])))
        elif run.get("comment"):
            try:
                with open(os.path.join(ctx.repo, run["comment"]), encoding="utf-8") as fh:
                    txt = [l for l in fh.read().split("\n") if l.strip() and not l.startswith("<!--") and not l.startswith("<sub>")]
            except OSError:
                txt = []
            body.append('<pre class="log">%s</pre>' % E("\n".join(txt[:20])))
            imgs = [(i["label"], _png(os.path.join(ctx.repo, i["path"]))) for i in run.get("images", [])]
            if any(src for _, src in imgs):
                body.append('<div class="shots">%s</div>' % "".join('<figure><img src="%s" alt="Not-found page on a phone: %s"><figcaption>%s</figcaption></figure>' % (src, E(lab), E(lab)) for lab, src in imgs if src))
        elif run.get("sha"):
            cs = next((c for c in commits if c.sha.startswith(run["sha"])), None)
            if cs is not None:
                a = assess(cs)
                lines = ["%s  %s  (%d files, %d lines)" % (cs.sha[:7], cs.subject[:70], len(cs.files), a["lines"]), ""]
                for r in a["auto"]:
                    vs = guard.evaluate(cs, ctx, [r], use_exceptions=False)[0] if r["type"] in guard.REPLAYABLE and "diff" in guard.enforced_at(r) else (
                        [h for f in cs.files for h in guard.check_edit(ctx, f.path, f.status != "A", [t for _, t in f.added]) if h["rule"] == r["id"]] if "edit" in guard.enforced_at(r) else None)
                    state = "SENT BACK" if vs else ("checked" if vs is not None else "in CI")
                    lines.append("%-9s %s" % (state, rq.get(r["id"]) or r.get("description", "")[:80]))
                for c in a["checks"]:
                    lines.append("%-9s %s" % ("checked", c["label"]))
                lines.append("")
                for x in a["person"]:
                    lines.append("%-9s %s" % ("YOU", x["text"]))
                lines.append("")
                lines.append("Questions: %d without the fence, %d left for a person." % (len(a["auto"]) + len(a["checks"]) + len(a["person"]), len(a["person"])))
                body.append('<pre class="log">%s</pre>' % E("\n".join(lines)))
        return '<article class="run"><div class="hd">%s<h3>%s</h3></div>%s%s</article>' % (
            tag, E(run["label"]), ('<p>%s</p>' % E(run["note"])) if run.get("note") else "", "".join(body))

    S.append('<section class="sec"><div class="lead"><span class="num">2.3</span><h2>What it looks like when it runs</h2>'
             '<p>Each card is one run. Real runs are copied from the hook log, the verifier\'s report and the pull request; replayed runs judge a past commit with today\'s checks.</p></div>%s</section>' %
             "".join(run_card(r) for r in oc.get("runs", [])))

    S.append('<footer>Generated by <code>guard.py --story</code> from outcomes.json, guardrails.json, the hook and replay logs, saved runs in .cartographer/runs and git history. '
             '</footer>')
    page = ('<title>Fence and Review</title><meta name="claude:viewport-fit-sides" content="cover">'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600&display=swap">'
            "<style>%s</style><main class=\"doc\">%s</main>" % (CSS, "".join(S)))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">" + page + "</html>\n")
    return {"path": out_path, "questionsBefore": q_before, "questionsAfter": q_after, "minutesBefore": round(m_before), "minutesAfter": round(m_after),
            "roundTrips": round_trips, "saved": round(saved)}
