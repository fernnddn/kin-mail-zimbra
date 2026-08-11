import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Hint, Lede, LogPane, NavRow, Title, WarnBox } from "../../ui";
import { useWizard } from "../WizardContext";

type StreamEvent = {
  type: string;
  cmd?: string;
  action?: string;
  data?: string;
  code?: string;
  message?: string;
  exit_code?: number;
};

type ActionId =
  | "apply_draft"
  | "run_hardening"
  | "hardening_status"
  | "full_install"
  | "cancel_firewall_deadman";

const ACTIONS: { id: ActionId; label: string; cmd: string; danger?: boolean }[] = [
  {
    id: "apply_draft",
    label: "Apply wizard draft",
    cmd: "apply_wizard_draft",
  },
  {
    id: "run_hardening",
    label: "Run hardening (full)",
    cmd: "run_hardening",
  },
  {
    id: "hardening_status",
    label: "Hardening status",
    cmd: "run_script:09-hardening.sh --status",
  },
  {
    id: "full_install",
    label: "Full install pipeline",
    cmd: "run_full_install",
    danger: true,
  },
  {
    id: "cancel_firewall_deadman",
    label: "Confirm firewall OK — cancel dead-man",
    cmd: "cancel_firewall_deadman",
    danger: true,
  },
];

export default function DeployStep() {
  const { draft, save } = useWizard();
  const navigate = useNavigate();
  const [log, setLog] = useState(
    "# Privileged helper log viewer\n# Pick an action below (whitelist only)\n",
  );
  const [message, setMessage] = useState("");
  const [pipelineBusy, setPipelineBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [deadmanHint, setDeadmanHint] = useState(false);
  const [confirmFull, setConfirmFull] = useState(false);
  const pipelineEsRef = useRef<EventSource | null>(null);
  const cancelEsRef = useRef<EventSource | null>(null);

  function append(chunk: string) {
    setLog((prev) => prev + chunk);
    if (/dead-man|Dead-man|DEADMAN|cancel-deadman|Leaving dead-man ARMED/i.test(chunk)) {
      setDeadmanHint(true);
    }
  }

  function attachStream(
    action: ActionId,
    es: EventSource,
    onFinished: () => void,
  ) {
    es.onmessage = (ev) => {
      let parsed: StreamEvent;
      try {
        parsed = JSON.parse(ev.data) as StreamEvent;
      } catch {
        append(ev.data + "\n");
        return;
      }
      if (parsed.type === "meta") {
        append(`[meta] action=${parsed.action || "?"} cmd=${parsed.cmd || "?"}\n`);
        return;
      }
      if (parsed.type === "accepted") {
        append(`[accepted] ${parsed.cmd || ""}\n`);
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
        if (parsed.code === "busy") {
          onFinished();
          es.close();
        }
        return;
      }
      if (parsed.type === "done") {
        append(`[done] exit_code=${parsed.exit_code ?? "?"}\n`);
        onFinished();
        es.close();
        if (action === "cancel_firewall_deadman" && parsed.exit_code === 0) {
          setDeadmanHint(false);
        }
      }
    };

    es.onerror = () => {
      append(`[stream] connection error or closed\n`);
      setMessage((m) => m || "Stream closed");
      onFinished();
      es.close();
    };
  }

  async function start(action: ActionId) {
    const isCancel = action === "cancel_firewall_deadman";

    // Dead-man cancel must work while full install is still streaming (daemon bypasses busy).
    if (isCancel) {
      if (cancelBusy) return;
    } else if (pipelineBusy) {
      return;
    }

    if (action === "full_install" && !confirmFull) {
      setMessage("Tick the confirmation box before starting full install.");
      return;
    }
    const meta = ACTIONS.find((a) => a.id === action);
    setMessage("");

    if (action === "apply_draft") {
      try {
        await save({ current_step: "deploy" });
        append(`[info] Draft saved before apply\n`);
      } catch (err) {
        setMessage(err instanceof Error ? err.message : "Failed to save draft");
        return;
      }
    }

    append(
      `\n[${new Date().toISOString()}] → ${meta?.cmd || action}\n` +
        `# Draft topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );
    if (action === "full_install") {
      append(
        `# KIN_CONSOLE_CONFIRMED=1 — TTY prompts skipped; ufw dead-man still armed on stage 10.\n` +
          `# Pipeline pauses until you cancel dead-man after verify (button stays enabled).\n`,
      );
    }

    if (isCancel) {
      if (cancelEsRef.current) {
        cancelEsRef.current.close();
        cancelEsRef.current = null;
      }
      setCancelBusy(true);
      const es = new EventSource(
        `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}`,
      );
      cancelEsRef.current = es;
      attachStream(action, es, () => {
        setCancelBusy(false);
        cancelEsRef.current = null;
      });
      return;
    }

    if (pipelineEsRef.current) {
      pipelineEsRef.current.close();
      pipelineEsRef.current = null;
    }
    setPipelineBusy(true);
    const es = new EventSource(
      `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}`,
    );
    pipelineEsRef.current = es;
    attachStream(action, es, () => {
      setPipelineBusy(false);
      pipelineEsRef.current = null;
    });
  }

  return (
    <>
      <Title>Perform deployment</Title>
      <Lede>
        Whitelisted privileged actions only. Full install streams{" "}
        <code>kin-mail.sh --full-install</code> with <code>KIN_CONSOLE_CONFIRMED=1</code> (replaces
        TTY y/n). The ufw dead-man switch is independent and is never auto-cancelled.
      </Lede>
      <WarnBox>
        <strong>Full install / firewall:</strong> stage 10 re-applies ufw and arms a ~300s dead-man.
        The pipeline <em>pauses</em> until you verify SSH/cluster/mail and click{" "}
        <em>Confirm firewall OK — cancel dead-man</em>. That cancel is never automatic.
      </WarnBox>
      {deadmanHint && (
        <WarnBox>
          <strong>Dead-man may be armed.</strong> After verification, cancel it explicitly — ufw
          will auto-disable when the timer expires if you do not. The cancel button stays available
          while full install is running.
        </WarnBox>
      )}
      <LogPane aria-label="Deployment log">{log}</LogPane>
      {message && <Hint>{message}</Hint>}
      <label
        style={{
          display: "flex",
          gap: "0.55rem",
          alignItems: "flex-start",
          margin: "0.75rem 0 0.35rem",
          fontSize: "0.88rem",
          lineHeight: 1.4,
          cursor: "pointer",
        }}
      >
        <input
          type="checkbox"
          checked={confirmFull}
          onChange={(e) => setConfirmFull(e.target.checked)}
          style={{ marginTop: "0.2rem" }}
        />
        <span>
          I confirm running the full install pipeline on this host (console confirm replaces CLI
          y/n; dead-man on firewall still requires a separate cancel after verify).
        </span>
      </label>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
          Back
        </Button>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.55rem", justifyContent: "flex-end" }}>
          {ACTIONS.map((a) => {
            const isCancel = a.id === "cancel_firewall_deadman";
            const disabled = isCancel
              ? cancelBusy
              : pipelineBusy || (a.id === "full_install" && !confirmFull);
            let label = a.label;
            if (isCancel && cancelBusy) label = "Cancelling…";
            else if (!isCancel && pipelineBusy) label = "Running…";
            return (
              <Button
                key={a.id}
                type="button"
                variant={a.danger ? "danger" : a.id === "run_hardening" ? "primary" : "ghost"}
                disabled={disabled}
                onClick={() => void start(a.id)}
              >
                {label}
              </Button>
            );
          })}
        </div>
      </NavRow>
    </>
  );
}
