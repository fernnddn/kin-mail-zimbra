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
  bans?: { resource?: string; node?: string; role?: string }[];
  quorum_hint?: string;
  no_quorum_policy?: string;
  observability?: { status?: string };
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

  const obs = cluster.observability?.status;
  if (obs === "unreachable") {
    out.push({
      id: "observability",
      title: "Observability VM unreachable",
      detail: "Quorum witness and SBD fencing are both down.",
      severity: "danger",
    });
  } else if (obs === "absent") {
    out.push({
      id: "observability-absent",
      title: "No Observability VM configured",
      detail: "The pair has no quorum witness and no fencing.",
      severity: "warn",
    });
  } else if (!cluster.qdevice_ok) {
    out.push({
      id: "qdevice",
      title: "Quorum device is not voting",
      severity: "warn",
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
    out.push({
      id: "maintenance",
      title: "A node is in maintenance",
      detail: (cluster.standby || []).join(", ") || undefined,
      severity: "warn",
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
