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
of pull requests labelled for a person ([{number, title, url}]).

With --repo-url, each proposed rule has Accept and Reject buttons. They open a "Rule decision" issue on GitHub
(.github/ISSUE_TEMPLATE/rule-decision.yml); the rule-decision workflow turns it into a pull request for the rules'
code owner. The page itself changes nothing.

Deterministic: the page names the commit it was built from and no clock time, so the same commit gives the same
page. Styled with IBM Carbon (toolkit/viewer/carbon-rules.css, built by toolkit/viewer/carbon/build.sh).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
# IBM Carbon (@carbon/styles 1.116.0, Apache-2.0), cut to the components this page uses.
CARBON = os.path.join(HERE, "..", "viewer", "carbon-rules.css")

sys.path.insert(0, HERE)
from guard import DEFAULT_SEVERITY, REPLAYABLE, enforced_label  # noqa: E402
import rules_model  # noqa: E402

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
    "guard": "guard.py with the accepted rules",
    "formal": "formal.py: TLC on verification/",
    "exceptions": ".fence/exceptions.json",
}
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


def history_short(rule):
    rp = rule.get("_replay")
    if not rp:
        return "Not replayed yet"
    if not rp.get("replayable") or rule.get("type") not in REPLAYABLE:
        return "Applies from acceptance"
    n, fired, bad = rp.get("commits", 0), rp.get("fired", 0), rp.get("followedByFixOrRevert", 0)
    if fired == 0:
        return f"Fired on 0 of {n} commits"
    return f"Fired on {fired} of {n} commits; {bad} followed by a fix or revert"


def onboarding_person_steps(repo):
    path = os.path.join(repo, "ONBOARDING.md")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        m = re.match(r"\s*-\s*\[ \]\s*\(person\)\s*(.+)", line)
        if m:
            out.append(m.group(1).strip())
    return out


def codeowners(repo):
    for p in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = os.path.join(repo, p)
        if os.path.exists(path):
            with open(path) as f:
                lines = f.read().splitlines()
            rows = []
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


def inline_md(s):
    """`code` spans in a line of ONBOARDING.md, escaped."""
    parts = s.split("`")
    return "".join(f"<code>{e(p)}</code>" if i % 2 else e(p) for i, p in enumerate(parts))


def decision_url(repo_url, rule_id, decision):
    q = urllib.parse.urlencode({"template": "rule-decision.yml", "labels": "rule decision", "title": f"{decision} guard rule {rule_id}",
                                "rule": rule_id, "decision": decision})
    return f"{repo_url.rstrip('/')}/issues/new?{q}"


# ----------------------------------------------------------------------------- Carbon pieces

CHEVRON = ('<svg class="cds--table-expand__svg" focusable="false" aria-hidden="true" width="16" height="16" viewBox="0 0 16 16">'
           '<path d="M11 8L6 13 5.3 12.3 9.6 8 5.3 3.7 6 3z"></path></svg>')
INFO = ('<svg class="cds--inline-notification__icon" focusable="false" aria-hidden="true" width="20" height="20" viewBox="0 0 32 32">'
        '<path d="M16,2A14,14,0,1,0,30,16,14,14,0,0,0,16,2Zm0,6a1.5,1.5,0,1,1-1.5,1.5A1.5,1.5,0,0,1,16,8Zm4,16.125H12v-2.25h2.875v-5.75H13v-2.25h4.125v8H20Z"></path></svg>')


def tag(text, color):
    return f'<span class="cds--tag cds--tag--{color}"><span class="cds--tag__label">{e(text)}</span></span>'


def sev_tag(sev):
    text, color = {"block": ("Blocks", "red"), "warn": ("Warns", "warm-gray"), "info": ("Note", "outline")}.get(sev, (sev, "outline"))
    return tag(text, color)


def table(key, title, desc, head, rows, empty=None, expandable=False):
    """A Carbon data table. rows: lists of cells, or (cells, detail_html) pairs when expandable."""
    th = ('<th class="cds--table-expand" scope="col"><span class="sf-sr">Details</span></th>' if expandable else "") + "".join(
        f'<th scope="col"><div class="cds--table-header-label">{h}</div></th>' for h in head)
    body = []
    if not rows and empty:
        body.append(f'<tr><td colspan="{len(head) + expandable}"><div class="sf-empty"><p class="sf-empty__title">{empty[0]}</p><p>{empty[1]}</p></div></td></tr>')
    for i, row in enumerate(rows):
        if expandable:
            cells, detail = row
            rid = f"{key}-{i}"
            body.append(f'<tr class="cds--parent-row" data-parent-row><td class="cds--table-expand"><button type="button" class="cds--table-expand__button" '
                        f'aria-expanded="false" aria-controls="{rid}" aria-label="Show details">{CHEVRON}</button></td>'
                        + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            body.append(f'<tr data-child-row id="{rid}" hidden><td colspan="{len(head) + 1}"><div class="cds--child-row-inner-container">{detail}</div></td></tr>')
        else:
            body.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
    return (f'<div class="cds--data-table-container sf-table"><div class="cds--data-table-header"><h2 class="cds--data-table-header__title">{title}</h2>'
            + (f'<p class="cds--data-table-header__description">{desc}</p>' if desc else "")
            + '</div><div class="cds--data-table-content"><table class="cds--data-table cds--data-table--lg">'
            + f'<thead><tr>{th}</tr></thead><tbody>{"".join(body)}</tbody></table></div></div>')


NOTE_NO_RULES = ('<div class="cds--inline-notification cds--inline-notification--low-contrast cds--inline-notification--info sf-note" role="status">'
                 f'<div class="cds--inline-notification__details">{INFO}<div class="cds--inline-notification__text-wrapper">'
                 '<div class="cds--inline-notification__title">No guard rule is in force yet.</div>'
                 '<div class="cds--inline-notification__subtitle">The fence\'s guard check has nothing to enforce until a person accepts one of the proposals below.</div>'
                 '</div></div></div>')


NOTE_ISSUES_OFF = ('<div class="cds--inline-notification cds--inline-notification--low-contrast cds--inline-notification--warning sf-note" role="status">'
                   f'<div class="cds--inline-notification__details">{INFO}<div class="cds--inline-notification__text-wrapper">'
                   '<div class="cds--inline-notification__title">GitHub Issues are turned off for this repository.</div>'
                   '<div class="cds--inline-notification__subtitle">Accept and Reject open a decision as an issue, so they cannot work until a person turns Issues on in '
                   '<a class="cds--link" href="{settings}">Settings</a> under General, Features. Forks start with Issues off.</div>'
                   '</div></div></div>')


# ----------------------------------------------------------------------------- the page

def build(repo, fence_report=None, waiting=None, repo_url=None, issues_off=False):
    home = os.path.join(repo, ".cartographer")
    accepted = load(os.path.join(home, "guardrails.json"), {"rules": [], "exceptions": []})
    proposed = load(os.path.join(home, "guardrails.proposed.json"), {"rules": []})
    config = load(os.path.join(home, "config.json"), {})
    fence = load(os.path.join(repo, ".fence", "fence.json"), None)
    fence_exc = load(os.path.join(repo, ".fence", "exceptions.json"), [])
    deps = load(os.path.join(repo, ".fence", "dependencies.json"), {})
    report = load(fence_report, None) if fence_report else None
    prs = load(waiting, []) if waiting else []

    owner_repo = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else ""
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
    bad_exc = [x for x in exceptions if x["expired"] or x["incomplete"]]

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
    waiting_n = len(props) + len(person_steps) + len(prs) + len(bad_exc)

    O = []
    crumb = (f'<li class="cds--breadcrumb-item"><a class="cds--link" href="{e(repo_url)}">{e(owner_repo)}</a></li>' if repo_url
             else f'<li class="cds--breadcrumb-item"><span>{e(name)}</span></li>')
    meta = (f"Built from commit <code>{e(sha[:12])}</code>" + (f" on <code>{e(branch)}</code>" if branch and branch != "HEAD" else "")
            + (f", {e(day)}" if day else "") + ". Every line comes from a file in the repository, listed at the end. "
            "Decisions go through a pull request a code owner reviews; this page changes nothing by itself.")
    O.append(f'<header class="cds--header" aria-label="Software factory"><a class="cds--header__name" href="{e(repo_url or "#")}">'
             '<span class="cds--header__name--prefix">Software factory</span>&nbsp;[Rules]</a></header>')
    O.append('<main class="sf-main"><div class="sf-pagehead">'
             f'<nav aria-label="Breadcrumb"><ol class="cds--breadcrumb cds--breadcrumb--no-trailing-slash">{crumb}<li class="cds--breadcrumb-item"><span>Rules</span></li></ol></nav>'
             f'<h1 class="sf-title">Rules and approvals</h1><p class="sf-meta">{meta}</p></div>')

    order = ["style", "practices", "patterns", "security", "performance", "structure", "tests", "database"]
    model = {"n_rules": len(rules), "n_props": len(props), "n_checks": len(checks), "checks": checks,
             "categories": sorted({c["category"] for c in checks}, key=lambda c: order.index(c) if c in order else 99),
             "fixer_ids": [c["id"] for c in checks if c["fixer"]],
             "replay_commits": max([r.get("_replay", {}).get("commits") or 0 for r in proposed.get("rules", [])] or [0]),
             "n_judgment": len(config.get("judgment", [])), "has_package_json": os.path.exists(os.path.join(repo, "package.json")),
             "has_verification": os.path.isdir(os.path.join(repo, "verification")),
             "hidden_workflow": os.path.exists(os.path.join(repo, ".github", "workflows", "hidden-tests.yml"))}
    tabs = [("how", "How it works", "the model"), ("waiting", "Waiting on a person", f"{waiting_n} item{'s' if waiting_n != 1 else ''}"),
            ("guard", "Guard rules", f"{len(rules)} in force"),
            ("fence", "Fence checks", f"{len(checks)} checks" if listed else "not listed"),
            ("exceptions", "Exceptions", f"{len(exceptions)}"),
            ("owners", "Owners", f"{len(owners)} path{'s' if len(owners) != 1 else ''}")]
    if config.get("judgment"):
        tabs.insert(4, ("judgment", "Checked by a person", f"{len(config['judgment'])} rules"))
    O.append('<div class="cds--tabs cds--tabs--contained sf-tabs"><div class="cds--tab--list" role="tablist">'
             + "".join(f'<a role="tab" class="cds--tabs__nav-item cds--tabs__nav-link" href="#{t}" data-tab="{t}">'
                       f'<div class="cds--tabs__nav-item-label-wrapper"><span class="cds--tabs__nav-item-label">{e(lab)}</span></div>'
                       f'<div class="cds--tabs__nav-item-secondary-label">{e(sec)}</div></a>' for t, lab, sec in tabs)
             + '</div></div><div class="sf-panels">')

    O.append('<section id="how" class="sf-panel" role="tabpanel">' + rules_model.section(model, table, tag) + "</section>")

    # Waiting on a person
    O.append('<section id="waiting" class="sf-panel" role="tabpanel">')
    if props and repo_url and issues_off:
        O.append(NOTE_ISSUES_OFF.replace("{settings}", e(repo_url.rstrip("/") + "/settings")))
    if props and not rules:
        O.append(NOTE_NO_RULES)
    rows = []
    for r in props:
        sev = severity(r)
        enf = "A note on the pull request" if sev == "info" else enforced_label(r)
        if repo_url and issues_off:
            decide = '<span class="sf-muted">Turn on Issues to decide here</span>'
        elif repo_url:
            decide = (f'<div class="sf-actions"><a class="cds--btn cds--btn--sm cds--layout--size-sm cds--btn--primary" href="{e(decision_url(repo_url, r["id"], "Accept"))}">Accept</a>'
                      f'<a class="cds--btn cds--btn--sm cds--layout--size-sm cds--btn--ghost" href="{e(decision_url(repo_url, r["id"], "Reject"))}">Reject</a></div>')
        else:
            decide = '<span class="sf-muted">Decided on GitHub; build with --repo-url for the buttons</span>'
        detail = (f'<dl class="sf-detail"><dt>Why proposed</dt><dd>{e(r.get("_why") or r.get("source") or "not recorded")}</dd>'
                  f'<dt>Evidence from history</dt><dd>{replay_text(r)}</dd><dt>Once accepted, enforced by</dt><dd>{e(enf)}</dd></dl>')
        rows.append(([f'<span class="sf-id">{e(r["id"])}</span>', sev_tag(sev), e(r.get("description") or r.get("type")), e(history_short(r)), decide], detail))
    if props:
        O.append(table("proposed", "Proposed guard rules",
                       "Proposed by an agent or <code>guard.py --suggest</code> and replayed against the repository's history. Accept or Reject opens a decision "
                       "on GitHub with a box for why; a workflow turns it into a pull request for the rules' code owner.",
                       ["Rule", "Severity", "What it requires", "Evidence from history", "Decision"], rows, expandable=True))
    if prs:
        O.append(table("prs", "Pull requests labelled for a person", "The fence failed and the gardener could not fix it. <code>/tend-fence</code> lays out the options for each finding.",
                       ["Pull request", "Title"], [[f'<a class="cds--link" href="{e(p.get("url", ""))}">#{e(p.get("number", ""))}</a>', e(p.get("title", ""))] for p in prs]))
    if bad_exc:
        O.append(table("renew", "Exceptions to renew or remove", None, ["Exception", "Where", "Problem"],
                       [[x["what"], f"<code>{e(x['where'])}</code>", "expired " + e(x["expires"]) if x["expired"] else "missing a reason, an approver or an expiry"] for x in bad_exc]))
    if person_steps:
        O.append(table("setup", "Setup only a person can do", "Unticked person steps in <code>ONBOARDING.md</code>. Until they are done, some checks do not bind.",
                       ["Step"], [[inline_md(st)] for st in person_steps]))
    if not waiting_n:
        O.append(table("none", "Waiting on a person", None, ["Item"], [], empty=("Nothing is waiting on a person", "Every proposed rule has been decided and every exception is current.")))
    O.append("</section>")

    # Guard rules in force
    rows = []
    for r in rules:
        sev = severity(r)
        fix = (r.get("fix", "") + (" The gardener can fix this." if r["id"] in guard_fixers else "")).strip()
        rows.append([f'<span class="sf-id">{e(r["id"])}</span>', sev_tag(sev),
                     e(r.get("description") or r.get("type")) + (f'<div class="sf-sub">On failure: {e(fix)}</div>' if fix else ""),
                     e("PR comment" if sev == "info" else enforced_label(r)), e(", ".join(r.get("evidence", [])[:3]) or r.get("source", ""))])
    O.append('<section id="guard" class="sf-panel" role="tabpanel">')
    O.append(table("rules", "Guard rules in force",
                   "From <code>.cartographer/guardrails.json</code>, checked by <code>guard.py</code>: the same change always gets the same verdict. No model decides pass or fail.",
                   ["Rule", "Severity", "What it requires", "Enforced by", "Evidence"], rows,
                   empty=("No guard rules in force", f"{len(props)} proposed rule{'s' if len(props) != 1 else ''} wait on a person under Waiting on a person.")))
    O.append("</section>")

    # Fence checks
    O.append('<section id="fence" class="sf-panel" role="tabpanel">')
    if not fence:
        O.append(table("fence", "Fence checks", None, ["Check"], [], empty=("Not onboarded to the fence", "There is no <code>.fence/fence.json</code> in this repository.")))
    elif not listed:
        O.append(table("fence", "Fence checks", None, ["Check"], [], empty=(
            "The checks are not listed", "<code>.fence/fence.json</code> does not list the checks: this repository's fence defines them in its own code, "
            "so this page cannot show them. Onboarding with the factory template writes the list.")))
    else:
        rows = []
        for c in checks:
            cells = [f'<span class="sf-id">{e(c["id"])}</span>', e(c["category"]), e(c["title"]) + (f'<div class="sf-sub">{e(c["hint"])}</div>' if c["hint"] else ""),
                     f'<code class="sf-tool">{e(c["tool"])}</code>', tag("Gardener fixes", "green") if c["fixer"] else '<span class="sf-muted">A person decides</span>']
            if report:
                res = results.get(c["id"])
                cells.append(tag(res["status"], "red" if res["status"] == "fail" else "green" if res["status"] == "pass" else "cool-gray") if res else "")
            rows.append(cells)
        O.append(table("fence", "Fence checks",
                       "From <code>.fence/fence.json</code>. Where a check has a fixer, the gardener runs it, runs every check again, and commits only if nothing that "
                       "passed now fails. Any other failure is labelled for a person.",
                       ["Check", "Category", "What it requires", "Tool", "On failure"] + (["Last result"] if report else []), rows))
        if deps:
            O.append(table("deps", "Approved dependencies", None, ["Package", "Reason", "Approved by"],
                           [[f'<span class="sf-id">{e(k)}</span>', e(v.get("reason", "")), e(v.get("approvedBy", ""))] for k, v in sorted(deps.items()) if isinstance(v, dict)]))
    O.append("</section>")

    # Exceptions
    O.append('<section id="exceptions" class="sf-panel" role="tabpanel">')
    O.append(table("exceptions", "Exceptions", "An exception needs a reason, a named approver and an expiry date; the fence fails when one expires.",
                   ["Exception", "Where", "Reason", "Approved by", "Expires"],
                   [[x["what"], f"<code>{e(x['where'])}</code>", e(x["reason"]), e(x["by"]), e(x["expires"]) + (" " + tag("Expired", "red") if x["expired"] else "")]
                    for x in exceptions],
                   empty=("No exceptions", "Nothing in this repository is excused from a rule or a check.")))
    O.append("</section>")

    if config.get("judgment"):
        O.append('<section id="judgment" class="sf-panel" role="tabpanel">')
        O.append(table("judgment", "Checked by a person", "Rules no script can check, from <code>.cartographer/config.json</code>. A reviewer checks these on each pull request.",
                       ["Rule", "Why a person checks it"], [[e(j.get("text", "")), e(j.get("why", ""))] for j in config["judgment"]]))
        O.append("</section>")

    O.append('<section id="owners" class="sf-panel" role="tabpanel">')
    O.append(table("owners", "Who approves changes to the rules",
                   f"From <code>{e(owners_file or '.github/CODEOWNERS')}</code>. With code owner review required in the branch settings, GitHub will not merge "
                   "a change to these paths without one of these people.",
                   ["Path", "Owner"], [[f"<code>{e(p)}</code>", e(o)] for p, o in owners],
                   empty=("No owners for the rule files", "Without CODEOWNERS entries, anyone with write access, an agent included, can change the rules in a pull request.")))
    O.append("</section></div>")

    sources = [".cartographer/guardrails.json", ".cartographer/guardrails.proposed.json", ".cartographer/config.json", ".fence/fence.json",
               ".fence/exceptions.json", ".fence/dependencies.json", owners_file or ".github/CODEOWNERS", "ONBOARDING.md"]
    present = [x for x in sources if x and os.path.exists(os.path.join(repo, x))]
    extra = ([f"the fence report {e(fence_report)}"] if report else []) + (["the list of pull requests labelled for a person"] if prs else [])
    O.append('<footer class="sf-foot">Generated by <code>.cartographer/bin/rules_page.py</code> from '
             + ", ".join(f"<code>{e(x)}</code>" for x in present) + ("; " + "; ".join(extra) if extra else "")
             + ". Styled with IBM Carbon (@carbon/styles 1.116.0, Apache-2.0).</footer></main>")
    return page(f"{name} rules", "\n".join(O))


# Layout on top of Carbon: spacing, the page heading in Carbon's type scale, tab panels and expandable rows.
CSS = """
html{color-scheme:light}body{margin:0;background:#ffffff}
.sf-root{background:var(--cds-background);color:var(--cds-text-primary);min-height:100vh;font-family:'IBM Plex Sans',system-ui,-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif}
.sf-main{max-width:84rem;margin:0 auto;padding-block:5rem 3rem;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
@media (min-width:42rem){.sf-main{padding-left:max(2rem,env(safe-area-inset-left,0px));padding-right:max(2rem,env(safe-area-inset-right,0px))}}
.sf-pagehead{margin-bottom:2rem}.sf-title{font-size:2rem;line-height:1.25;font-weight:400;margin:.75rem 0 .5rem}
.sf-meta{font-size:.875rem;line-height:1.43;color:var(--cds-text-secondary);max-width:48rem;margin:0}
.sf-root code{font-family:'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace;font-size:.8125rem}
.sf-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.sf-tabs{overflow-x:auto}.sf-tabs .cds--tab--list{display:flex}.sf-tabs .cds--tabs__nav-link{text-decoration:none;min-width:11rem}
.sf-panels{background:var(--cds-layer-01);padding:1.5rem 0 .5rem}.sf-panel{padding:0 1rem}.sf-panel[hidden]{display:none!important}
.sf-table{margin-bottom:2rem}.sf-table .cds--data-table-content{overflow-x:auto}.sf-table table{min-width:52rem}
.sf-table .cds--data-table tbody td.cds--table-expand{vertical-align:top!important}
.sf-table .cds--data-table td{vertical-align:top;padding-block:1rem}.sf-table .cds--data-table td.cds--table-expand{padding-block:.5rem}
.sf-id{font-family:'IBM Plex Mono',ui-monospace,Menlo,Consolas,monospace;font-size:.875rem;font-weight:600;white-space:nowrap}
.sf-sub{font-size:.75rem;line-height:1.33;color:var(--cds-text-secondary);margin-top:.25rem}.sf-muted{color:var(--cds-text-secondary);font-size:.875rem}
.sf-tool{display:block;max-width:22rem;white-space:normal;word-break:break-word;color:var(--cds-text-secondary)}
.sf-actions{display:flex;gap:.5rem;white-space:nowrap}.sf-actions .cds--btn{text-decoration:none}
.sf-note{max-width:none;margin:0 0 1.5rem}
.sf-detail{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.5rem 1.5rem;margin:0;font-size:.875rem;line-height:1.43;padding-block:.5rem}
.sf-detail dt{color:var(--cds-text-secondary)}.sf-detail dd{margin:0}
.cds--table-expand__svg{fill:var(--cds-icon-primary)}
.sf-empty{padding:1.5rem 0}.sf-empty p{margin:0;font-size:.875rem;color:var(--cds-text-secondary)}
.sf-empty .sf-empty__title{font-size:1.25rem;line-height:1.4;color:var(--cds-text-primary);margin-bottom:.5rem}
.sf-foot{margin-top:2rem;font-size:.75rem;line-height:1.33;color:var(--cds-text-secondary)}
@media (max-width:42rem){.sf-detail{grid-template-columns:1fr}.sf-detail dt{margin-top:.5rem}.sf-panel{padding:0}.sf-panels{padding-top:1rem}}
"""

# Tabs follow the address (#waiting, #guard, ...), so a link can open one; without script every section shows.
# Always Carbon's White theme: the page stays light whatever the viewer's setting.
JS = """(function(){var tabs=[].slice.call(document.querySelectorAll('[data-tab]'));var panels=[].slice.call(document.querySelectorAll('.sf-panel'));
function show(){var id=(location.hash||'').slice(1);if(!panels.some(function(p){return p.id===id}))id=panels[0]&&panels[0].id;
panels.forEach(function(p){p.hidden=p.id!==id});tabs.forEach(function(t){var on=t.getAttribute('data-tab')===id;t.classList.toggle('cds--tabs__nav-item--selected',on);t.setAttribute('aria-selected',on?'true':'false')})}
window.addEventListener('hashchange',show);show();if(location.hash)window.scrollTo(0,0);
tabs.forEach(function(t){t.addEventListener('click',function(ev){ev.preventDefault();history.replaceState(null,'','#'+t.getAttribute('data-tab'));show()})});
document.addEventListener('click',function(ev){var b=ev.target.closest('.cds--table-expand__button');if(!b)return;var row=document.getElementById(b.getAttribute('aria-controls'));
var open=b.getAttribute('aria-expanded')!=='true';b.setAttribute('aria-expanded',open?'true':'false');row.hidden=!open;b.closest('tr').classList.toggle('cds--expandable-row',open);
var td=b.closest('td');if(open)td.setAttribute('data-previous-value','collapsed');else td.removeAttribute('data-previous-value')})})();"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@400;600&display=swap">')


def carbon_css():
    try:
        with open(CARBON) as f:
            return f.read()
    except OSError:
        return ""


def page(title, body):
    return ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            f"<title>{e(title)}</title>\n{FONTS}\n<style>{carbon_css()}</style>\n<style>{CSS}{rules_model.CSS}</style>\n</head>\n<body>\n"
            f'<div id="sf-root" class="sf-root cds--white">\n{body}\n</div>\n<script>{JS}</script>\n</body>\n</html>\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fence-report")
    ap.add_argument("--waiting", help="JSON list of pull requests labelled for a person")
    ap.add_argument("--repo-url")
    ap.add_argument("--issues-off", action="store_true", help="GitHub Issues are disabled: show why Accept and Reject cannot work")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    out = a.out or os.path.join(repo, ".cartographer", "dist", "rules", "index.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as f:
        f.write(build(repo, a.fence_report, a.waiting, a.repo_url, a.issues_off))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
