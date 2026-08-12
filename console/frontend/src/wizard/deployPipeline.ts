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

/** Derive install progress from streamed kin-mail.sh output. */
export function parseInstallProgress(log: string): InstallProgress {
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

  const failed = /Pipeline stopped at /i.test(log);
  const complete =
    /Full install complete/i.test(log) || /All selected pipeline stages exited 0/i.test(log);

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
