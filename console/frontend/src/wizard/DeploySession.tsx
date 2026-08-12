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

type DeploySessionCtx = {
  log: string;
  message: string;
  okMessage: string;
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

export function DeploySessionProvider({ children }: { children: ReactNode }) {
  const { draft, save } = useWizard();
  const [log, setLog] = useState("# Deployment activity\n");
  const [message, setMessage] = useState("");
  const [okMessage, setOkMessage] = useState("");
  const [pipelineBusy, setPipelineBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [deadmanHint, setDeadmanHint] = useState(false);
  const [applyDone, setApplyDone] = useState(false);
  const [confirmFull, setConfirmFull] = useState(false);
  const [installProgress, setInstallProgress] = useState<InstallProgress>(emptyInstallProgress);
  const pipelineEsRef = useRef<EventSource | null>(null);
  const cancelEsRef = useRef<EventSource | null>(null);
  const logBufRef = useRef("# Deployment activity\n");

  // Keep EventSource alive across Deploy ↔ Logs views; only tear down on unmount.
  useEffect(() => {
    return () => {
      pipelineEsRef.current?.close();
      pipelineEsRef.current = null;
      cancelEsRef.current?.close();
      cancelEsRef.current = null;
    };
  }, []);

  const append = useCallback((chunk: string) => {
    logBufRef.current += chunk;
    setLog(logBufRef.current);
    setInstallProgress(parseInstallProgress(logBufRef.current));
    if (/dead-man|Dead-man|DEADMAN|cancel-deadman|Leaving dead-man ARMED/i.test(chunk)) {
      setDeadmanHint(true);
    }
    const summary = summarizeApply(chunk);
    if (summary) {
      setOkMessage(summary);
      setApplyDone(true);
    }
  }, []);

  const attachStream = useCallback(
    (action: DeployActionId, es: EventSource, onFinished: (exit?: number) => void) => {
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
          setMessage(msg);
          setOkMessage("");
          if (parsed.code === "busy") {
            onFinished();
            es.close();
          }
          return;
        }
        if (parsed.type === "done") {
          append(`[done] exit=${parsed.exit_code ?? "?"}\n`);
          onFinished(parsed.exit_code);
          es.close();
          if (action === "cancel_firewall_deadman" && parsed.exit_code === 0) {
            setDeadmanHint(false);
            setOkMessage("Firewall confirmation recorded — temporary safety timer cancelled.");
          }
        }
      };

      es.onerror = () => {
        append(`[stream] connection error or closed\n`);
        setMessage((m) => m || "Stream closed");
        onFinished();
        es.close();
      };
    },
    [append],
  );

  const openStream = useCallback(
    (action: DeployActionId, onFinished: (exit?: number) => void) => {
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}`,
      );
      attachStream(action, es, onFinished);
      return es;
    },
    [attachStream],
  );

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
    setPipelineBusy(true);
    pipelineEsRef.current = openStream("apply_draft", (exit) => {
      setPipelineBusy(false);
      pipelineEsRef.current = null;
      if (exit === 0) setApplyDone(true);
    });
  }, [append, draft.mail_domain, draft.topology, openStream, pipelineBusy, save]);

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
    setPipelineBusy(true);

    pipelineEsRef.current = openStream("apply_draft", (applyExit) => {
      if (applyExit !== 0) {
        setPipelineBusy(false);
        pipelineEsRef.current = null;
        setMessage("Could not save settings — Deploy stopped before install.");
        return;
      }
      append(`\n[${new Date().toISOString()}] Starting mail system install…\n`);
      pipelineEsRef.current = openStream("full_install", () => {
        setPipelineBusy(false);
        pipelineEsRef.current = null;
      });
    });
  }, [
    append,
    confirmFull,
    draft.mail_domain,
    draft.topology,
    openStream,
    pipelineBusy,
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
    cancelEsRef.current = openStream("cancel_firewall_deadman", () => {
      setCancelBusy(false);
      cancelEsRef.current = null;
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
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDeploySession(): DeploySessionCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useDeploySession outside DeploySessionProvider");
  return ctx;
}
