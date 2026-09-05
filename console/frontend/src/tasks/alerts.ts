/** Turn a cluster status snapshot into header alerts.
 *
 * Pure on purpose: this is what the notification bell shows on every page, so
 * it is worth testing without a browser. Phase 4-1 feedback was that problems
 * were only visible if you happened to be on the Cluster page and opened the
 * health dropdown.
 */

export type AlertSeverity = "warn" | "danger" | "resolved";
export type Alert = {
  id: string;
  title: string;
  detail?: string;
  severity: AlertSeverity;
  /** Set when this alert has cleared and is being shown as resolved. */
  resolvedAt?: number;
};

/** How long a cleared alert stays visible, struck through, before it goes. */
export const RESOLVED_ALERT_LINGER_MS = 5 * 60 * 1000;

/** Merge a fresh derivation with what was on screen a moment ago.
 *
 * A poll that clears an alert used to make the row vanish mid-read, and a
 * flapping condition made rows appear and disappear - the operator saw exactly
 * that and could not tell whether anything was actually wrong. A cleared alert
 * now stays, marked resolved, until it has been settled for a while.
 */
export function mergeAlerts(
  previous: Alert[],
  fresh: Alert[],
  now: number = Date.now(),
): Alert[] {
  const freshIds = new Set(fresh.map((a) => a.id));
  const out: Alert[] = fresh.map((a) => ({ ...a, resolvedAt: undefined }));
  for (const old of previous) {
    if (freshIds.has(old.id)) continue;
    const since = old.resolvedAt ?? now;
    if (now - since > RESOLVED_ALERT_LINGER_MS) continue;
    out.push({ ...old, severity: "resolved", resolvedAt: since });
  }
  return out;
}

/** Only unresolved alerts count towards the header badge. */
export function activeAlertCount(alerts: Alert[]): number {
  return alerts.filter((a) => a.severity !== "resolved").length;
}

// Thirty days is a full Let's Encrypt renewal window plus slack, and long
// enough that a human-driven renewal can be scheduled rather than rushed.
export const TLS_WARN_DAYS = 30;

export type AlertSnap = {
  topology?: string;
  nodes?: string[];
  offline?: string[];
  stale_peers?: string[];
  standby?: string[];
  promoted?: string | null;
  promoted_conflict?: boolean;
  drbd_uptodate?: boolean;
  drbd_sync_percent?: number | null;
  qdevice_ok?: boolean;
  failcount_ok?: boolean;
  maintenance_active?: boolean;
  maintenance_mode?: boolean;
  maintenance_since?: string;
  maintenance_source?: string;
  drbd_link?: string;
  tls_days_left?: number | null;
  tls_method?: string;
  bans?: { resource?: string; node?: string; role?: string }[];
  quorum_hint?: string;
  no_quorum_policy?: string;
  observability?: { status?: string };
  fencing_enabled?: boolean | null;
};

export function deriveAlerts(cluster: AlertSnap | null | undefined): Alert[] {
  if (!cluster) return [];
  const out: Alert[] = [];
  // A single-server install has no peer, no witness and no DRBD: none of the
  // HA alerts below can be true, and raising them would be noise.
  if (cluster.topology !== "2vm") return out;

  if (cluster.quorum_hint) {
    const serving = (cluster.no_quorum_policy || "").toLowerCase() === "ignore";
    out.push({
      id: "quorum",
      title: serving ? "Serving without redundancy" : "Cluster has lost quorum",
      detail: cluster.quorum_hint,
      severity: serving ? "warn" : "danger",
    });
  }

  const down = [...(cluster.offline || []), ...(cluster.stale_peers || [])];
  if (down.length) {
    out.push({
      id: "nodes-down",
      title: down.length > 1 ? "Mail nodes unreachable" : "Mail node unreachable",
      detail: down.join(", "),
      severity: "danger",
    });
  }

  if (cluster.promoted_conflict) {
    out.push({
      id: "dual-promoted",
      title: "Two nodes report Promoted",
      detail: "DRBD may have split-brained. Do not write to both nodes.",
      severity: "danger",
    });
  } else if (!cluster.promoted) {
    out.push({
      id: "no-promoted",
      title: "No node is Promoted",
      detail: "Mail cannot be served until one node holds the DRBD Master role.",
      severity: "danger",
    });
  }

  if (!cluster.drbd_uptodate) {
    const pct = cluster.drbd_sync_percent;
    out.push({
      id: "drbd",
      title: typeof pct === "number" ? `DRBD resyncing (${pct.toFixed(0)}%)` : "DRBD not UpToDate",
      detail:
        typeof pct === "number"
          ? "Normal after a fresh Build HA pair. Failover is unsafe until it reaches 100%."
          : "Replication is not healthy on both nodes.",
      severity: typeof pct === "number" ? "warn" : "danger",
    });
  }

  // Losing the witness costs the pair its third vote AND the SBD fencing LUN,
  // which lives on that VM. What THAT costs depends on no-quorum-policy, so
  // the sentence is derived from the configured policy rather than assumed:
  //
  //   ignore (this appliance's default) - a lone survivor keeps serving, so
  //     availability is fine. What is gone is the ability to fence, and with
  //     it the protection against a partition where both nodes stay up.
  //   stop  - the survivor is not quorate and Pacemaker stops mail on a node
  //     that is otherwise perfectly healthy.
  //
  // Saying "losing a node stops mail" on an ignore appliance would be plainly
  // wrong, and naming only the missing witness reads as "still fine, I have
  // two nodes", which is the wrong conclusion either way.
  const stopsWithoutQuorum = (cluster.no_quorum_policy || "").toLowerCase() === "stop";
  const witnessCost = stopsWithoutQuorum
    ? "Quorum now needs both mail nodes, so losing one stops mail instead of " +
      "failing over."
    : "A surviving node keeps serving, but nothing can fence a peer any more: " +
      "if the two nodes are cut off from each other while both stay up, both " +
      "can go Primary and DRBD will refuse to merge the two copies.";
  const obs = cluster.observability?.status;
  if (obs === "unreachable") {
    out.push({
      id: "observability",
      title: "Observability VM unreachable",
      detail: `The quorum witness and SBD fencing are both down. ${witnessCost}`,
      severity: "danger",
    });
  } else if (obs === "absent") {
    out.push({
      id: "observability-absent",
      title: stopsWithoutQuorum
        ? "Pair cannot survive a node failure"
        : "No fencing: a network split could diverge the replicas",
      detail: `There is no quorum witness and no fencing. ${witnessCost} ` +
        "Add an Observability VM to restore it.",
      severity: "warn",
    });
  } else if (!cluster.qdevice_ok) {
    out.push({
      id: "qdevice",
      title: "Quorum device is not voting",
      severity: "warn",
    });
  }

  // Only when the witness is healthy, so fencing SHOULD be on. While
  // Observability is absent or unreachable, fencing is off by design and the
  // alert above already says so - raising this one too would just be noise.
  // Undefined means the property could not be read, which is not evidence
  // that fencing is off.
  if (obs === "healthy" && cluster.fencing_enabled === false) {
    out.push({
      id: "fencing-off",
      title: "Fencing is disabled",
      detail:
        "stonith-enabled is false, so a split-brain would not be arbitrated " +
        "and the two replicas could diverge. Left over from a failed Remove " +
        "Observability, or from a manual override.",
      severity: "danger",
    });
  }

  if (cluster.failcount_ok === false) {
    out.push({
      id: "failcount",
      title: "Pacemaker fail-count is non-zero",
      detail: "A resource failed at least once. Clear it from the Cluster page after checking.",
      severity: "warn",
    });
  }

  if (cluster.maintenance_active) {
    const who = (cluster.standby || []).join(", ");
    // Standby survives a reboot, so "in maintenance" on its own leaves the
    // operator unable to tell a deliberate one from a leftover.
    out.push({
      id: "maintenance",
      title: who ? `${who} is in maintenance` : "A node is in maintenance",
      detail:
        cluster.maintenance_source === "console"
          ? `Entered from this console on ${cluster.maintenance_since}.`
          : "No record of this console entering it. Exit maintenance to clear it.",
      severity: "warn",
    });
  }

  // The certificate is the one fault that stays invisible until the morning it
  // takes mail down, and only the Cloudflare method renews on its own: manual
  // and customer both need a human, and on an HA pair renewal only happens on
  // the node that issued it. Warn with enough time to act.
  const tlsDays = cluster.tls_days_left;
  if (typeof tlsDays === "number") {
    const auto = cluster.tls_method === "cloudflare";
    if (tlsDays < 0) {
      out.push({
        id: "tls_expired",
        title: "The TLS certificate has expired",
        detail:
          `It ran out ${Math.abs(tlsDays)} day${Math.abs(tlsDays) === 1 ? "" : "s"} ago. ` +
          "Mail clients and browsers will refuse to connect. Renew it from Settings.",
        severity: "danger",
      });
    } else if (tlsDays <= TLS_WARN_DAYS) {
      out.push({
        id: "tls_expiring",
        title: `TLS certificate expires in ${tlsDays} day${tlsDays === 1 ? "" : "s"}`,
        detail: auto
          ? "Automatic renewal should handle this. If the number keeps falling, check Settings."
          : "This appliance does not renew automatically. Renew it from Settings before it runs out.",
        severity: tlsDays <= 7 ? "danger" : "warn",
      });
    }
  }

  // Both replicas can read UpToDate while nothing is being copied between
  // them. This is the one fault where every other indicator stays green and
  // the cost is losing every message written since it started.
  if (cluster.drbd_link === "down") {
    out.push({
      id: "drbd_link",
      title: "Replication between the nodes is down",
      detail:
        "Mail written on the serving node is not reaching the other one. " +
        "Both may still report UpToDate and hold different data. Do not fail " +
        "over until DRBD is reconnected.",
      severity: "danger",
    });
  }

  // The two states that stop Pacemaker acting while reporting nothing wrong.
  // They are the reason a cluster can sit with mail down and every other
  // indicator green, so they belong in front of the operator, not buried.
  if (cluster.maintenance_mode) {
    out.push({
      id: "maintenance_mode",
      title: "Pacemaker is in maintenance-mode",
      detail:
        "Resources will not start, stop, or move, and monitors are not running. " +
        "Mail cannot recover on its own until this is turned off.",
      severity: "danger",
    });
  }

  if ((cluster.bans || []).length) {
    out.push({
      id: "stale_ban",
      title: "A Move Master ban is still set",
      detail:
        "No node is allowed to take that role, so DRBD has no Primary and mail " +
        "cannot start. Clear it from the Cluster page.",
      severity: "danger",
    });
  }

  return out;
}
