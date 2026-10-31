#!/usr/bin/env python3
"""The rules page: every rule an agent works under in this repository, why it exists, the evidence for it, who
approves changes to it, and what is waiting on a person. One static HTML page, written from the repository's own
committed files, so it cannot drift from them.

  python3 .cartographer/bin/rules_page.py [--repo .] [--out .cartographer/dist/rules/index.html]
                                           [--fence-report .cache/fence/report.json] [--waiting prs.json]
                                           [--repo-url https://github.com/OWNER/REPO]

Reads: .cartographer/guardrails.json and guardrails.proposed.json (with each proposal's replay against history),
.cartographer/config.json (checks a person does), .fence/fence.json, .fence/exceptions.json,
.fence/dependencies.json, .github/CODEOWNERS and ONBOARDING.md. Optional: the fence's last report, and a JSON list
of pull requests labelled for a person ([{number, title, url}], e.g. from `gh pr list --label ... --json`).

Deterministic: the page names the commit it was built from and no clock time, so the same commit gives the same
page. Nothing here decides anything; decisions are made with the commands the page shows, through review.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from guard import DEFAULT_SEVERITY, REPLAYABLE, enforced_label  # noqa: E402

# The fence's built-in checks: id -> (category, what it requires). Titles as factory/fence/checks.mjs reports them.
BUILTINS = {
    "secrets": ("security", "No secrets in the change"),
    "workflows": ("security", "CI workflows: least privilege, no script injection"),
    "duplicates": ("patterns", "No new copied code"),
    "dependencies": ("patterns", "New dependencies are approved"),
    "map-coverage": ("patterns", "New code belongs to a mapped component"),
    "guard": ("structure", "Architecture and process rules (guard.py)"),
    "formal": ("tests", "Formal models: the code's model holds, broken variants still fail"),
    "exceptions": ("structure", "No expired exceptions"),
}
BUILTIN_TOOL = {
    "secrets": "pattern scan of added lines",
    "workflows": "workflow scan for write permissions and pasted event text",
    "duplicates": "jscpd",
    "dependencies": "package.json against .fence/dependencies.json",
    "map-coverage": "the codebase map (.cartographer/model)",
    "guard": "guard.py with the accepted rules below",
    "formal": "formal.py: TLC on verification/",
    "exceptions": ".fence/exceptions.json",
}
SEVERITY = {"block": ("Blocks", "blk"), "warn": ("Warns", "wrn"), "info": ("Note", "inf")}
RULE_FILES = (".fence", ".cartographer/guardrails.json", ".cartographer/baseline.json", ".cartographer/hooks", ".github/workflows",
              "verification", ".github/CODEOWNERS", "laws")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def git(repo, *args):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def e(x) -> str:
    return html.escape(str(x), quote=True)


def severity(rule):
    return rule.get("severity") or DEFAULT_SEVERITY.get(rule.get("type"), "block")


def pill(sev):
    label, cls = SEVERITY.get(sev, (sev, "inf"))
    return f'<span class="pill {cls}">{e(label)}</span>'


def replay_text(rule):
    """The proposal's replay against history, in a sentence a reviewer can check against guard.py --replay."""
    rp = rule.get("_replay")
    if not rp:
        return "Not replayed yet. Run <code>guard.py --replay --rule %s</code> before deciding." % e(rule["id"])
    if not rp.get("replayable") or rule.get("type") not in REPLAYABLE:
        return "This kind of rule cannot be replayed against past commits; it applies from the day it is accepted."
    n, fired, bad = rp.get("commits", 0), rp.get("fired", 0), rp.get("followedByFixOrRevert", 0)
    base = rp.get("baseRatePct")
    if fired == 0:
        return f"Replayed on the last {n} commits: it would not have fired on any of them, so accepting it costs nothing today."
    s = f"Replayed on the last {n} commits: it would have fired on {fired}. "
    if bad == 0:
        s += f"None of {'those' if fired > 1 else 'them'} was later reverted or followed by a fix to the same files within 14 days"
    else:
        s += f"{bad} of those {fired} {'were' if bad > 1 else 'was'} later reverted or followed by a fix to the same files within 14 days"
        if rp.get("hitRatePct") is not None:
            s += f" ({rp['hitRatePct']}%)"
    if base is not None:
        s += f"; across all commits that rate is {base}%"
    return s + "."


def onboarding_person_steps(repo):
    path = os.path.join(repo, "ONBOARDING.md")
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        lines = f.read().splitlines()
    for line in lines:
        m = re.match(r"\s*-\s*\[ \]\s*\(person\)\s*(.+)", line)
        if m:
            out.append(m.group(1).strip())
    return out


def codeowners(repo):
    for p in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = os.path.join(repo, p)
        if os.path.exists(path):
            rows = []
            with open(path) as f:
                lines = f.read().splitlines()
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pat, *owners = line.split()
                if pat.lstrip("/").startswith(RULE_FILES):
                    rows.append((pat, " ".join(owners)))
            return p, rows
    return None, []


def cmd_text(argv):
    return " ".join(a if re.fullmatch(r"[\w@%+=:,./{}-]+", a) else "'" + a.replace("'", "'\\''") + "'" for a in argv)


def command_box(cmd):
    return f'<div class="cmd"><code>{e(cmd)}</code><button type="button" data-copy="{e(cmd)}">Copy</button></div>'


def table(head, rows, cls=""):
    th = "".join(f"<th>{h}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def build(repo, fence_report=None, waiting=None, repo_url=None):
    home = os.path.join(repo, ".cartographer")
    accepted = load(os.path.join(home, "guardrails.json"), {"rules": [], "exceptions": []})
    proposed = load(os.path.join(home, "guardrails.proposed.json"), {"rules": []})
    config = load(os.path.join(home, "config.json"), {})
    fence = load(os.path.join(repo, ".fence", "fence.json"), None)
    fence_exc = load(os.path.join(repo, ".fence", "exceptions.json"), [])
    deps = load(os.path.join(repo, ".fence", "dependencies.json"), {})
    report = load(fence_report, None) if fence_report else None
    prs = load(waiting, []) if waiting else []

    name = os.path.basename(os.path.abspath(repo))
    if repo_url:
        name = repo_url.rstrip("/").split("/")[-1]
    sha = git(repo, "rev-parse", "HEAD")
    day = git(repo, "log", "-1", "--format=%cs")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    active_ids = {r["id"] for r in accepted.get("rules", [])}
    props = [r for r in proposed.get("rules", []) if r["id"] not in active_ids]
    person_steps = onboarding_person_steps(repo)
    owners_file, owners = codeowners(repo)

    # Exceptions from both places, with whether each has expired by the commit's date (not today's: deterministic).
    exceptions = []
    for x in accepted.get("exceptions", []):
        exceptions.append({"what": f"guard rule <code>{e(x.get('rule', '?'))}</code>", "where": x.get("path", ""), "reason": x.get("reason", ""),
                           "by": x.get("approvedBy", ""), "expires": x.get("expires", "")})
    for x in fence_exc if isinstance(fence_exc, list) else []:
        exceptions.append({"what": f"fence check <code>{e(x.get('check', '?'))}</code>", "where": x.get("file", ""), "reason": x.get("reason", ""),
                           "by": x.get("approvedBy") or x.get("approver", ""), "expires": x.get("expires", "")})
    for x in exceptions:
        x["expired"] = bool(day and x["expires"] and x["expires"] < day)
        x["incomplete"] = not (x["reason"] and x["by"] and x["expires"])

    # Fence checks: built-ins, then the repository's own tools.
    checks = []
    results = {r["id"].split(":")[0]: r for r in (report or {}).get("results", [])}
    listed = bool(fence) and ("builtins" in fence or bool(fence.get("checks")))
    if listed:
        fixers = fence.get("fixers", {})
        for cid in fence.get("builtins", []):
            cat, title = BUILTINS.get(cid, ("structure", cid))
            checks.append({"id": cid, "category": cat, "title": title, "tool": BUILTIN_TOOL.get(cid, ""), "fixer": fixers.get(cid), "hint": ""})
        for c in fence.get("checks", []):
            checks.append({"id": c["id"], "category": c.get("category", "structure"), "title": c.get("title", c["id"]),
                           "tool": cmd_text(c.get("run", [])), "fixer": fixers.get(c["id"]), "hint": c.get("hint", "")})
    def not_here(cid):
        """Why a built-in check has nothing to do in this repository, from files that are there or not."""
        if cid == "dependencies" and not os.path.exists(os.path.join(repo, "package.json")):
            return "Does nothing here: it reads package.json, and this repository has none."
        if cid == "formal" and not os.path.isdir(os.path.join(repo, "verification")):
            return "Does nothing here: no verification/ folder."
        if cid == "map-coverage" and not os.path.exists(os.path.join(home, "model", "model.json")):
            return "Does nothing here yet: no codebase map."
        if cid == "guard" and not accepted.get("rules"):
            return "Nothing to enforce yet: no guard rule has been accepted."
        return ""
    for c in checks:
        if c["id"] in BUILTINS and not c["hint"]:
            c["hint"] = not_here(c["id"])
    guard_fixers = sorted(k.split(":", 1)[1] for k in (fence or {}).get("fixers", {}) if k.startswith("guard:"))

    waiting_n = len(props) + len(person_steps) + len(prs) + sum(1 for x in exceptions if x["expired"] or x["incomplete"])
    O = []
    O.append(f'''<header class="top"><div class="inner">
<div class="kicker">Rules and approvals</div>
<h1>{e(name)}</h1>
<p class="meta">Built from commit <code>{e(sha[:12])}</code>{f" on {e(branch)}" if branch and branch != "HEAD" else ""}{f", {e(day)}" if day else ""}. Every line on this page comes from a file in the repository; the files are listed at the end.</p>
<nav aria-label="Sections"><a href="#waiting">Waiting on a person</a><a href="#guard">Guard rules</a><a href="#fence">Fence checks</a><a href="#exceptions">Exceptions</a>{'<a href="#judgment">Checked by a person</a>' if config.get("judgment") else ""}<a href="#owners">Who approves changes</a></nav>
</div></header>''')
    O.append('<main class="inner">')
    O.append(f'''<div class="nums">
<a href="#waiting" class="{"hot" if waiting_n else ""}"><b>{waiting_n}</b>waiting on a person</a>
<a href="#guard"><b>{len(active_ids)}</b>guard rules in force</a>
<a href="#waiting"><b>{len(props)}</b>proposed rules</a>
<a href="#fence"><b>{len(checks) if listed or not fence else "–"}</b>fence checks{"" if listed or not fence else " (not listed)"}</a>
<a href="#exceptions"><b>{len(exceptions)}</b>exceptions</a>
</div>''')

    # Waiting on a person
    O.append('<section id="waiting"><h2>Waiting on a person</h2>')
    if not waiting_n:
        O.append("<p>Nothing. Every proposed rule has been decided and every exception is current.</p>")
    if props:
        O.append(f"<h3>Proposed guard rules ({len(props)})</h3><p class=\"lede\">An agent or <code>guard.py --suggest</code> proposed these; none applies until a person accepts it. Accepting moves the rule into <code>.cartographer/guardrails.json</code>, which CODEOWNERS routes to its owner for review.</p>")
        for r in props:
            sev = severity(r)
            O.append(f'''<article class="card" id="rule-{e(r["id"])}">
<div class="cardhead"><code class="rid">{e(r["id"])}</code>{pill(sev)}{f'<span class="type">rule type: {e(r.get("type"))}</span>' if r.get("type") and r.get("type") != r["id"] else ""}</div>
<p class="req">{e(r.get("description") or r.get("type"))}</p>
<dl><dt>Why proposed</dt><dd>{e(r.get("_why") or r.get("source") or "not recorded")}</dd>
<dt>Evidence from history</dt><dd>{replay_text(r)}</dd>
<dt>Once accepted, enforced by</dt><dd>{e("A note on the pull request" if sev == "info" else enforced_label(r))}</dd></dl>
<div class="decide"><span>Accept</span>{command_box(f"python3 .cartographer/bin/guard.py --accept {r['id']}")}<span>Reject</span>{command_box(f"python3 .cartographer/bin/guard.py --reject {r['id']}")}</div>
</article>''')
    if prs:
        O.append(f"<h3>Pull requests labelled for a person ({len(prs)})</h3><p class=\"lede\">The fence failed and the gardener could not fix it. For each finding a person picks: change the code, change the request, change the fence, or record an exception (<code>/tend-fence</code> lays out the options).</p>")
        O.append(table(["Pull request", "Title"], [[f'<a href="{e(p.get("url", ""))}">#{e(p.get("number", ""))}</a>', e(p.get("title", ""))] for p in prs]))
    bad_exc = [x for x in exceptions if x["expired"] or x["incomplete"]]
    if bad_exc:
        O.append(f"<h3>Exceptions to renew or remove ({len(bad_exc)})</h3>")
        O.append(table(["Exception", "Where", "Problem"], [[x["what"], f"<code>{e(x['where'])}</code>", "expired " + e(x["expires"]) if x["expired"] else "missing a reason, an approver or an expiry"] for x in bad_exc]))
    if person_steps:
        O.append(f"<h3>Setup only a person can do ({len(person_steps)})</h3><p class=\"lede\">Unticked person steps in <code>ONBOARDING.md</code>. Until they are done, some of the checks below do not bind.</p><ul class=\"steps\">")
        O += [f"<li>{inline_md(s)}</li>" for s in person_steps]
        O.append("</ul>")
    O.append("</section>")

    # Guard rules in force
    O.append('<section id="guard"><h2>Guard rules in force</h2>')
    O.append("<p class=\"lede\">Deterministic rules from <code>.cartographer/guardrails.json</code>, checked by <code>guard.py</code>: the same change always gets the same verdict. No model decides pass or fail.</p>")
    if not accepted.get("rules"):
        O.append(f"<p class=\"empty\">None yet. {len(props)} proposed rule{'s' if len(props) != 1 else ''} above wait for a person; until one is accepted, the fence's <code>guard</code> check has nothing to enforce.</p>")
    else:
        rows = []
        for r in accepted["rules"]:
            sev = severity(r)
            where = "PR comment" if sev == "info" else enforced_label(r)
            ev = ", ".join(r.get("evidence", [])[:3]) or r.get("source", "")
            fix = r.get("fix", "")
            fx = " The gardener can fix this." if r["id"] in guard_fixers else ""
            rows.append([f'<code>{e(r["id"])}</code>', e(r.get("description") or r.get("type")), pill(sev) + (f'<div class="sub">{e(fix)}{fx}</div>' if fix or fx else ""), e(where), e(ev)])
        O.append(table(["Rule", "What it requires", "On failure", "Enforced by", "Evidence"], rows, "rules"))
    O.append("</section>")

    # Fence checks
    O.append('<section id="fence"><h2>Fence checks</h2>')
    if not fence:
        O.append("<p class=\"empty\">No <code>.fence/fence.json</code>: this repository is not onboarded to the fence.</p>")
    elif not listed:
        O.append("<p class=\"empty\"><code>.fence/fence.json</code> does not list the checks: this repository's fence defines them in its own code, so this page cannot show them. Onboarding with the factory template writes the list.</p>")
    else:
        O.append("<p class=\"lede\">Every check the fence runs on a pull request, from <code>.fence/fence.json</code>. Where a check has a fixer, the gardener runs it, runs every check again, and commits only if nothing that passed now fails. A failure with no fixer is labelled for a person.</p>")
        head = ["Check", "Category", "What it requires", "Tool", "Gardener"] + (["Last result"] if report else [])
        rows = []
        for c in checks:
            row = [f'<code>{e(c["id"])}</code>', e(c["category"]), e(c["title"]) + (f'<div class="sub">{e(c["hint"])}</div>' if c["hint"] else ""),
                   f'<code class="tool">{e(c["tool"])}</code>', "runs a fixer" if c["fixer"] else "no fixer"]
            if report:
                res = results.get(c["id"])
                row.append(f'<span class="st {e(res["status"])}">{e(res["status"])}</span> {e(res.get("summary", ""))}' if res else "")
            rows.append(row)
        O.append(table(head, rows, "checks"))
        if report:
            O.append(f'<p class="cap">Last result: the fence report for commit <code>{e(str(report.get("head", ""))[:12])}</code> against <code>{e(report.get("base", ""))}</code>.</p>')
        if deps:
            O.append("<h3>Approved dependencies</h3>")
            O.append(table(["Package", "Reason", "Approved by"], [[f"<code>{e(k)}</code>", e(v.get("reason", "")), e(v.get("approvedBy", ""))] for k, v in sorted(deps.items()) if isinstance(v, dict)]))
    O.append("</section>")

    # Exceptions
    O.append('<section id="exceptions"><h2>Exceptions</h2>')
    if not exceptions:
        O.append("<p class=\"empty\">None. An exception needs a reason, a named approver and an expiry date; the fence fails when one expires.</p>")
    else:
        O.append(table(["Exception", "Where", "Reason", "Approved by", "Expires"],
                       [[x["what"], f"<code>{e(x['where'])}</code>", e(x["reason"]), e(x["by"]), e(x["expires"]) + (' <span class="pill blk">expired</span>' if x["expired"] else "")] for x in exceptions]))
    O.append("</section>")

    if config.get("judgment"):
        O.append('<section id="judgment"><h2>Checked by a person</h2><p class="lede">Rules no script can check, from <code>.cartographer/config.json</code>. A reviewer checks these on each pull request.</p><ul class="steps">')
        O += [f"<li>{e(j.get('text', ''))}{(' <span class=\"sub\">(' + e(j['why']) + ')</span>') if j.get('why') else ''}</li>" for j in config["judgment"]]
        O.append("</ul></section>")

    O.append('<section id="owners"><h2>Who approves changes to the rules</h2>')
    if owners:
        O.append(f"<p class=\"lede\">From <code>{e(owners_file)}</code>. With code owner review required in the branch settings, GitHub will not merge a change to these paths without one of these people.</p>")
        O.append(table(["Path", "Owner"], [[f"<code>{e(p)}</code>", e(o)] for p, o in owners]))
    else:
        O.append("<p class=\"empty\">No CODEOWNERS entries for the rule files. Without them, anyone with write access, an agent included, can change the rules in a pull request.</p>")
    sources = [".cartographer/guardrails.json", ".cartographer/guardrails.proposed.json", ".cartographer/config.json", ".fence/fence.json",
               ".fence/exceptions.json", ".fence/dependencies.json", owners_file or ".github/CODEOWNERS", "ONBOARDING.md"]
    present = [s for s in sources if s and os.path.exists(os.path.join(repo, s))]
    extra = ([f"the fence report {e(fence_report)}"] if report else []) + (["the list of pull requests labelled for a person"] if prs else [])
    O.append(f'</section><footer class="foot">Generated by <code>.cartographer/bin/rules_page.py</code> from {", ".join(f"<code>{e(s)}</code>" for s in present)}{"; " + "; ".join(extra) if extra else ""}.</footer>')
    O.append("</main>")
    return page(f"{name} rules", "\n".join(O))


def inline_md(s):
    """`code` spans in a line of ONBOARDING.md, escaped."""
    parts = s.split("`")
    return "".join(f"<code>{e(p)}</code>" if i % 2 else e(p) for i, p in enumerate(parts))


CSS = """
:root{--ink:#111418;--muted:#5b6470;--line:#d9dee5;--tint:#f4f6f9;--paper:#ffffff;--blue:#0047bb;--navy:#0a2a5e;--warn:#5b6470;--block:#0047bb;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:-apple-system,"Segoe UI",system-ui,Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box}html{color-scheme:light}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 var(--sans)}
.inner{max-width:1120px;margin:0 auto;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.top{background:var(--navy);color:#fff;padding-block:28px 0}.top .inner{display:flex;flex-direction:column;gap:6px}
.kicker{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:#c8d3e6;font-weight:600}
h1{margin:0;font-size:30px;line-height:1.2;text-wrap:balance}.meta{margin:0;color:#c8d3e6;max-width:70ch}.meta code{color:#fff;background:rgba(255,255,255,.12)}
nav{display:flex;flex-wrap:wrap;gap:4px 18px;margin-top:14px;padding-block:10px;border-top:1px solid rgba(255,255,255,.18)}nav a{color:#fff;text-decoration:none;font-size:14px}nav a:hover{text-decoration:underline}
main{padding-block:22px 40px}h2{font-size:21px;margin:34px 0 8px;color:var(--navy);text-wrap:balance}h3{font-size:16px;margin:22px 0 6px}
.lede{color:var(--muted);margin:0 0 12px;max-width:80ch}.empty{color:var(--muted);background:var(--tint);border:1px solid var(--line);border-radius:6px;padding:12px 14px}
code{font-family:var(--mono);font-size:.88em;background:var(--tint);padding:1px 5px;border-radius:4px}
.nums{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));border:1px solid var(--line);border-radius:8px;overflow:hidden}
.nums a{padding:12px 16px;color:var(--muted);text-decoration:none;font-size:13px;border-right:1px solid var(--line)}.nums a:last-child{border-right:0}
.nums b{display:block;font-size:26px;color:var(--ink);line-height:1.2}.nums a.hot{background:var(--blue);color:#dfe8f7}.nums a.hot b{color:#fff}
.card{border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:6px;padding:14px 16px;margin:10px 0;background:var(--paper)}
.cardhead{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.rid{font-size:15px;font-weight:600;background:none;padding:0}.type{color:var(--muted);font-size:13px}
.req{margin:6px 0 8px;font-weight:500}dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0;font-size:14px}dt{color:var(--muted)}dd{margin:0;min-width:0}
.decide{display:grid;grid-template-columns:max-content minmax(0,1fr);align-items:center;gap:6px 10px;margin-top:12px;max-width:640px}.decide>span{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600}
.cmd{display:flex;align-items:center;justify-content:space-between;gap:6px;border:1px solid var(--line);border-radius:6px;padding:3px 3px 3px 8px;min-width:0}
.cmd code{background:none;padding:0;overflow-x:auto;white-space:nowrap}.cmd button{font:inherit;font-size:12px;border:1px solid var(--line);background:var(--tint);border-radius:4px;padding:3px 8px;cursor:pointer;color:var(--ink)}
.pill{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;border-radius:999px;padding:2px 8px}
.pill.blk{background:var(--blue);color:#fff}.pill.wrn{background:#e3e7ed;color:var(--ink)}.pill.inf{background:var(--paper);color:var(--muted);border:1px solid var(--line)}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px}table{border-collapse:collapse;width:100%;font-size:14px}
th{background:var(--navy);color:#fff;text-align:left;font-weight:600;padding:8px 10px;white-space:nowrap}td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
tbody tr:nth-child(even) td{background:var(--tint)}.sub{color:var(--muted);font-size:13px;margin-top:3px}code.tool{white-space:normal;word-break:break-word}
table.checks td:nth-child(4){max-width:320px}table.rules td:first-child,table.checks td:first-child{white-space:nowrap}.st{font-weight:600}.st.fail{color:var(--blue)}.st.pass{color:var(--ink)}
.steps{margin:0;padding-left:20px}.steps li{margin:4px 0}.cap{font-size:13px;color:var(--muted)}
.foot{margin-top:36px;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}a{color:var(--blue)}
@media (max-width:560px){h1{font-size:24px}dl{grid-template-columns:1fr}dt{margin-top:6px}}
"""

JS = """document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-copy]');if(!b)return;var t=b.getAttribute('data-copy');
function done(){var o=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=o},1200)}
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,function(){sel(b)})}else{sel(b)}
function sel(b){var c=b.previousElementSibling;var r=document.createRange();r.selectNodeContents(c);var s=getSelection();s.removeAllRanges();s.addRange(r)}});"""


def page(title, body):
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f"<title>{e(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n{body}\n<script>{JS}</script>\n</body>\n</html>\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fence-report")
    ap.add_argument("--waiting", help="JSON list of pull requests labelled for a person")
    ap.add_argument("--repo-url")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    out = a.out or os.path.join(repo, ".cartographer", "dist", "rules", "index.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        f.write(build(repo, a.fence_report, a.waiting, a.repo_url))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
