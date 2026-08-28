import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

// mailDns.check.ts has carried its own assertions since it was written, and
// nothing ever called them: the file type-checked, so it looked covered. The
// DNS records it builds are what an operator pastes into their zone, and a
// wrong SPF or A record is not the kind of thing you notice quickly.
const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-dns-"));
const bundle = join(out, "mailDns.mjs");
execFileSync(join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "wizard", "mailDns.check.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"], { stdio: "inherit" });
const M = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

try {
  M.assertMailDnsContract();
  console.log("ok  mail DNS contract holds");
  console.log("\nALL OK (1 check)");
} catch (err) {
  console.log("FAIL  mail DNS contract: " + (err && err.message));
  process.exit(1);
}
