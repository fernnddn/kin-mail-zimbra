// Regression tests for parseHaOrchProgress (no test framework in this app;
// esbuild is already a Vite dependency, so we transform and run directly).
//
// This parser drives the only progress the operator sees during a 20+ minute
// Build HA pair, and it has now had two separate freezing bugs, so it is worth
// pinning down:
//
//   1. It only advanced on START lines. live_join_check (step 13/13) is
//      SKIPped before its START on EVERY join_mode=apply run, so a completely
//      healthy deployment sat at "12/13" until ORCH_DONE (live Phase 4-1).
//   2. Matching the label with \s* let a bare "[12/13] FINISH step" line
//      swallow the newline and eat the following line as its own label,
//      hiding that step entirely.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-dp-"));
const bundle = join(out, "deployPipeline.mjs");
execFileSync(
  join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "wizard", "deployPipeline.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"],
  { stdio: "inherit" },
);
const { parseHaOrchProgress, parseInstallProgress } = await import(
  pathToFileURL(bundle).href
);
rmSync(out, { recursive: true, force: true });

let pass = 0;
let fail = 0;
const chk = (name, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) {
    pass++;
    console.log("ok  " + name);
  } else {
    fail++;
    console.log(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
};

// The real 13-step apply run: 12 steps run, step 13 is skipped before START.
const steps = [
  "peer_os_prep", "mail_zpush_snippets", "os_hardening", "mon_qnetd",
  "mail_cluster_setup", "mail_qdevice", "mon_iscsi", "mail_fencing",
  "mail_drbd_install", "mail_drbd_activate", "mail_pacemaker_agents",
  "mail_pacemaker_stack",
];
const ran = steps
  .map((s, i) => `[${i + 1}/13] START ${s}: label ${s} proof=apply - full sequence\n[${i + 1}/13] FINISH ${s}`)
  .join("\n");
const skipped = `${ran}\n[13/13] SKIP live_join_check proof=skip (join_mode=apply - live dry-run is check-mode only)`;

let r = parseHaOrchProgress(skipped);
chk("a skipped final step advances 12 -> 13", [r.current, r.total, r.complete], [13, 13, false]);
chk("skipped step is labelled as skipped", r.label, "live_join_check (skipped)");

r = parseHaOrchProgress(`${skipped}\nORCH_DONE`);
chk("ORCH_DONE still completes", [r.current, r.total, r.complete], [13, 13, true]);

r = parseHaOrchProgress(
  `${steps.slice(0, 4).map((s, i) => `[${i + 1}/13] START ${s}: x proof=apply - y\n[${i + 1}/13] FINISH ${s}`).join("\n")}\n` +
  "[5/13] START mail_cluster_setup: Cluster setup proof=apply - full sequence",
);
chk("a bare FINISH does not swallow the next line", [r.current, r.total, r.label], [5, 13, "Cluster setup"]);

r = parseHaOrchProgress(
  `${steps.slice(0, 9).map((s, i) => `[${i + 1}/13] START ${s}: x proof=apply - y\n[${i + 1}/13] FINISH ${s}`).join("\n")}\n` +
  "[10/13] START mail_drbd_activate: DRBD proof=apply - y\n" +
  "[10/13] FAIL mail_drbd_activate exit=1; stopping.\nORCH_FAILED step=mail_drbd_activate join_mode=apply",
);
chk("failure reports the failing step, not the total", [r.current, r.failed, r.complete], [10, true, false]);

r = parseHaOrchProgress("[1/13] START a: x proof=apply - y\n[7/13] START b: x proof=apply - y\n[2/13] FINISH a");
chk("a late low-numbered line never drags the bar backwards", r.current, 7);

r = parseHaOrchProgress("[3/13] FINISH mail_drbd_install");
chk("a bare FINISH line parses on its own", [r.current, r.total, r.script], [3, 13, "mail_drbd_install"]);

r = parseHaOrchProgress("");
chk("empty log is not complete and not failed", [r.current, r.complete, r.failed], [0, false, false]);

// Full-install bar: run_stage used to print "Running 01-preflight.sh". It now
// prints `==> 01-preflight.sh` (with colour codes in the live SSE buffer).
let p = parseInstallProgress(
  "==> Console full install (KIN_CONSOLE_CONFIRMED=1)\n==> Full install\n",
);
chk("console heading alone stays at preparing 0/10", [p.current, p.label], [0, "Preparing..."]);

p = parseInstallProgress("Running 01-preflight.sh\nRunning 02-prepare-os.sh\n");
chk("historic Running lines still advance", [p.current, p.script], [2, "02-prepare-os.sh"]);

p = parseInstallProgress(
  "==> Console full install\n==> 01-preflight.sh\n==> 02-prepare-os.sh\n==> 03-install-zimbra.sh\n",
);
chk("current ==> headings advance to install mail software", [p.current, p.total, p.label], [
  3,
  10,
  "Install mail software",
]);

p = parseInstallProgress(
  "\u001b[36m==>\u001b[0m \u001b[1m01-preflight.sh\u001b[0m\n" +
    "\u001b[36m==>\u001b[0m \u001b[1m02-prepare-os.sh\u001b[0m\n" +
    "\u001b[36m==>\u001b[0m \u001b[1m03-install-zimbra.sh\u001b[0m\n",
);
chk("ANSI between ==> and the script name still counts", [p.current, p.label], [
  3,
  "Install mail software",
]);

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
