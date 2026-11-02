#!/usr/bin/env python3
"""Business intent as a checked record: what the owner wants, shown as worked examples, routed to the systems that
change, approved by the people who decide, and split into one contract per system for the factory.

    intent.py new <dir> --request "..." [--owner "Name, role"] [--title "..."]   a record to fill in
    intent.py check <dir> [--map system=path/to/model.json ...]                  what blocks it, and who acts next
    intent.py render <dir> [-o file.md]                                           the record as a page the owner reads
    intent.py approve <dir> --role owner|architect --by "Name"                    a person approves (agents are refused)
    intent.py contract <dir> --system <name> [-o file.json]                       one system's contract, once approved

A record is a folder: intent.json (the content, which agents draft) and approvals.json (who approved which version,
which only people write). Each approval holds the hash of intent.json; any later change to the content leaves the
approval stale until the person approves again.

The checks are the point. A record cannot be approved while a rule has no worked example, an example is not concrete,
a question is open, an assumption is unconfirmed, an example is not placed in a system on the map, or a system has not
been confirmed by its architect.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

SOURCES = {"owner", "code", "assumption"}
ROLES = ("owner", "architect")
# Words that make a stated result impossible to test. A then-clause with one of these needs a concrete value.
VAGUE = re.compile(r"\b(appropriate(ly)?|correct(ly)?|proper(ly)?|as expected|as needed|etc|user[- ]friendly|fast(er)?|quick(ly)?|"
                   r"easy|easier|seamless(ly)?|intuitive(ly)?|reasonable|relevant|optimal|efficient(ly)?|various|handle[sd]?)\b", re.I)


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def save(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def files(d):
    return os.path.join(d, "intent.json"), os.path.join(d, "approvals.json")


def digest(rec, role="architect"):
    """The version a person approves, canonical. The owner approves what is wanted (everything but the systems), so the
    architect can place it in systems without voiding the owner's approval; the architect approves all of it. Any change
    to the rules, examples, scope or answers voids both."""
    body = {k: v for k, v in rec.items() if not (role == "owner" and k == "impact")}
    return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def blank(rec_id, request, owner="", title=""):
    name, _, role = (owner or "").partition(",")
    return {
        "schemaVersion": 1, "id": rec_id, "title": title or "TODO: a short name for the change",
        "request": request, "owner": {"name": name.strip(), "role": role.strip()},
        "outcome": "", "for": [], "rules": [], "examples": [], "outOfScope": [], "questions": [], "impact": [],
    }


# ---------- checks ----------

def problems(rec, approvals=None, maps=None, root="."):
    """Everything that blocks the record, as (stage, who, text). Stages run in order: capture (the owner's answers),
    impact (the architect's confirmation), approval (both people, on this exact version)."""
    out = []

    def add(stage, who, text):
        out.append((stage, who, text))

    if not (rec.get("request") or "").strip():
        add("capture", "agent", "the owner's original request is not recorded (keep their words in request)")
    if not (rec.get("owner") or {}).get("name"):
        add("capture", "agent", "no business owner is named; someone has to own the decisions")
    if not (rec.get("outcome") or "").strip():
        add("capture", "owner", "no outcome: what is different for the business when this is done")
    if (rec.get("title") or "").startswith("TODO"):
        add("capture", "agent", "the record has no title")
    rules, examples = rec.get("rules", []), rec.get("examples", [])
    if not rules:
        add("capture", "owner", "no rules: what must be true when this is done")
    if not examples:
        add("capture", "owner", "no worked examples")
    seen = set()
    for kind, items in (("rule", rules), ("example", examples), ("question", rec.get("questions", []))):
        for it in items:
            if it.get("id") in seen:
                add("capture", "agent", f"{kind} id {it.get('id')} is used twice")
            seen.add(it.get("id"))
    rule_ids = {r.get("id") for r in rules}
    covered = {rid for e in examples for rid in e.get("rules", [])}
    for r in rules:
        if not (r.get("text") or "").strip():
            add("capture", "agent", f"rule {r.get('id')} has no text")
        if r.get("id") not in covered:
            add("capture", "owner", f"rule {r.get('id')} has no worked example: \"{r.get('text', '')}\"")
    for it in rules + examples + rec.get("outOfScope", []):
        src = it.get("source")
        label = it.get("id") or "out-of-scope item"
        if src not in SOURCES:
            add("capture", "agent", f"{label}: source must be owner, code or assumption, not {src!r}")
        elif src == "assumption":
            add("capture", "owner", f"{label} is an assumption nobody has confirmed: \"{it.get('text') or it.get('then', '')}\"")
        elif src == "code" and not it.get("ref"):
            add("capture", "agent", f"{label} says it was seen in the code but cites no file (ref)")
    for e in examples:
        for part in ("given", "when", "then"):
            if not (e.get(part) or "").strip():
                add("capture", "owner", f"example {e.get('id')} has no {part}")
        m = VAGUE.search(e.get("then") or "")
        if m:
            add("capture", "owner", f"example {e.get('id')}: \"{m.group(0)}\" cannot be tested; say exactly what the result is")
        for rid in e.get("rules", []):
            if rid not in rule_ids:
                add("capture", "agent", f"example {e.get('id')} names rule {rid}, which does not exist")
        if not e.get("rules"):
            add("capture", "agent", f"example {e.get('id')} is not tied to a rule")
    for q in rec.get("questions", []):
        ask = q.get("ask") or "owner"
        if ask not in ROLES:
            add("capture", "agent", f"question {q.get('id')}: ask must be owner or architect, not {ask!r}")
        elif not (q.get("answer") or "").strip():
            add("capture", ask, f"open question {q.get('id')}: {q.get('text')}")
        elif not q.get("answeredBy"):
            add("capture", "agent", f"question {q.get('id')} has an answer but not who gave it")

    impact = rec.get("impact", [])
    if not impact:
        add("impact", "architect", "no systems are named: which systems change for this")
    placed = {eid for s in impact for eid in s.get("examples", [])}
    for e in examples:
        if e.get("id") not in placed:
            add("impact", "architect", f"example {e.get('id')} is not placed in any system")
    ex_ids = {e.get("id") for e in examples}
    maps = maps or {}
    for s in impact:
        name = s.get("system") or "?"
        if not s.get("why"):
            add("impact", "agent", f"system {name}: no reason given")
        for eid in s.get("examples", []):
            if eid not in ex_ids:
                add("impact", "agent", f"system {name} names example {eid}, which does not exist")
        ev = s.get("evidence", [])
        if not ev:
            add("impact", "agent", f"system {name}: no evidence from its codebase map or code")
        model = maps.get(name)
        sroot = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(model[1])))) if model else root
        for x in ev:
            p = x.get("path", "")
            if p and not os.path.exists(os.path.join(sroot, p)):
                add("impact", "agent", f"system {name}: cites {p}, which does not exist")
        if model:
            known = {c.get("id") for c in model[0].get("components", [])}
            for c in s.get("components", []):
                if c not in known:
                    add("impact", "agent", f"system {name}: component {c} is not in its codebase map")
        if not s.get("confirmedBy"):
            add("impact", "architect", f"system {name} is not confirmed by its architect")

    approvals = approvals or []
    for role in ROLES:
        mine = [a for a in approvals if a.get("role") == role]
        if not mine:
            add("approval", role, f"the {role} has not approved")
        elif mine[-1].get("hash") != digest(rec, role):
            add("approval", role, f"the record changed after {mine[-1].get('by')} approved it as {role}; it needs approving again")
    return out


NEXT = {
    "agent": "the intake agent fixes the record",
    "owner": "the business owner answers",
    "architect": "the architect confirms the systems",
}


def report(rec, approvals, maps, root):
    ps = problems(rec, approvals, maps, root)
    lines = [f"{rec.get('id')}: {rec.get('title')}"]
    if not ps:
        lines.append("ready: approved by the owner and the architect on this version; contracts can be cut")
        return "\n".join(lines), 0
    for stage in ("capture", "impact", "approval"):
        mine = [p for p in ps if p[0] == stage]
        if mine:
            lines.append(f"\n{stage} ({len(mine)}):")
            lines += [f"  - [{who}] {text}" for _, who, text in mine]
    first = ps[0]
    who = first[1]
    lines.append("\nnext: " + (NEXT.get(who) if first[0] != "approval" else f"the {who} approves this version (intent.py approve)"))
    return "\n".join(lines), 1


# ---------- outputs ----------

def render(rec, approvals):
    q = lambda s: (s or "").replace("|", "\\|")
    out = [f"# {rec.get('title')}", "", f"**Owner:** {rec['owner'].get('name')}" + (f", {rec['owner'].get('role')}" if rec['owner'].get('role') else ""), "",
           "**What they asked for**", "", "> " + (rec.get("request") or "").replace("\n", "\n> "), "",
           "**Outcome:** " + (rec.get("outcome") or "_not yet stated_"), ""]
    if rec.get("for"):
        out += ["**For:** " + ", ".join(rec["for"]), ""]
    out += ["## Rules and worked examples", ""]
    for r in rec.get("rules", []):
        out += [f"**{r['id']}. {r.get('text')}**" + ("" if r.get("source") == "owner" else f" _(source: {r.get('source')})_"), ""]
        exs = [e for e in rec.get("examples", []) if r["id"] in e.get("rules", [])]
        if exs:
            out += ["| | Given | When | Then |", "|---|---|---|---|"]
            out += [f"| {e['id']} | {q(e.get('given'))} | {q(e.get('when'))} | {q(e.get('then'))} |" for e in exs]
        else:
            out += ["_No example yet._"]
        out.append("")
    if rec.get("outOfScope"):
        out += ["## Out of scope", ""] + [f"- {x.get('text')}" for x in rec["outOfScope"]] + [""]
    open_q = [x for x in rec.get("questions", []) if not (x.get("answer") or "").strip()]
    done_q = [x for x in rec.get("questions", []) if (x.get("answer") or "").strip()]
    if open_q:
        for role in ROLES:
            mine = [x for x in open_q if (x.get("ask") or "owner") == role]
            if mine:
                out += [f"## Questions for the {role}", ""] + [f"- **{x['id']}.** {x.get('text')}" + (f" _Why it matters: {x['why']}_" if x.get("why") else "") for x in mine] + [""]
    if done_q:
        out += ["## Decisions", ""] + [f"- **{x['id']}.** {x.get('text')} **{x.get('answer')}** ({x.get('answeredBy')})" for x in done_q] + [""]
    if rec.get("impact"):
        out += ["## Systems that change", "", "| System | Parts | Examples | Why | Confirmed by |", "|---|---|---|---|---|"]
        out += [f"| {q(s.get('system'))} | {', '.join(s.get('components', []))} | {', '.join(s.get('examples', []))} | {q(s.get('why'))} | {s.get('confirmedBy') or '_not yet_'} |" for s in rec["impact"]]
        out.append("")
    out += ["## Approvals", ""]
    for role in ROLES:
        mine = [a for a in approvals if a.get("role") == role]
        if not mine:
            out.append(f"- {role}: _not yet_")
        else:
            a = mine[-1]
            out.append(f"- {role}: {a.get('by')}, {a.get('at')}" + ("" if a.get("hash") == digest(rec, role) else " _(stale: the record changed after this)_"))
    return "\n".join(out) + "\n"


def contract(rec, approvals, system):
    s = next((x for x in rec.get("impact", []) if x.get("system") == system), None)
    if not s:
        raise SystemExit(f"{system} is not one of the systems this record changes: {', '.join(x.get('system', '') for x in rec.get('impact', []))}")
    exs = [e for e in rec.get("examples", []) if e.get("id") in s.get("examples", [])]
    rule_ids = []
    for e in exs:
        for rid in e.get("rules", []):
            if rid not in rule_ids:
                rule_ids.append(rid)
    rules = {r["id"]: r for r in rec.get("rules", [])}
    return {
        "intent": rec.get("id"), "intentHash": digest(rec), "system": system, "title": rec.get("title"),
        "owner": rec.get("owner"), "outcome": rec.get("outcome"), "components": s.get("components", []),
        # Titles are public: they appear in the pull request's check log. The examples go to the hidden-test author only.
        "requirements": [{"id": rid, "title": rules[rid].get("text"),
                          "examples": [{k: e.get(k) for k in ("id", "given", "when", "then")} for e in exs if rid in e.get("rules", [])]}
                         for rid in rule_ids],
        "decisions": [{"question": x.get("text"), "answer": x.get("answer"), "by": x.get("answeredBy")} for x in rec.get("questions", [])],
        "outOfScope": [x.get("text") for x in rec.get("outOfScope", [])],
        "approvedBy": [{"role": a.get("role"), "by": a.get("by"), "at": a.get("at")} for a in approvals if a.get("hash") == digest(rec, a.get("role"))],
    }


def parse_maps(items):
    maps = {}
    for it in items or []:
        name, _, path = it.partition("=")
        if not path:
            raise SystemExit(f"--map takes system=path/to/model.json, got {it!r}")
        maps[name] = (load(path), path)
    return maps


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("dir"); p.add_argument("--request", required=True); p.add_argument("--owner", default=""); p.add_argument("--title", default="")
    p = sub.add_parser("check"); p.add_argument("dir"); p.add_argument("--map", action="append", metavar="SYSTEM=MODEL_JSON")
    p.add_argument("--root", default=".", help="where cited paths resolve for systems without --map (default: here)")
    p = sub.add_parser("render"); p.add_argument("dir"); p.add_argument("-o", "--out")
    p = sub.add_parser("approve"); p.add_argument("dir"); p.add_argument("--role", choices=ROLES, required=True); p.add_argument("--by", required=True)
    p.add_argument("--map", action="append", metavar="SYSTEM=MODEL_JSON"); p.add_argument("--root", default=".")
    p.add_argument("--confirm-assumptions", action="store_true", help="owner only: confirm every rule, example and out-of-scope item marked as an assumption")
    p = sub.add_parser("contract"); p.add_argument("dir"); p.add_argument("--system", required=True); p.add_argument("-o", "--out")
    p.add_argument("--draft", action="store_true", help="cut it before approval, marked as a draft")
    a = ap.parse_args(argv)
    ipath, apath = files(a.dir)

    if a.cmd == "new":
        if os.path.exists(ipath):
            raise SystemExit(f"{ipath} exists")
        os.makedirs(a.dir, exist_ok=True)
        save(ipath, blank(os.path.basename(os.path.normpath(a.dir)), a.request, a.owner, a.title))
        print(ipath)
        return 0
    rec, approvals = load(ipath), load(apath, [])
    if a.cmd == "check":
        text, code = report(rec, approvals, parse_maps(a.map), a.root)
        print(text)
        return code
    if a.cmd == "render":
        md = render(rec, approvals)
        if a.out:
            with open(a.out, "w", encoding="utf-8", newline="\n") as f:
                f.write(md)
            print(a.out)
        else:
            sys.stdout.write(md)
        return 0
    if a.cmd == "approve":
        if not sys.stdin.isatty() and os.environ.get("INTENT_APPROVE_NONINTERACTIVE") != "1":
            raise SystemExit("approve runs only in a person's own terminal")
        if a.confirm_assumptions:
            if a.role != "owner":
                raise SystemExit("only the owner confirms what is wanted")
            # The owner reads each one here, in their own terminal, and confirming makes it theirs.
            for it in rec.get("rules", []) + rec.get("examples", []) + rec.get("outOfScope", []):
                if it.get("source") == "assumption":
                    print(f"confirming {it.get('id') or 'out of scope'}: {it.get('text') or it.get('then', '')}")
                    it["source"] = "owner"
            save(ipath, rec)
        if a.role == "architect":
            # The architect approving is the architect confirming the systems they are approving.
            for s in rec.get("impact", []):
                if not s.get("confirmedBy"):
                    s["confirmedBy"] = a.by
        stage_block = [p for p in problems(rec, [], parse_maps(a.map), a.root) if p[0] == "capture" or (a.role == "architect" and p[0] == "impact")]
        if stage_block:
            raise SystemExit("not ready to approve:\n" + "\n".join(f"  - {t}" for _, _, t in stage_block))
        if a.role == "architect":
            save(ipath, rec)
        approvals.append({"role": a.role, "by": a.by, "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "hash": digest(rec, a.role)})
        save(apath, approvals)
        print(f"approved {rec.get('id')} as {a.role}: {digest(rec, a.role)[:19]}")
        return 0
    if a.cmd == "contract":
        blocking = [p for p in problems(rec, approvals, None, ".") if p[0] == "approval"]
        if blocking and not a.draft:
            raise SystemExit("not approved on this version; run intent.py check, or pass --draft")
        c = contract(rec, approvals, a.system)
        if a.draft or blocking:
            c["draft"] = True
        if a.out:
            save(a.out, c)
            print(a.out)
        else:
            print(json.dumps(c, indent=2, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
