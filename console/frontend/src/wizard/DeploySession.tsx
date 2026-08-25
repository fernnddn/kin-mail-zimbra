import type { ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "../api";
import { useSetup } from "../setup";
import { formatDeployLog } from "./ansi";
import { useWizard } from "./WizardContext";
import {
  emptyInstallProgress,
  parseInstallProgress,
  type InstallProgress,
} from "./deployPipeline";

export type DeployActionId =
  | "apply_draft"
  | "full_install"
  | "cancel_firewall_deadman"
  | "ha_orchestration"
  | "add_host";

type FinishReason = "done" | "error" | "disconnect" | "busy";

type StreamEvent = {
  type: string;
  cmd?: string;
  action?: string;
  data?: string;
  code?: string;
  message?: string;
  exit_code?: number;
};

type SetupStatus = {
  deployed?: boolean;
  busy?: boolean;
  install_in_progress?: boolean;
};

type LastLogResponse = {
  text: string;
  missing?: boolean;
  install_in_progress?: boolean;
};

type DeploySessionCtx = {
  log: string;
  message: string;
  /** True while this tab owns an SSE *or* the server still reports an active install. */
  pipelineBusy: boolean;
  cancelBusy: boolean;
  deadmanHint: boolean;
  installProgress: InstallProgress;
  confirmFull: boolean;
  setConfirmFull: (v: boolean) => void;
  confirmHa: boolean;
  setConfirmHa: (v: boolean) => void;
  runDeploy: () => Promise<void>;
  runHaOrchestration: (opts?: { confirmed?: boolean }) => Promise<void>;
  runAddHost: (opts: {
    confirmed?: boolean;
    newName: string;
    newIp: string;
    retiredName?: string;
    retiredIp?: string;
    observabilityVmIp?: string;
    clusterVipIp?: string;
    dataDisk?: string;
    metaDisk?: string;
  }) => Promise<void>;
  runCancelDeadman: () => Promise<void>;
  openLogsTab: () => void;
};

const Ctx = createContext<DeploySessionCtx | null>(null);

/** Real firewall dead-man signals, not the banner "dead-man NOT auto-cancelled". */
function looksLikeDeadmanArmed(chunk: string): boolean {
  if (/dead-man NOT auto-cancelled/i.test(chunk)) return false;
  return /Leaving dead-man ARMED|dead-man timer|Dead-man is ARMED|DEADMAN_ARMED|cancel-deadman after/i.test(
    chunk,
  );
}

async function fetchSetupStatus(): Promise<SetupStatus> {
  return api<SetupStatus>("/api/setup/status");
}

async function fetchLastLog(): Promise<LastLogResponse> {
  return api<LastLogResponse>("/api/wizard/deploy/last-log");
}

const LOG_BASELINE = "# Deployment activity\n";

type DeploySessionProviderProps = {
  children: ReactNode;
  /**
   * The server keeps exactly one deploy transcript (deploy-last.log), shared
   * across every action - apply_draft, full_install, ha_orchestration. A
   * fresh DeploySessionProvider (a new page, e.g. Add second server) polls
   * this on mount, before the operator has started anything of its own. If
   * that leftover transcript happens to be a *completed* run of a
   * different action (typically: the single-node install that just
   * finished on this very host), the fresh page would misread it as its
   * own completion and skip straight to "Done" - hiding the form the
   * operator still needs to fill in.
   *
   * When set, a polled transcript is only trusted (log/installProgress
   * updated from it) if this predicate matches it - e.g. pass
   * isHaOrchestrationLog on the Add second server page so a leftover
   * full-install transcript is treated as "nothing here yet", not as this
   * page's own result. Leave unset on the main Deploy page, which owns
   * apply_draft/full_install and should keep resuming from whatever is
   * already there (existing, already-relied-on behavior).
   */
  logFilter?: (log: string) => boolean;
};

export function DeploySessionProvider({ children, logFilter }: DeploySessionProviderProps) {
  const { draft, save } = useWizard();
  const { installInProgress, refresh: refreshSetup, fullInstallComplete } = useSetup();
  const [log, setLog] = useState(LOG_BASELINE);
  const [message, setMessage] = useState("");
  const [localBusy, setLocalBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [deadmanHint, setDeadmanHint] = useState(false);
  const [confirmFull, setConfirmFull] = useState(false);
  const [confirmHa, setConfirmHa] = useState(false);
  const [installProgress, setInstallProgress] = useState<InstallProgress>(emptyInstallProgress);
  const pipelineEsRef = useRef<EventSource | null>(null);
  const cancelEsRef = useRef<EventSource | null>(null);
  const logBufRef = useRef(LOG_BASELINE);
  /** True only while this tab's EventSource is healthy and delivering events. */
  const localStreamRef = useRef(false);
  /** Last action that owned the pipeline EventSource (for messaging). */
  const pipelineActionRef = useRef<DeployActionId | null>(null);
  /** Date.now() of the last SSE message. Used to detect a hung-open stream. */
  const lastSseAtRef = useRef(0);
  /** Sync lock so a second click cannot start another stream before React re-renders. */
  const pipelineStartRef = useRef(false);
  /** Pending debounced flush of logBufRef into log/installProgress state. */
  const flushTimerRef = useRef<number | null>(null);

  const pipelineBusy = localBusy || installInProgress;

  useEffect(() => {
    return () => {
      pipelineEsRef.current?.close();
      pipelineEsRef.current = null;
      cancelEsRef.current?.close();
      cancelEsRef.current = null;
      if (flushTimerRef.current !== null) {
        window.clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
    };
  }, []);

  const hydrateFromServer = useCallback(async (opts?: { preserveLiveLog?: boolean }): Promise<boolean> => {
    try {
      const [st, res] = await Promise.all([fetchSetupStatus(), fetchLastLog()]);
      const installing = Boolean(st.install_in_progress || st.busy || res.install_in_progress);
      const cleaned = formatDeployLog(res.text || "");
      const live = logBufRef.current;
      const pDisk = parseInstallProgress(cleaned);
      const pLive = parseInstallProgress(live);
      const terminal = pDisk.complete || pDisk.failed || pLive.complete || pLive.failed;

      if (opts?.preserveLiveLog && localStreamRef.current) {
        // A live SSE owned by this provider is always this page's own run;
        // logFilter only gates the cold-poll path below.
        // Stall check while EventSource is still open: never shrink/replace a
        // live buffer with a shorter last-log (that flickers the viewer).
        if (cleaned.trim() && cleaned.length > live.length + 64) {
          logBufRef.current = cleaned;
          setLog(cleaned);
          setInstallProgress(pDisk);
        } else if ((pDisk.failed || pDisk.complete) && !(pLive.failed || pLive.complete)) {
          setInstallProgress(pDisk);
        }
        if (installing) {
          setLocalBusy(true);
          return true;
        }
        if (terminal) {
          setLocalBusy(false);
        }
        return false;
      }

      if (logFilter && !logFilter(cleaned)) {
        // Leftover transcript on the server belongs to a different action
        // (e.g. a just-finished single-node install, seen from the fresh
        // Add second server page). Not ours - leave this session's log and
        // installProgress at baseline instead of misreading it as our own
        // completed run.
        return false;
      }

      if (cleaned.trim()) {
        logBufRef.current = cleaned;
        setLog(cleaned);
        setInstallProgress(pDisk);
      }
      if (installing) {
        setLocalBusy(true);
        return true;
      }
      if (pDisk.complete || pDisk.failed) {
        setLocalBusy(false);
      }
      return false;
    } catch {
      return installInProgress;
    }
  }, [installInProgress, logFilter]);

  // Follow server transcript whenever we are not on a live SSE (new tab, or after disconnect).
  useEffect(() => {
    let cancelled = false;
    async function poll() {
      if (localStreamRef.current) return;
      if (cancelled) return;
      const still = await hydrateFromServer();
      if (!cancelled && still) {
        setMessage((m) =>
          m === "Stream closed" || m.startsWith("Live stream disconnected")
            ? "Following server progress (live stream disconnected)..."
            : m,
        );
      }
    }
    void poll();
    const id = window.setInterval(() => {
      void poll();
    }, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [hydrateFromServer, installInProgress]);

  // EventSource can stay "open" after the job has exited if the terminal
  // `done`/`error` event was buffered or dropped (nginx, tab freeze). Polling
  // above skips while localStreamRef is true, so the spinner never clears.
  // Independently re-read last-log + setup status without closing the socket.
  useEffect(() => {
    const stallMs = 35_000;
    const id = window.setInterval(() => {
      if (!localStreamRef.current) return;
      if (Date.now() - lastSseAtRef.current < stallMs) return;
      void hydrateFromServer({ preserveLiveLog: true });
    }, 5000);
    return () => window.clearInterval(id);
  }, [hydrateFromServer]);

  // Keep localBusy aligned with setup provider when install finishes elsewhere.
  useEffect(() => {
    if (!installInProgress && !localStreamRef.current) {
      // Let hydrate decide complete/failed; don't force idle if progress incomplete.
      void hydrateFromServer();
    } else if (installInProgress) {
      setLocalBusy(true);
    }
  }, [installInProgress, hydrateFromServer]);

  // parseInstallProgress rescans the whole accumulated transcript (regex +
  // substring search), so calling it on every single SSE chunk makes each
  // update cost grow with total log size - visibly laggy on a chatty stage
  // like the Zimbra install, worse the longer the run goes. Chunks still
  // land in logBufRef immediately; only the state update (and the parse it
  // triggers) is coalesced to a few times a second.
  const LOG_FLUSH_INTERVAL_MS = 200;

  const flushLog = useCallback(() => {
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    setLog(logBufRef.current);
    setInstallProgress(parseInstallProgress(logBufRef.current));
  }, []);

  const append = useCallback(
    (chunk: string) => {
      lastSseAtRef.current = Date.now();
      logBufRef.current += chunk;
      if (looksLikeDeadmanArmed(chunk)) {
        setDeadmanHint(true);
      }
      if (flushTimerRef.current === null) {
        flushTimerRef.current = window.setTimeout(flushLog, LOG_FLUSH_INTERVAL_MS);
      }
    },
    [flushLog],
  );

  const attachStream = useCallback(
    (
      action: DeployActionId,
      es: EventSource,
      onFinished: (exit?: number, reason?: FinishReason) => void,
    ) => {
      es.onmessage = (ev) => {
        lastSseAtRef.current = Date.now();
        let parsed: StreamEvent;
        try {
          parsed = JSON.parse(ev.data) as StreamEvent;
        } catch {
          append(ev.data + "\n");
          return;
        }
        if (parsed.type === "meta") {
          append(`[meta] ${parsed.action || "?"}\n`);
          return;
        }
        if (parsed.type === "accepted") {
          append(`[started]\n`);
          return;
        }
        if (parsed.type === "stdout" && parsed.data) {
          append(parsed.data);
          return;
        }
        if (parsed.type === "stderr" && parsed.data) {
          append(`[stderr] ${parsed.data}`);
          return;
        }
        if (parsed.type === "error") {
          const msg = parsed.message || parsed.code || "error";
          append(`[error] ${parsed.code || "error"}: ${msg}\n`);
          flushLog(); // stream is ending - do not leave the final state debounced
          if (parsed.code === "busy") {
            onFinished(undefined, "busy");
            es.close();
            return;
          }
          setMessage(msg);
          onFinished(undefined, "error");
          es.close();
          return;
        }
        if (parsed.type === "done") {
          append(`[done] exit=${parsed.exit_code ?? "?"}\n`);
          flushLog(); // stream is ending - do not leave the final state debounced
          onFinished(parsed.exit_code, "done");
          es.close();
          if (action === "cancel_firewall_deadman" && parsed.exit_code === 0) {
            setDeadmanHint(false);
            setMessage("Firewall confirmation recorded, temporary safety timer cancelled.");
          }
        }
      };

      es.onerror = () => {
        if (pipelineEsRef.current !== es) return;
        // EventSource drops on background tabs / brief network blips. Do NOT treat as finished
        // until the server confirms the install is no longer running.
        append(`[stream] disconnected, checking server status...\n`);
        flushLog();
        es.close();
        onFinished(undefined, "disconnect");
        void (async () => {
          await refreshSetup();
          const still = await hydrateFromServer();
          if (still) {
            setMessage("Live stream disconnected, following server progress...");
            setLocalBusy(true);
          } else {
            setMessage((m) =>
              m.startsWith("Live stream disconnected") || m.includes("checking server")
                ? ""
                : m,
            );
          }
        })();
      };
    },
    [append, flushLog, hydrateFromServer, refreshSetup],
  );

  const openStream = useCallback(
    (
      action: DeployActionId,
      onFinished: (exit?: number, reason?: FinishReason) => void,
      extraQuery?: string,
      attempt = 0,
    ) => {
      localStreamRef.current = true;
      lastSseAtRef.current = Date.now();
      pipelineActionRef.current = action;
      const qs = extraQuery ? `&${extraQuery}` : "";
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}${qs}`,
      );
      const wrapped = (exit?: number, reason?: FinishReason) => {
        if (reason === "busy") {
          if (attempt < 12) {
            setMessage("Waiting for the previous job to finish...");
            window.setTimeout(() => {
              pipelineEsRef.current = openStream(
                action,
                onFinished,
                extraQuery,
                attempt + 1,
              );
            }, 1000);
            return;
          }
          void (async () => {
            await refreshSetup();
            const still = await hydrateFromServer();
            if (still) {
              setMessage("Install already running on the server, showing live progress.");
              setLocalBusy(true);
              if (pipelineEsRef.current === es) {
                pipelineEsRef.current = null;
                localStreamRef.current = false;
              }
              onFinished(undefined, "error");
              return;
            }
            setMessage(
              "Could not start because another helper job was still finishing. Confirm and click Deploy again.",
            );
            if (pipelineEsRef.current === es) {
              pipelineEsRef.current = null;
              localStreamRef.current = false;
            }
            setLocalBusy(false);
            onFinished(undefined, "error");
          })();
          return;
        }
        if (pipelineEsRef.current !== es) return;
        localStreamRef.current = false;
        pipelineEsRef.current = null;
        onFinished(exit, reason);
      };
      attachStream(action, es, wrapped);
      return es;
    },
    [attachStream, hydrateFromServer, refreshSetup],
  );

  const openLogsTab = useCallback(() => {
    window.open("/wizard/deploy/logs", "_blank", "noopener,noreferrer");
  }, []);

  const resetLogBuffer = useCallback(() => {
    // Cancel a pending debounced flush from the previous run so it cannot
    // land after this reset and resurrect stale text.
    if (flushTimerRef.current !== null) {
      window.clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    // Drop the previous run's transcript so parseInstallProgress cannot keep
    // matching "Pipeline stopped at" / ORCH_FAILED from a failed attempt.
    logBufRef.current = LOG_BASELINE;
    setLog(LOG_BASELINE);
    setInstallProgress(emptyInstallProgress());
    // Polling hydrate would restore the old last-log until privhelper truncates
    // it; treat this tab as owning the stream from this moment.
    localStreamRef.current = true;
  }, []);

  const runDeploy = useCallback(async () => {
    if (pipelineStartRef.current || pipelineBusy) {
      setMessage("A deploy or HA job is already running.");
      return;
    }
    if (!confirmFull) {
      setMessage("Tick the confirmation box before starting Deploy.");
      return;
    }
    if (fullInstallComplete) {
      setMessage(
        "Mail is already installed on this host. Do not run Deploy again. On a 2-server pair use Build HA pair.",
      );
      return;
    }
    setMessage("");

    try {
      await save({ current_step: "deploy" });
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Failed to save draft");
      return;
    }

    if (pipelineStartRef.current) {
      setMessage("A deploy or HA job is already running.");
      return;
    }
    pipelineStartRef.current = true;

    if (pipelineEsRef.current) {
      const prev = pipelineEsRef.current;
      pipelineEsRef.current = null;
      prev.close();
    }
    resetLogBuffer();
    append(
      `\n[${new Date().toISOString()}] Deploy (save settings, then install)\n` +
        `# topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );
    setLocalBusy(true);

    const onInstallFinished = (_exit?: number, reason?: FinishReason) => {
      pipelineStartRef.current = false;
      void refreshSetup();
      void hydrateFromServer().then((still) => {
        if (reason === "disconnect" || reason === "error") {
          // Keep progress UI if the server job is still running.
          setLocalBusy(still);
          return;
        }
        setLocalBusy(still);
      });
    };

    pipelineEsRef.current = openStream("apply_draft", (applyExit, reason) => {
      if (reason === "disconnect") {
        pipelineStartRef.current = false;
        void hydrateFromServer();
        void refreshSetup();
        return;
      }
      if (reason === "error") {
        pipelineStartRef.current = false;
        void hydrateFromServer();
        return;
      }
      if (applyExit !== 0) {
        pipelineStartRef.current = false;
        setLocalBusy(false);
        pipelineEsRef.current = null;
        setMessage("Could not save settings, Deploy stopped before install.");
        return;
      }
      append(`\n[${new Date().toISOString()}] Starting mail system install...\n`);
      pipelineEsRef.current = openStream("full_install", onInstallFinished);
    });
  }, [
    append,
    confirmFull,
    draft.mail_domain,
    draft.topology,
    fullInstallComplete,
    hydrateFromServer,
    openStream,
    pipelineBusy,
    refreshSetup,
    resetLogBuffer,
    save,
  ]);

  const runHaOrchestration = useCallback(async (opts?: { confirmed?: boolean }) => {
    if (pipelineStartRef.current || pipelineBusy) {
      setMessage("A deploy or HA job is already running.");
      return;
    }
    if (opts?.confirmed) setConfirmHa(true);
    if (!(opts?.confirmed || confirmHa)) {
      setMessage("Tick the confirmation box before starting HA orchestration.");
      return;
    }
    setMessage("");
    pipelineStartRef.current = true;
    if (pipelineEsRef.current) {
      const prev = pipelineEsRef.current;
      pipelineEsRef.current = null;
      prev.close();
    }
    resetLogBuffer();
    append(
      `\n[${new Date().toISOString()}] HA orchestration (Ansible sequence)\n` +
        `# topology=${draft.topology || "unset"} peer=${draft.peer_host_ip || "unset"} ` +
        `obs=${draft.observability_vm_ip || "unset"} vip=${draft.cluster_vip_ip || "unset"}\n`,
    );
    setLocalBusy(true);
    pipelineEsRef.current = openStream(
      "ha_orchestration",
      (_exit, reason) => {
        pipelineStartRef.current = false;
        void refreshSetup();
        void hydrateFromServer().then((still) => {
          if (reason === "disconnect" || reason === "error") {
            setLocalBusy(still);
            return;
          }
          setLocalBusy(false);
        });
      },
      "join_mode=apply",
    );
  }, [
    append,
    confirmHa,
    draft.observability_vm_ip,
    draft.peer_host_ip,
    draft.topology,
    hydrateFromServer,
    openStream,
    pipelineBusy,
    refreshSetup,
    resetLogBuffer,
  ]);

  const runAddHost = useCallback(
    async (opts: {
      confirmed?: boolean;
      newName: string;
      newIp: string;
      retiredName?: string;
      retiredIp?: string;
      observabilityVmIp?: string;
      clusterVipIp?: string;
      dataDisk?: string;
      metaDisk?: string;
    }) => {
      if (pipelineStartRef.current || pipelineBusy) {
        setMessage("A deploy or HA job is already running.");
        return;
      }
      if (opts?.confirmed) setConfirmHa(true);
      if (!(opts?.confirmed || confirmHa)) {
        setMessage("Tick the confirmation box before attaching the second server.");
        return;
      }
      setMessage("");
      pipelineStartRef.current = true;
      if (pipelineEsRef.current) {
        const prev = pipelineEsRef.current;
        pipelineEsRef.current = null;
        prev.close();
      }
      resetLogBuffer();
      append(
        `\n[${new Date().toISOString()}] Add host (attach blank peer to live cluster)\n` +
          `# new=${opts.newName} ip=${opts.newIp}\n`,
      );
      setLocalBusy(true);
      const qs = new URLSearchParams({
        op: "apply",
        new_name: opts.newName,
        new_ip: opts.newIp,
        retired_name: opts.retiredName || "",
        retired_ip: opts.retiredIp || "",
        observability_vm_ip: opts.observabilityVmIp || "",
        cluster_vip_ip: opts.clusterVipIp || "",
        data_disk: opts.dataDisk || "",
        meta_disk: opts.metaDisk || "",
      });
      pipelineEsRef.current = openStream(
        "add_host",
        (_exit, reason) => {
          pipelineStartRef.current = false;
          void refreshSetup();
          void hydrateFromServer().then((still) => {
            if (reason === "disconnect" || reason === "error") {
              setLocalBusy(still);
              return;
            }
            setLocalBusy(false);
          });
        },
        qs.toString(),
      );
    },
    [
      append,
      confirmHa,
      hydrateFromServer,
      openStream,
      pipelineBusy,
      refreshSetup,
      resetLogBuffer,
    ],
  );

  const runCancelDeadman = useCallback(async () => {
    if (cancelBusy) return;
    setMessage("");
    if (cancelEsRef.current) {
      cancelEsRef.current.close();
      cancelEsRef.current = null;
    }
    setCancelBusy(true);
    append(`\n[${new Date().toISOString()}] Confirm firewall access OK\n`);
    cancelEsRef.current = openStream("cancel_firewall_deadman", (_exit, reason) => {
      setCancelBusy(false);
      cancelEsRef.current = null;
      if (reason === "disconnect") {
        setMessage("Stream interrupted while confirming access, try again if needed.");
      }
    });
  }, [append, cancelBusy, openStream]);

  const value = useMemo(
    () => ({
      log,
      message,
      pipelineBusy,
      cancelBusy,
      deadmanHint,
      installProgress,
      confirmFull,
      setConfirmFull,
      confirmHa,
      setConfirmHa,
      runDeploy,
      runHaOrchestration,
      runAddHost,
      runCancelDeadman,
      openLogsTab,
    }),
    [
      log,
      message,
      pipelineBusy,
      cancelBusy,
      deadmanHint,
      installProgress,
      confirmFull,
      confirmHa,
      runDeploy,
      runHaOrchestration,
      runAddHost,
      runCancelDeadman,
      openLogsTab,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDeploySession(): DeploySessionCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useDeploySession outside DeploySessionProvider");
  return ctx;
}
