import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Hint, Lede, LogPane, NavRow, Title, WarnBox } from "../../ui";
import { useWizard } from "../WizardContext";

type StreamEvent = {
  type: string;
  cmd?: string;
  data?: string;
  code?: string;
  message?: string;
  exit_code?: number;
};

const DEPLOY_CMD = "run_script:09-hardening.sh --status";

export default function DeployStep() {
  const { draft } = useWizard();
  const navigate = useNavigate();
  const [log, setLog] = useState(
    `# Privileged helper log viewer\n# Start deployment runs: ${DEPLOY_CMD}\n`,
  );
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  function append(chunk: string) {
    setLog((prev) => prev + chunk);
  }

  function start() {
    if (busy) return;
    setBusy(true);
    setMessage("");
    append(
      `\n[${new Date().toISOString()}] Start deployment → privhelper ${DEPLOY_CMD}\n` +
        `# Draft topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );

    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    const es = new EventSource("/api/wizard/deploy/stream");
    esRef.current = es;

    es.onmessage = (ev) => {
      let parsed: StreamEvent;
      try {
        parsed = JSON.parse(ev.data) as StreamEvent;
      } catch {
        append(ev.data + "\n");
        return;
      }
      if (parsed.type === "meta") {
        append(`[meta] cmd=${parsed.cmd || "?"}\n`);
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
          setBusy(false);
          es.close();
          esRef.current = null;
        }
        return;
      }
      if (parsed.type === "done") {
        append(`[done] exit_code=${parsed.exit_code ?? "?"}\n`);
        setBusy(false);
        es.close();
        esRef.current = null;
      }
    };

    es.onerror = () => {
      append(`[stream] connection error or closed\n`);
      setMessage((m) => m || "Stream closed");
      setBusy(false);
      es.close();
      esRef.current = null;
    };
  }

  return (
    <>
      <Title>Perform deployment</Title>
      <Lede>
        Start deployment runs the read-only whitelist command{" "}
        <code>{DEPLOY_CMD}</code> via <code>kin-mail-privhelperd</code>, streaming subprocess
        output live into this log viewer.
      </Lede>
      <WarnBox>
        <strong>Plumbing only.</strong> This uses <code>09-hardening.sh --status</code> (STATUS_ONLY)
        — no firewall apply, no install stages that mutate config.
      </WarnBox>
      <LogPane aria-label="Deployment log">{log}</LogPane>
      {message && <Hint>{message}</Hint>}
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
          Back
        </Button>
        <Button type="button" disabled={busy} onClick={() => start()}>
          {busy ? "Running…" : "Start deployment"}
        </Button>
      </NavRow>
    </>
  );
}
