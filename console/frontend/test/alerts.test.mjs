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
const { deriveAlerts, mergeAlerts, activeAlertCount, RESOLVED_ALERT_LINGER_MS } = await import(pathToFileURL(bundle).href);
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
chk("observability absent is a warning",
  deriveAlerts({ ...healthy, observability: { status: "absent" } }).map((a) => [a.id, a.severity]),
  [["observability-absent", "warn"]]);

// Losing the witness costs the pair its third vote, so quorum then needs both
// mail nodes and a single node failure stops mail. Saying only "no witness"
// reads as "still fine, I have two nodes", which is the wrong conclusion.
const detailOf = (snap, id) =>
  (deriveAlerts(snap).find((a) => a.id === id) || {}).detail || "";
chk("absent witness says the pair cannot survive a node failure",
  /both mail\s+nodes/.test(detailOf({ ...healthy, observability: { status: "absent" } }, "observability-absent")),
  true);
chk("absent witness titles the consequence, not just the missing VM",
  /cannot survive/.test(
    (deriveAlerts({ ...healthy, observability: { status: "absent" } })[0] || {}).title || ""),
  true);
chk("unreachable witness says losing a node stops mail",
  /stops mail/.test(detailOf({ ...healthy, observability: { status: "unreachable" } }, "observability")),
  true);

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

// --- resolved-alert merge (Phase 5 QA: alerts flickered in and out) --------
const A = (id, sev = "warn") => ({ id, title: id, severity: sev });
let merged = mergeAlerts([A("drbd")], [], 1000);
chk("a cleared alert stays, marked resolved", merged.map((a) => [a.id, a.severity]), [["drbd", "resolved"]]);
chk("a resolved alert does not count towards the badge", activeAlertCount(merged), 0);
chk("active alerts do count", activeAlertCount([A("x"), A("y", "danger")]), 2);

// It must not linger forever, and the clock starts when it first cleared.
const stamped = mergeAlerts([A("drbd")], [], 1000);
const stillThere = mergeAlerts(stamped, [], 1000 + RESOLVED_ALERT_LINGER_MS - 1);
chk("still shown just inside the linger window", stillThere.length, 1);
const gone = mergeAlerts(stamped, [], 1000 + RESOLVED_ALERT_LINGER_MS + 1);
chk("dropped once the linger window passes", gone.length, 0);

// A flapping alert must go back to active, not stay struck through.
const back = mergeAlerts(stamped, [A("drbd")], 2000);
chk("a returning alert becomes active again", back.map((a) => [a.id, a.severity]), [["drbd", "warn"]]);
chk("a returning alert clears its resolved stamp", back[0].resolvedAt, undefined);

chk("fresh alerts pass through unchanged", mergeAlerts([], [A("a"), A("b", "danger")], 1).map((a) => a.id), ["a", "b"]);
chk("nothing in, nothing out", mergeAlerts([], [], 1), []);

// The two states that stop Pacemaker acting while every other indicator stays
// green. During the Phase 7 run the page showed Zimbra stopped everywhere, the
// VIP nowhere, and fail-count clear, with nothing saying why.
const okPair = {
  topology: "2vm",
  nodes: ["mail1", "mail2"],
  promoted: "mail1",
  zimbra_node: "mail1",
  vip_node: "mail1",
  vip_ip: "192.0.2.9",
  drbd_uptodate: true,
  qdevice_ok: true,
  failcount_ok: true,
  observability: { status: "healthy" },
};
const alertIds = (c) => deriveAlerts(c).map((a) => a.id);

chk("a healthy pair raises nothing about these", 
  alertIds(okPair).filter((i) => i === "maintenance_mode" || i === "stale_ban"), []);
chk("maintenance-mode is raised", alertIds({ ...okPair, maintenance_mode: true }).includes("maintenance_mode"), true);
chk("a leftover ban is raised",
  alertIds({ ...okPair, bans: [{ resource: "kin-drbd-clone", node: "mail1", role: "Master" }] }).includes("stale_ban"),
  true);
chk("an empty ban list is not an alert", alertIds({ ...okPair, bans: [] }).includes("stale_ban"), false);

// Both mean mail is down and cannot recover unattended, so neither is a
// warning to scroll past.
const alertSeverity = (c, id) => deriveAlerts(c).find((a) => a.id === id)?.severity;
chk("maintenance-mode is not a mere warning", alertSeverity({ ...okPair, maintenance_mode: true }, "maintenance_mode"), "danger");
chk("a leftover ban is not a mere warning",
  alertSeverity({ ...okPair, bans: [{ resource: "kin-drbd-clone" }] }, "stale_ban"), "danger");

// --- TLS certificate expiry ------------------------------------------------
// The one fault that is invisible until the morning it takes mail down. Two of
// the three TLS methods never renew on their own, and on an HA pair renewal
// only runs on the node that issued the certificate.
chk("a certificate with plenty of time is silent", ids({ ...healthy, tls_days_left: 60, tls_method: "cloudflare" }), []);
chk("no certificate reading raises nothing", ids({ ...healthy, tls_days_left: null }), []);
chk("missing field raises nothing", ids(healthy), []);
chk("expiring soon is flagged", ids({ ...healthy, tls_days_left: 20, tls_method: "cloudflare" }), ["tls_expiring"]);
chk("exactly at the threshold is flagged", ids({ ...healthy, tls_days_left: 30, tls_method: "manual" }), ["tls_expiring"]);
chk("one day past the threshold is silent", ids({ ...healthy, tls_days_left: 31, tls_method: "manual" }), []);
chk("an expired certificate is its own alert", ids({ ...healthy, tls_days_left: -2, tls_method: "customer" }), ["tls_expired"]);

// Severity: a week out is urgent whoever renews it; expired is always danger.
{
  const sev = (c) => deriveAlerts(c).filter((a) => a.id.startsWith("tls_")).map((a) => a.severity);
  chk("three weeks out warns", sev({ ...healthy, tls_days_left: 21, tls_method: "cloudflare" }), ["warn"]);
  chk("one week out is danger", sev({ ...healthy, tls_days_left: 5, tls_method: "cloudflare" }), ["danger"]);
  chk("expired is danger", sev({ ...healthy, tls_days_left: -1, tls_method: "cloudflare" }), ["danger"]);
}

// The advice has to match who is responsible for renewing it.
{
  const detail = (m) => deriveAlerts({ ...healthy, tls_days_left: 10, tls_method: m })[0].detail;
  chk("cloudflare says renewal is automatic", /[Aa]utomatic renewal/.test(detail("cloudflare")), true);
  chk("manual says it will not renew itself", /does not renew automatically/.test(detail("manual")), true);
  chk("customer says it will not renew itself", /does not renew automatically/.test(detail("customer")), true);
}

// Singular/plural, because "expires in 1 days" is the kind of thing an
// operator notices and stops trusting the rest of the page over.
{
  const title = (d) => deriveAlerts({ ...healthy, tls_days_left: d, tls_method: "manual" })[0].title;
  chk("one day is singular", title(1), "TLS certificate expires in 1 day");
  chk("two days is plural", title(2), "TLS certificate expires in 2 days");
  const expired = (d) => deriveAlerts({ ...healthy, tls_days_left: d, tls_method: "manual" })[0].detail;
  chk("expired one day ago is singular", /ran out 1 day ago/.test(expired(-1)), true);
  chk("expired two days ago is plural", /ran out 2 days ago/.test(expired(-2)), true);
}


console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
