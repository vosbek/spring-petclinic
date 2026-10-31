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

HERE = os.path.dirname(os.path.abspath(__file__))
# Primer CSS 21.5.1 (GitHub's design system, MIT), cut to the components this page uses: toolkit/viewer/primer/.
PRIMER = os.path.join(HERE, "..", "viewer", "primer-rules.css")

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
SEVERITY = {"block": ("Blocks", "Label--danger"), "warn": ("Warns", "Label--attention"), "info": ("Note", "Label--secondary")}
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


def label(sev):
    text, cls = SEVERITY.get(sev, (sev, "Label--secondary"))
    return f'<span class="Label {cls}">{e(text)}</span>'


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


def command_field(cmd, what):
    """A read-only command with a copy button: Primer's input group."""
    return (f'<div class="input-group mt-1"><input class="form-control input-sm input-monospace" type="text" readonly '
            f'value="{e(cmd)}" aria-label="{e(what)}"><span class="input-group-button">'
            f'<button class="btn btn-sm" type="button" data-copy="{e(cmd)}">Copy</button></span></div>')


def box(title, count, rows, header_note=""):
    c = f' <span class="Counter">{count}</span>' if count is not None else ""
    note = f'<div class="f6 color-fg-muted mt-1">{header_note}</div>' if header_note else ""
    return (f'<div class="Box mb-4"><div class="Box-header"><h3 class="Box-title">{title}{c}</h3>{note}</div>'
            + "".join(f'<div class="Box-row">{r}</div>' for r in rows) + "</div>")


def blankslate(title, text):
    return f'<div class="blankslate blankslate-narrow Box mb-4"><h3 class="blankslate-heading">{title}</h3><p>{text}</p></div>'


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

    owner_repo = ""
    if repo_url:
        owner_repo = "/".join(repo_url.rstrip("/").split("/")[-2:])
    name = owner_repo.split("/")[-1] if owner_repo else os.path.basename(os.path.abspath(repo))
    sha = git(repo, "rev-parse", "HEAD")
    day = git(repo, "log", "-1", "--format=%cs")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    rules = accepted.get("rules", [])
    active_ids = {r["id"] for r in rules}
    props = [r for r in proposed.get("rules", []) if r["id"] not in active_ids]
    person_steps = onboarding_person_steps(repo)
    owners_file, owners = codeowners(repo)

    # Exceptions from both places, with whether each has expired by the commit's date (not today's: deterministic).
    exceptions = []
    for x in accepted.get("exceptions", []):
        exceptions.append({"what": f"Guard rule <code>{e(x.get('rule', '?'))}</code>", "where": x.get("path", ""), "reason": x.get("reason", ""),
                           "by": x.get("approvedBy", ""), "expires": x.get("expires", "")})
    for x in fence_exc if isinstance(fence_exc, list) else []:
        exceptions.append({"what": f"Fence check <code>{e(x.get('check', '?'))}</code>", "where": x.get("file", ""), "reason": x.get("reason", ""),
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
        if cid == "guard" and not rules:
            return "Nothing to enforce yet: no guard rule has been accepted."
        return ""
    for c in checks:
        if c["id"] in BUILTINS and not c["hint"]:
            c["hint"] = not_here(c["id"])
    guard_fixers = sorted(k.split(":", 1)[1] for k in (fence or {}).get("fixers", {}) if k.startswith("guard:"))
    bad_exc = [x for x in exceptions if x["expired"] or x["incomplete"]]
    waiting_n = len(props) + len(person_steps) + len(prs) + len(bad_exc)

    # ---- the page
    O = []
    where = f'<a class="Link--muted" href="{e(repo_url)}">{e(owner_repo)}</a>' if repo_url else e(name)
    O.append(f"""<div class="container-lg px-3 pt-4">
<div class="f4 color-fg-muted mb-1">{where}</div>
<div class="Subhead mb-0 border-bottom-0">
  <h1 class="Subhead-heading f2">Rules and approvals</h1>
  <div class="Subhead-description">Built from commit <code>{e(sha[:12])}</code>{f" on <code>{e(branch)}</code>" if branch and branch != "HEAD" else ""}{f", {e(day)}" if day else ""}. Every line comes from a file in the repository, listed at the end. This page shows the rules; it decides nothing.</div>
</div>
</div>""")
    tabs = [("waiting", "Waiting on a person", waiting_n), ("guard", "Guard rules", len(rules)),
            ("fence", "Fence checks", len(checks) if listed else None), ("exceptions", "Exceptions", len(exceptions)),
            ("owners", "Owners", len(owners))]
    if config.get("judgment"):
        tabs.insert(4, ("judgment", "Checked by a person", len(config["judgment"])))
    O.append('<div class="border-bottom mt-3"><nav class="UnderlineNav container-lg px-3 border-bottom-0" aria-label="Sections"><div class="UnderlineNav-body">'
             + "".join(f'<a class="UnderlineNav-item" href="#{t}" data-tab="{t}">{e(label_)}'
                       + (f' <span class="Counter">{n}</span>' if n is not None else "") + "</a>" for t, label_, n in tabs)
             + "</div></nav></div>")
    O.append('<main class="container-lg px-3 py-4">')

    # Waiting on a person
    O.append('<section id="waiting" class="tab-panel">')
    if not waiting_n:
        O.append(blankslate("Nothing is waiting on a person", "Every proposed rule has been decided and every exception is current."))
    if props and not rules:
        O.append('<div class="flash mb-3">No guard rule is in force yet, so the fence\'s <code>guard</code> check has nothing to enforce. '
                 'A rule applies only after a person accepts it.</div>')
    if props:
        rows = []
        for r in props:
            sev = severity(r)
            enf = "A note on the pull request" if sev == "info" else enforced_label(r)
            typ = f' <span class="color-fg-muted f6">{e(r.get("type"))}</span>' if r.get("type") and r.get("type") != r["id"] else ""
            rows.append(f"""<div id="rule-{e(r["id"])}"><div class="d-flex flex-wrap flex-items-center gap-2"><strong class="text-mono">{e(r["id"])}</strong> {label(sev)}{typ}</div>
<p class="mt-1 mb-1">{e(r.get("description") or r.get("type"))}</p>
<p class="f6 color-fg-muted mb-1">{replay_text(r)}</p>
<details class="details-reset mt-2"><summary class="btn-link f6">Why it was proposed, and how to decide</summary>
<div class="mt-2 f6"><p class="mb-1"><span class="color-fg-muted">Why proposed:</span> {e(r.get("_why") or r.get("source") or "not recorded")}</p>
<p class="mb-2"><span class="color-fg-muted">Once accepted, enforced by:</span> {e(enf)}</p>
<label class="d-block color-fg-muted">Accept</label>{command_field(f"python3 .cartographer/bin/guard.py --accept {r['id']}", "Accept command")}
<label class="d-block color-fg-muted mt-2">Reject</label>{command_field(f"python3 .cartographer/bin/guard.py --reject {r['id']}", "Reject command")}
</div></details></div>""")
        O.append(box("Proposed guard rules", len(props), rows,
                     "Proposed by an agent or <code>guard.py --suggest</code> and replayed against the repository's history. Accepting moves a rule into "
                     "<code>.cartographer/guardrails.json</code>, which CODEOWNERS routes to its owner for review."))
    if prs:
        O.append(box("Pull requests labelled for a person", len(prs),
                     [f'<a class="Link--primary text-bold" href="{e(p.get("url", ""))}">#{e(p.get("number", ""))}</a> {e(p.get("title", ""))}' for p in prs],
                     "The fence failed and the gardener could not fix it. <code>/tend-fence</code> lays out the options for each finding."))
    if bad_exc:
        O.append(box("Exceptions to renew or remove", len(bad_exc),
                     [f'{x["what"]} on <code>{e(x["where"])}</code> <span class="color-fg-muted">{"expired " + e(x["expires"]) if x["expired"] else "missing a reason, an approver or an expiry"}</span>' for x in bad_exc]))
    if person_steps:
        O.append(box("Setup only a person can do", len(person_steps), [inline_md(s) for s in person_steps],
                     "Unticked person steps in <code>ONBOARDING.md</code>. Until they are done, some checks do not bind."))
    O.append("</section>")

    # Guard rules in force
    O.append('<section id="guard" class="tab-panel">')
    if not rules:
        O.append(blankslate("No guard rules in force",
                            f"{len(props)} proposed rule{'s' if len(props) != 1 else ''} wait on a person. Rules live in <code>.cartographer/guardrails.json</code> and are checked by <code>guard.py</code>: the same change always gets the same verdict."))
    else:
        rows = []
        for r in rules:
            sev = severity(r)
            enf = "PR comment" if sev == "info" else enforced_label(r)
            fix = r.get("fix", "") + (" The gardener can fix this." if r["id"] in guard_fixers else "")
            ev = ", ".join(r.get("evidence", [])[:3]) or r.get("source", "")
            rows.append(f"""<div class="d-flex flex-wrap flex-items-center gap-2"><strong class="text-mono">{e(r["id"])}</strong> {label(sev)} <span class="f6 color-fg-muted">{e(enf)}</span></div>
<p class="mt-1 mb-1">{e(r.get("description") or r.get("type"))}</p>
{f'<p class="f6 mb-1"><span class="color-fg-muted">On failure:</span> {e(fix.strip())}</p>' if fix.strip() else ""}
{f'<p class="f6 color-fg-muted mb-0">Evidence: {e(ev)}</p>' if ev else ""}""")
        O.append(box("Guard rules in force", len(rules), rows,
                     "From <code>.cartographer/guardrails.json</code>, checked by <code>guard.py</code>. No model decides pass or fail."))
    O.append("</section>")

    # Fence checks
    O.append('<section id="fence" class="tab-panel">')
    if not fence:
        O.append(blankslate("Not onboarded to the fence", "There is no <code>.fence/fence.json</code> in this repository."))
    elif not listed:
        O.append(blankslate("The checks are not listed", "<code>.fence/fence.json</code> does not list the checks: this repository's fence defines them in its own code, so this page cannot show them. Onboarding with the factory template writes the list."))
    else:
        rows = []
        for c in checks:
            res = results.get(c["id"]) if report else None
            state = (f' <span class="Label {"Label--danger" if res["status"] == "fail" else "Label--success" if res["status"] == "pass" else "Label--secondary"}">{e(res["status"])}</span>' if res else "")
            fixer = '<span class="Label Label--success">Gardener fixes</span>' if c["fixer"] else ""
            rows.append(f"""<div class="d-flex flex-justify-between flex-items-start gap-2"><div class="min-width-0">
<div class="d-flex flex-wrap flex-items-center gap-2"><strong class="text-mono">{e(c["id"])}</strong> <span class="Label Label--secondary">{e(c["category"])}</span>{state}</div>
<p class="mt-1 mb-1">{e(c["title"])}</p>
<p class="f6 color-fg-muted mb-0 text-mono wb-break-word">{e(c["tool"])}</p>
{f'<p class="f6 mt-1 mb-0">{e(c["hint"])}</p>' if c["hint"] else ""}
</div><div class="flex-shrink-0">{fixer}</div></div>""")
        O.append(box("Fence checks", len(checks), rows,
                     "From <code>.fence/fence.json</code>. Where a check has a fixer, the gardener runs it, runs every check again, and commits only if nothing that passed now fails. Any other failure is labelled for a person."))
        if report:
            O.append(f'<p class="f6 color-fg-muted">Results: the fence report for commit <code>{e(str(report.get("head", ""))[:12])}</code> against <code>{e(report.get("base", ""))}</code>.</p>')
        if deps:
            O.append(box("Approved dependencies", len(deps),
                         [f'<strong class="text-mono">{e(k)}</strong> <span class="color-fg-muted">approved by {e(v.get("approvedBy", ""))}</span><div class="f6">{e(v.get("reason", ""))}</div>' for k, v in sorted(deps.items()) if isinstance(v, dict)]))
    O.append("</section>")

    # Exceptions
    O.append('<section id="exceptions" class="tab-panel">')
    if not exceptions:
        O.append(blankslate("No exceptions", "An exception needs a reason, a named approver and an expiry date, and the fence fails when one expires."))
    else:
        O.append(box("Exceptions", len(exceptions),
                     [f'{x["what"]} on <code>{e(x["where"])}</code>' + (' <span class="Label Label--danger">expired</span>' if x["expired"] else "")
                      + f'<div class="f6 mt-1">{e(x["reason"])}</div><div class="f6 color-fg-muted">Approved by {e(x["by"] or "nobody named")}, expires {e(x["expires"] or "never set")}</div>' for x in exceptions]))
    O.append("</section>")

    if config.get("judgment"):
        O.append('<section id="judgment" class="tab-panel">')
        O.append(box("Checked by a person", len(config["judgment"]),
                     [e(j.get("text", "")) + (f'<div class="f6 color-fg-muted">{e(j["why"])}</div>' if j.get("why") else "") for j in config["judgment"]],
                     "Rules no script can check, from <code>.cartographer/config.json</code>. A reviewer checks these on each pull request."))
        O.append("</section>")

    O.append('<section id="owners" class="tab-panel">')
    if owners:
        O.append(box("Who approves changes to the rules", len(owners),
                     [f'<div class="d-flex flex-justify-between flex-wrap gap-2"><code>{e(p)}</code><span>{e(o)}</span></div>' for p, o in owners],
                     f"From <code>{e(owners_file)}</code>. With code owner review required in the branch settings, GitHub will not merge a change to these paths without one of these people."))
    else:
        O.append(blankslate("No owners for the rule files", "Without CODEOWNERS entries, anyone with write access, an agent included, can change the rules in a pull request."))
    O.append("</section>")

    sources = [".cartographer/guardrails.json", ".cartographer/guardrails.proposed.json", ".cartographer/config.json", ".fence/fence.json",
               ".fence/exceptions.json", ".fence/dependencies.json", owners_file or ".github/CODEOWNERS", "ONBOARDING.md"]
    present = [s for s in sources if s and os.path.exists(os.path.join(repo, s))]
    extra = ([f"the fence report {e(fence_report)}"] if report else []) + (["the list of pull requests labelled for a person"] if prs else [])
    O.append(f'<footer class="border-top pt-3 mt-5 f6 color-fg-muted">Generated by <code>.cartographer/bin/rules_page.py</code> from '
             f'{", ".join(f"<code>{e(s)}</code>" for s in present)}{"; " + "; ".join(extra) if extra else ""}. Styled with Primer CSS 21.5.1, GitHub\'s design system (MIT).</footer>')
    O.append("</main>")
    return page(f"{name} rules", "\n".join(O))


def inline_md(s):
    """`code` spans in a line of ONBOARDING.md, escaped."""
    parts = s.split("`")
    return "".join(f"<code>{e(p)}</code>" if i % 2 else e(p) for i, p in enumerate(parts))


# Page-specific glue on top of Primer: tab panels, copy feedback, and the min-width fix for long commands.
CSS = """
body{background:var(--bgColor-default,var(--color-canvas-default))}
.tab-panel[hidden]{display:none!important}
.UnderlineNav-item[aria-current]{font-weight:600}
.Box-row p:last-child{margin-bottom:0}
.input-group .form-control{min-width:0}
.gap-2{gap:8px}
"""

# Tabs follow the address (#waiting, #guard, ...), so a link can open one. Without script every section shows.
JS = """(function(){var tabs=[].slice.call(document.querySelectorAll('[data-tab]'));var panels=[].slice.call(document.querySelectorAll('.tab-panel'));
function show(){var id=(location.hash||'').slice(1);if(!panels.some(function(p){return p.id===id}))id=panels[0]&&panels[0].id;
panels.forEach(function(p){p.hidden=p.id!==id});tabs.forEach(function(t){if(t.getAttribute('data-tab')===id)t.setAttribute('aria-current','page');else t.removeAttribute('aria-current')})}
window.addEventListener('hashchange',show);show();
document.addEventListener('click',function(ev){var b=ev.target.closest('button[data-copy]');if(!b)return;var t=b.getAttribute('data-copy');
function done(){var o=b.textContent;b.textContent='Copied';setTimeout(function(){b.textContent=o},1200)}
function sel(){var i=b.closest('.input-group').querySelector('input');i.focus();i.select()}
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,sel)}else{sel()}})})();"""


def primer_css():
    try:
        with open(PRIMER) as f:
            return f.read()
    except OSError:
        return ""


def page(title, body):
    return ('<!doctype html>\n<html lang="en" data-color-mode="auto" data-light-theme="light" data-dark-theme="dark">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f"<title>{e(title)}</title>\n<style>{primer_css()}</style>\n<style>{CSS}</style>\n</head>\n<body>\n{body}\n<script>{JS}</script>\n</body>\n</html>\n")


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
