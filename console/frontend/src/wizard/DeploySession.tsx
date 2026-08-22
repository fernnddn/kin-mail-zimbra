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
  | "ha_orchestration";

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

export function DeploySessionProvider({ children }: { children: ReactNode }) {
  const { draft, save } = useWizard();
  const { installInProgress, refresh: refreshSetup } = useSetup();
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

  const pipelineBusy = localBusy || installInProgress;

  useEffect(() => {
    return () => {
      pipelineEsRef.current?.close();
      pipelineEsRef.current = null;
      cancelEsRef.current?.close();
      cancelEsRef.current = null;
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
  }, [installInProgress]);

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
            ? "Following server progress (live stream disconnected)…"
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

  const append = useCallback((chunk: string) => {
    lastSseAtRef.current = Date.now();
    logBufRef.current += chunk;
    setLog(logBufRef.current);
    setInstallProgress(parseInstallProgress(logBufRef.current));
    if (looksLikeDeadmanArmed(chunk)) {
      setDeadmanHint(true);
    }
  }, []);

  const attachStream = useCallback(
    (
      action: DeployActionId,
      es: EventSource,
      onFinished: (exit?: number, reason?: "done" | "error" | "disconnect") => void,
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
          if (parsed.code === "busy") {
            // Another job owns the helper. Follow it only if it is still running;
            // a leftover busy during a just-finished fail must not look like a live install.
            onFinished(undefined, "error");
            es.close();
            void (async () => {
              await refreshSetup();
              const still = await hydrateFromServer();
              if (still) {
                setMessage("Install already running on the server, showing live progress.");
                setLocalBusy(true);
              } else {
                setMessage(
                  "Could not start because another helper job was still finishing. Confirm and click Deploy again.",
                );
                setLocalBusy(false);
              }
            })();
            return;
          }
          setMessage(msg);
          return;
        }
        if (parsed.type === "done") {
          append(`[done] exit=${parsed.exit_code ?? "?"}\n`);
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
        append(`[stream] disconnected, checking server status…\n`);
        es.close();
        onFinished(undefined, "disconnect");
        void (async () => {
          await refreshSetup();
          const still = await hydrateFromServer();
          if (still) {
            setMessage("Live stream disconnected, following server progress…");
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
    [append, hydrateFromServer, refreshSetup],
  );

  const openStream = useCallback(
    (
      action: DeployActionId,
      onFinished: (exit?: number, reason?: "done" | "error" | "disconnect") => void,
      extraQuery?: string,
    ) => {
      localStreamRef.current = true;
      lastSseAtRef.current = Date.now();
      pipelineActionRef.current = action;
      const qs = extraQuery ? `&${extraQuery}` : "";
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}${qs}`,
      );
      const wrapped = (exit?: number, reason?: "done" | "error" | "disconnect") => {
        if (pipelineEsRef.current !== es) return;
        localStreamRef.current = false;
        pipelineEsRef.current = null;
        onFinished(exit, reason);
      };
      attachStream(action, es, wrapped);
      return es;
    },
    [attachStream],
  );

  const openLogsTab = useCallback(() => {
    window.open("/wizard/deploy/logs", "_blank", "noopener,noreferrer");
  }, []);

  const resetLogBuffer = useCallback(() => {
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

    const onInstallFinished = (_exit?: number, reason?: "done" | "error" | "disconnect") => {
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
      append(`\n[${new Date().toISOString()}] Starting mail system install…\n`);
      pipelineEsRef.current = openStream("full_install", onInstallFinished);
    });
  }, [
    append,
    confirmFull,
    draft.mail_domain,
    draft.topology,
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
