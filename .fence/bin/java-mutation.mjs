#!/usr/bin/env node
// Mutation testing on the lines a change adds, for a Maven repository with PIT configured in its pom.
//   node .fence/bin/java-mutation.mjs maven <base> [-Dflag ...] <changed .java files ...>
// PIT changes the changed classes one small way at a time and runs the tests that reach them (same package, or named
// after the class). The check fails when a mutant on a changed line survives (every test still passed) or no test
// runs that line. Mutants on lines the change did not touch are reported by PIT but do not fail the change.
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { testClassesFor } from "./java-affected.mjs";
import { addedLines, mutantsOn, parseMutations, ranges } from "./review.mjs";

const MAIN = /^(.*?)src\/main\/java\/(.+)\.java$/;
const REPORT = "target/pit-reports/mutations.xml";

/** Fully qualified classes for the changed main files, with their inner classes. */
export function targetClasses(files) {
  return files.flatMap((f) => {
    const m = MAIN.exec(f);
    if (!m) return [];
    const fqcn = m[2].split("/").join(".");
    return [fqcn, `${fqcn}$*`];
  });
}

/** What failed, for the changed lines of each file: [{ file, line, status, description, method }]. */
export function missedOnChangedLines(files, mutations, diffOf) {
  const out = [];
  for (const f of files) {
    if (!MAIN.test(f)) continue;
    out.push(...mutantsOn(f, addedLines(diffOf(f)), mutations).missed.map((m) => ({ ...m, file: f })));
  }
  return out;
}

const git = (args) => spawnSync("git", args, { encoding: "utf8" }).stdout ?? "";

if (path.resolve(process.argv[1] ?? "") === path.resolve(new URL(import.meta.url).pathname)) {
  const [build, base, ...rest] = process.argv.slice(2);
  const flags = rest.filter((a) => a.startsWith("-"));
  const files = rest.filter((a) => !a.startsWith("-") && MAIN.test(a));
  if (build !== "maven") {
    console.error(`Mutation testing runs on Maven only so far (asked for ${build}).`);
    process.exit(1);
  }
  const classes = targetClasses(files);
  if (!classes.length) {
    console.log("No changed main classes, so nothing to mutate.");
    process.exit(0);
  }
  const listJava = (dir) => (existsSync(dir) ? readdirSync(dir).filter((n) => n.endsWith(".java")) : []);
  const tests = testClassesFor(files, listJava);
  const argv = [
    existsSync("mvnw") ? "./mvnw" : "mvn", "-B", "-ntp", ...flags, "test-compile", "org.pitest:pitest-maven:mutationCoverage",
    `-DtargetClasses=${classes.join(",")}`, ...(tests.length ? [`-DtargetTests=${tests.join(",")}`] : []),
    "-DoutputFormats=XML,HTML", "-DtimestampedReports=false", "-DfailWhenNoMutations=false",
  ];
  console.log(`Mutating ${files.join(", ")}; tests: ${tests.length ? tests.join(", ") : "all"}`);
  const r = spawnSync(argv[0], argv.slice(1), { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 });
  if (!existsSync(REPORT)) {
    console.error(`${r.stdout ?? ""}\n${r.stderr ?? ""}`.trim().split("\n").slice(-30).join("\n"));
    console.error(`PIT wrote no ${REPORT} (exit ${r.status}). Is pitest-maven, with pitest-junit5-plugin for JUnit 5, in the pom?`);
    process.exit(1);
  }
  const mb = git(["merge-base", base, "HEAD"]).trim() || base;
  const missed = missedOnChangedLines(files, parseMutations(readFileSync(REPORT, "utf8")), (f) => git(["diff", "-U0", mb, "--", f]));
  if (!missed.length) {
    console.log("Every mutant on a changed line was caught by a test.");
    process.exit(0);
  }
  const byFile = Map.groupBy(missed, (m) => m.file);
  for (const [f, ms] of byFile) {
    console.log(`${f}: lines ${ranges(ms.map((m) => m.line))}`);
    for (const m of ms) console.log(`  line ${m.line} ${m.method}(): ${m.description} (${m.status === "NO_COVERAGE" ? "no test runs it" : "every test still passed"})`);
  }
  console.log(`${missed.length} change(s) to changed lines no test noticed.`);
  process.exit(1);
}
