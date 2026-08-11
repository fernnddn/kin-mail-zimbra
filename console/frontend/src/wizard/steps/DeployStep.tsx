import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api";
import { Button, Hint, Lede, LogPane, NavRow, Title, WarnBox } from "../../ui";
import { useWizard } from "../WizardContext";

export default function DeployStep() {
  const { draft } = useWizard();
  const navigate = useNavigate();
  const [log, setLog] = useState(
    "# Deployment log viewer (stub)\n# Waiting for operator to start…\n",
  );
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    setMessage("");
    try {
      const res = await api<{ status: string; message: string }>("/api/wizard/deploy", {
        method: "POST",
        body: "{}",
      });
      setMessage(res.message);
      setLog(
        (prev) =>
          prev +
          `\n[${new Date().toISOString()}] Start deployment requested\n` +
          `[${new Date().toISOString()}] ${res.message}\n` +
          `# Draft topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n` +
          `# No installer scripts were executed.\n`,
      );
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Title>Perform deployment</Title>
      <Lede>
        Ready to deploy — this page is a UI shell only for slice 9.2. The execution engine that
        would invoke install stages is intentionally not connected yet.
      </Lede>
      <WarnBox>
        <strong>Stub.</strong> Clicking Start deployment will not run install/01–11, Ansible, or
        any privileged helper. It only returns a not-connected message.
      </WarnBox>
      <LogPane aria-label="Deployment log">{log}</LogPane>
      {message && <Hint>{message}</Hint>}
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
          Back
        </Button>
        <Button type="button" disabled={busy} onClick={() => void start()}>
          {busy ? "Starting…" : "Start deployment"}
        </Button>
      </NavRow>
    </>
  );
}
