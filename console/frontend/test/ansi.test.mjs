import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-an-"));
const bundle = join(out, "ansi.mjs");
execFileSync(join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "wizard", "ansi.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"], { stdio: "inherit" });
const A = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};

// The escape byte itself, built rather than typed so this file stays free of
// control characters.
const E = String.fromCharCode(27);

// Real lines from a certificate renewal, as they were rendered in the console.
chk(
  "a coloured install line reads as words",
  A.stripAnsi(E + "[36m==> " + E + "[0m " + E + "[1mTLS method: manual" + E + "[0m"),
  "==>  TLS method: manual",
);
chk(
  "warnings lose their colour, not their text",
  A.stripAnsi("  " + E + "[33m[WARN]" + E + "[0m   TLS_METHOD=manual - renewal is NOT automatic."),
  "  [WARN]   TLS_METHOD=manual - renewal is NOT automatic.",
);
chk(
  "OK markers survive",
  A.stripAnsi(E + "[32m[ OK ]" + E + "[0m   certbot ready"),
  "[ OK ]   certbot ready",
);

// The escape byte does not always survive whatever carried the text here, and
// a bare "[36m" renders as literal noise rather than as colour.
chk("an orphaned colour code is removed too", A.stripAnsi("[36m==> [0m [1mstarting[0m"), "==>  starting");
chk("orphan with parameters", A.stripAnsi("[1;33mwarn[0m"), "warn");

// That orphan pattern must not eat ordinary bracketed text out of a log line.
chk(
  "array indexes are left alone",
  A.stripAnsi("node.conn[0].timeo.noop_out_timeout"),
  "node.conn[0].timeo.noop_out_timeout",
);
chk("bracketed words are left alone", A.stripAnsi("[WARN] disk [sdb1] is busy"), "[WARN] disk [sdb1] is busy");
chk("a bare m in brackets is left alone", A.stripAnsi("[m]"), "[m]");

// formatDeployLog is what the panes actually call.
chk("carriage returns become newlines", A.formatDeployLog("a\r\nb\rc"), "a\nb\nc");
chk("trailing whitespace goes", A.formatDeployLog("a   \nb\t\n"), "a\nb\n");
chk("leading blank lines go", A.formatDeployLog("\n\n\nfirst"), "first");
chk("empty stays empty", A.formatDeployLog(""), "");

console.log(fail ? "\n" + fail + " FAILED (" + pass + " ok)" : "\nALL OK (" + pass + " checks)");
process.exit(fail ? 1 : 0);
