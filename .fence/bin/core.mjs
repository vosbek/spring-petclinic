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
function excepted(check, f, list, today) {
  return list.some(
    (e) => e.check === check && e.expires >= today && (!e.file || f.file != null && (e.file.endsWith("/") ? f.file.startsWith(e.file) : f.file === e.file))
  );
}
function expiredExceptions(list, today) {
  return list.filter((e) => e.expires < today);
}
export {
  CATEGORIES,
  SECRET_PATTERNS,
  budgetFindings,
  budgetFor,
  clonePairs,
  compareCounts,
  excepted,
  expiredExceptions,
  lintCategory,
  lintKey,
  scanSecrets,
  sortCounts,
  tightenCounts,
  workflowFindings
};
