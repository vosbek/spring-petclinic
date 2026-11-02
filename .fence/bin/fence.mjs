#!/usr/bin/env node
// The fence: every check that decides whether a change is inside the repository's code and process
// rules, in one command and one report. Deterministic: no AI model decides pass or fail.
//
//   node .fence/bin/fence.mjs                  every enabled check, against origin/<default branch>
//   node .fence/bin/fence.mjs --only lint,format
//   node .fence/bin/fence.mjs --fix            run the fixers for failing checks, then every check again
//   node .fence/bin/fence.mjs --ci             CI mode (guard on base...HEAD)
//   node .fence/bin/fence.mjs --base <ref>     compare with another base
//   node .fence/bin/fence.mjs --md <file>      also write the markdown report there
//
// Configuration: .fence/fence.json (people own it). Report: .cache/fence/report.json and report.md.
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { BUILTINS, GATE, MANDATORY, OUT_DIR, changedFiles, commandCheck, readJson, run } from "./checks.mjs";
import { CATEGORIES } from "./core.mjs";

const args = process.argv.slice(2);
const has = (f) => args.includes(`--${f}`);
const opt = (f) => (args.includes(`--${f}`) ? args[args.indexOf(`--${f}`) + 1] : undefined);

// Configuration and exceptions come from the gate: the base branch in CI (FENCE_GATE_DIR), the working tree locally.
const config = readJson(path.join(GATE, ".fence", "fence.json"), null);
if (!config) {
  console.error("No .fence/fence.json. Run the onboarding script first.");
  process.exit(2);
}
const defaultBase = () => {
  const head = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]).stdout.trim();
  return head || "origin/main";
};
const ctx = {
  ci: has("ci"),
  base: opt("base") ?? process.env.FENCE_BASE ?? config.base ?? defaultBase(),
  config,
  exceptions: readJson(path.join(GATE, ".fence", "exceptions.json"), []),
};

const builtins = [...new Set([...(config.builtins ?? Object.keys(BUILTINS)), ...MANDATORY])];
const all = [...builtins.map((id) => ({ id, builtin: true })), ...(config.checks ?? []).map((c) => ({ ...c, builtin: false }))];
const only = opt("only")?.split(",");
const selected = all.filter((c) => !only || only.includes(c.id));

function runChecks(list) {
  const out = [];
  for (const c of list) {
    const t = Date.now();
    process.stderr.write(`fence: ${c.id} …`);
    let res;
    try {
      res = c.builtin ? BUILTINS[c.id](ctx) : commandCheck(c, ctx);
    } catch (e) {
      res = [{ id: c.id, category: c.category ?? "structure", title: c.title ?? c.id, status: "fail", summary: "the check could not run", findings: [{ message: String(e.message ?? e) }], fixable: false }];
    }
    process.stderr.write(` ${res.map((r) => r.status).join("/")} (${((Date.now() - t) / 1000).toFixed(1)}s)\n`);
    out.push(...res);
  }
  return out;
}

const owner = (id) => id.split(":")[0];

function applyFixes(results) {
  const keys = new Set();
  for (const r of results.filter((x) => x.status === "fail" && x.fixable)) {
    if (r.id === "guard") {
      for (const f of r.findings) {
        const rule = /^\[([\w-]+)\]/.exec(f.message)?.[1];
        if (rule && config.fixers?.[`guard:${rule}`]) keys.add(`guard:${rule}`);
      }
    } else if (config.fixers?.[owner(r.id)]) keys.add(owner(r.id));
  }
  // "{changed}" in a fixer stands for the changed files, so large repositories fix only what the change touched.
  const changed = changedFiles(ctx.base);
  for (const k of keys)
    for (const cmd of config.fixers[k]) run(cmd.flatMap((a) => (a === "{changed}" ? changed : [a])));
  return [...keys];
}

function markdown(results, fixed, regressions) {
  const head = run(["git", "rev-parse", "--short", "HEAD"]).stdout.trim();
  const failing = results.filter((r) => r.status === "fail");
  const icon = { pass: "✅", fail: "❌", note: "📝", skip: "⏭️" };
  const lines = [`## Fence: ${failing.length ? "FAIL" : "PASS"} on ${head}`, "", "| Category | Result | Checks |", "|---|---|---|"];
  const cats = [...new Set([...CATEGORIES, ...results.map((r) => r.category)])];
  for (const cat of cats) {
    const rs = results.filter((r) => r.category === cat);
    if (!rs.length) continue;
    const bad = rs.filter((r) => r.status === "fail").length;
    lines.push(`| ${cat} | ${bad ? `❌ ${bad} failing` : "✅"} | ${rs.map((r) => `${icon[r.status]} ${r.title}: ${r.summary}`).join("<br>")} |`);
  }
  if (fixed.length) lines.push("", `**Fixers run:** ${fixed.join(", ")}.`);
  if (regressions.length) lines.push("", `**A fixer broke ${regressions.join(", ")}**, so the gardener does not commit its fixes.`);
  for (const r of failing) {
    lines.push("", `### ❌ ${r.category} · ${r.title} (\`${r.id}\`)${r.fixable ? " · the gardener can fix this" : ""}`);
    for (const f of r.findings.slice(0, 25)) {
      const where = f.file ? `\`${f.file}${f.line ? `:${f.line}` : ""}\` ` : "";
      lines.push(`- ${where}${f.message}${f.fix ? `<br>  ↳ ${f.fix}` : ""}`);
    }
    if (r.findings.length > 25) lines.push(`- … and ${r.findings.length - 25} more in the full report`);
  }
  if (failing.some((r) => !r.fixable))
    lines.push("", "### For a person", "Run `/tend-fence`: for each finding, change the code, change the request, change the fence (with evidence), or record an exception (a named person and an expiry).");
  return `${lines.join("\n")}\n`;
}

let results = runChecks(selected);
const fixed = [];
const regressions = [];
if (has("fix")) {
  const applied = applyFixes(results);
  if (applied.length) {
    fixed.push(...applied);
    const before = new Set(results.filter((r) => r.status !== "fail").map((r) => r.id));
    process.stderr.write(`fence: fixed ${applied.join(", ")}; checking everything again\n`);
    results = runChecks(selected);
    regressions.push(...results.filter((r) => r.status === "fail" && before.has(r.id)).map((r) => r.id));
  }
}
mkdirSync(OUT_DIR, { recursive: true });
// Where the fence's code and configuration came from: the base branch (CI), or the working tree.
const gate = process.env.FENCE_GATE_DIR ? "base" : "working tree";
writeFileSync(path.join(OUT_DIR, "report.json"), `${JSON.stringify({ head: run(["git", "rev-parse", "HEAD"]).stdout.trim(), base: ctx.base, gate, fixed, regressions, results }, null, 2)}\n`);
const md = markdown(results, fixed, regressions);
writeFileSync(path.join(OUT_DIR, "report.md"), md);
if (opt("md")) writeFileSync(opt("md"), md);
console.log(md);
process.exit(results.some((r) => r.status === "fail") ? 1 : 0);
