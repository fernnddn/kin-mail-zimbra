// The Deploy banner used to say only "Could not save settings" and hide the
// apply_draft refusal. These lines are what the operator actually sees.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-ad-"));
const bundle = join(out, "applyDraft.mjs");
execFileSync(
  join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "wizard", "applyDraft.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"],
  { stdio: "inherit" },
);
const { applyDraftFailureMessage } = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0;
let fail = 0;
const chk = (name, got, want) => {
  if (got === want) {
    pass++;
    console.log("ok  " + name);
  } else {
    fail++;
    console.log(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
};

chk(
  "an empty log still names the stop and points at the transcript",
  applyDraftFailureMessage(""),
  "Could not save settings, Deploy stopped before install. Open the log below for the reason.",
);

chk(
  "a validation refusal is copied onto the banner",
  applyDraftFailureMessage(
    "=== apply_wizard_draft ===\nERROR: draft validation failed:\n  - edge_host is required when topology is split\n",
  ),
  "Could not save settings, Deploy stopped before install. draft validation failed: - edge_host is required when topology is split",
);

chk(
  "a Cloudflare token refusal is copied onto the banner",
  applyDraftFailureMessage(
    "ERROR: TLS_METHOD=cloudflare needs a real API token in /etc/letsencrypt/cloudflare.ini (the wizard does not store it).\n",
  ),
  "Could not save settings, Deploy stopped before install. TLS_METHOD=cloudflare needs a real API token in /etc/letsencrypt/cloudflare.ini (the wizard does not store it).",
);

if (fail) {
  console.log(`\n${fail} failed, ${pass} passed`);
  process.exit(1);
}
console.log(`\n${pass} passed`);
