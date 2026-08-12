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

export type DeployActionId = "apply_draft" | "full_install" | "cancel_firewall_deadman";

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
  okMessage: string;
  /** True while this tab owns an SSE *or* the server still reports an active install. */
  pipelineBusy: boolean;
  cancelBusy: boolean;
  deadmanHint: boolean;
  applyDone: boolean;
  installProgress: InstallProgress;
  confirmFull: boolean;
  setConfirmFull: (v: boolean) => void;
  runApply: () => Promise<void>;
  runDeploy: () => Promise<void>;
  runCancelDeadman: () => Promise<void>;
  openLogsTab: () => void;
};

const Ctx = createContext<DeploySessionCtx | null>(null);

function summarizeApply(logChunk: string): string | null {
  if (/Apply result: created/i.test(logChunk)) {
    return "Settings saved — created the first config on this server.";
  }
  if (/Apply result: changed/i.test(logChunk)) {
    return "Settings saved — changes were applied.";
  }
  if (/Apply result: unchanged/i.test(logChunk)) {
    return "Settings already up to date — nothing changed.";
  }
  return null;
}

/** Real firewall dead-man signals — not the banner "dead-man NOT auto-cancelled". */
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

export function DeploySessionProvider({ children }: { children: ReactNode }) {
  const { draft, save } = useWizard();
  const { installInProgress, refresh: refreshSetup } = useSetup();
  const [log, setLog] = useState("# Deployment activity\n");
  const [message, setMessage] = useState("");
  const [okMessage, setOkMessage] = useState("");
  const [localBusy, setLocalBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [deadmanHint, setDeadmanHint] = useState(false);
  const [applyDone, setApplyDone] = useState(false);
  const [confirmFull, setConfirmFull] = useState(false);
  const [installProgress, setInstallProgress] = useState<InstallProgress>(emptyInstallProgress);
  const pipelineEsRef = useRef<EventSource | null>(null);
  const cancelEsRef = useRef<EventSource | null>(null);
  const logBufRef = useRef("# Deployment activity\n");
  /** True only while this tab's EventSource is healthy and delivering events. */
  const localStreamRef = useRef(false);
  /** Last action that owned the pipeline EventSource (for messaging). */
  const pipelineActionRef = useRef<DeployActionId | null>(null);

  const pipelineBusy = localBusy || installInProgress;

  useEffect(() => {
    return () => {
      pipelineEsRef.current?.close();
      pipelineEsRef.current = null;
      cancelEsRef.current?.close();
      cancelEsRef.current = null;
    };
  }, []);

  const hydrateFromServer = useCallback(async (): Promise<boolean> => {
    try {
      const [st, res] = await Promise.all([fetchSetupStatus(), fetchLastLog()]);
      const installing = Boolean(st.install_in_progress || st.busy || res.install_in_progress);
      const cleaned = formatDeployLog(res.text || "");
      if (cleaned.trim()) {
        logBufRef.current = cleaned;
        setLog(cleaned);
        setInstallProgress(parseInstallProgress(cleaned));
      }
      if (installing) {
        setLocalBusy(true);
        return true;
      }
      const p = parseInstallProgress(cleaned);
      if (p.complete || p.failed) {
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
    logBufRef.current += chunk;
    setLog(logBufRef.current);
    setInstallProgress(parseInstallProgress(logBufRef.current));
    if (looksLikeDeadmanArmed(chunk)) {
      setDeadmanHint(true);
    }
    const summary = summarizeApply(chunk);
    if (summary) {
      setOkMessage(summary);
      setApplyDone(true);
    }
  }, []);

  const attachStream = useCallback(
    (
      action: DeployActionId,
      es: EventSource,
      onFinished: (exit?: number, reason?: "done" | "error" | "disconnect") => void,
    ) => {
      es.onmessage = (ev) => {
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
            // Another job owns the install — follow server state, do not look "idle".
            setMessage("Install already running on the server — showing live progress.");
            setOkMessage("");
            onFinished(undefined, "error");
            es.close();
            void hydrateFromServer();
            void refreshSetup();
            return;
          }
          setMessage(msg);
          setOkMessage("");
          return;
        }
        if (parsed.type === "done") {
          append(`[done] exit=${parsed.exit_code ?? "?"}\n`);
          onFinished(parsed.exit_code, "done");
          es.close();
          if (action === "cancel_firewall_deadman" && parsed.exit_code === 0) {
            setDeadmanHint(false);
            setOkMessage("Firewall confirmation recorded — temporary safety timer cancelled.");
          }
        }
      };

      es.onerror = () => {
        // EventSource drops on background tabs / brief network blips. Do NOT treat as finished
        // until the server confirms the install is no longer running.
        append(`[stream] disconnected — checking server status…\n`);
        es.close();
        onFinished(undefined, "disconnect");
        void (async () => {
          await refreshSetup();
          const still = await hydrateFromServer();
          if (still) {
            setMessage("Live stream disconnected — following server progress…");
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
    ) => {
      localStreamRef.current = true;
      pipelineActionRef.current = action;
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}`,
      );
      const wrapped = (exit?: number, reason?: "done" | "error" | "disconnect") => {
        localStreamRef.current = false;
        if (pipelineEsRef.current === es) {
          pipelineEsRef.current = null;
        }
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

  const runApply = useCallback(async () => {
    if (pipelineBusy) return;
    setMessage("");
    setOkMessage("");
    try {
      await save({ current_step: "deploy" });
      append(`[info] Draft saved before apply\n`);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Failed to save draft");
      return;
    }
    append(
      `\n[${new Date().toISOString()}] Save settings\n` +
        `# topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );
    if (pipelineEsRef.current) {
      pipelineEsRef.current.close();
      pipelineEsRef.current = null;
    }
    setLocalBusy(true);
    pipelineEsRef.current = openStream("apply_draft", (exit, reason) => {
      if (reason === "disconnect") {
        // hydrateFromServer will keep busy if install somehow started; apply alone:
        void hydrateFromServer().then((still) => {
          if (!still) setLocalBusy(false);
        });
        return;
      }
      if (reason === "error") {
        // busy → follow server
        return;
      }
      setLocalBusy(false);
      if (exit === 0) setApplyDone(true);
    });
  }, [
    append,
    draft.mail_domain,
    draft.topology,
    hydrateFromServer,
    openStream,
    pipelineBusy,
    save,
  ]);

  const runDeploy = useCallback(async () => {
    if (pipelineBusy) return;
    if (!confirmFull) {
      setMessage("Tick the confirmation box before starting Deploy.");
      return;
    }
    setMessage("");
    setOkMessage("");
    setInstallProgress(emptyInstallProgress());

    try {
      await save({ current_step: "deploy" });
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Failed to save draft");
      return;
    }

    append(
      `\n[${new Date().toISOString()}] Deploy (save settings, then install)\n` +
        `# topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );

    if (pipelineEsRef.current) {
      pipelineEsRef.current.close();
      pipelineEsRef.current = null;
    }
    setLocalBusy(true);

    const onInstallFinished = (_exit?: number, reason?: "done" | "error" | "disconnect") => {
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
        void hydrateFromServer();
        void refreshSetup();
        return;
      }
      if (reason === "error") {
        void hydrateFromServer();
        return;
      }
      if (applyExit !== 0) {
        setLocalBusy(false);
        pipelineEsRef.current = null;
        setMessage("Could not save settings — Deploy stopped before install.");
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
    save,
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
        setMessage("Stream interrupted while confirming access — try again if needed.");
      }
    });
  }, [append, cancelBusy, openStream]);

  const value = useMemo(
    () => ({
      log,
      message,
      okMessage,
      pipelineBusy,
      cancelBusy,
      deadmanHint,
      applyDone,
      installProgress,
      confirmFull,
      setConfirmFull,
      runApply,
      runDeploy,
      runCancelDeadman,
      openLogsTab,
    }),
    [
      log,
      message,
      okMessage,
      pipelineBusy,
      cancelBusy,
      deadmanHint,
      applyDone,
      installProgress,
      confirmFull,
      runApply,
      runDeploy,
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
