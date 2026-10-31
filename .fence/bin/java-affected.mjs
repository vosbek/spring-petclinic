#!/usr/bin/env node
// Run only the JUnit test classes that reach a change, for a large Java repository.
//   node .fence/bin/java-affected.mjs <maven|gradle> [-Dflag ...] <changed or affected .java files ...>
// A test class reaches a changed class when it is in the same package (Java needs no import there), imports it
// (the fence passes importers from the codebase map's import graph), or is named after it (Foo -> FooTest,
// FooTests, FooIT). Exit 0 when no test class reaches the change.
import { existsSync, readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";

const SOURCE = /^(.*?)src\/(main|test)\/java\/(.+)\.java$/;
const TEST_NAME = /(Test|Tests|IT|TestCase)$/;

/** Test classes (fully qualified) to run for these changed files; `list(dir)` lists .java files in a directory. */
export function testClassesFor(files, list) {
  const out = new Set();
  for (const f of files) {
    const m = SOURCE.exec(f);
    if (!m) continue;
    const [, module, kind, rel] = m;
    const fqcn = rel.split("/").join(".");
    const simple = fqcn.slice(fqcn.lastIndexOf(".") + 1);
    const pkgPath = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
    if (kind === "test") {
      if (TEST_NAME.test(simple)) out.add(fqcn);
      continue;
    }
    for (const name of list(`${module}src/test/java/${pkgPath}`)) {
      const cls = name.replace(/\.java$/, "");
      if (TEST_NAME.test(cls)) out.add([...pkgPath.split("/").filter(Boolean), cls].join("."));
    }
  }
  return [...out].sort();
}

function listJava(dir) {
  return existsSync(dir) ? readdirSync(dir).filter((n) => n.endsWith(".java")) : [];
}

if (path.resolve(process.argv[1] ?? "") === path.resolve(new URL(import.meta.url).pathname)) {
  const [build, ...rest] = process.argv.slice(2);
  const flags = rest.filter((a) => a.startsWith("-"));
  const files = rest.filter((a) => !a.startsWith("-"));
  const classes = testClassesFor(files, listJava);
  if (!classes.length) {
    console.log("No test class reaches the changed files.");
    process.exit(0);
  }
  console.log(`Test classes: ${classes.join(", ")}`);
  const argv =
    build === "gradle"
      ? [existsSync("gradlew") ? "./gradlew" : "gradle", "--console=plain", "-q", "test", ...classes.flatMap((c) => ["--tests", c]), ...flags]
      : [existsSync("mvnw") ? "./mvnw" : "mvn", "-B", "-ntp", "-q", ...flags, `-Dtest=${classes.join(",")}`, "-Dsurefire.failIfNoSpecifiedTests=false", "test"];
  const r = spawnSync(argv[0], argv.slice(1), { stdio: "inherit" });
  process.exit(r.status ?? 1);
}
