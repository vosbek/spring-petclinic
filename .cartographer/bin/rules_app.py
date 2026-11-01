#!/usr/bin/env python3
"""The rules workbench: every rule an agent works under in this repository, the evidence for each proposed rule, and
the place where a person accepts or rejects them.

  python3 .cartographer/bin/guard.py --serve            open it for this checkout (a person runs this)
  python3 .cartographer/bin/rules_app.py export OUT     a read-only copy for GitHub Pages (CI runs this)

Local (`serve`): a small server on 127.0.0.1 that only this computer can reach. Accept and Reject change
.cartographer/guardrails.json and guardrails.proposed.json in the checkout, with the person's reason. "Commit and
open pull request" commits only those two files, as the person (their git name and email), on a new branch from
HEAD, pushes it and opens the pull request with their own `gh` login. The rest of the checkout is not touched. The
code owner's merge is what puts a decision in force.

Published (`export`): the same page with the data written in and no buttons that change anything; each proposed rule
says how to decide it in a checkout.

Every line comes from the repository's committed files; the export names the commit it was built from and no clock
time, so the same commit gives the same page. Standard library only.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import carto  # noqa: E402
import guard  # noqa: E402
import rules_model  # noqa: E402

APP = os.path.join(HERE, "..", "viewer", "rules-app.html")
RULE_FILES = (".cartographer/guardrails.json", ".cartographer/guardrails.proposed.json")
LOG = os.path.join(".cartographer", "log", "rule-decisions.json")

# The fence's built-in checks: id -> (category, what it requires, the tool). Titles as factory/fence/checks.mjs reports them.
BUILTINS = {
    "secrets": ("security", "No secrets in the change", "pattern scan of added lines"),
    "workflows": ("security", "CI workflows: least privilege, no script injection", "workflow scan for write permissions and pasted event text"),
    "duplicates": ("patterns", "No new copied code", "jscpd"),
    "dependencies": ("patterns", "New dependencies are approved", "package.json against .fence/dependencies.json"),
    "map-coverage": ("patterns", "New code belongs to a mapped component", "the codebase map (.cartographer/model)"),
    "guard": ("structure", "Architecture and process rules (guard.py)", "guard.py with the accepted rules"),
    "formal": ("tests", "Formal models: the code's model holds, broken variants still fail", "formal.py: TLC on verification/"),
    "exceptions": ("structure", "No expired exceptions", ".fence/exceptions.json"),
}
OWNED = (".fence", ".cartographer/guardrails.json", ".cartographer/baseline.json", ".cartographer/hooks", ".github/workflows",
         "verification", ".github/CODEOWNERS", "laws")
CATEGORY_ORDER = ["style", "practices", "patterns", "security", "performance", "structure", "tests", "database"]


class Refused(Exception):
    """An action the workbench will not take, with the reason shown to the person."""


# ----------------------------------------------------------------------------- reading the repository

def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def run(repo, *argv, env=None, input=None, timeout=60):
    return subprocess.run(list(argv), cwd=repo, capture_output=True, text=True, env=env, input=input, timeout=timeout)


def git(repo, *args, env=None):
    r = run(repo, "git", *args, env=env)
    return r.stdout.strip() if r.returncode == 0 else ""


def at_head(repo, path, default):
    r = run(repo, "git", "show", f"HEAD:{path}")
    if r.returncode != 0:
        return default
    try:
        return json.loads(r.stdout)
    except ValueError:
        return default


def severity(rule):
    return rule.get("severity") or guard.DEFAULT_SEVERITY.get(rule.get("type"), "block")


def num(x):
    """A count from an agent-written file, as a number or None; anything else is dropped, never shown."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return int(x) if float(x).is_integer() else round(float(x), 1)


def replay_of(rule):
    rp = rule.get("_replay") if isinstance(rule.get("_replay"), dict) else {}
    out = {k: num(rp.get(k)) for k in ("commits", "fired", "followedByFixOrRevert", "hitRatePct", "baseRatePct")}
    out["replayable"] = rp.get("replayable") is True
    out["head"] = rp["head"] if isinstance(rp.get("head"), str) and re.fullmatch(r"[0-9a-f]{4,40}", rp["head"]) else ""
    out["examples"] = [{"sha": x["sha"], "subject": str(x.get("subject", ""))[:200], "outcome": str(x.get("outcome") or "")[:40]}
                       for x in (rp.get("examples") or []) if isinstance(x, dict) and isinstance(x.get("sha"), str) and re.fullmatch(r"[0-9a-f]{4,40}", x["sha"])][:5]
    out["any"] = bool(rp)
    return out


def replayable(rule):
    rp = replay_of(rule)
    return rp["any"] and rp["replayable"] and rp["commits"] is not None and rp["fired"] is not None and rule.get("type") in guard.REPLAYABLE


def replay_text(rule):
    """The proposal's replay against history in a sentence a reviewer can check against guard.py --replay."""
    rp = replay_of(rule)
    if not rp["any"]:
        return f"Not replayed yet. guard.py --replay --rule {rule['id']} shows how often it would have fired."
    if not replayable(rule):
        return "This kind of rule cannot be replayed against past commits; it applies from the day it is accepted."
    n, fired, bad, base = rp["commits"], rp["fired"], rp["followedByFixOrRevert"] or 0, rp["baseRatePct"]
    if fired == 0:
        return f"It would not have fired on any of the last {n} commits, so accepting it costs nothing today."
    s = f"It would have flagged {fired} of the last {n} commits. "
    if bad == 0:
        s += f"None of {'those' if fired > 1 else 'them'} was reverted or needed a fix to the same files within 14 days"
        s += (f" (across all commits that happens {base}% of the time)" if base is not None else "")
        return s + ", so today it would mostly flag changes that were fine."
    s += f"{bad} of {'those' if fired > 1 else 'them'} {'were' if bad > 1 else 'was'} reverted or needed a fix within 14 days"
    if rp["hitRatePct"] is not None and base is not None:
        s += f" ({rp['hitRatePct']}%, against {base}% across all commits)"
    return s + "."


def evidence_rows(rule):
    rp = replay_of(rule)
    if not rp["any"]:
        return []
    if not replayable(rule):
        return [["Replayable on past commits", "No"], ["Applies from", "The day it is accepted"]]
    rows = [["Commits replayed", str(rp["commits"])], ["Commits it would have flagged", str(rp["fired"])]]
    rows.append(["Of those, reverted or fixed within 14 days", str(rp["followedByFixOrRevert"] or 0) if rp["fired"] else "–"])
    if rp["baseRatePct"] is not None:
        rows.append(["Reverted or fixed within 14 days, all commits", f"{rp['baseRatePct']}%"])
    return rows


def definition(rule):
    return json.dumps({k: v for k, v in rule.items() if not k.startswith("_") and k != "decided"}, indent=2, ensure_ascii=False)


def codeowners(repo):
    for p in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = os.path.join(repo, p)
        if os.path.exists(path):
            rows = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        pat, *owners = line.split()
                        if pat.lstrip("/").startswith(OWNED):
                            rows.append({"path": pat, "owners": " ".join(owners)})
            return p, rows
    return None, []


def rule_owners(owner_rows):
    for row in owner_rows:
        if row["path"].lstrip("/") == ".cartographer/guardrails.json":
            return row["owners"]
    return ""


def person_steps(repo):
    path = os.path.join(repo, "ONBOARDING.md")
    out = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*-\s*\[ \]\s*\(person\)\s*(.+)", line)
                if m:
                    out.append(m.group(1).strip())
    return out


def cmd_text(argv):
    return " ".join(a if re.fullmatch(r"[\w@%+=:,./{}-]+", a) else "'" + a.replace("'", "'\\''") + "'" for a in argv)


def gh_login(repo):
    try:
        r = run(repo, "gh", "api", "user", "--jq", ".login", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def remote_slug(repo):
    url = git(repo, "config", "--get", "remote.origin.url")  # as written, before any insteadOf rewriting
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else ""


def pending(repo):
    """Decisions in the checkout that are not committed: accepted rules new since HEAD, rejections new since HEAD."""
    home = os.path.join(repo, ".cartographer")
    head_g = {r["id"] for r in at_head(repo, RULE_FILES[0], {}).get("rules", [])}
    head_rej = {x.get("id") for x in at_head(repo, RULE_FILES[1], {}).get("rejected", [])}
    work_g = load(os.path.join(home, "guardrails.json"), {}).get("rules", [])
    work_rej = load(os.path.join(home, "guardrails.proposed.json"), {}).get("rejected", [])
    out = []
    for r in work_g:
        if r["id"] not in head_g:
            d = r.get("decided") or {}
            out.append({"id": r["id"], "decision": "accept", "why": d.get("why", ""), "by": d.get("by", "")})
    for x in work_rej:
        if x.get("id") not in head_rej:
            out.append({"id": x["id"], "decision": "reject", "why": x.get("why", ""), "by": x.get("by", "")})
    return out


def committed(repo):
    """Both rule files as they are at HEAD, or None when either is not committed (decisions are made against HEAD)."""
    g = at_head(repo, RULE_FILES[0], None)
    p = at_head(repo, RULE_FILES[1], None)
    return (g, p) if isinstance(g, dict) and isinstance(p, dict) else None


def expected(repo):
    """The rule files as HEAD plus the decisions recorded in the checkout, and nothing else. The checkout must match."""
    base = committed(repo)
    if not base:
        return None
    home = os.path.join(repo, ".cartographer")
    g, p = copy.deepcopy(base[0]), copy.deepcopy(base[1])
    props = {r["id"]: r for r in p.get("rules", [])}
    head_ids = {r["id"] for r in g.get("rules", [])}
    head_rej = {x.get("id") for x in p.get("rejected", [])}
    work_g = load(os.path.join(home, "guardrails.json"), {})
    work_p = load(os.path.join(home, "guardrails.proposed.json"), {})
    decided = set()
    for r in work_g.get("rules", []):
        if r.get("id") in head_ids or r.get("id") not in props:
            continue
        orig = props[r["id"]]
        built = {k: v for k, v in orig.items() if not k.startswith("_")}
        built["status"] = "active"
        built.setdefault("added", r.get("added"))
        d = r.get("decided")
        if isinstance(d, dict):
            built["decided"] = {k: d[k] for k in ("by", "on", "why") if k in d}
            if orig.get("_replay"):
                built["decided"]["replay"] = orig["_replay"]
        g.setdefault("rules", []).append(built)
        decided.add(r["id"])
    for x in work_p.get("rejected", []):
        if x.get("id") in head_rej or x.get("id") not in props:
            continue
        p.setdefault("rejected", []).append({k: x[k] for k in ("id", "rejected", "by", "why") if k in x})
        decided.add(x["id"])
    p["rules"] = [r for r in p.get("rules", []) if r["id"] not in decided]
    return g, p


def stray(repo):
    """Changes to the rule files that are not decisions made in the workbench (a hand edit, an agent's edit)."""
    exp = expected(repo)
    if exp is None:
        return ["The rule files are not both committed. Commit .cartographer/guardrails.json and guardrails.proposed.json first: "
                "decisions are made against the committed proposals."]
    home = os.path.join(repo, ".cartographer")
    out = []
    for path, want in zip(RULE_FILES, exp):
        have = load(os.path.join(repo, path), None)
        if have != want:
            out.append(f"{path} has changes that are not decisions made here. Review them with git diff, and commit or discard them before deciding.")
    return out


def opened_prs(repo, check_state=False):
    """Pull requests this workbench opened, from the local log. A link is shown only if it is this repository's pull
    request and GitHub says it is open, so an edited log cannot hide a rule's decision or point anywhere else."""
    slug = remote_slug(repo)
    entries = load(os.path.join(repo, LOG), [])
    out = []
    for x in entries if isinstance(entries, list) else []:
        m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", str(x.get("url", "")))
        if not m or m.group(1) != slug:
            continue
        x = dict(x, number=int(m.group(2)), state="unverified")
        if check_state:
            try:
                r = run(repo, "gh", "api", f"repos/{slug}/pulls/{x['number']}", "--jq", ".state", timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    x["state"] = r.stdout.strip().lower()
            except (OSError, subprocess.TimeoutExpired):
                pass
        out.append(x)
    return out


def state(repo, fence_report=None, waiting=None, repo_url=None, local=False):
    """Everything the workbench shows, as one JSON-ready dict."""
    repo = os.path.abspath(repo)
    home = os.path.join(repo, ".cartographer")
    accepted = load(os.path.join(home, "guardrails.json"), {"rules": [], "exceptions": []})
    proposed = load(os.path.join(home, "guardrails.proposed.json"), {"rules": []})
    config = load(os.path.join(home, "config.json"), {})
    fence = load(os.path.join(repo, ".fence", "fence.json"), None)
    fence_exc = load(os.path.join(repo, ".fence", "exceptions.json"), [])
    deps = load(os.path.join(repo, ".fence", "dependencies.json"), {})
    report = load(fence_report, None) if fence_report else None
    waiting_prs = load(waiting, []) if waiting else []

    slug = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else remote_slug(repo)
    url = repo_url or (f"https://github.com/{slug}" if slug else "")
    sha = git(repo, "rev-parse", "HEAD")
    day = git(repo, "log", "-1", "--format=%cs")
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    owners_file, owners = codeowners(repo)
    head_g = {r["id"] for r in at_head(repo, RULE_FILES[0], {}).get("rules", [])} if local else None

    prs = [x for x in opened_prs(repo, check_state=True) if x["state"] == "open"] if local else []
    in_pr = {}
    for x in prs:
        for d in x.get("decisions", []):
            if isinstance(d, dict) and isinstance(d.get("id"), str):
                in_pr[d["id"]] = {"url": x["url"], "branch": str(x.get("branch", "")), "decision": "accept" if d.get("decision") == "accept" else "reject"}

    pend = {p["id"]: p for p in pending(repo)} if local else {}
    rules = accepted.get("rules", [])
    in_force = []
    for r in rules:
        if local and r["id"] not in head_g:
            continue  # accepted in the checkout but not committed: still waiting, shown with its pending decision
        d = r.get("decided") or {}
        in_force.append({"id": r["id"], "type": r.get("type"), "severity": severity(r), "description": r.get("description") or r.get("type"),
                         "enforcedBy": "A note on the pull request" if severity(r) == "info" else guard.enforced_label(r),
                         "fix": r.get("fix", ""), "decided": {k: d.get(k, "") for k in ("by", "on", "why")}, "definition": definition(r)})
    # Proposals from HEAD's file when local, so a rule decided in the checkout stays in the list with its decision.
    prop_rules = at_head(repo, RULE_FILES[1], proposed).get("rules", []) if local else proposed.get("rules", [])
    committed_ids = {r["id"] for r in in_force}
    waiting_rules = []
    for r in prop_rules:
        if r["id"] in committed_ids:
            continue
        rp = replay_of(r)
        waiting_rules.append({
            "id": r["id"], "type": r.get("type"), "severity": severity(r), "description": r.get("description") or r.get("type"),
            "why": r.get("_why") or "", "added": r.get("added", ""), "source": r.get("source", ""),
            "replayable": replayable(r), "fired": rp["fired"] if replayable(r) else None, "commits": rp["commits"] if replayable(r) else None,
            "replayHead": rp["head"], "evidence": evidence_rows(r), "reading": replay_text(r), "examples": rp["examples"],
            "enforcedBy": "A note on the pull request" if severity(r) == "info" else guard.enforced_label(r),
            "definition": definition(r), "pending": pend.get(r["id"]), "pr": in_pr.get(r["id"])})
    rejected = []
    head_rej = {x.get("id") for x in at_head(repo, RULE_FILES[1], {}).get("rejected", [])} if local else None
    for x in (at_head(repo, RULE_FILES[1], proposed) if local else proposed).get("rejected", []):
        rejected.append({"id": x.get("id"), "on": x.get("rejected", ""), "by": x.get("by", ""), "why": x.get("why", "")})

    exceptions = []
    for x in accepted.get("exceptions", []):
        exceptions.append({"kind": "Guard rule", "name": x.get("rule", "?"), "where": x.get("path", ""), "reason": x.get("reason", ""),
                           "by": x.get("approvedBy", ""), "expires": x.get("expires", "")})
    for x in fence_exc if isinstance(fence_exc, list) else []:
        exceptions.append({"kind": "Fence check", "name": x.get("check", "?"), "where": x.get("file", ""), "reason": x.get("reason", ""),
                           "by": x.get("approvedBy") or x.get("approver", ""), "expires": x.get("expires", "")})
    for x in exceptions:
        x["expired"] = bool(day and x["expires"] and x["expires"] < day)
        x["incomplete"] = not (x["reason"] and x["by"] and x["expires"])

    checks = []
    results = {r["id"].split(":")[0]: r for r in (report or {}).get("results", [])}
    listed = bool(fence) and ("builtins" in fence or bool(fence.get("checks")))
    if listed:
        fixers = fence.get("fixers", {})
        for cid in fence.get("builtins", []):
            cat, title, tool = BUILTINS.get(cid, ("structure", cid, ""))
            checks.append({"id": cid, "category": cat, "title": title, "tool": tool, "fixer": bool(fixers.get(cid)), "note": ""})
        for c in fence.get("checks", []):
            checks.append({"id": c["id"], "category": c.get("category", "structure"), "title": c.get("title", c["id"]),
                           "tool": cmd_text(c.get("run", [])), "fixer": bool(fixers.get(c["id"])), "note": c.get("hint", "")})
    for c in checks:
        if c["id"] == "dependencies" and not os.path.exists(os.path.join(repo, "package.json")):
            c["note"] = c["note"] or "Does nothing here: it reads package.json, and this repository has none."
        elif c["id"] == "formal" and not os.path.isdir(os.path.join(repo, "verification")):
            c["note"] = c["note"] or "Does nothing here: no verification/ folder."
        elif c["id"] == "map-coverage" and not os.path.exists(os.path.join(home, "model", "model.json")):
            c["note"] = c["note"] or "Does nothing here yet: no codebase map."
        elif c["id"] == "guard" and not in_force:
            c["note"] = c["note"] or "Nothing to enforce yet: no guard rule is in force."
        res = results.get(c["id"])
        c["result"] = res["status"] if res else ""

    model = {"n_rules": len(in_force), "n_props": len(waiting_rules), "n_checks": len(checks), "checks": checks,
             "categories": sorted({c["category"] for c in checks}, key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99),
             "replay_commits": max([w["commits"] or 0 for w in waiting_rules] or [0]),
             "n_judgment": len(config.get("judgment", [])), "has_package_json": os.path.exists(os.path.join(repo, "package.json")),
             "has_verification": os.path.isdir(os.path.join(repo, "verification")),
             "hidden_workflow": os.path.exists(os.path.join(repo, ".github", "workflows", "hidden-tests.yml"))}

    sources = [RULE_FILES[0], RULE_FILES[1], ".cartographer/config.json", ".fence/fence.json", ".fence/exceptions.json",
               ".fence/dependencies.json", owners_file or ".github/CODEOWNERS", "ONBOARDING.md"]
    return {
        "mode": "local" if local else "published",
        "repo": {"name": slug.split("/")[-1] if slug else os.path.basename(repo), "slug": slug, "url": url, "sha": sha, "branch": branch if local else "",
                 "day": day, "login": gh_login(repo) if local else "", "ruleOwners": rule_owners(owners)},
        "waiting": waiting_rules,
        "inForce": in_force,
        "rejected": rejected,
        "pending": list(pend.values()),
        "openPrs": [{"url": x["url"], "title": str(x.get("title", ""))} for x in prs],
        "stray": stray(repo) if local else [],
        "exceptions": exceptions,
        "checks": checks,
        "checksListed": listed,
        "hasFence": bool(fence),
        "deps": [{"name": k, "reason": v.get("reason", ""), "by": v.get("approvedBy", "")} for k, v in sorted(deps.items()) if isinstance(v, dict)],
        "judgment": [{"text": j.get("text", ""), "why": j.get("why", "")} for j in config.get("judgment", [])],
        "owners": {"file": owners_file or ".github/CODEOWNERS", "rows": owners},
        "personSteps": person_steps(repo),
        "waitingPrs": waiting_prs,
        "howItWorks": rules_model.section(model, simple_table, None),
        "sources": [s for s in sources if os.path.exists(os.path.join(repo, s))],
    }


def e(x):
    return html.escape(str(x), quote=True)


def simple_table(key, title, desc, head, rows, empty=None, expandable=False):
    """A plain table for the How it works section (rules_model passes HTML cells)."""
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (f'<div class="sf-block"><h2 class="sf-h2">{title}</h2>' + (f'<p class="sf-lede">{desc}</p>' if desc else "")
            + '<div class="scroll-x"><table class="data"><thead><tr>' + "".join(f'<th scope="col">{h}</th>' for h in head)
            + f"</tr></thead><tbody>{body}</tbody></table></div></div>")


# ----------------------------------------------------------------------------- changing the checkout

def _home(repo):
    return os.path.join(repo, ".cartographer")


def _who(repo):
    return gh_login(repo) or git(repo, "config", "user.name") or "unknown"


def decide(repo, rule_id, decision, why, by=None):
    """Accept or reject one proposed rule in the checkout, with the person's reason."""
    why = (why or "").strip()
    if decision not in ("accept", "reject"):
        raise Refused("A decision is Accept or Reject.")
    if not why:
        raise Refused("Write a reason: it is recorded with the decision.")
    if len(why) > 2000:
        raise Refused("Keep the reason under 2,000 characters.")
    problems = stray(repo)
    if problems:
        raise Refused(problems[0])
    # The proposal decided is the committed one, the one the page showed: the checkout matches HEAD plus decisions.
    props = {r["id"] for r in load(os.path.join(_home(repo), "guardrails.proposed.json"), {}).get("rules", [])}
    if rule_id not in props:
        raise Refused(f"{rule_id} is not a proposed rule in this checkout (already decided, or not proposed).")
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):  # guard.accept prints for the command line
        guard.accept(_home(repo), [rule_id] if decision == "accept" else [], [rule_id] if decision == "reject" else [], why=why, by=by or _who(repo))


def restore(repo, force=False):
    """Put both rule files back as they are at HEAD. Refuses when they hold changes that are not workbench decisions,
    so a hand edit is never thrown away from here."""
    if not force and committed(repo) and stray(repo):
        raise Refused(stray(repo)[0])
    for path in RULE_FILES:
        if run(repo, "git", "cat-file", "-e", f"HEAD:{path}").returncode == 0:
            run(repo, "git", "checkout", "HEAD", "--", path)


def undo(repo, rule_id):
    """Take back one decision that is not committed yet."""
    problems = stray(repo)
    if problems:
        raise Refused(problems[0])
    home = _home(repo)
    head_g = at_head(repo, RULE_FILES[0], {"rules": []})
    head_p = at_head(repo, RULE_FILES[1], {"rules": []})
    if rule_id not in {p["id"] for p in pending(repo)}:
        raise Refused(f"There is no uncommitted decision on {rule_id}.")
    original = next((r for r in head_p.get("rules", []) if r["id"] == rule_id), None)
    g = load(os.path.join(home, "guardrails.json"), {"schemaVersion": 1, "rules": [], "exceptions": []})
    p = load(os.path.join(home, "guardrails.proposed.json"), {"schemaVersion": 1, "rules": []})
    if rule_id not in {r["id"] for r in head_g.get("rules", [])}:
        g["rules"] = [r for r in g.get("rules", []) if r["id"] != rule_id]
    head_rej = {x.get("id") for x in head_p.get("rejected", [])}
    if rule_id not in head_rej and "rejected" in p:
        p["rejected"] = [x for x in p["rejected"] if x.get("id") != rule_id]
        if not p["rejected"] and "rejected" not in head_p:
            del p["rejected"]
    if original and rule_id not in {r["id"] for r in p.get("rules", [])}:
        order = [r["id"] for r in head_p.get("rules", [])]
        p["rules"] = sorted(p.get("rules", []) + [original], key=lambda r: order.index(r["id"]) if r["id"] in order else len(order))
    carto.save(os.path.join(home, "guardrails.json"), g, pretty=True)
    carto.save(os.path.join(home, "guardrails.proposed.json"), p, pretty=True)
    if not pending(repo):
        restore(repo, force=True)


def suggested_branch(repo):
    day = dt.date.today().isoformat()
    taken = set(git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes").split())
    name, n = f"rules/{day}", 2
    while name in taken or f"origin/{name}" in taken:
        name, n = f"rules/{day}-{n}", n + 1
    return name


def pr_text(repo, decisions, who):
    acc = [d["id"] for d in decisions if d["decision"] == "accept"]
    rej = [d["id"] for d in decisions if d["decision"] == "reject"]
    parts = ([f"accept {', '.join(acc)}"] if acc else []) + ([f"reject {', '.join(rej)}"] if rej else [])
    title = "Guard rules: " + "; ".join(parts) if parts else "Guard rules"
    lines = []
    for d in decisions:
        lines += [f"**{'Accept' if d['decision'] == 'accept' else 'Reject'}** `{d['id']}`", "", f"> {d['why']}", ""]
    lines += [f"Decided by @{who} in the rules workbench (`guard.py --serve`). This changes only `.cartographer/guardrails.json` "
              "and `.cartographer/guardrails.proposed.json`; the evidence each decision rests on is recorded with it. "
              "Merging this pull request puts the accepted rules in force."]
    return title[:120], "\n".join(lines) + "\n"


def open_pr(repo, branch=None):
    """Commit the rule files as the person on a new branch from HEAD, push it and open the pull request with their gh login."""
    decisions = pending(repo)
    if not decisions:
        raise Refused("There are no decisions to commit.")
    problems = stray(repo)
    if problems:
        raise Refused(problems[0])
    files = expected(repo)
    who = gh_login(repo)
    if not who:
        raise Refused("GitHub CLI is not signed in. Run gh auth login in a terminal, then try again.")
    slug = remote_slug(repo)
    if not slug:
        raise Refused("This checkout has no GitHub remote named origin.")
    if not (git(repo, "config", "user.name") and git(repo, "config", "user.email")):
        raise Refused("Set your git name and email (git config user.name and user.email): the commit is made as you.")
    branch = branch or suggested_branch(repo)
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", branch) or ".." in branch or branch.startswith(("-", "/")):
        raise Refused("That branch name is not allowed.")
    base = guard.default_branch(repo).replace("origin/", "")
    if branch in (base, "main", "master", "HEAD") or branch.startswith(("refs/", "origin/")):
        raise Refused(f"Use a new branch for the pull request, not {branch}.")
    run(repo, "git", "fetch", "-q", "origin", base, timeout=120)
    if run(repo, "git", "ls-remote", "--exit-code", "--heads", "origin", branch, timeout=60).returncode == 0:
        raise Refused(f"A branch named {branch} already exists on GitHub. Choose another name.")
    if run(repo, "git", "merge-base", "--is-ancestor", "HEAD", f"origin/{base}").returncode != 0:
        raise Refused(f"Your checkout has commits that are not on origin/{base}, and the pull request would carry them. "
                      f"Switch to {base} (or push those commits first) and decide again.")
    title, body = pr_text(repo, decisions, who)
    message = f"{title}\n\n" + "\n".join(f"{'Accept' if d['decision'] == 'accept' else 'Reject'} {d['id']}: {d['why']}" for d in decisions) + f"\n\nDecided-by: {who}\n"
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(tmp, "index"))
        if run(repo, "git", "read-tree", "HEAD", env=env).returncode != 0:
            raise Refused("Could not read HEAD.")
        for path, content in zip(RULE_FILES, files):
            # What is committed is HEAD plus the decisions, rebuilt here, so nothing else can ride along.
            r = run(repo, "git", "hash-object", "-w", "--stdin", input=json.dumps(content, indent=2, ensure_ascii=False) + "\n")
            blob = r.stdout.strip()
            if run(repo, "git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env).returncode != 0:
                raise Refused(f"Could not stage {path}.")
        tree = git(repo, "write-tree", env=env)
    commit = git(repo, "commit-tree", tree, "-p", "HEAD", "-m", message)
    if not commit:
        raise Refused("Could not make the commit.")
    # --force-with-lease with an empty expectation: the push succeeds only if the branch does not exist yet.
    push = run(repo, "git", "push", f"--force-with-lease=refs/heads/{branch}:", "origin", f"{commit}:refs/heads/{branch}", timeout=120)
    if push.returncode != 0:
        raise Refused("The push was refused: " + (push.stderr.strip().splitlines() or ["no reason given"])[-1])
    # The REST API, which GitHub Enterprise Server and every gh version support.
    pr = run(repo, "gh", "api", "-X", "POST", f"repos/{slug}/pulls", "--input", "-",
             input=json.dumps({"title": title, "head": branch, "base": base, "body": body}), timeout=120)
    try:
        made = json.loads(pr.stdout) if pr.returncode == 0 else {}
    except ValueError:
        made = {}
    if not made.get("html_url"):
        raise Refused(f"Pushed {branch}, but gh could not open the pull request: " + ((pr.stderr or pr.stdout).strip().splitlines() or ["no reason given"])[-1])
    url = made["html_url"]
    reviewers = [o.lstrip("@") for o in rule_owners(codeowners(repo)[1]).split() if o.startswith("@") and "/" not in o and o.lstrip("@") != who]
    if reviewers:  # best effort: GitHub refuses a reviewer without access, and that must not lose the pull request
        run(repo, "gh", "api", "-X", "POST", f"repos/{slug}/pulls/{made.get('number')}/requested_reviewers", "--input", "-",
            input=json.dumps({"reviewers": reviewers}), timeout=60)
    log = load(os.path.join(repo, LOG), [])
    log = log if isinstance(log, list) else []
    log.append({"url": url, "branch": branch, "commit": commit, "title": title,
                "decisions": [{k: d[k] for k in ("id", "decision", "why")} for d in decisions]})
    carto.save(os.path.join(repo, LOG), log, pretty=True)
    restore(repo, force=True)
    return {"url": url, "branch": branch, "commit": commit}


# ----------------------------------------------------------------------------- the page

CSP = ("default-src 'none'; script-src {script}; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src data:; connect-src 'self'; base-uri 'none'; form-action 'none'")


def page(data):
    """The page with the data written in, and the Content-Security-Policy that lets only that one script run.

    The data is JSON with <, > and & written as \\u escapes, so no string in it (a rule description, a reason) can
    end the script element or start a comment inside it. The page's script is allowed by its hash, so nothing else
    runs, whatever a string turns out to contain."""
    with open(APP, encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    out = tpl.replace("/*__STATE__*/null", blob, 1)
    i = out.index("<script>") + len("<script>")
    j = out.index("</script>", i)
    digest = base64.b64encode(hashlib.sha256(out[i:j].encode("utf-8")).digest()).decode()
    csp = CSP.format(script=f"'sha256-{digest}'")
    out = out.replace("<head>", f'<head>\n<meta http-equiv="Content-Security-Policy" content="{csp}">', 1)
    return out, csp


def export(repo, out, fence_report=None, waiting=None, repo_url=None):
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page(state(repo, fence_report, waiting, repo_url, local=False))[0])
    return out


COOKIE = "rules_session"


class Handler(BaseHTTPRequestHandler):
    server_version = "rules-workbench"

    def log_message(self, fmt, *args):  # quiet: the terminal shows only what the person needs
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", csp=None, extra=()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", (csp or CSP.format(script="'none'")) + "; frame-ancestors 'none'")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def _session_ok(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and secrets.compare_digest(v, self.server.session):
                return True
        return False

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, '{"error":"wrong host"}')
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            q = urllib.parse.parse_qs(u.query)
            key = q.get("t", [""])[0]
            if key:
                # The printed address is used once: it sets a session cookie the page's script cannot read, and is then
                # dead, so the copy in the browser's history opens nothing.
                if self.server.url_key and secrets.compare_digest(key, self.server.url_key):
                    self.server.url_key = None
                    return self._send(303, "", "text/plain", extra=(("Location", "/"), ("Set-Cookie", f"{COOKIE}={self.server.session}; HttpOnly; SameSite=Strict; Path=/")))
                if not self._session_ok():
                    return self._send(403, "<!doctype html><title>Rules</title><p>That address has been used. Stop the workbench and run guard.py --serve again for a new one.</p>",
                                      "text/html; charset=utf-8")
                return self._send(303, "", "text/plain", extra=(("Location", "/"),))
            if not self._session_ok():
                return self._send(403, "<!doctype html><title>Rules</title><p>Open the address printed in the terminal that started the workbench.</p>",
                                  "text/html; charset=utf-8")
            body, csp = page(state(self.server.repo, local=True))
            return self._send(200, body, "text/html; charset=utf-8", csp=csp)
        if u.path == "/api/state":
            if not self._session_ok() or self.headers.get("X-Rules") != "1":
                return self._send(403, '{"error":"not this page"}')
            return self._send(200, json.dumps(state(self.server.repo, local=True), ensure_ascii=False))
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        if not self._host_ok() or origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
            return self._send(403, '{"error":"same origin only"}')
        if not self._session_ok() or self.headers.get("X-Rules") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(403, '{"error":"not this page"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, '{"error":"bad json"}')
        repo = self.server.repo
        try:
            with self.server.lock:
                result = {}
                if self.path == "/api/decide":
                    decide(repo, str(body.get("rule", "")), str(body.get("decision", "")), str(body.get("why", "")))
                elif self.path == "/api/undo":
                    undo(repo, str(body.get("rule", "")))
                elif self.path == "/api/discard":
                    restore(repo)
                elif self.path == "/api/branch":
                    who = gh_login(repo) or "you"
                    title, text = pr_text(repo, pending(repo), who)
                    diff = run(repo, "git", "diff", "--no-color", "HEAD", "--", *RULE_FILES).stdout
                    result = {"branch": suggested_branch(repo), "title": title, "body": text, "diff": diff[:20000]}
                elif self.path == "/api/pr":
                    result = open_pr(repo, str(body.get("branch") or "") or None)
                else:
                    return self._send(404, '{"error":"not found"}')
        except Refused as x:
            return self._send(409, json.dumps({"error": str(x)}))
        data = state(repo, local=True)
        data["result"] = result
        return self._send(200, json.dumps(data, ensure_ascii=False))


def make_server(repo, port=4340, token=None):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.repo = os.path.abspath(repo)
    srv.url_key = token or secrets.token_urlsafe(24)
    srv.token = srv.url_key
    srv.session = secrets.token_urlsafe(32)
    srv.lock = threading.Lock()
    return srv


def serve(repo, port=4340, open_browser=True):
    if not os.path.exists(os.path.join(repo, ".cartographer", "guardrails.proposed.json")) and not os.path.exists(os.path.join(repo, ".cartographer", "guardrails.json")):
        print("No rule files in .cartographer/. Propose rules first with guard.py --suggest --write.", file=sys.stderr)
        return 1
    try:
        srv = make_server(repo, port)
    except OSError:
        srv = make_server(repo, 0)
    url = f"http://127.0.0.1:{srv.server_port}/?t={srv.token}"
    # The address carries the key to this checkout's rule files. It goes to a person's terminal and browser, never into
    # a log an agent reads: with no terminal attached, the workbench does not start.
    if not sys.stdout.isatty():
        print("The rules workbench is for a person: run guard.py --serve in your own terminal.", file=sys.stderr)
        srv.server_close()
        return 2
    print(f"Rules workbench for {os.path.basename(os.path.abspath(repo))}: {url}")
    print("Only this computer can reach it. Ctrl+C to stop.")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="open the workbench for this checkout (same as guard.py --serve)")
    s.add_argument("--repo", default=".")
    s.add_argument("--port", type=int, default=4340)
    s.add_argument("--no-browser", action="store_true")
    x = sub.add_parser("export", help="write the read-only page")
    x.add_argument("out")
    x.add_argument("--repo", default=".")
    x.add_argument("--fence-report")
    x.add_argument("--waiting", help="JSON list of pull requests labelled for a person: [{number, title, url}]")
    x.add_argument("--repo-url")
    a = ap.parse_args(argv[1:])
    if a.cmd == "serve":
        return serve(os.path.abspath(a.repo), a.port, not a.no_browser)
    print("wrote " + export(os.path.abspath(a.repo), a.out, a.fence_report, a.waiting, a.repo_url))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
