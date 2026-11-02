const CATEGORIES = [
  "style",
  "practices",
  "patterns",
  "security",
  "performance",
  "structure",
  "tests",
  "database"
];
function lintCategory(ruleId, message, map) {
  const tag = /\[(\w+)\]/.exec(message)?.[1];
  if (tag && CATEGORIES.includes(tag)) return tag;
  if (!ruleId) return "practices";
  if (map[ruleId]) return map[ruleId];
  const prefix = Object.keys(map).filter((k) => k.endsWith("*") && ruleId.startsWith(k.slice(0, -1))).sort((a, b) => b.length - a.length)[0];
  return prefix ? map[prefix] : "practices";
}
function lintKey(ruleId, message) {
  const id = ruleId ?? "parse-error";
  return id.startsWith("no-restricted-") ? `${id}: ${message.slice(0, 60)}` : id;
}
function compareCounts(current, baseline) {
  const over = [];
  const under = [];
  for (const [file, keys] of Object.entries(current)) {
    for (const [key, count] of Object.entries(keys)) {
      const allowed = baseline[file]?.[key] ?? 0;
      if (count > allowed) over.push({ file, key, count, allowed });
      else if (count < allowed) under.push({ file, key, count, allowed });
    }
  }
  for (const [file, keys] of Object.entries(baseline)) {
    for (const [key, allowed] of Object.entries(keys)) {
      if (allowed > 0 && !(current[file]?.[key] ?? 0)) under.push({ file, key, count: 0, allowed });
    }
  }
  return { over, under };
}
function tightenCounts(current, baseline) {
  const out = {};
  for (const [file, keys] of Object.entries(baseline)) {
    for (const [key, allowed] of Object.entries(keys)) {
      const n = Math.min(allowed, current[file]?.[key] ?? 0);
      if (n > 0) (out[file] ??= {})[key] = n;
    }
  }
  return sortCounts(out);
}
function sortCounts(c) {
  const out = {};
  for (const file of Object.keys(c).sort()) {
    out[file] = {};
    for (const key of Object.keys(c[file]).sort()) out[file][key] = c[file][key];
  }
  return out;
}
function clonePairs(clones) {
  const out = {};
  for (const c of clones) {
    const [a, b] = [c.firstFile.name, c.secondFile.name].sort();
    const pair = `${a} <> ${b}`;
    (out[pair] ??= { clones: 0 }).clones += 1;
  }
  return sortCounts(out);
}
const SECRET_PATTERNS = [
  { name: "AWS access key", rx: /\b(AKIA|ASIA)[0-9A-Z]{16}\b/ },
  { name: "GitHub token", rx: /\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b/ },
  { name: "Anthropic API key", rx: /\bsk-ant-[A-Za-z0-9_-]{30,}/ },
  { name: "OpenAI API key", rx: /\bsk-(proj-)?[A-Za-z0-9]{32,}\b/ },
  { name: "Slack token", rx: /\bxox[abpors]-[A-Za-z0-9-]{10,}/ },
  { name: "Private key", rx: /-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY( BLOCK)?-----/ },
  {
    name: "Hard-coded credential",
    rx: /\b(api[_-]?key|secret|password|passwd|token)\b["']?\s*[:=]\s*["'][A-Za-z0-9+/_-]{20,}["']/i
  }
];
function scanSecrets(text, file) {
  const out = [];
  text.split("\n").forEach((line, i) => {
    for (const p of SECRET_PATTERNS) {
      if (p.rx.test(line)) {
        out.push({
          file,
          line: i + 1,
          message: `Looks like a secret: ${p.name}.`,
          fix: "Remove it, rotate the credential, and read it from an environment variable or a CI secret."
        });
      }
    }
  });
  return out;
}
const UNTRUSTED = /\$\{\{\s*github\.(event\.(pull_request\.(title|body|head\.ref|head\.label)|issue\.(title|body)|comment\.body|review\.body|review_comment\.body|head_commit\.(message|author\.(name|email))|commits)|head_ref)[^}]*\}\}/;
function workflowFindings(file, text, doc) {
  const out = [];
  const lineOf = (needle) => {
    const i = text.split("\n").findIndex((l) => l.includes(needle));
    return i >= 0 ? i + 1 : void 0;
  };
  const on = doc.on ?? doc[true];
  const triggers = typeof on === "string" ? [on] : Array.isArray(on) ? on : Object.keys(on ?? {});
  if (triggers.includes("pull_request_target")) {
    out.push({
      file,
      line: lineOf("pull_request_target"),
      message: "pull_request_target runs fork code with this repository's secrets and write token.",
      fix: "Use pull_request. If write access is needed, split into a pull_request job and a workflow_run job."
    });
  }
  const jobs = doc.jobs ?? {};
  for (const [name, job] of Object.entries(jobs)) {
    if (!doc.permissions && !job.permissions) {
      out.push({
        file,
        line: lineOf(`${name}:`),
        message: `Job ${name} has no permissions block, so it gets the repository's default token scope.`,
        fix: "Declare the least permissions the job needs (permissions: contents: read)."
      });
    }
    for (const step of job.steps ?? []) {
      const run = typeof step.run === "string" ? step.run : "";
      const m = UNTRUSTED.exec(run);
      if (m) {
        out.push({
          file,
          line: lineOf(m[0]),
          message: `${m[0]} is interpolated into a shell script; a crafted title or branch name runs as code.`,
          fix: "Pass it through env: and use the variable in the script."
        });
      }
      const uses = typeof step.uses === "string" ? step.uses : "";
      if (/@(main|master|HEAD)$/.test(uses)) {
        out.push({
          file,
          line: lineOf(uses),
          message: `${uses} follows a moving branch, so its code can change without a change here.`,
          fix: "Pin to a release tag or a commit SHA."
        });
      }
    }
  }
  return out;
}
function budgetFor(page, b) {
  const o = (b.overrides ?? []).filter((x) => page.startsWith(x.prefix)).sort((x, y) => y.prefix.length - x.prefix.length)[0];
  return { ...b.perPage, ...o?.budget ?? {} };
}
function budgetFindings(pages, totalMB, b) {
  const out = [];
  for (const p of pages) {
    const limit = budgetFor(p.page, b);
    for (const k of Object.keys(limit)) {
      if (p[k] > limit[k]) {
        out.push({
          file: p.page,
          message: `${k} is ${p[k].toFixed(1)}, over the budget of ${limit[k]}.`,
          fix: "Make it lighter (smaller images, less script or CSS), or ask a person to raise the budget with a reason."
        });
      }
    }
  }
  if (totalMB > b.totalDistMB) {
    out.push({ message: `dist/ is ${totalMB.toFixed(1)} MB, over the budget of ${b.totalDistMB} MB.` });
  }
  return out;
}
// An exception only counts when it is complete: a check, a file (no blanket waivers of a whole check), a reason,
// a named approver and an expiry no more than MAX_EXCEPTION_DAYS ahead. Incomplete ones waive nothing.
const MAX_EXCEPTION_DAYS = 90;
function addDays(day, n) {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function exceptionProblems(e, today) {
  const p = [];
  if (!e.check) p.push("names no check");
  if (!e.file) p.push("names no file, so it would waive the whole check");
  if (!e.reason) p.push("gives no reason");
  if (!(e.approvedBy ?? e.approver)) p.push("names no approver (approvedBy)");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(e.expires ?? "")) p.push("has no expiry date (YYYY-MM-DD)");
  else if (e.expires > addDays(today, MAX_EXCEPTION_DAYS)) p.push(`expires more than ${MAX_EXCEPTION_DAYS} days from today`);
  return p;
}
function excepted(check, f, list, today) {
  return list.some(
    (e) => e.check === check && !exceptionProblems(e, today).length && e.expires >= today && f.file != null && (e.file.endsWith("/") ? f.file.startsWith(e.file) : f.file === e.file)
  );
}
function expiredExceptions(list, today) {
  return list.filter((e) => e.expires < today);
}

// Files that make up the gate itself. A change that touches them and anything else is refused: a pull request
// must not be able to weaken the checks that judge it. Gate changes go in their own pull request, which
// CODEOWNERS routes to a person.
const GATE_PATTERNS = [
  ".fence/", ".cartographer/bin/", ".cartographer/guardrails.json", ".cartographer/baseline.json", ".cartographer/hooks/",
  ".github/workflows/fence.yml", ".github/workflows/gardener.yml", ".github/workflows/hidden-tests.yml",
  ".github/workflows/guardrails.yml", ".github/workflows/cartographer-pr.yml", ".github/workflows/rules-page.yml",
  ".github/CODEOWNERS", "CODEOWNERS", ".claude/settings.json", ".claude/settings.local.json", "verification/",
];
function isGateFile(f, patterns = GATE_PATTERNS) {
  return patterns.some((p) => (p.endsWith("/") ? f.startsWith(p) : f === p));
}
function gateFindings(changed, patterns = GATE_PATTERNS) {
  const gate = changed.filter((f) => isGateFile(f, patterns));
  const other = changed.filter((f) => !isGateFile(f, patterns));
  if (!gate.length || !other.length) return [];
  return gate.map((file) => ({
    file,
    message: `This change edits the factory's own checks and ${other.length} other file(s) together.`,
    fix: "Split it: put changes to the checks in their own pull request, which a code owner reviews, and keep this one to the code.",
  }));
}

// Test integrity: a change must not quietly weaken the tests that would catch it.
const TEST_FILE = [/(^|\/)(test|tests|__tests__|spec)\//, /\.(test|spec)\.[cm]?[jt]sx?$/, /(Test|Tests|IT)\.(java|kt|cs)$/, /(^|\/)test_[^/]+\.py$/, /_test\.(py|go)$/];
const ASSERTION = /\b(assert\w*|expect|verify|should|assertThat|then)\s*[.(]/g;
// Anchored at the start of a line, so a marker quoted inside a string (a test of a test parser, say) does not count.
const SKIP_MARKER = /^\s*(?:@Disabled\b|@Ignore\b|@pytest\.mark\.skip|(?:it|test|describe)\.(?:skip|todo|only)\s*\(|x(?:it|describe|test)\s*\(|t\.Skip\()/;
function isTestFile(f) {
  return TEST_FILE.some((r) => r.test(f));
}
function countAssertions(text) {
  return (text.match(ASSERTION) ?? []).length;
}
/** files: [{ file, status: "D" | "M" | "A", before?: string, after?: string, added?: string[] }] */
function testIntegrityFindings(files) {
  const out = [];
  for (const f of files) {
    if (!isTestFile(f.file)) continue;
    if (f.status === "D") {
      out.push({ file: f.file, message: "A test file was deleted.", fix: "Keep the test, or ask a person for an exception with the reason it no longer applies." });
      continue;
    }
    if (f.status === "M") {
      const b = countAssertions(f.before ?? ""), a = countAssertions(f.after ?? "");
      if (a < b) out.push({ file: f.file, message: `This test file has ${b - a} fewer assertion(s) than before (${b} to ${a}).`, fix: "Keep the assertions, or ask a person for an exception." });
    }
    const skips = (f.added ?? []).filter((l) => SKIP_MARKER.test(l));
    if (skips.length) out.push({ file: f.file, message: `Adds ${skips.length} skip or only marker(s): ${skips[0].trim().slice(0, 80)}`, fix: "Remove the marker; a skipped test checks nothing." });
  }
  return out;
}
export {
  CATEGORIES,
  SECRET_PATTERNS,
  budgetFindings,
  budgetFor,
  clonePairs,
  compareCounts,
  GATE_PATTERNS,
  countAssertions,
  excepted,
  exceptionProblems,
  expiredExceptions,
  gateFindings,
  isGateFile,
  isTestFile,
  testIntegrityFindings,
  lintCategory,
  lintKey,
  scanSecrets,
  sortCounts,
  tightenCounts,
  workflowFindings
};
