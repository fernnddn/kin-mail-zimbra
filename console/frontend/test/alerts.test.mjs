import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-al-"));
const bundle = join(out, "alerts.mjs");
execFileSync(
  join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "tasks", "alerts.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"],
  { stdio: "inherit" },
);
const { deriveAlerts } = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};
const ids = (c) => deriveAlerts(c).map((a) => a.id).sort();
// A fully healthy 2vm pair.
const healthy = {
  topology: "2vm", nodes: ["a", "b"], offline: [], stale_peers: [], standby: [],
  promoted: "a", drbd_uptodate: true, qdevice_ok: true, failcount_ok: true,
  maintenance_active: false, observability: { status: "healthy" },
};

chk("healthy pair raises nothing", deriveAlerts(healthy), []);
chk("null snapshot is safe", deriveAlerts(null), []);
chk("undefined snapshot is safe", deriveAlerts(undefined), []);

// A single-server install must not be told its HA is broken.
chk("1vm raises no HA alerts", deriveAlerts({ ...healthy, topology: "1vm", drbd_uptodate: false, qdevice_ok: false, promoted: null }), []);
chk("unknown topology raises nothing", deriveAlerts({ ...healthy, topology: "", promoted: null }), []);

chk("offline node is flagged", ids({ ...healthy, offline: ["b"] }), ["nodes-down"]);
chk("stale peer counts as down", ids({ ...healthy, stale_peers: ["b"] }), ["nodes-down"]);

chk("no promoted node is danger",
  deriveAlerts({ ...healthy, promoted: null }).map((a) => [a.id, a.severity]), [["no-promoted", "danger"]]);
chk("dual promoted is danger and not 'no promoted'",
  ids({ ...healthy, promoted: null, promoted_conflict: true }), ["dual-promoted"]);

// Resync is expected after a build: warn, not danger.
chk("drbd resync is a warning",
  deriveAlerts({ ...healthy, drbd_uptodate: false, drbd_sync_percent: 62 }).map((a) => [a.id, a.severity]),
  [["drbd", "warn"]]);
chk("drbd not uptodate with no percent is danger",
  deriveAlerts({ ...healthy, drbd_uptodate: false }).map((a) => [a.id, a.severity]),
  [["drbd", "danger"]]);

chk("observability unreachable is danger",
  deriveAlerts({ ...healthy, observability: { status: "unreachable" } }).map((a) => [a.id, a.severity]),
  [["observability", "danger"]]);
// qdevice noise must not stack on top of the real observability alert.
chk("observability alert replaces the qdevice alert",
  ids({ ...healthy, qdevice_ok: false, observability: { status: "unreachable" } }), ["observability"]);
chk("qdevice alone is a warning",
  deriveAlerts({ ...healthy, qdevice_ok: false }).map((a) => [a.id, a.severity]), [["qdevice", "warn"]]);

chk("failcount is a warning", ids({ ...healthy, failcount_ok: false }), ["failcount"]);
chk("maintenance is a warning", ids({ ...healthy, maintenance_active: true }), ["maintenance"]);

// Serving-alone must read as a warning, a real quorum stop as danger.
chk("quorum ignore is a warning",
  deriveAlerts({ ...healthy, quorum_hint: "Running alone", no_quorum_policy: "ignore" })
    .map((a) => [a.id, a.severity]), [["quorum", "warn"]]);
chk("quorum stop is danger",
  deriveAlerts({ ...healthy, quorum_hint: "Mail stopped", no_quorum_policy: "stop" })
    .map((a) => [a.id, a.severity]), [["quorum", "danger"]]);

// The both-down scenario should surface every relevant fact at once.
chk("both down surfaces quorum + node + observability",
  ids({ ...healthy, offline: ["b"], observability: { status: "unreachable" },
        quorum_hint: "Running alone", no_quorum_policy: "ignore" }),
  ["nodes-down", "observability", "quorum"]);

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
