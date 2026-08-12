import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { isOpsRole, useAuth } from "../../auth";
import { useSetup } from "../../setup";
import {
  Button,
  Hint,
  Lede,
  LogPane,
  NavRow,
  OkMsg,
  Spinner,
  Title,
  WarnBox,
} from "../../ui";
import { useWizard } from "../WizardContext";
import styled from "@emotion/styled";
import { theme } from "../../styles/theme";

type StreamEvent = {
  type: string;
  cmd?: string;
  action?: string;
  data?: string;
  code?: string;
  message?: string;
  exit_code?: number;
};

type ActionId = "apply_draft" | "full_install" | "cancel_firewall_deadman";

const StepCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius};
  padding: 1rem 1.05rem;
  margin-bottom: 0.85rem;
`;

const StepHeading = styled.p`
  margin: 0 0 0.35rem;
  font-weight: 650;
  font-size: 0.95rem;
`;

const StepBody = styled.p`
  margin: 0 0 0.85rem;
  color: ${theme.muted};
  font-size: 0.86rem;
  line-height: 1.45;
`;

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

export default function DeployStep() {
  const { draft, save } = useWizard();
  const { user } = useAuth();
  const { deployed } = useSetup();
  const navigate = useNavigate();
  // Pre-deploy anonymous setup acts as ops; after deploy require an ops role.
  const canOps = !deployed || isOpsRole(user?.role);
  const [log, setLog] = useState("# Deployment activity\n");
  const [message, setMessage] = useState("");
  const [okMessage, setOkMessage] = useState("");
  const [pipelineBusy, setPipelineBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [deadmanHint, setDeadmanHint] = useState(false);
  const [confirmFull, setConfirmFull] = useState(false);
  const [applyDone, setApplyDone] = useState(false);
  const pipelineEsRef = useRef<EventSource | null>(null);
  const cancelEsRef = useRef<EventSource | null>(null);
  const logBufRef = useRef("");

  function append(chunk: string) {
    logBufRef.current += chunk;
    setLog((prev) => prev + chunk);
    if (/dead-man|Dead-man|DEADMAN|cancel-deadman|Leaving dead-man ARMED/i.test(chunk)) {
      setDeadmanHint(true);
    }
    const summary = summarizeApply(chunk);
    if (summary) {
      setOkMessage(summary);
      setApplyDone(true);
    }
  }

  function attachStream(action: ActionId, es: EventSource, onFinished: (exit?: number) => void) {
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
  }

  function openStream(action: ActionId, onFinished: (exit?: number) => void) {
    const es = new EventSource(
      `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}`,
    );
    attachStream(action, es, onFinished);
    return es;
  }

  async function runApply() {
    if (!canOps || pipelineBusy) return;
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
      `\n[${new Date().toISOString()}] Step 1 — save settings\n` +
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
  }

  async function runDeploy() {
    if (!canOps || pipelineBusy) return;
    if (!confirmFull) {
      setMessage("Tick the confirmation box before starting Deploy.");
      return;
    }
    setMessage("");
    setOkMessage("");

    // Always save + apply settings first so Deploy is one guided flow.
    try {
      await save({ current_step: "deploy" });
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Failed to save draft");
      return;
    }

    append(
      `\n[${new Date().toISOString()}] Step 2 — Deploy (save settings, then install)\n` +
        `# topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );

    if (pipelineEsRef.current) {
      pipelineEsRef.current.close();
      pipelineEsRef.current = null;
    }
    setPipelineBusy(true);

    // Apply first, then full install sequentially via two streams.
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
  }

  async function runCancelDeadman() {
    if (!canOps || cancelBusy) return;
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
  }

  return (
    <>
      <Title>Deploy</Title>
      <Lede>
        Guided setup for this server. Settings are saved first, then the mail system is installed.
        {!canOps && (
          <>
            {" "}
            Your role (<strong>{user?.role_label || "Customer Admin"}</strong>) cannot run deploy —
            ask a KIN Super Admin or Support-Ops.
          </>
        )}
      </Lede>

      {canOps && (
        <>
          <StepCard>
            <StepHeading>1. Save your wizard settings</StepHeading>
            <StepBody>
              Writes the choices from this wizard to the server. Safe to run more than once — you
              will see whether anything actually changed.
            </StepBody>
            <Button type="button" variant="ghost" disabled={pipelineBusy} onClick={() => void runApply()}>
              {pipelineBusy ? (
                <>
                  <Spinner /> Saving…
                </>
              ) : (
                "Save settings"
              )}
            </Button>
            {okMessage && <OkMsg style={{ marginTop: "0.75rem", marginBottom: 0 }}>{okMessage}</OkMsg>}
          </StepCard>

          <StepCard>
            <StepHeading>2. Deploy the mail system</StepHeading>
            <StepBody>
              Saves settings again if needed, then installs and configures mail on this host. This
              can take a long time — watch the log below.
            </StepBody>
            <label
              style={{
                display: "flex",
                gap: "0.55rem",
                alignItems: "flex-start",
                margin: "0 0 0.75rem",
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
              <span>I confirm Deploy should run on this server now.</span>
            </label>
            <Button
              type="button"
              disabled={pipelineBusy || !confirmFull}
              onClick={() => void runDeploy()}
            >
              {pipelineBusy ? (
                <>
                  <Spinner /> Working…
                </>
              ) : (
                "Deploy"
              )}
            </Button>
            {!applyDone && (
              <Hint style={{ marginTop: "0.75rem", marginBottom: 0 }}>
                Tip: you can Save settings alone first, or just press Deploy (it saves automatically).
              </Hint>
            )}
          </StepCard>

          {(deadmanHint || cancelBusy) && (
            <StepCard>
              <StepHeading>3. Confirm admin access still works</StepHeading>
              <StepBody>
                After the firewall step runs, verify you can still reach this console and SSH, then
                confirm below so the temporary safety timer is cancelled.
              </StepBody>
              <WarnBox style={{ marginBottom: "0.75rem" }}>
                <strong>Safety timer may be active.</strong> Confirm only after you have checked
                access.
              </WarnBox>
              <Button
                type="button"
                variant="danger"
                disabled={cancelBusy}
                onClick={() => void runCancelDeadman()}
              >
                {cancelBusy ? (
                  <>
                    <Spinner /> Confirming…
                  </>
                ) : (
                  "Confirm access is OK"
                )}
              </Button>
            </StepCard>
          )}
        </>
      )}

      <LogPane aria-label="Deployment log">{log}</LogPane>
      {message && <Hint>{message}</Hint>}

      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
          Back
        </Button>
        <span />
      </NavRow>
    </>
  );
}
