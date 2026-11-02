// The fence's checks for any repository. Built-in checks need no setup; command checks run the
// repository's own tools (formatter, linter, tests, type checker) as configured in .fence/fence.json.
// Large repositories run in "changed" mode: only files the change touches are checked, and a
// lint-style check passes when no file's count of any rule went up compared with the merge base.
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  clonePairs,
  compareCounts,
  excepted,
  exceptionProblems,
  expiredExceptions,
  gateFindings,
  testIntegrityFindings,
  lintCategory,
  lintKey,
  scanSecrets,
} from "./core.mjs";

export const ROOT = spawnSync("git", ["rev-parse", "--show-toplevel"], { encoding: "utf8" }).stdout.trim();
export const OUT_DIR = path.join(ROOT, ".cache", "fence");
// Where the gate's own code and configuration come from. In CI this is a checkout of the base branch, so a pull
// request cannot change the checks, rules or exceptions that judge it. Locally it is the working tree.
export const GATE = process.env.FENCE_GATE_DIR ? path.resolve(process.env.FENCE_GATE_DIR) : ROOT;

// The repository's own commands never see where the gate lives: a test suite that starts another fence (as this
// repository's do) would otherwise read the outer run's gate instead of its own.
const CHILD_ENV = Object.fromEntries(Object.entries(process.env).filter(([k]) => k !== "FENCE_GATE_DIR"));

export function run(argv, opts = {}) {
  const [cmd, ...args] = argv;
  const r = spawnSync(cmd, args, {
    cwd: opts.cwd ?? ROOT,
    env: CHILD_ENV,
    encoding: "utf8",
    maxBuffer: 512 * 1024 * 1024,
    shell: false,
    timeout: opts.timeout ?? 30 * 60 * 1000,
  });
  return { code: r.status ?? 1, stdout: r.stdout ?? "", stderr: r.stderr ?? "", error: r.error };
}

const today = () => new Date().toISOString().split("T")[0];

export function readJson(file, fallback) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return fallback;
  }
}

export function result(id, category, title, findings, ctx, extra = {}) {
  const kept = findings.filter((f) => !excepted(id, f, ctx.exceptions, today()));
  const waivedList = findings.filter((f) => excepted(id, f, ctx.exceptions, today()));
  const waived = waivedList.length;
  return {
    id,
    category,
    title,
    status: kept.length ? "fail" : "pass",
    summary: kept.length ? `${kept.length} finding${kept.length === 1 ? "" : "s"}` : `clean${waived ? ` (${waived} under an exception)` : ""}`,
    findings: kept,
    waived: waivedList,
    fixable: Boolean(ctx.config.fixers?.[id]),
    ...extra,
  };
}

const skip = (id, category, title, why) => ({ id, category, title, status: "skip", summary: why, findings: [], fixable: false });

export function mergeBase(base) {
  return run(["git", "merge-base", base, "HEAD"]).stdout.trim() || base;
}

/** Files added, modified or renamed since the merge base, plus untracked files. */
export function changedFiles(base) {
  const committed = run(["git", "diff", "--name-only", "--diff-filter=AMR", mergeBase(base)]).stdout;
  const untracked = run(["git", "ls-files", "--others", "--exclude-standard"]).stdout;
  return [...new Set(`${committed}\n${untracked}`.split("\n").filter(Boolean))].filter((f) => {
    const p = path.join(ROOT, f);
    return existsSync(p) && statSync(p).isFile();
  });
}

function globToRegex(glob) {
  return new RegExp(
    `^${glob
      .split("**/")
      .map((part) =>
        part
          .split("**")
          .map((s) => s.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "[^/]*").replace(/\?/g, "[^/]"))
          .join(".*"),
      )
      .join("(?:.*/)?")}$`,
  );
}
/** "src/*.{ts,tsx}" -> ["src/*.ts", "src/*.tsx"], nested braces included. */
export function expandBraces(glob) {
  const m = /\{([^{}]*)\}/.exec(glob);
  if (!m) return [glob];
  return m[1].split(",").flatMap((alt) => expandBraces(glob.slice(0, m.index) + alt + glob.slice(m.index + m[0].length)));
}
export const matcher = (globs = ["**"]) => {
  const rx = (Array.isArray(globs) ? globs : [globs]).flatMap(expandBraces).map(globToRegex);
  return (f) => rx.some((r) => r.test(f));
};

/** A throwaway checkout of a revision that shares node_modules, for "what was it before this change". */
function worktreeAt(rev) {
  const dir = mkdtempSync(path.join(tmpdir(), "fence-base-"));
  const r = run(["git", "worktree", "add", "--detach", dir, rev]);
  if (r.code !== 0) throw new Error(r.stderr);
  if (existsSync(path.join(ROOT, "node_modules"))) symlinkSync(path.join(ROOT, "node_modules"), path.join(dir, "node_modules"));
  return dir;
}
const dropWorktree = (dir) => run(["git", "worktree", "remove", "--force", dir]);

// ----------------------------------------------------------------------------- command checks

/**
 * One configured check: { id, category, title, run: [...], parse: "exit" | "lines" | "eslint-json",
 * files: "all" | "changed", glob, ratchet: "diff" | "none", maxFiles }.
 * In "changed" mode the changed files matching glob are appended to run. With parse "eslint-json"
 * and ratchet "diff", the same command runs on the merge base for the same files, and only counts
 * that went up are findings, so existing debt in a large repository does not block a change.
 */
/** Fields of a check that name a file or directory its tools write, for the review page. */
export const EVIDENCE = ["results", "coverage", "mutations"];

export function commandCheck(spec, ctx) {
  // Record what the check looked at, so the review page can say which checks covered each changed file:
  // the files it ran on (changed or affected mode), or the paths a person declared it covers (whole-repository mode).
  const scope = { files: [] };
  // Evidence files the check's own tools write, read by the review page: per-test results (JUnit XML, a file or a
  // directory of them, as Maven and Gradle write), line coverage (JaCoCo XML or lcov) and mutation results (PIT XML).
  // A stale file from an earlier run must not stand in for this one.
  const evidence = EVIDENCE.filter((k) => spec[k]);
  for (const k of evidence) {
    rmSync(path.join(ROOT, spec[k]), { force: true, recursive: true });
    mkdirSync(path.dirname(path.join(ROOT, spec[k].replace(/\/+$/, ""))), { recursive: true });
  }
  const out = commandCheckRun(spec, ctx, scope);
  for (const r of out) {
    r.run = spec.run;
    for (const k of evidence) r[k] = existsSync(path.join(ROOT, spec[k])) ? spec[k] : null;
    if (spec.explain) r.explain = spec.explain;
    if (spec.files === "changed" || spec.files === "affected") r.files = scope.files;
    else if (spec.covers) r.covers = Array.isArray(spec.covers) ? spec.covers : [spec.covers];
  }
  return out;
}

function commandCheckRun(spec, ctx, scope) {
  const title = spec.title ?? spec.id;
  let files = [];
  if (spec.files === "changed" || spec.files === "affected") {
    const changed = changedFiles(ctx.base);
    files = (spec.files === "affected" ? affectedFiles(changed) : changed).filter(matcher(spec.glob));
    if (!files.length) return [skip(spec.id, spec.category, title, `no ${spec.files} files for this check`)];
    if (spec.maxFiles && files.length > spec.maxFiles) {
      // Checking only part of a change and reporting it clean would be a pass nobody earned.
      if (spec.truncate !== "allow")
        return [{ id: spec.id, category: spec.category, title, status: "fail", summary: `${files.length} files, limit ${spec.maxFiles}`, findings: [{ message: `This change has ${files.length} ${spec.files} files and the check is limited to ${spec.maxFiles}, so it would check only part of it.`, fix: "Split the change, raise maxFiles, or have a person set truncate: \"allow\" for this check." }], fixable: false }];
      files = files.slice(0, spec.maxFiles);
    }
  }
  scope.files = files;
  if (spec.parse === "tsc") return tscCheck(spec, files, ctx);
  // "{base}" in a command stands for the commit the change is compared with (a script that needs changed lines).
  const argv = [...spec.run.map((a) => (a === "{base}" ? ctx.base ?? "HEAD" : a)), ...files];
  if (spec.parse === "exit") {
    const r = run(argv);
    const tail = `${r.stdout}\n${r.stderr}`.trim().split("\n").slice(-15).join("\n");
    return [result(spec.id, spec.category, title, r.code === 0 ? [] : [{ message: `exit ${r.code}: ${tail}`, fix: spec.hint }], ctx)];
  }
  if (spec.parse === "lines") {
    const r = run(argv);
    const lines = r.stdout.split("\n").map((l) => l.trim()).filter(Boolean);
    // A tool that failed and listed nothing did not check anything; that is not a pass.
    if (r.code !== 0 && !lines.length)
      return [result(spec.id, spec.category, title, [{ message: `the command failed (exit ${r.code}) and listed no files: ${(r.stderr || String(r.error ?? "")).trim().slice(-300)}`, fix: "Fix the command in .fence/fence.json or the tool's setup." }], ctx)];
    return [result(spec.id, spec.category, title, lines.map((file) => ({ file, message: spec.message ?? "needs attention", fix: spec.hint })), ctx)];
  }
  if (spec.parse === "eslint-json") return eslintCheck(spec, argv, files, ctx);
  throw new Error(`Unknown parse mode ${spec.parse} for ${spec.id}`);
}

function eslintCounts(argv, cwd) {
  const r = run(argv, { cwd });
  let report;
  try {
    report = JSON.parse(r.stdout);
  } catch {
    throw new Error(`the linter did not print JSON: ${(r.stderr || r.stdout).slice(-400)}`);
  }
  const counts = {};
  const msgs = {};
  for (const f of report) {
    const file = path.relative(cwd, f.filePath).split(path.sep).join("/");
    for (const m of f.messages) {
      const key = lintKey(m.ruleId, m.message);
      (counts[file] ??= {})[key] = (counts[file][key] ?? 0) + 1;
      (msgs[`${file}\u0000${key}`] ??= []).push(m);
    }
  }
  return { counts, msgs };
}

function eslintCheck(spec, argv, files, ctx) {
  const title = spec.title ?? spec.id;
  const cats = ctx.config.lintCategories ?? {};
  const head = eslintCounts(argv, ROOT);
  let before = {};
  if (spec.ratchet === "diff") {
    const dir = worktreeAt(mergeBase(ctx.base));
    try {
      const existing = files.filter((f) => existsSync(path.join(dir, f)));
      before = existing.length ? eslintCounts([...spec.run, ...existing], dir).counts : {};
    } finally {
      dropWorktree(dir);
    }
  } else if (spec.ratchet === "baseline") {
    before = readJson(path.join(ROOT, ".fence", "baselines", `${spec.id}.json`), {});
  }
  const diff = compareCounts(head.counts, before);
  const byCat = {};
  for (const o of diff.over) {
    const ms = head.msgs[`${o.file}\u0000${o.key}`];
    const cat = lintCategory(ms[0].ruleId, ms[0].message, cats);
    (byCat[cat] ??= []).push({
      file: o.file,
      line: ms[0].line,
      message: `${ms[0].message.replace(/\[\w+\]\s*/, "")} (${o.key}: ${o.count} now, ${o.allowed} before; lines ${ms.map((m) => m.line).join(", ")})`,
      fix: ms.some((m) => m.fix) ? "the linter can fix this (the gardener runs it)" : undefined,
    });
  }
  const used = new Set(Object.keys(byCat));
  const all = [...new Set([spec.category ?? "practices", ...used])];
  return all.map((cat) => result(`${spec.id}:${cat}`, cat, `${title}: ${cat}`, byCat[cat] ?? [], ctx, { fixable: (byCat[cat] ?? []).some((f) => f.fix) }));
}

/**
 * The changed files plus every file that imports one of them directly, from the codebase map's import
 * graph (.cartographer/facts/graph.json). In a large repository this is how a check reaches the callers
 * and tests of a change without running everything.
 */
export function affectedFiles(changed) {
  const graph = readJson(path.join(ROOT, ".cartographer", "facts", "graph.json"), { fileEdges: [] });
  const set = new Set(changed);
  const out = new Set(changed);
  for (const [from, to] of graph.fileEdges ?? []) if (set.has(to) && existsSync(path.join(ROOT, from))) out.add(from);
  return [...out];
}

/** Type errors per file and code, from tsc run on just these files with the repository's own tsconfig. */
function tscCounts(spec, files, cwd) {
  const dir = path.join(cwd, ".cache", "fence");
  mkdirSync(dir, { recursive: true });
  const cfg = path.join(dir, `tsconfig.${spec.id}.json`);
  const rel = (f) => path.relative(dir, path.join(cwd, f)).split(path.sep).join("/");
  writeFileSync(
    cfg,
    `${JSON.stringify(
      {
        extends: rel(spec.tsconfig ?? "tsconfig.json"),
        compilerOptions: { noEmit: true, incremental: false, plugins: [], ...(spec.compilerOptions ?? {}) },
        files: files.map(rel),
        include: (spec.include ?? []).map(rel),
      },
      null,
      2,
    )}\n`,
  );
  const r = run(["npx", "tsc", "-p", cfg, "--pretty", "false"], { cwd });
  const wanted = new Set(files);
  const counts = {};
  const msgs = {};
  for (const line of r.stdout.split("\n")) {
    const m = /^(.+?)\((\d+),\d+\): error (TS\d+): (.*)$/.exec(line.trim());
    if (!m) continue;
    const file = path.relative(cwd, path.resolve(cwd, m[1])).split(path.sep).join("/");
    // Only the files asked about: errors elsewhere come from checking part of a program, not from the change.
    if (!wanted.has(file)) continue;
    (counts[file] ??= {})[m[3]] = (counts[file][m[3]] ?? 0) + 1;
    (msgs[`${file}\u0000${m[3]}`] ??= []).push({ line: Number(m[2]), message: m[4] });
  }
  return { counts, msgs };
}

/** { parse: "tsc", tsconfig, include, compilerOptions, ratchet: "diff" }: a type check of the changed (or affected) files. */
function tscCheck(spec, files, ctx) {
  const title = spec.title ?? spec.id;
  const head = tscCounts(spec, files, ROOT);
  let before = {};
  if (spec.ratchet === "diff") {
    const dir = worktreeAt(mergeBase(ctx.base));
    try {
      const existing = files.filter((f) => existsSync(path.join(dir, f)));
      before = existing.length ? tscCounts(spec, existing, dir).counts : {};
    } finally {
      dropWorktree(dir);
    }
  }
  const diff = compareCounts(head.counts, before);
  const findings = diff.over.map((o) => {
    const ms = head.msgs[`${o.file}\u0000${o.key}`];
    return { file: o.file, line: ms[0].line, message: `${ms[0].message} (${o.key}: ${o.count} now, ${o.allowed} before)` };
  });
  const r = result(spec.id, spec.category ?? "tests", title, findings, ctx);
  if (r.status === "pass") r.summary = `clean: ${files.length} file${files.length === 1 ? "" : "s"} checked`;
  return [r];
}

// ----------------------------------------------------------------------------- built-in checks

export function secrets(ctx) {
  const ignore = matcher(ctx.config.secrets?.ignore ?? ["**/package-lock.json", "**/*.lock"]);
  const all = () => [...new Set([...run(["git", "ls-files"]).stdout.split("\n"), ...run(["git", "ls-files", "--others", "--exclude-standard"]).stdout.split("\n")].filter(Boolean))];
  const files = (ctx.config.secrets?.scope === "all" ? all() : changedFiles(ctx.base)).filter(
    (f) => !ignore(f) && !/\.(png|jpe?g|gif|webp|avif|ico|pdf|woff2?|ttf|zip|gz|db|sqlite|jar)$/i.test(f) && existsSync(path.join(ROOT, f)),
  );
  const findings = files.flatMap((f) => {
    const p = path.join(ROOT, f);
    if (!statSync(p).isFile() || statSync(p).size > 2_000_000) return [];
    return scanSecrets(readFileSync(p, "utf8"), f);
  });
  return [result("secrets", "security", "No secrets in the change", findings, ctx)];
}

export function workflows(ctx) {
  const dir = path.join(ROOT, ".github", "workflows");
  if (!existsSync(dir)) return [skip("workflows", "security", "CI workflows", "no .github/workflows")];
  const changed = new Set(changedFiles(ctx.base));
  const findings = readdirSync(dir)
    .filter((f) => /\.ya?ml$/.test(f))
    .filter((f) => ctx.config.workflows?.scope === "all" || changed.has(`.github/workflows/${f}`))
    .flatMap((f) => {
      const text = readFileSync(path.join(dir, f), "utf8");
      return workflowTextFindings(`.github/workflows/${f}`, text);
    });
  return [result("workflows", "security", "CI workflows: least privilege, no script injection", findings, ctx)];
}

const UNTRUSTED =
  /\$\{\{\s*github\.(event\.(pull_request\.(title|body|head\.ref|head\.label)|issue\.(title|body)|comment\.body|review\.body|review_comment\.body|head_commit\.(message|author\.(name|email))|commits)|head_ref)[^}]*\}\}/;

/** Workflow rules from the text, so no YAML library is needed in the target repository. */
export function workflowTextFindings(file, text) {
  const out = [];
  const lines = text.split("\n");
  const at = (i) => i + 1;
  lines.forEach((raw, i) => {
    const l = raw.replace(/\s+#.*$/, "");
    if (/^\s*pull_request_target\b|^on:.*\bpull_request_target\b/.test(l))
      out.push({ file, line: at(i), message: "pull_request_target runs fork code with this repository's secrets and write token.", fix: "Use pull_request; split write steps into a workflow_run job." });
    const m = UNTRUSTED.exec(l);
    // A value on its own after a key (env:, with:) is passed as data; anywhere else it is pasted into a script.
    if (m && !/^\s*-?\s*[\w-]+:\s*["']?\$\{\{[^}]*\}\}["']?\s*$/.test(l))
      out.push({ file, line: at(i), message: `${m[0]} is pasted into a script; a crafted title or branch name runs as code.`, fix: "Pass it through env: and use the variable." });
    const u = /^\s*-?\s*uses:\s*([^\s#]+@(main|master|HEAD))\s*$/.exec(l);
    if (u) out.push({ file, line: at(i), message: `${u[1]} follows a moving branch.`, fix: "Pin to a release tag or a commit SHA." });
  });
  if (!lines.some((l) => /^permissions:/.test(l))) {
    const jobsAt = lines.findIndex((l) => /^jobs:/.test(l));
    const jobStarts = lines.map((l, i) => (i > jobsAt && /^ {2}[\w-]+:\s*$/.test(l) ? i : -1)).filter((i) => i >= 0);
    jobStarts.forEach((start, k) => {
      const end = jobStarts[k + 1] ?? lines.length;
      if (!lines.slice(start, end).some((l) => /^ {4}permissions:/.test(l)))
        out.push({ file, line: at(start), message: `Job ${lines[start].trim().slice(0, -1)} has no permissions block, so it gets the default token scope.`, fix: "Declare the least permissions the job needs." });
    });
  }
  return out;
}

export function duplicates(ctx) {
  const d = ctx.config.duplicates;
  if (!d) return [skip("duplicates", "patterns", "No new copied code", "not configured")];
  const out = path.join(OUT_DIR, "jscpd");
  const pairsOf = (paths, cwd) => {
    // ignorePattern: code that repeats by necessity, such as a Java file's import list.
    const ignore = [...(d.ignorePattern ? ["--ignore-pattern", d.ignorePattern] : []), ...(d.ignore?.length ? ["--ignore", d.ignore.join(",")] : [])];
    run(["npx", "--yes", "jscpd@4.0.5", ...paths, "--pattern", d.pattern ?? "**/*", "--min-tokens", String(d.minTokens ?? 50), ...ignore, "--silent", "--reporters", "json", "--output", out], { cwd });
    return clonePairs(readJson(path.join(out, "jscpd-report.json"), { duplicates: [] }).duplicates);
  };
  const changed = new Set(changedFiles(ctx.base).filter(matcher(d.glob)));
  if (!changed.size) return [skip("duplicates", "patterns", "No new copied code", "no changed source files")];
  const now = pairsOf(d.paths, ROOT);
  const mine = Object.fromEntries(Object.entries(now).filter(([pair]) => pair.split(" <> ").some((f) => changed.has(f))));
  const base = readJson(path.join(ROOT, ".fence", "baselines", "duplicates.json"), null);
  const findings = compareCounts(mine, base ?? {}).over.map((o) => ({
    file: o.file.split(" <> ")[0],
    message: `${o.count} copied block(s) between ${o.file.replace(" <> ", " and ")}, ${o.allowed} before`,
    fix: "Reuse the existing code: extract the shared part into a function both call.",
  }));
  return [result("duplicates", "patterns", "No new copied code", findings, ctx)];
}

export function dependencies(ctx) {
  const pkg = path.join(ROOT, "package.json");
  if (!existsSync(pkg)) return [skip("dependencies", "patterns", "New dependencies are approved", "no package.json")];
  const names = (p) => Object.keys({ ...(p?.dependencies ?? {}), ...(p?.devDependencies ?? {}) });
  const now = names(readJson(pkg, {}));
  const baseText = run(["git", "show", `${mergeBase(ctx.base)}:package.json`]).stdout;
  let before = [];
  try {
    before = names(JSON.parse(baseText));
  } catch {
    before = [];
  }
  const approved = readJson(path.join(ROOT, ".fence", "dependencies.json"), {});
  const findings = now
    .filter((n) => !before.includes(n))
    .filter((n) => !approved[n]?.reason || !approved[n]?.approvedBy)
    .map((n) => ({ file: "package.json", message: `${n} is new and not in .fence/dependencies.json with a reason and an approver.`, fix: "Use what the repository already has, or ask a person to approve it." }));
  return [result("dependencies", "patterns", "New dependencies are approved", findings, ctx)];
}

export function mapCoverage(ctx) {
  const model = readJson(path.join(ROOT, ".cartographer", "model", "model.json"), null);
  if (!model?.components?.length) return [skip("map-coverage", "patterns", "New code belongs to a mapped component", "no codebase map yet (run /cartograph-survey)")];
  const code = matcher(ctx.config.code ?? ["**/*.{ts,tsx,js,mjs,cjs,py,go,java,cs,rs}"]);
  const owned = (f) =>
    model.components.some((c) => (c.paths ?? []).some((p) => f === p || f.startsWith(`${p.replace(/\/$/, "")}/`) || (p.includes("*") && matcher(p)(f))));
  const added = run(["git", "diff", "--name-only", "--diff-filter=A", mergeBase(ctx.base)]).stdout.split("\n").filter(Boolean);
  const untracked = run(["git", "ls-files", "--others", "--exclude-standard"]).stdout.split("\n").filter(Boolean);
  const findings = [...new Set([...added, ...untracked])]
    .filter(code)
    .filter((f) => !owned(f))
    .map((file) => ({ file, message: "This new file belongs to no component in the codebase map.", fix: "Put it in an existing component's folder, or add it to the map in the same change." }));
  return [result("map-coverage", "patterns", "New code belongs to a mapped component", findings, ctx)];
}

export function guard(ctx) {
  const title = "Architecture and process rules (guard.py)";
  const bin = path.join(GATE, ".cartographer", "bin", "guard.py");
  if (!existsSync(bin)) {
    if (existsSync(path.join(ROOT, ".cartographer"))) return [result("guard", "structure", title, [{ message: "Cartographer is in this repository but the gate has no guard.py, so its rules cannot run.", fix: "Restore .cartographer/bin/guard.py on the base branch." }], ctx)];
    return [skip("guard", "structure", title, "Cartographer not installed")];
  }
  const out = path.join(OUT_DIR, "guard.json");
  mkdirSync(OUT_DIR, { recursive: true });
  rmSync(out, { force: true });
  const mode = ctx.ci ? ["--base", ctx.base, "--head", "HEAD"] : ["--worktree", "--base", ctx.base];
  // Rules and baseline come from the gate (the base branch in CI); the code and the map come from the change.
  const rulesFrom = GATE === ROOT ? [] : ["--rules-from", path.join(GATE, ".cartographer")];
  const r = run(["python3", bin, ...mode, ...rulesFrom, "--json", out, "--quiet", "--no-log"]);
  const res = readJson(out, null);
  // Fail closed: no result, a crash, or rules that could not be evaluated are not a pass.
  if (!res) return [result("guard", "structure", title, [{ message: `guard.py wrote no result (exit ${r.code}): ${(r.stderr || r.stdout).trim().slice(-400)}`, fix: "Run the guard locally and fix what stops it." }], ctx)];
  const findings = (res.blocking ?? []).map((v) => ({ file: v.path, line: v.line, message: `[${v.rule}] ${v.message}`, fix: v.fix }));
  for (const e of res.errors ?? []) findings.push({ message: `A rule could not be evaluated: ${typeof e === "string" ? e : JSON.stringify(e)}`, fix: "Fix the rule or the file it reads; an unevaluated rule enforces nothing." });
  if (!res.rules && !ctx.config.guardMayBeEmpty)
    findings.push({ file: ".cartographer/guardrails.json", message: "No accepted rules: the guard enforces nothing on this change.", fix: "A person accepts proposed rules in the rules workbench, or sets guardMayBeEmpty in .fence/fence.json while onboarding." });
  if (r.code !== 0 && !findings.length) findings.push({ message: `guard.py exited ${r.code} with no finding: ${(r.stderr || "").trim().slice(-300)}`, fix: "Run the guard locally." });
  const out2 = result("guard", "structure", title, findings, ctx, { rules: res.rules ?? 0 });
  out2.fixable = (res.blocking ?? []).some((v) => ctx.config.fixers?.[`guard:${v.rule}`]);
  return [out2];
}

export function formal(ctx) {
  const title = "Formal models: the code's model holds, broken variants still fail";
  if (!existsSync(path.join(ROOT, "verification"))) return [skip("formal", "tests", title, "no verification/ folder")];
  if (run(["java", "-version"]).code !== 0) {
    // Models exist but cannot be checked here. In CI that is a failure; locally it is reported, not hidden.
    if (ctx.ci) return [result("formal", "tests", title, [{ message: "This repository has formal models but the runner has no Java, so they were not checked.", fix: "Install Java 11+ in the fence workflow." }], ctx)];
    return [skip("formal", "tests", title, "no Java (TLC needs Java 11+)")];
  }
  const r = run(["python3", path.join(GATE, ".cartographer", "bin", "formal.py"), "check"]);
  const findings = r.stdout
    .split("\n")
    .filter((l) => l.startsWith("FAIL"))
    .map((l) => ({ message: l.replace(/^FAIL\s+/, ""), fix: "If the code changed on purpose, update the model; if not, the model found a bug." }));
  if (r.code !== 0 && !findings.length) findings.push({ message: `formal.py exited ${r.code} without naming a failure: ${(r.stderr || r.stdout).trim().slice(-300)}`, fix: "Run python3 .cartographer/bin/formal.py check locally." });
  return [result("formal", "tests", title, findings, ctx)];
}

export function exceptionsCheck(ctx) {
  const t = today();
  const findings = expiredExceptions(ctx.exceptions, t).map((e) => ({
    file: ".fence/exceptions.json",
    message: `The exception for ${e.check}${e.file ? ` on ${e.file}` : ""} expired on ${e.expires} (${e.reason}).`,
    fix: "Fix what it covered, or have a person renew it.",
  }));
  for (const e of ctx.exceptions) {
    const problems = exceptionProblems(e, t);
    if (problems.length)
      findings.push({ file: ".fence/exceptions.json", message: `The exception for ${e.check ?? "an unnamed check"}${e.file ? ` on ${e.file}` : ""} ${problems.join(", ")}, so it waives nothing.`, fix: "Give it a check, a file, a reason, a named approver and an expiry within 90 days." });
    else if (e.file && !e.file.endsWith("/") && !existsSync(path.join(ROOT, e.file)))
      findings.push({ file: ".fence/exceptions.json", message: `The exception for ${e.check} names ${e.file}, which no longer exists.`, fix: "Remove the exception." });
  }
  return [result("exceptions", "structure", "Exceptions are complete and current", findings, ctx)];
}

/** Every file the change adds, modifies, renames or deletes, plus untracked files. */
function allChanged(base) {
  const mb = mergeBase(base);
  const tracked = run(["git", "diff", "--name-only", "--no-renames", mb]).stdout;
  const untracked = run(["git", "ls-files", "--others", "--exclude-standard"]).stdout;
  return [...new Set(`${tracked}\n${untracked}`.split("\n").filter(Boolean))];
}

export function gateIntegrity(ctx) {
  const title = "Fence left alone: the change does not edit its own checks alongside other code";
  // The pull request that installs the factory has no gate to protect yet; a code owner reviews it as a whole.
  if (run(["git", "cat-file", "-e", `${mergeBase(ctx.base)}:.fence/fence.json`]).code !== 0)
    return [skip("gate-integrity", "structure", title, "first install: the base branch has no fence yet")];
  const findings = gateFindings(allChanged(ctx.base), ctx.config.gatePatterns);
  return [result("gate-integrity", "structure", title, findings, ctx)];
}

export function testIntegrity(ctx) {
  const mb = mergeBase(ctx.base);
  const files = [];
  for (const line of run(["git", "diff", "--name-status", "-M", mb]).stdout.split("\n").filter(Boolean)) {
    const [st, a, b] = line.split("\t");
    const status = st[0] === "R" ? "M" : st[0];
    const file = b ?? a;
    if (status === "D") files.push({ file, status });
    else if (status === "M") {
      const before = run(["git", "show", `${mb}:${a}`]).stdout;
      const after = existsSync(path.join(ROOT, file)) ? readFileSync(path.join(ROOT, file), "utf8") : "";
      const added = run(["git", "diff", "-U0", mb, "--", file]).stdout.split("\n").filter((l) => l.startsWith("+") && !l.startsWith("+++")).map((l) => l.slice(1));
      files.push({ file, status, before, after, added });
    } else if (status === "A") {
      const p = path.join(ROOT, file);
      files.push({ file, status, added: existsSync(p) ? readFileSync(p, "utf8").split("\n") : [] });
    }
  }
  for (const file of run(["git", "ls-files", "--others", "--exclude-standard"]).stdout.split("\n").filter(Boolean)) {
    const p = path.join(ROOT, file);
    if (existsSync(p) && statSync(p).isFile()) files.push({ file, status: "A", added: readFileSync(p, "utf8").split("\n") });
  }
  return [result("test-integrity", "tests", "Tests kept: none deleted, emptied of assertions, or skipped", testIntegrityFindings(files), ctx)];
}

export const BUILTINS = {
  secrets,
  workflows,
  duplicates,
  dependencies,
  "map-coverage": mapCoverage,
  guard,
  formal,
  exceptions: exceptionsCheck,
  "gate-integrity": gateIntegrity,
  "test-integrity": testIntegrity,
};
// These run whatever .fence/fence.json lists: a configuration cannot switch off the checks that protect the gate.
export const MANDATORY = ["exceptions", "gate-integrity", "test-integrity"];
