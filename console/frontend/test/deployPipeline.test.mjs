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
const {
  parseHaOrchProgress,
  parseInstallProgress,
  parseMetricsStage,
  shouldOfferHaPair,
  FULL_INSTALL_STAGES,
} = await import(pathToFileURL(bundle).href);
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
chk("console heading alone stays at preparing", [p.current, p.label], [0, "Preparing..."]);

p = parseInstallProgress("Running 01-preflight.sh\nRunning 02-prepare-os.sh\n");
chk("historic Running lines still advance", [p.current, p.script], [2, "02-prepare-os.sh"]);

p = parseInstallProgress(
  "==> Console full install\n==> 01-preflight.sh\n==> 02-prepare-os.sh\n==> 03-install-zimbra.sh\n",
);
// Total is 11, not 10: built-in monitoring is now a listed stage. It runs
// from the privhelper after the installer exits, and leaving it off the
// checklist is how a 1vm appliance reported a finished deploy with nothing
// collecting behind it.
chk("current ==> headings advance to install mail software", [p.current, p.total, p.label], [
  3,
  11,
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

// --- built-in monitoring is a visible stage ---------------------------------
//
// It runs from the privhelper AFTER kin-mail.sh exits, detached so that an SSE
// drop cannot kill it. That detachment is why it was invisible: the deploy had
// already reported success, so a metrics install that failed, timed out, or
// never started produced an appliance that looked finished and healthy with
// permanently empty Monitoring and Reports tabs. It is on the checklist now,
// and a non-zero exit fails the run rather than passing quietly.

const monitoringStage = FULL_INSTALL_STAGES[FULL_INSTALL_STAGES.length - 1];
chk(
  "built-in monitoring is the last listed stage",
  [monitoringStage.script, monitoringStage.marker],
  ["install-monitoring", "KIN_METRICS_BEGIN"],
);

const fullRun =
  "==> 01-preflight.sh\n==> 02-prepare-os.sh\n==> 03-install-zimbra.sh\n" +
  "==> 04-tls-dkim.sh\n==> 06-hybrid-auth.sh\n==> 07-zpush.sh\n" +
  "==> 09-hardening.sh\n==> 10-host-firewall.sh\n" +
  "==> 11-admin-path-lockdown.sh\n==> 05-healthcheck.sh\nFull install complete\n";

// A transcript with no metrics marker at all is from before this stage
// existed (or from the HA path). It completes rather than hanging the page;
// a real 1vm deploy always emits a marker, including when it could not start.
p = parseInstallProgress(fullRun);
chk("a transcript with no metrics marker still completes", [p.total, p.complete], [11, true]);

p = parseInstallProgress(fullRun + "KIN_METRICS_BEGIN host=mail1\n");
chk("the monitoring stage becomes current when it starts", [p.current, p.label], [
  11,
  "Built-in monitoring",
]);

p = parseInstallProgress(fullRun + "KIN_METRICS_BEGIN host=mail1\nKIN_METRICS_END exit=0\n");
chk("a clean metrics install does not fail the run", [p.current, p.failed], [11, false]);

p = parseInstallProgress(fullRun + "KIN_METRICS_BEGIN host=mail1\nKIN_METRICS_END exit=2\n");
chk("a failed metrics install fails the run", p.failed, true);

// exit=10 must not be read as exit=1 followed by a stray 0, and exit=0 must
// not match the failure pattern just because a digit follows.
p = parseInstallProgress(fullRun + "KIN_METRICS_END exit=10\n");
chk("a two-digit non-zero exit still fails", p.failed, true);

chk("no metrics marker at all is absent", parseMetricsStage(fullRun), "absent");
chk(
  "begin without end is pending, not success",
  parseMetricsStage(fullRun + "KIN_METRICS_BEGIN host=mail1\n"),
  "pending",
);
chk(
  "end zero is ok",
  parseMetricsStage(fullRun + "KIN_METRICS_BEGIN\nKIN_METRICS_END exit=0\n"),
  "ok",
);
chk(
  "end non-zero is failed",
  parseMetricsStage(fullRun + "KIN_METRICS_BEGIN\nKIN_METRICS_END exit=3\n"),
  "failed",
);
chk(
  "ANSI around the marker still counts",
  parseMetricsStage("\u001b[36mKIN_METRICS_BEGIN\u001b[0m host=mail1\n"),
  "pending",
);

// --- the two-server path is hidden for this release ------------------------
//
// Not deleted. An operator who finds a Build HA pair button, presses it and
// hits a rough edge reasonably concludes the whole product is rough, so the
// entry points are gated behind one flag while the code stays in the build.
// This asserts the gate is actually closed, because a flag nothing reads is
// the same as no flag.

chk(
  "Build HA pair is not offered even on a completed 2vm primary",
  shouldOfferHaPair({ topology: "2vm", fullInstallComplete: true }),
  false,
);
chk(
  "and certainly not on a single server",
  shouldOfferHaPair({ topology: "1vm", fullInstallComplete: true }),
  false,
);
chk(
  "nor before the install has finished",
  shouldOfferHaPair({ topology: "2vm", fullInstallComplete: false }),
  false,
);

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
