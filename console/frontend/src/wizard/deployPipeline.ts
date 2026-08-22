/**
 * Full-install stages in the same order as install/kin-mail.sh run_full_install().
 * Conditional skips (07 / 11) still count toward the planned Primary pipeline total.
 */
export type InstallStage = {
  script: string;
  label: string;
};

/** Ordered stages executed by `kin-mail.sh --full-install` on a Primary host. */
export const FULL_INSTALL_STAGES: readonly InstallStage[] = [
  { script: "01-preflight.sh", label: "Host readiness" },
  { script: "02-prepare-os.sh", label: "Prepare OS" },
  { script: "03-install-zimbra.sh", label: "Install mail software" },
  { script: "04-tls-dkim.sh", label: "Certificates & DKIM" },
  { script: "06-hybrid-auth.sh", label: "Directory login" },
  { script: "07-zpush.sh", label: "Mobile email" },
  { script: "09-hardening.sh", label: "Hardening" },
  { script: "10-host-firewall.sh", label: "Host firewall" },
  { script: "11-admin-path-lockdown.sh", label: "Admin path lockdown" },
  { script: "05-healthcheck.sh", label: "Health check" },
] as const;

export const FULL_INSTALL_STAGE_COUNT = FULL_INSTALL_STAGES.length;

/**
 * HA orchestration steps, same order and labels as
 * console/backend/kin_privhelper/orchestration.py STEPS.
 * Backend tests pin this list against that tuple so the Deploy checklist
 * cannot silently drift.
 *
 * Conditional skips (join_mode=check cluster_join steps, and live_join_check
 * on apply) have no distinct visual state. parseHaOrchProgress tracks START
 * lines, so a skipped index becomes done once a later START advances current,
 * matching stageState.
 */
export const HA_ORCH_STAGES: readonly InstallStage[] = [
  { script: "peer_os_prep", label: "OS prep + Zimbra install on the new node" },
  { script: "mail_zpush_snippets", label: "mail-zpush.yml tags=snippets (nginx includes on every mail node)" },
  { script: "os_hardening", label: "mail-os-hardening.yml (fail2ban + unattended-upgrades)" },
  { script: "mon_qnetd", label: "mon-qnetd.yml (qdevice witness)" },
  { script: "mail_cluster_setup", label: "mail-cluster-setup.yml (pcsd + pcs cluster setup)" },
  { script: "mail_qdevice", label: "mail-qdevice.yml (qdevice client packages + TLS)" },
  { script: "mon_iscsi", label: "mon-iscsi-target.yml (SBD LUN)" },
  { script: "mail_fencing", label: "mail-fencing.yml (initiator packages, softdog, SBD agent)" },
  { script: "mail_drbd_install", label: "mail-drbd.yml tags=install (DRBD packages)" },
  { script: "mail_drbd_activate", label: "mail-drbd.yml resource activation" },
  { script: "mail_pacemaker_agents", label: "mail-pacemaker.yml tags=agents (OCF scripts)" },
  { script: "mail_pacemaker_stack", label: "mail-pacemaker.yml stack (constraints, VIP, hostname remap)" },
  {
    script: "live_join_check",
    label: "dry-run mail-drbd + mail-pacemaker against the live Pacemaker pair",
  },
] as const;

export const HA_ORCH_STAGE_COUNT = HA_ORCH_STAGES.length;

export type InstallProgress = {
  /** 0 = not started; 1..total = current stage number */
  current: number;
  total: number;
  label: string;
  script: string;
  complete: boolean;
  failed: boolean;
};

export function emptyInstallProgress(): InstallProgress {
  return {
    current: 0,
    total: FULL_INSTALL_STAGE_COUNT,
    label: "Not started",
    script: "",
    complete: false,
    failed: false,
  };
}

/** True when the deploy transcript has entered the Ansible HA sequence. */
export function isHaOrchestrationLog(log: string): boolean {
  return (
    /HA orchestration Slice 2/i.test(log) ||
    /\bORCH_(DONE|FAILED)\b/.test(log) ||
    /\[\d+\/\d+\] START /.test(log)
  );
}

/** True if this transcript ever recorded a successful Primary full-install.

  Use only for parsing the live progress bar during a full-install run.
  Do not gate Build HA pair on this: HA orchestration truncates last-log and
  resetLogBuffer wipes the viewer, so the success lines disappear forever.
  UI must use the persisted setup-complete flag from /api/setup/status.
 */
export function hasFullInstallCompleted(log: string): boolean {
  return /Full install complete/i.test(log) || /All selected pipeline stages exited 0/i.test(log);
}

/** Offer Build HA pair after a successful Primary full-install on 2-server topology.

  `fullInstallComplete` is the server-side /etc/kin-mail/setup-complete marker,
  not a grep of the live log buffer.
 */
export function shouldOfferHaPair(opts: {
  topology: string;
  fullInstallComplete: boolean;
}): boolean {
  return opts.topology === "2vm" && opts.fullInstallComplete;
}

/** Derive install progress from streamed kin-mail.sh output. */
export function parseInstallProgress(log: string): InstallProgress {
  if (isHaOrchestrationLog(log)) {
    return parseHaOrchProgress(log);
  }

  const total = FULL_INSTALL_STAGE_COUNT;
  let current = 0;
  let label = "Starting…";
  let script = "";

  for (let i = 0; i < FULL_INSTALL_STAGES.length; i++) {
    const stage = FULL_INSTALL_STAGES[i];
    // run_stage prints: Running 01-preflight.sh   or  Running 09-hardening.sh (--os-only)
    if (log.includes(`Running ${stage.script}`)) {
      current = i + 1;
      label = stage.label;
      script = stage.script;
    }
  }

  const failed =
    /Pipeline stopped at /i.test(log) ||
    /Install stopped unexpectedly/i.test(log) ||
    /\[FAIL\]/.test(log);
  const complete = hasFullInstallCompleted(log);

  if (complete) {
    return {
      current: total,
      total,
      label: "Complete",
      script: FULL_INSTALL_STAGES[total - 1]?.script || "",
      complete: true,
      failed: false,
    };
  }

  if (failed) {
    return {
      current: current || 1,
      total,
      label: current ? `${label} (failed)` : "Failed",
      script,
      complete: false,
      failed: true,
    };
  }

  if (current === 0 && /Starting mail system install|Console full install|Full install/i.test(log)) {
    return {
      current: 0,
      total,
      label: "Preparing…",
      script: "",
      complete: false,
      failed: false,
    };
  }

  return {
    current,
    total,
    label: current ? label : "Waiting…",
    script,
    complete: false,
    failed: false,
  };
}

/** Per-playbook checkpoints from privhelper HA orchestration (Slice 2). */
export function parseHaOrchProgress(log: string): InstallProgress {
  const startRe = /\[(\d+)\/(\d+)\] START (\S+):\s*(.+)$/gm;
  let current = 0;
  let total = 0;
  let label = "Starting…";
  let script = "";
  let m: RegExpExecArray | null;
  while ((m = startRe.exec(log)) !== null) {
    current = Number(m[1]);
    total = Number(m[2]);
    script = m[3];
    label = m[4].replace(/\s+proof=.*$/, "").trim() || script;
  }
  if (!total) {
    const skip = /\[(\d+)\/(\d+)\] SKIP /.exec(log);
    if (skip) {
      current = Number(skip[1]);
      total = Number(skip[2]);
    }
  }
  const failed =
    /\bORCH_FAILED\b/.test(log) || /\] FAIL /.test(log) || /^Refusing:/m.test(log);
  const complete = /\bORCH_DONE\b/.test(log);
  const resolvedTotal = total || HA_ORCH_STAGE_COUNT;
  if (complete) {
    return {
      current: resolvedTotal,
      total: resolvedTotal,
      label: "HA sequence complete",
      script,
      complete: true,
      failed: false,
    };
  }
  if (failed) {
    return {
      current: current || 1,
      total: resolvedTotal,
      label: current ? `${label} (failed)` : "HA sequence failed",
      script,
      complete: false,
      failed: true,
    };
  }
  if (!current && /Auto-partition:|Re-checking DRBD backing disks/.test(log)) {
    return {
      current: 0,
      total: resolvedTotal,
      label: "Preparing disks",
      script: "",
      complete: false,
      failed: false,
    };
  }
  return {
    current,
    total: resolvedTotal,
    label: current ? label : "Waiting…",
    script,
    complete: false,
    failed: false,
  };
}
