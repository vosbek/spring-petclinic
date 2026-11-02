#!/usr/bin/env node
// The review page: one page from which a reviewer can tell whether a change does what was asked, what it changed,
// whether anything else broke, and what is left for them to decide. Built only from the intent record people approved,
// the fence's report, each test's result and git. Nothing on it comes from the pull request's description or from an
// AI model, so leaving something out of the description cannot hide it.
//
//   node .fence/bin/review.mjs [--report .cache/fence/report.json] [--base <ref>] [--out <file>]
//
// The chain it checks: an intent record (rules and worked examples, approved by the owner and the architect on this
// exact version) -> one test per example, tagged with the example's id ([E1] in the title, or test_e1_ in Python) ->
// the code those tests reach. Changed code that no example's test reaches is a change nobody asked for.
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { countAssertions, isGateFile, isTestFile } from "./core.mjs";

const CODE = /\.(mjs|cjs|js|jsx|ts|tsx|py|java|kt|go|rb|cs|sh|ps1|sql)$/;
const WORKFLOW = /^(\.github\/workflows\/|factory\/workflows\/).+\.ya?ml$/;
const DIFF_BUDGET = 400; // lines of code diff shown inline before the page points to the Files tab

// What each built-in check protects against, in the words a reviewer needs.
const BUILTIN = {
  secrets: "A password, token or private key pasted into the change.",
  workflows: "A CI workflow that hands out more access than it needs, or runs text from a pull request title or branch name as code.",
  duplicates: "Blocks of code copied instead of reused.",
  dependencies: "A new package nobody approved.",
  "map-coverage": "A new file that belongs to no component of the codebase map.",
  guard: "A change that breaks an architecture or process rule a person accepted.",
  formal: "A change to the code a formal model describes that breaks what the model proves.",
  exceptions: "An exception to a check that lacks a reason, a named approver or a near expiry date.",
  "gate-integrity": "A change that edits the checks that judge it together with other code.",
  "test-integrity": "A test deleted, emptied of assertions, or marked to skip.",
};

export function classify(file) {
  if (isTestFile(file)) return "test";
  if (WORKFLOW.test(file)) return "workflow";
  if (CODE.test(file)) return "code";
  return "text";
}

/** Default area: the first two folders, or the folder, or the repository root. */
export function folderArea(f) {
  const parts = f.split("/");
  return parts.length > 2 ? parts.slice(0, 2).join("/") : parts.length === 2 ? parts[0] : "(repository root)";
}

/** Test titles in source lines: JavaScript test()/it(), Python def test_, Java and Kotlin test methods. */
export function testTitles(lines, file) {
  const out = [];
  for (const l of lines) {
    let m = /^\s*(?:test|it)(?:\.\w+)?\(\s*(["'`])(.+?)\1/.exec(l);
    if (!m && /\.py$/.test(file)) m = /^\s*(?:async\s+)?def\s+(test_\w+)/.exec(l);
    if (!m && /\.(java|kt)$/.test(file)) m = /^\s*(?:public\s+|private\s+|protected\s+)?(?:void|fun)\s+(\w+)\s*\(/.exec(l);
    if (m) out.push(m[2] ?? m[1]);
  }
  return out;
}

/** The example a test proves: "[E2]" in its title, or test_e2_ / e2_ at the start of a Python or Java name. */
export function exampleOf(title) {
  const m = /\[(E\d+)\]/i.exec(title) ?? /^(?:test_)?(e\d+)_/i.exec(title);
  return m ? m[1].toUpperCase() : null;
}

/** JUnit XML (node --test-reporter=junit, pytest --junitxml) -> [{ name, status, message }]. */
export function parseJUnit(xml) {
  const unesc = (s) => s.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  const out = [];
  const rx = /<testcase\b([^>]*?)(\/>|>([\s\S]*?)<\/testcase>)/g;
  let m;
  while ((m = rx.exec(xml))) {
    // Gradle and Maven name a JUnit 5 test "method()" or "method(String)[2]"; the source names it "method".
    const raw = unesc(/\bname="([^"]*)"/.exec(m[1])?.[1] ?? "");
    const name = /^\w+\(.*\)(\[\d+\])?$/.test(raw) ? raw.slice(0, raw.indexOf("(")) : raw.replace(/\[\d+\]$/, "");
    const body = m[3] ?? "";
    const fail = /<(failure|error)\b([^>]*)/.exec(body);
    const status = fail ? "failed" : /<skipped\b/.test(body) ? "skipped" : "passed";
    const classname = unesc(/\bclassname="([^"]*)"/.exec(m[1])?.[1] ?? "");
    out.push({ name, status, message: fail ? unesc(/\bmessage="([^"]*)"/.exec(fail[2])?.[1] ?? "").slice(0, 200) : "", ...(classname ? { classname } : {}) });
  }
  return out;
}

/** New-file line numbers a unified diff adds. */
export function addedLines(diff) {
  const out = [];
  let n = 0;
  for (const l of (diff ?? "").split("\n")) {
    if (l.startsWith("diff --git")) n = 0;
    const h = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(l);
    if (h) n = Number(h[1]);
    else if (!n || l.startsWith("\\")) continue;
    else if (l.startsWith("+")) out.push(n++);
    else if (!l.startsWith("-")) n++;
  }
  return out;
}

/** Line coverage, JaCoCo XML or lcov -> Map("<package path>/<File.java>" or lcov path -> Map(line -> run by a test)). */
export function parseCoverage(text) {
  const out = new Map();
  if (/<report\b/.test(text)) {
    for (const p of text.matchAll(/<package\s+name="([^"]*)"[^>]*>([\s\S]*?)<\/package>/g))
      for (const sf of p[2].matchAll(/<sourcefile\s+name="([^"]*)"[^>]*>([\s\S]*?)<\/sourcefile>/g)) {
        const lines = new Map();
        for (const l of sf[2].matchAll(/<line\s+nr="(\d+)"\s+mi="(\d+)"\s+ci="(\d+)"/g)) lines.set(Number(l[1]), Number(l[3]) > 0);
        out.set(p[1] ? `${p[1]}/${sf[1]}` : sf[1], lines);
      }
    return out;
  }
  let lines = null;
  for (const l of text.split("\n")) {
    if (l.startsWith("SF:")) out.set(l.slice(3).trim().replace(/\\/g, "/"), (lines = new Map()));
    const da = /^DA:(\d+),(\d+)/.exec(l);
    if (da && lines) lines.set(Number(da[1]), Number(da[2]) > 0);
  }
  return out;
}
/** The coverage lines for a repository file: an exact path, or a path that ends with the report's key. */
export function coverageOf(file, cov) {
  if (!cov) return null;
  for (const [k, v] of cov) if (file === k || file.endsWith(`/${k}`) || k.endsWith(`/${file}`)) return v;
  return null;
}

/** PIT mutations.xml -> [{ status, file, line, method, mutator, description, test }]; file is "<package path>/<File.java>". */
export function parseMutations(xml) {
  const unesc = (s) => s.replace(/&quot;/g, '"').replace(/&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
  const out = [];
  for (const m of xml.matchAll(/<mutation\b([^>]*)>([\s\S]*?)<\/mutation>/g)) {
    const tag = (n) => unesc(new RegExp(`<${n}>([\\s\\S]*?)</${n}>`).exec(m[2])?.[1] ?? "").trim();
    const cls = tag("mutatedClass").replace(/\$.*/, "");
    const pkg = cls.includes(".") ? cls.slice(0, cls.lastIndexOf(".")).split(".").join("/") : "";
    out.push({
      status: /status=['"](\w+)['"]/.exec(m[1])?.[1] ?? "UNKNOWN",
      file: pkg ? `${pkg}/${tag("sourceFile")}` : tag("sourceFile"),
      line: Number(tag("lineNumber")),
      method: tag("mutatedMethod"),
      mutator: tag("mutator").replace(/^.*\./, ""),
      description: tag("description"),
      test: tag("killingTest"),
    });
  }
  return out;
}
const CAUGHT = new Set(["KILLED", "TIMED_OUT", "MEMORY_ERROR"]);
const MISSED = new Set(["SURVIVED", "NO_COVERAGE"]);
/** Mutants on the lines a change adds to a file. */
export function mutantsOn(file, lines, mutations) {
  const set = new Set(lines);
  const mine = mutations.filter((x) => (file === x.file || file.endsWith(`/${x.file}`)) && set.has(x.line));
  return { caught: mine.filter((x) => CAUGHT.has(x.status)), missed: mine.filter((x) => MISSED.has(x.status)) };
}
/** [3,4,5,9] -> "3-5, 9" */
export function ranges(ns) {
  const out = [];
  for (const n of [...ns].sort((a, b) => a - b)) {
    const last = out[out.length - 1];
    if (last && n === last[1] + 1) last[1] = n;
    else out.push([n, n]);
  }
  return out.map(([a, b]) => (a === b ? `${a}` : `${a}-${b}`)).join(", ");
}

// The same canonical form intent.py hashes: sorted keys, no spaces, non-ASCII kept as is.
function canonical(v) {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (v && typeof v === "object") return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`).join(",")}}`;
  return JSON.stringify(v);
}
/** The version a person approves (intent.py digest): the owner approves everything but the systems; the architect all. */
export function intentDigest(rec, role) {
  const body = Object.fromEntries(Object.entries(rec).filter(([k]) => !(role === "owner" && k === "impact")));
  return `sha256:${createHash("sha256").update(canonical(body), "utf8").digest("hex")}`;
}
export function approvalState(rec, approvals = []) {
  const out = {};
  for (const role of ["owner", "architect"]) {
    const mine = approvals.filter((a) => a.role === role);
    const last = mine[mine.length - 1];
    out[role] = !last ? { state: "none" } : last.hash === intentDigest(rec, role) ? { state: "approved", by: last.by, at: last.at } : { state: "stale", by: last.by, at: last.at };
  }
  return out;
}

// Top-level declarations only (and methods one level into a Python or Java class), not local variables.
const DECL = /^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=|class\s+(\w+))|^\s{0,4}(?:async\s+)?def\s+(\w+)|^\s{0,4}(?:public|private|protected)\s+(?:static\s+)?[\w<>[\]]+\s+(\w+)\s*\(/;
const declName = (l) => {
  const m = DECL.exec(l);
  return m ? m.slice(1).find(Boolean) : null;
};
const COMMENT = /^\s*(\/\/|#|\*|\/\*|$)/;
/** For each line of a file, the top-level declaration it sits in (the nearest one at or above it). */
function enclosing(text) {
  const out = [];
  let cur = null;
  for (const l of text.split("\n")) {
    if (/^(?:import|export)\s*(?:type\s*)?\{/.test(l) || /^(?:from\s+\S+\s+)?import\s/.test(l)) cur = null; // import and export lists name no function
    cur = declName(l) ?? cur;
    out.push(cur);
  }
  return out;
}

/** What a diff does, by name: top-level functions and constants added, removed, and changed. */
export function summarizeDiff(diff, before = "", after = "") {
  const inOld = enclosing(before), inNew = enclosing(after);
  const added = new Set(), removed = new Set(), changed = new Set();
  let o = 0, n = 0, ctx = null;
  for (const l of diff.split("\n")) {
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@\s?(.*)$/.exec(l);
    if (h) {
      o = Number(h[1]);
      n = Number(h[2]);
      ctx = declName(h[3]);
      continue;
    }
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("\\")) continue;
    if (l.startsWith("+")) {
      const d = declName(l.slice(1));
      if (d) added.add(d);
      else if (!COMMENT.test(l.slice(1))) {
        const e = after ? inNew[n - 1] : ctx;
        if (e) changed.add(e);
      }
      n++;
    } else if (l.startsWith("-")) {
      const d = declName(l.slice(1));
      if (d) removed.add(d);
      else if (!COMMENT.test(l.slice(1))) {
        const e = before ? inOld[o - 1] : ctx;
        if (e) changed.add(e);
      }
      o++;
    } else if (l.startsWith(" ")) {
      o++;
      n++;
    }
  }
  for (const d of [...added].filter((x) => removed.has(x))) {
    added.delete(d);
    removed.delete(d);
    changed.add(d);
  }
  for (const d of added) changed.delete(d);
  for (const d of removed) changed.delete(d);
  return { added: [...added], removed: [...removed], changed: [...changed] };
}

const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
const c = (s) => `\`${s}\``;
const cell = (s) => String(s ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ");

/**
 * Everything the page needs, gathered by the command line below (or by a test):
 *   report    the fence's report.json
 *   files     [{ file, status, added, deleted, diff, plus?, minus?, before?, after? }] for the change
 *   results   [{ check, name, status, message }] every test that ran, from the checks' JUnit files
 *   testFiles [{ file, titles, newTitles }] every test file in the repository, after the change
 *   reach     { "<changed code file>": ["<test file>", ...] } test files that reach it through imports
 *   intent    { path, record, approvals } | null
 *   history   [{ sha, date, subject }] recent changes to the fence's configuration
 *   coverage  parseCoverage() of the tests' line coverage, or null
 *   mutations { check, list: parseMutations() } from mutation testing, or null
 */
export function buildReview({ report, files, results = [], testFiles = [], reach = {}, near = null, intent = null, history = [], head = "", base = "", diffBudget = DIFF_BUDGET, intentTool = "toolkit/bin/intent.py", coverage = null, mutations = null }) {
  const checks = report.results ?? [];
  const failed = checks.filter((r) => r.status === "fail");
  const skipped = checks.filter((r) => r.status === "skip");
  const byName = new Map();
  // A title with \${...} in it is a parameterized test: it runs once per value under the filled-in name.
  const templateResult = (title) => {
    // Only titles with real words around the placeholders: "${name}" alone would match every result.
    if (!title.includes("${") || title.replace(/\$\{[^}]*\}/g, "").replace(/\W/g, "").length < 8) return undefined;
    const rx = new RegExp(`^${title.split(/\$\{[^}]*\}/).map((p) => p.replace(/[.*+?^$()|[\]\\{}]/g, "\\$&")).join(".+")}$`);
    const hits = results.filter((r) => rx.test(r.classname && r.classname !== "test" ? r.classname : r.name) || rx.test(r.name));
    if (!hits.length) return undefined;
    return { status: hits.some((r) => r.status === "failed") ? "failed" : hits.every((r) => r.status === "passed") ? "passed" : "skipped" };
  };
  // Java and Kotlin results name the class, so a method is looked up under its own class before by name alone.
  const classOf = (file) => /src\/test\/(?:java|kotlin)\/(.+)\.(?:java|kt)$/.exec(file ?? "")?.[1].split("/").join(".");
  const resultOf = (title, file) => (classOf(file) && byName.get(`${classOf(file)}#${title}`)) || byName.get(title);
  const statusOf = (title, file) => resultOf(title, file)?.status ?? templateResult(title)?.status;
  for (const r of results)
    for (const key of [r.name, r.classname && r.classname !== "test" ? r.classname : null, r.classname ? `${r.classname}#${r.name}` : null].filter(Boolean))
      if (!byName.has(key) || r.status === "failed") byName.set(key, r);
  const blockers = [], decisions = [], next = [];

  // 1. What was asked
  const rec = intent?.record;
  const approval = rec ? approvalState(rec, intent.approvals) : null;
  const bothApproved = Boolean(rec) && approval.owner.state === "approved" && approval.architect.state === "approved";
  const examples = rec?.examples ?? [];
  const rules = rec?.rules ?? [];
  const guesses = [...rules, ...examples, ...(rec?.outOfScope ?? [])].filter((x) => x.source === "assumption");

  // 2. Each example's tests
  const titlesOf = new Map(testFiles.map((tf) => [tf.file, tf]));
  const tagged = new Map(); // E1 -> [{ title, file, status, isNew, code }]
  for (const tf of testFiles)
    for (const t of tf.titles) {
      const id = exampleOf(t);
      if (!id) continue;
      (tagged.get(id) ?? tagged.set(id, []).get(id)).push({ title: t, file: tf.file, status: statusOf(t, tf.file) ?? "did not run", isNew: tf.newTitles.includes(t), code: testBody(tf.text ?? "", t, tf.file) });
    }
  const proof = examples.map((e) => {
    const ts = tagged.get(String(e.id).toUpperCase()) ?? [];
    const status = !ts.length ? "no test" : ts.every((t) => t.status === "passed") ? "passed" : ts.some((t) => t.status === "failed") ? "failed" : "did not run";
    return { e, ts, status };
  });
  const exampleTestFiles = new Set([...tagged.values()].flat().map((t) => t.file));

  // 3. What changed
  const inRecord = (f) => intent && f.file.startsWith(`${intent.path}/`);
  const kinds = { code: [], workflow: [], test: [], text: [] };
  for (const f of files) if (!inRecord(f)) kinds[classify(f.file)].push(f);
  const liveCode = kinds.code.filter((f) => f.status !== "D");
  const removed = [], weaker = [];
  for (const f of kinds.test) {
    const a = testTitles(f.plus ?? [], f.file), d = testTitles(f.minus ?? [], f.file);
    removed.push(...d.filter((t) => !a.includes(t)).map((t) => ({ t, file: f.file })));
    if (f.status === "D") removed.push({ t: "(the whole file)", file: f.file });
    if (f.status === "M" && countAssertions(f.after ?? "") < countAssertions(f.before ?? "")) weaker.push(f.file);
  }
  const testsNow = testFiles.reduce((n, tf) => n + tf.titles.length, 0);
  const testsAdded = testFiles.reduce((n, tf) => n + tf.newTitles.length, 0);

  // 4. What else uses the changed code
  // A test file "ran" when any of its tests has a result. In large-repository mode only affected tests run; the rest
  // are counted, not listed as passing.
  // A file ran when one of its tests ran under its exact title; a pattern match alone is not evidence the file ran.
  const ran = (t) => titlesOf.get(t).titles.some((x) => (classOf(t) ? byName.has(`${classOf(t)}#${x}`) : false) || byName.has(x));
  const holds = liveCode.map((f) => {
    const tfs = (reach[f.file] ?? []).filter((t) => titlesOf.has(t));
    const ranTfs = tfs.filter(ran);
    const existing = ranTfs.flatMap((t) => titlesOf.get(t).titles.filter((x) => !titlesOf.get(t).newTitles.includes(x) && !exampleOf(x)).map((x) => ({ title: x, file: t, status: statusOf(x, t) ?? "did not run" })));
    return { f, tfs, ranTfs, notRun: tfs.length - ranTfs.length, existing };
  });
  const unreached = holds.filter((h) => !h.tfs.length);
  // Explained: an example's test is close to the file (it, the code it tests, or what that code uses directly).
  // In a large codebase almost everything is reachable through enough imports, so distance matters.
  const closeTo = (f) => near?.[f] ?? reach[f] ?? [];
  const unexplained = rec ? holds.filter((h) => !closeTo(h.f.file).some((t) => exampleTestFiles.has(t))) : [];

  // 4b. Do the tests check the changed lines? Coverage: which added lines a test ran. Mutation testing: whether a test
  // fails when one of those lines is changed in a small way.
  const mutList = mutations?.list ?? [];
  const depth = (coverage || mutations)
    ? liveCode.map((f) => {
        const added = addedLines(f.diff);
        const cov = coverageOf(f.file, coverage);
        const exec = cov ? added.filter((n) => cov.has(n)) : [];
        return { f, added, cov, exec, run: exec.filter((n) => cov.get(n)), notRun: exec.filter((n) => !cov.get(n)), ...mutantsOn(f.file, added, mutList) };
      }).filter((d) => d.cov || d.caught.length || d.missed.length)
    : [];
  const missedAll = depth.flatMap((d) => d.missed.map((m) => ({ ...m, path: d.f.file })));
  const describeMissed = (list) => list.slice(0, 6).map((m) => `${c(m.path)} line ${m.line}, ${m.description || m.mutator} in ${m.method}() (${m.status === "NO_COVERAGE" ? "no test runs it" : "every test still passed"})`).join("; ") + (list.length > 6 ? `; and ${list.length - 6} more` : "");

  // Blockers: the change must not merge while any is open. Decisions: a person agrees or not; nothing else decides them.
  for (const r of failed) {
    const broken = results.filter((x) => x.check === r.id && x.status === "failed");
    const mutated = mutations?.check === r.id && missedAll.length;
    const what = mutated
      ? `The tests run ${plural(missedAll.length, "changed-line mutant")} without noticing: ${describeMissed(missedAll)}. A test that runs a line but would pass whatever it did is not checking it; section 4 has each one.`
      : broken.length
      ? `${plural(broken.length, "test")} failed: ${broken.slice(0, 6).map((x) => `${x.name}${x.message ? ` (${x.message.split("\n")[0]})` : ""}`).join("; ")}${broken.length > 6 ? `; and ${broken.length - 6} more` : ""}`
      : `${r.findings.slice(0, 3).map((f) => `${f.file ? `${c(f.file)}: ` : ""}${f.message.split("\n")[0]}`).join("; ")}${r.findings.length > 3 ? `; and ${r.findings.length - 3} more` : ""}`;
    blockers.push(`**Check failed: ${r.title}.** ${what}`);
    next.push(`The author fixes what ${c(r.id)} reports. If the check is wrong, a person runs \`/tend-fence\` and changes the check or records an exception, with a reason, a named approver and an end date.`);
  }
  // Skipped because nothing it covers changed: not a gap. Skipped for any other reason (a missing tool): a gap.
  const nothingToCheck = (r) => /^no (changed|affected) files|^no verification\/|^first install/.test(r.summary ?? "");
  for (const r of skipped.filter((x) => !nothingToCheck(x))) blockers.push(`**Check did not run: ${r.title}** (${r.summary}). Nothing it covers is checked on this change.`);
  for (const r of checks.filter((x) => x.status !== "skip" && x.category === "tests" && x.run && "results" in x && !x.results))
    blockers.push(`**${r.title} wrote no per-test results,** so this page cannot say which of its tests ran.`);
  if (!rec) {
    blockers.push("**No stated requirement.** No intent record is linked (an `Intent: intent/<id>` line in a commit message, or a changed `intent/<id>/intent.json`). Without one, nothing here can tell you the change does the right thing, only that it passes its own tests.");
    next.push("The owner states what they want as an intent record with worked examples (`/capture-intent`), and approves it.");
  } else {
    for (const role of ["owner", "architect"]) {
      const a = approval[role];
      const cmd = `python3 ${intentTool} approve ${intent.path} --role ${role} --by "<name>"${role === "owner" && guesses.length ? " --confirm-assumptions" : ""}`;
      if (a.state === "none") blockers.push(`**The ${role} has not approved the intent record.** Until they do, the examples below prove the author's reading of the request${role === "owner" && guesses.length ? ` (${plural(guesses.length, "item")} are the author's assumptions: ${guesses.map((x) => x.id ?? "out of scope").join(", ")})` : ""}. They run \`${cmd}\` in their own terminal (agents are refused), then commit and push the record.`);
      if (a.state === "stale") blockers.push(`**The intent record changed after ${a.by} approved it as ${role}.** What is tested is not what they approved. They approve the new version (\`${cmd}\`), or the change goes back.`);
    }
    if (files.some(inRecord)) decisions.push(`**The intent record is ${files.find(inRecord)?.status === "A" ? "new in" : "edited by"} this change,** written by the same author as the code and tests. Section 1 is what the owner is asked to approve.`);
  }
  for (const p of proof) {
    if (p.status === "no test") blockers.push(`**Example ${p.e.id} has no test.** Nothing proves: ${p.e.then}`);
    if (p.status === "failed") blockers.push(`**Example ${p.e.id} fails.** ${p.ts.filter((t) => t.status === "failed").map((t) => t.title).join("; ")}`);
    if (p.status === "did not run") blockers.push(`**Example ${p.e.id}'s test did not run** in any check, so it proves nothing here.`);
  }
  for (const h of rec ? unexplained : unreached) {
    blockers.push(`**${c(h.f.file)} changes, and ${rec ? "no example's test reaches it" : "no test reaches it"}:**\n${quoteChange(h.f.diff ?? "")}\n   ${rec ? "The intent record does not ask for this, or its examples do not show it." : ""} Either the owner adds an example that covers it, or the change comes out.`);
    next.push(`For ${c(h.f.file)}: if the change is wanted, the owner adds an example to the intent record and the author adds its test; if not, the author removes it.`);
  }
  if (removed.length) {
    blockers.push(`**${plural(removed.length, "test")} removed:** ${removed.map((x) => `${x.t} (${c(x.file)})`).join(", ")}. Whatever ${removed.length === 1 ? "it" : "they"} checked is no longer checked; the removed code is first in section 3.`);
    next.push("If a removed test is obsolete, a person records why in an exception (reason, approver, end date). If not, it goes back.");
  }
  const gate = files.filter((f) => isGateFile(f.file));
  if (gate.length) decisions.push(`**The fence itself changes:** ${gate.map((f) => c(f.file)).join(", ")}. This decides what every later change must pass, so it is a person's decision.`);
  if (kinds.workflow.length) decisions.push(`**CI workflows change** (they run with access tokens): ${kinds.workflow.map((f) => c(f.file)).join(", ")}.`);
  const waived = checks.flatMap((r) => (r.waived ?? []).map((f) => ({ ...f, check: r.title })));
  if (waived.length) decisions.push(`**${plural(waived.length, "finding")} let through by an exception:** ${waived.map((f) => `${f.check} on ${c(f.file)}`).join(", ")}.`);
  const notRunLines = depth.filter((d) => d.notRun.length);
  if (notRunLines.length) decisions.push(`**${plural(notRunLines.reduce((n, d) => n + d.notRun.length, 0), "changed line")} no test runs:** ${notRunLines.map((d) => `${c(d.f.file)} lines ${ranges(d.notRun)}`).join("; ")}. Whether they need a test is a person's call; nothing here shows they work.`);
  if (kinds.text.length) decisions.push(`**${plural(kinds.text.length, "file")} no script checks** (docs, pages, data): ${kinds.text.map((f) => c(f.file)).join(", ")}. Read them if the wording matters.`);

  // ------------------------------------------------------------------------- the page
  const L = [];
  const exPassed = proof.filter((p) => p.status === "passed").length;
  const uniqueExisting = [...new Map(holds.flatMap((h) => h.existing).map((x) => [`${x.file}\u0000${x.title}`, x])).values()];
  const holdPassed = uniqueExisting.filter((x) => x.status === "passed").length;
  L.push(`## ${rec ? rec.title : "Change review"}`, "");
  L.push(blockers.length ? `**Not ready to merge: ${plural(blockers.length, "blocker")}.**` : `**Ready to merge${decisions.length ? ` once you agree with ${plural(decisions.length, "decision")}` : ""}.**`);
  const facts = [];
  if (rec) facts.push(`${exPassed} of ${plural(examples.length, "worked example")} pass${bothApproved ? "" : ", but the intent record is not approved, so they prove the author's reading of the request"}.`);
  facts.push(`${holdPassed} of ${plural(uniqueExisting.length, "existing test")} that use the changed code pass${unreached.length ? `; ${plural(unreached.length, "changed file")} no test uses at all` : ""}.`);
  if (depth.some((d) => d.cov)) {
    const exec = depth.reduce((n, d) => n + d.exec.length, 0), run = depth.reduce((n, d) => n + d.run.length, 0);
    facts.push(`Changed lines a test runs: ${run} of ${exec}.`);
  }
  if (mutations) {
    const caught = depth.reduce((n, d) => n + d.caught.length, 0);
    facts.push(`Small changes to those lines the tests notice: ${caught} of ${caught + missedAll.length}.`);
  }
  facts.push(`Tests in the repository: ${testsNow - testsAdded + removed.length} before, ${testsNow} after (${testsAdded} added, ${removed.length} removed).`);
  L.push("", facts.join(" "), "");
  L.push("<sub>**Fence**: the checks every change must pass, run in CI outside the coding agent. **Intent record**: what the owner asked for, as rules and worked examples (given, when, then), approved against a hash of the exact text. This page is built by a script from those, the check results and git; it does not read the pull request's description.</sub>", "");

  L.push("### 1. What was asked", "");
  if (!rec) L.push("Nothing. No intent record is linked to this change.", "");
  else {
    const who = (r) => (approval[r].state === "approved" ? `approved by ${approval[r].by}` : approval[r].state === "stale" ? `**approval out of date** (${approval[r].by} approved an earlier version)` : "**not approved yet**");
    L.push(`From ${c(`${intent.path}/intent.json`)}. Owner: ${rec.owner?.name ?? "?"}${rec.owner?.role ? ` (${rec.owner.role})` : ""}, ${who("owner")}. Architect: ${who("architect")}.`, "");
    L.push(`> ${cell(rec.request)}`, "");
    if (rec.outcome) L.push(`**Outcome:** ${rec.outcome}`, "");
    if (rules.length) {
      L.push("**Rules:**");
      for (const r of rules) L.push(`- **${r.id}** ${r.text}${r.source === "assumption" ? " _(the author's assumption; the owner has not confirmed it)_" : ""}`);
      L.push("");
    }
    if ((rec.outOfScope ?? []).length) L.push(`**Not part of this:** ${rec.outOfScope.map((o) => o.text).join("; ")}`, "");
  }

  L.push("### 2. Does it do that?", "");
  if (!rec) L.push("No examples to check against.", "");
  else {
    L.push(`Each worked example, and the test that runs it on this commit. The given, when and then are ${bothApproved ? "the words the owner approved" : "the record's words, not yet approved"}. Tests marked _new_ were written in this change by the same author as the code; an independent check of the same examples is what hidden tests are for.`, "", "| Example | Given | When | Then | Test | Result |", "|---|---|---|---|---|---|");
    for (const p of proof) L.push(`| ${p.e.id} (${(p.e.rules ?? []).join(", ")})${p.e.source === "assumption" ? " _assumption_" : ""} | ${cell(p.e.given)} | ${cell(p.e.when)} | ${cell(p.e.then)} | ${p.ts.map((t) => `${cell(t.title)}${t.isNew ? " _new_" : ""}`).join("<br>") || "none"} | ${p.status === "passed" ? "passed" : `**${p.status}**`} |`);
    L.push("");
    const withCode = proof.flatMap((p) => p.ts.filter((t) => t.code).map((t) => ({ id: p.e.id, ...t })));
    if (withCode.length) {
      L.push("<details><summary>What each example's test actually checks (its code)</summary>", "");
      for (const t of withCode) L.push(`**${t.id}**: ${c(t.file)}`, "", "```js", t.code, "```", "");
      L.push("</details>", "");
    }
  }

  L.push(`### 3. What changed: ${plural(files.length, "file")}`, "");
  let budget = diffBudget;
  const show = (f, label, open) => {
    const lines = (f.diff ?? "").split("\n").filter((l) => !/^(diff --git|index |--- |\+\+\+ )/.test(l));
    if (!lines.length) return;
    if (lines.length > budget) {
      L.push(`<details><summary>${label}: ${lines.length} diff lines, too long to show here; open the Files changed tab</summary></details>`, "");
      return;
    }
    budget -= lines.length;
    L.push(`<details${open ? " open" : ""}><summary>${label}</summary>`, "", "```diff", ...lines, "```", "", "</details>", "");
  };
  for (const f of [...kinds.code, ...kinds.workflow]) {
    const s = summarizeDiff(f.diff ?? "", f.beforeText ?? "", f.afterText ?? "");
    const what = [s.added.length && `adds ${s.added.map((n) => c(n)).join(", ")}`, s.removed.length && `removes ${s.removed.map((n) => c(n)).join(", ")}`, s.changed.length && `changes ${s.changed.map((n) => c(n)).join(", ")}`].filter(Boolean).join("; ");
    L.push(`- **${c(f.file)}**${f.status === "A" ? " (new)" : f.status === "D" ? " (deleted)" : ""} +${f.added} −${f.deleted}${what ? `: ${what}` : ""}`);
  }
  for (const f of kinds.test) {
    const tf = titlesOf.get(f.file);
    const gone = removed.filter((x) => x.file === f.file);
    L.push(`- **${c(f.file)}** (tests) +${f.added} −${f.deleted}${[tf?.newTitles.length && `adds ${plural(tf.newTitles.length, "test")}`, gone.length && `**removes ${plural(gone.length, "test")}**`, weaker.includes(f.file) && "**fewer assertions**"].filter(Boolean).map((x) => `: ${x}`).join("")}`);
  }
  if (files.some(inRecord)) L.push(`- **The intent record** (${c(intent.path)}): section 1`);
  L.push("");
  // Removed and weakened tests first, open: they are where a change hides what it broke.
  for (const f of kinds.test.filter((x) => weaker.includes(x.file) || removed.some((r) => r.file === x.file))) show(f, `Tests removed or weakened: ${c(f.file)}`, true);
  for (const f of [...kinds.code, ...kinds.workflow]) show(f, `The code: ${c(f.file)}`, (f.diff ?? "").split("\n").length <= 60);
  for (const f of kinds.test.filter((x) => !weaker.includes(x.file) && !removed.some((r) => r.file === x.file))) show(f, `The tests: ${c(f.file)}`, false);

  L.push("### 4. What else uses this code, and does it still work?", "", "A test uses a file when it imports it, directly or through other files. That shows the test touches the file, not that it runs every changed line.", "");
  if (!holds.length) L.push("No code changed.", "");
  for (const h of holds) {
    if (!h.tfs.length) {
      L.push(`- ${c(h.f.file)}: **no test uses this file.**`);
      continue;
    }
    const bad = h.existing.filter((x) => x.status !== "passed");
    const shown = h.ranTfs.slice(0, 6).map(c).join(", ") + (h.ranTfs.length > 6 ? ` and ${h.ranTfs.length - 6} more` : "");
    const others = h.notRun ? ` ${plural(h.notRun, "other test file")} use${h.notRun === 1 ? "s" : ""} it and ${h.notRun === 1 ? "was" : "were"} not run here (only affected tests run on a large repository; the rest run in the repository's own CI).` : "";
    L.push(h.ranTfs.length
      ? `- ${c(h.f.file)} is used by ${shown}, which ran: ${h.existing.length - bad.length} of ${plural(h.existing.length, "existing test")} pass${bad.length ? `; **not passing:** ${bad.slice(0, 5).map((x) => `${x.title} (${x.status})`).join(", ")}${bad.length > 5 ? `, and ${bad.length - 5} more` : ""}` : ""}.${others}`
      : `- ${c(h.f.file)}: no test that uses it ran here${results.length ? "" : " (no check wrote per-test results, so this page cannot say which tests ran)"}.${others}`);
  }
  L.push("");
  if (uniqueExisting.length) {
    L.push("<details><summary>Every existing test that uses the changed code, by name</summary>", "");
    // Not-passing first, then at most 100 names: a page is read, not scrolled.
    const ordered = [...uniqueExisting.filter((x) => x.status !== "passed"), ...uniqueExisting.filter((x) => x.status === "passed")];
    for (const x of ordered.slice(0, 100)) L.push(`- ${x.status === "passed" ? "passed" : `**${x.status}**`}: ${x.title} (${c(x.file)})`);
    if (ordered.length > 100) L.push(`- and ${ordered.length - 100} more`);
    L.push("", "</details>", "");
  }

  if (coverage || mutations) {
    L.push("#### Do the tests check the changed lines?", "");
    L.push(`${coverage ? "Line coverage shows which lines the change adds that a test ran. " : ""}${mutations ? "Mutation testing (PIT) then changes each of those lines in one small way (a \`<\` becomes \`<=\`, a return value becomes 0) and runs the tests again. A change no test notices means the tests run the line without checking what it does." : ""}`, "");
    if (!depth.length) L.push("No changed code file appears in the coverage or mutation results.", "");
    else {
      L.push("| Changed file | Changed lines a test runs | Small changes the tests notice |", "|---|---|---|");
      for (const d of depth)
        L.push(`| ${c(d.f.file)} | ${d.cov ? `${d.run.length} of ${d.exec.length}${d.notRun.length ? `; not run: ${ranges(d.notRun)}` : ""}` : "not measured"} | ${mutations ? `${d.caught.length} of ${d.caught.length + d.missed.length}` : "not measured"} |`);
      L.push("");
      for (const m of missedAll.slice(0, 20))
        L.push(`- **${c(m.path)} line ${m.line}**, in ${c(`${m.method}()`)}: ${m.description || m.mutator}. ${m.status === "NO_COVERAGE" ? "No test runs this line." : "Every test still passed."}`);
      if (missedAll.length > 20) L.push(`- and ${missedAll.length - 20} more`);
      if (missedAll.length) L.push("");
    }
  }

  L.push(`### 5. What is left for you`, "", "**Merge rule:** merge only when there are no blockers and you agree with each decision.", "");
  L.push(`**Blockers (${blockers.length})**`, "");
  if (blockers.length) blockers.forEach((x, i) => L.push(`${i + 1}. ${x}`));
  else L.push(`None. Every example passes, every changed code file is used by an example's test, no test was removed${mutations ? ", the tests notice every small change to the changed lines" : ""}, and every check ran and passed.`);
  L.push("", `**Decisions (${decisions.length})**`, "");
  if (decisions.length) decisions.forEach((x, i) => L.push(`${i + 1}. ${x}`));
  else L.push("None.");
  L.push("");

  const gateWords = report.gate === "base"
    ? "Every check ran outside the coding agent, from the base branch's copy of the fence and its configuration. Edits this change makes to the fence apply only to later changes."
    : "This run used the change's own copy of the fence (the first install, or a run outside CI), so it does not show that the change left the fence alone.";
  L.push("### 6. The fence that judged this change", "", `${gateWords} A check that cannot produce a result fails.`, "", "| Check | What it protects against | On this change |", "|---|---|---|");
  for (const r of checks) {
    const mine = results.filter((x) => x.check === r.id);
    const tests = mine.length ? `${plural(mine.length, "test")} ran, ${mine.filter((x) => x.status === "passed").length} passed. ` : "";
    const mark = r.status === "skip" && nothingToCheck(r) ? "nothing to check" : ({ pass: "passed", fail: "**failed**", skip: "**did not run**" }[r.status] ?? r.status);
    L.push(`| ${cell(r.title)} | ${cell(r.explain ?? BUILTIN[r.id] ?? "")} | ${tests}${mark}${r.status === "pass" ? "" : `: ${cell(r.summary)}`} |`);
  }
  L.push("");

  L.push("### 7. What happens next", "");
  if (report.fixed?.length) L.push(`- The gardener fixed ${report.fixed.map(c).join(", ")} on this change and ran every check again.`);
  for (const n of [...new Set(next)]) L.push(`- ${n}`);
  if (!next.length && !report.fixed?.length) L.push("- Nothing on this change needs a follow-up.");
  L.push("- After merge, a problem nobody caught goes back in two places: a missing or wrong example in the intent record, which the owner approves, and a missing check, which `/tend-fence` proposes, replays on past commits, and a person accepts.");
  if (history.length) L.push(`- The last change to this repository's fence: ${history[0].date} ${c(history[0].sha)} ${history[0].subject}.`);
  return `${L.join("\n")}\n`;
}

/** The lines a change adds and removes, quoted for the reader (at most eight). */
function quoteChange(diff) {
  const lines = diff.split("\n").filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---)/.test(l));
  if (!lines.length) return "   (no lines to show)";
  return ["   ```diff", ...lines.slice(0, 8).map((l) => `   ${l}`), ...(lines.length > 8 ? [`   … ${lines.length - 8} more lines`] : []), "   ```"].join("\n");
}

/** A test's code, found by its title: from the title line to the end of its block (at most 30 lines). */
export function testBody(text, title, file) {
  const lines = text.split("\n");
  const i = lines.findIndex((l) => l.includes(title) && (/\.py$/.test(file) ? /def\s/.test(l) : /\b(test|it)(\.\w+)?\(/.test(l)));
  if (i < 0) return "";
  const indent = lines[i].match(/^\s*/)[0].length;
  let j = i + 1;
  if (/\.py$/.test(file)) while (j < lines.length && (lines[j].trim() === "" || lines[j].match(/^\s*/)[0].length > indent)) j++;
  else {
    while (j < lines.length && !(lines[j].match(/^\s*/)[0].length === indent && /^\s*\}\)?;?\s*$/.test(lines[j]))) j++;
    j++;
  }
  const body = lines.slice(i, Math.min(j, i + 30));
  return `${body.join("\n").replace(/\s+$/, "")}${j - i > 30 ? "\n  // …" : ""}`;
}

// ----------------------------------------------------------------------------- command line

function git(args, opts = {}) {
  return spawnSync("git", args, { encoding: "utf8", maxBuffer: 256 * 1024 * 1024, ...opts }).stdout ?? "";
}

/** Files a source file refers to: imports, requires, and repository paths it names (a test that copies or runs a folder). */
export function references(file, text, exists) {
  const dir = path.posix.dirname(file);
  const out = new Set();
  const tryAdd = (p) => {
    const ts = /\.js$/.test(p) ? [p.replace(/\.js$/, ".ts"), p.replace(/\.js$/, ".tsx")] : [];
    for (const cand of [p, ...ts, `${p}.mjs`, `${p}.js`, `${p}.ts`, `${p}.py`, `${p}/index.mjs`, `${p}/index.js`]) {
      const t = exists(cand);
      if (t) {
        out.add(cand);
        return;
      }
    }
  };
  for (const m of text.matchAll(/(?:from\s+|import\s*\(\s*|require\s*\(\s*|import\s+)["'`]([^"'`]+)["'`]/g))
    if (m[1].startsWith(".")) tryAdd(path.posix.normalize(path.posix.join(dir, m[1])));
  // path.join(REPO, "factory", "fence") or os.path.join(HERE, "..", "hooks", "protect.py"): the literal parts form a path.
  // Only joins that start from the repository or the test's own folder; a temporary folder says nothing about this repo.
  for (const m of text.matchAll(/path\.join\(\s*((?:[A-Z_]*(?:REPO|ROOT|HERE|SRC)[A-Z_]*)|__dirname|[\w.]*dirname\([^,]*?(?:import\.meta\.url|__file__)\)*)\s*,\s*((?:["'][^"']+["']\s*,?\s*)+)\)/g)) {
    const parts = [...m[2].matchAll(/["']([^"']+)["']/g)].map((x) => x[1]);
    for (const root of ["", dir]) tryAdd(path.posix.normalize(path.posix.join(root, ...parts)));
  }
  // Python: import guard / from guard import x, resolved by module name.
  if (/\.py$/.test(file)) for (const m of text.matchAll(/^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))/gm)) out.add(`py:${(m[1] ?? m[2]).split(".").pop()}`);
  return out;
}

/**
 * The repository files a source file uses. A folder it names (a test that copies the fence into a scratch repository,
 * or puts a folder on Python's path) counts only for the files in it whose names the source also mentions; imports
 * then carry the rest. Without that, naming a folder would make every file in it look tested.
 */
export function resolver(tracked, read) {
  const isFile = new Set(tracked);
  const dirs = new Set(tracked.flatMap((f) => f.split("/").slice(0, -1).map((_, i, a) => a.slice(0, i + 1).join("/"))));
  const exists = (p) => isFile.has(p) || dirs.has(p);
  const byModule = new Map();
  for (const f of tracked) if (/\.py$/.test(f)) (byModule.get(path.posix.basename(f, ".py")) ?? byModule.set(path.posix.basename(f, ".py"), []).get(path.posix.basename(f, ".py"))).push(f);
  return (f) => {
    const text = read(f);
    const out = new Set();
    for (const r of references(f, text, exists)) {
      if (r.startsWith("py:")) {
        const cands = (byModule.get(r.slice(3)) ?? []).filter((x) => !isTestFile(x));
        if (cands.length === 1) out.add(cands[0]);
      } else if (dirs.has(r)) {
        for (const x of tracked)
          if (x.startsWith(`${r}/`) && CODE.test(x) && !isTestFile(x) && text.includes(path.posix.basename(x))) out.add(x);
      } else if (isFile.has(r)) out.add(r);
    }
    return out;
  };
}

function gather(base, root) {
  const mb = git(["merge-base", base, "HEAD"]).trim() || base;
  const stats = new Map();
  for (const l of git(["diff", "--numstat", "-M", mb]).split("\n").filter(Boolean)) {
    const [a, d, ...rest] = l.split("\t");
    const name = rest.join("\t").replace(/^(.*)\{(.*) => (.*)\}(.*)$/, "$1$3$4").replace(/^.* => /, "");
    stats.set(name, { added: Number(a) || 0, deleted: Number(d) || 0 });
  }
  const files = [];
  for (const l of git(["diff", "--name-status", "-M", mb]).split("\n").filter(Boolean)) {
    const [st, a, b] = l.split("\t");
    const file = b ?? a;
    const status = st[0] === "R" ? "M" : st[0];
    const f = { file, status, ...(stats.get(file) ?? { added: 0, deleted: 0 }) };
    const kind = classify(file);
    if (kind !== "text") {
      f.diff = git(["diff", "-M", mb, "--", ...(b ? [a, b] : [file])]);
      f.beforeText = status === "A" ? "" : git(["show", `${mb}:${a}`], { stdio: ["ignore", "pipe", "ignore"] });
      f.afterText = status === "D" ? "" : existsSync(path.join(root, file)) ? readFileSync(path.join(root, file), "utf8") : "";
    }
    if (kind === "test") {
      const lines = git(["diff", "-U0", mb, "--", ...(b ? [a, b] : [file])]).split("\n");
      f.plus = lines.filter((x) => x.startsWith("+") && !x.startsWith("+++")).map((x) => x.slice(1));
      f.minus = lines.filter((x) => x.startsWith("-") && !x.startsWith("---")).map((x) => x.slice(1));
      if (status === "M") {
        f.before = git(["show", `${mb}:${a}`]);
        f.after = existsSync(path.join(root, file)) ? readFileSync(path.join(root, file), "utf8") : "";
      }
    }
    files.push(f);
  }

  const tracked = git(["ls-files"]).split("\n").filter(Boolean);
  const read = (f) => {
    try {
      return readFileSync(path.join(root, f), "utf8");
    } catch {
      return "";
    }
  };
  const resolve = resolver(tracked, read);
  // Who uses whom, read once for every source file, then searched backwards from each changed file. A forward search
  // from every test file does not scale to a repository with thousands of tests.
  const users = new Map();
  for (const f of tracked)
    if (CODE.test(f) || isTestFile(f)) for (const g of resolve(f)) (users.get(g) ?? users.set(g, []).get(g)).push(f);
  const reach = {}, near = {};
  for (const f of files) {
    if (classify(f.file) !== "code") continue;
    const depth = new Map([[f.file, 0]]);
    const queue = [f.file];
    while (queue.length) {
      const x = queue.shift();
      for (const u of users.get(x) ?? []) if (!depth.has(u)) depth.set(u, depth.get(x) + 1), queue.push(u);
    }
    const tests = [...depth].filter(([t]) => isTestFile(t));
    reach[f.file] = tests.map(([t]) => t);
    near[f.file] = tests.filter(([, d]) => d <= 2).map(([t]) => t);
  }
  const changedTests = new Set(files.filter((f) => isTestFile(f.file)).map((f) => f.file));
  const testFiles = tracked.filter(isTestFile).map((file) => {
    const text = read(file);
    const titles = testTitles(text.split("\n"), file);
    const before = changedTests.has(file) ? new Set(testTitles(git(["show", `${mb}:${file}`], { stdio: ["ignore", "pipe", "ignore"] }).split("\n"), file)) : null;
    const tagged = titles.some((t) => exampleOf(t));
    return { file, titles, newTitles: before ? titles.filter((t) => !before.has(t)) : [], ...(tagged || before ? { text } : {}) };
  });

  // The intent record: named by an "Intent:" line in a commit message, or the one whose intent.json the change touches.
  let ipath = null;
  const trailer = /^Intent:\s*(\S+)/m.exec(git(["log", "--format=%B", `${mb}..HEAD`]));
  if (trailer) ipath = trailer[1].replace(/\/(intent\.json)?$/, "");
  else ipath = files.map((f) => /^(.*intent\/[^/]+)\/intent\.json$/.exec(f.file)?.[1]).find(Boolean) ?? null;
  let intent = null;
  if (ipath && existsSync(path.join(root, ipath, "intent.json"))) {
    const record = JSON.parse(read(`${ipath}/intent.json`));
    let approvals = [];
    try {
      approvals = JSON.parse(read(`${ipath}/approvals.json`) || "[]");
    } catch {}
    intent = { path: ipath, record, approvals };
  }
  const history = git(["log", "-n", "5", "--date=short", "--format=%h%x09%ad%x09%s", mb, "--", ".fence", ".cartographer/guardrails.json", ".github/workflows/fence.yml"])
    .split("\n")
    .filter(Boolean)
    .map((l) => {
      const [sha, date, subject] = l.split("\t");
      return { sha, date, subject };
    });
  // Where intent.py lives: .cartographer/bin in an onboarded repository, toolkit/bin in the factory's own.
  const intentTool = existsSync(path.join(root, ".cartographer", "bin", "intent.py")) ? ".cartographer/bin/intent.py" : "toolkit/bin/intent.py";
  return { files, reach, near, testFiles, intent, history, intentTool };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const opt = (f, d) => (args.includes(`--${f}`) ? args[args.indexOf(`--${f}`) + 1] : d);
  const root = git(["rev-parse", "--show-toplevel"]).trim();
  const report = JSON.parse(readFileSync(path.join(root, opt("report", ".cache/fence/report.json")), "utf8"));
  const base = opt("base", report.base);
  // A check's evidence is a file, or a directory of them (Maven's target/surefire-reports, Gradle's build/test-results).
  const evidence = (rel, ext) => {
    const at = rel ? path.join(root, rel) : null;
    if (!at || !existsSync(at)) return [];
    if (statSync(at).isFile()) return [readFileSync(at, "utf8")];
    return readdirSync(at).filter((n) => n.endsWith(ext)).sort().map((n) => path.join(at, n)).filter((f) => statSync(f).isFile()).map((f) => readFileSync(f, "utf8"));
  };
  const results = [];
  let coverage = null, mutations = null;
  for (const r of report.results ?? []) {
    for (const xml of evidence(r.results, ".xml")) for (const t of parseJUnit(xml)) results.push({ check: r.id, ...t });
    for (const text of evidence(r.coverage, r.coverage?.endsWith(".info") ? ".info" : ".xml")) for (const [k, v] of parseCoverage(text)) (coverage ??= new Map()).set(k, v);
    for (const xml of evidence(r.mutations, ".xml")) (mutations ??= { check: r.id, list: [] }).list.push(...parseMutations(xml));
  }
  const data = { report, results, coverage, mutations, ...gather(base, root), head: git(["rev-parse", "--short", "HEAD"]).trim(), base };
  // A pull request comment holds 65,536 characters: show less code inline until the page fits.
  let md = "";
  for (const diffBudget of [DIFF_BUDGET, 150, 0]) {
    md = buildReview({ ...data, diffBudget });
    if (md.length < 60000) break;
  }
  if (opt("out")) writeFileSync(opt("out"), md);
  process.stdout.write(md);
}
