// Runs every frontend test file and fails if any of them fails.
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(here).filter((f) => f.endsWith(".test.mjs")).sort();
if (files.length === 0) {
  console.error("ERROR: no *.test.mjs found - refusing silent pass");
  process.exit(1);
}
let failed = 0;
for (const f of files) {
  console.log(`\n--- ${f}`);
  try {
    execFileSync(process.execPath, [join(here, f)], { stdio: "inherit" });
  } catch {
    failed += 1;
  }
}
console.log(failed ? `\n${failed} test file(s) failed` : `\nAll ${files.length} test files passed`);
process.exit(failed ? 1 : 0);
