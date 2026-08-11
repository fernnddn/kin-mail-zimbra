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

type ActionId = "apply_draft" | "run_hardening" | "hardening_status";

const ACTIONS: { id: ActionId; label: string; cmd: string }[] = [
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
];

export default function DeployStep() {
  const { draft, save } = useWizard();
  const navigate = useNavigate();
  const [log, setLog] = useState(
    "# Privileged helper log viewer\n# Pick an action below (whitelist only)\n",
  );
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  function append(chunk: string) {
    setLog((prev) => prev + chunk);
  }

  async function start(action: ActionId) {
    if (busy) return;
    const meta = ACTIONS.find((a) => a.id === action);
    setBusy(true);
    setMessage("");

    // Persist latest form state before apply_draft so disk draft matches UI.
    if (action === "apply_draft") {
      try {
        await save({ current_step: "deploy" });
        append(`[info] Draft saved before apply\n`);
      } catch (err) {
        setMessage(err instanceof Error ? err.message : "Failed to save draft");
        setBusy(false);
        return;
      }
    }

    append(
      `\n[${new Date().toISOString()}] → ${meta?.cmd || action}\n` +
        `# Draft topology=${draft.topology || "unset"} domain=${draft.mail_domain || "unset"}\n`,
    );

    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }

    const es = new EventSource(
      `/api/wizard/deploy/stream?action=${encodeURIComponent(action)}`,
    );
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
        Whitelisted privileged actions only. Apply draft writes{" "}
        <code>/etc/kin-mail/config</code> (with backup) — it does not run install stages.
        Full hardening streams <code>09-hardening.sh</code> (idempotent Part A).
      </Lede>
      <WarnBox>
        <strong>Still out of scope:</strong> firewall apply, full install 01–11, Ansible, and
        dedicated restart commands for Zimbra/Pacemaker/DRBD.
      </WarnBox>
      <LogPane aria-label="Deployment log">{log}</LogPane>
      {message && <Hint>{message}</Hint>}
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/review")}>
          Back
        </Button>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.55rem", justifyContent: "flex-end" }}>
          {ACTIONS.map((a) => (
            <Button
              key={a.id}
              type="button"
              variant={a.id === "run_hardening" ? "primary" : "ghost"}
              disabled={busy}
              onClick={() => void start(a.id)}
            >
              {busy ? "Running…" : a.label}
            </Button>
          ))}
        </div>
      </NavRow>
    </>
  );
}
