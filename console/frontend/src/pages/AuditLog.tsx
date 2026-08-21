import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { AuditIcon, Button, Hint, LogPane, Page, PageHeader } from "../ui";

type StreamEvent = {
  type: string;
  data?: string;
  code?: string;
  message?: string;
  exit_code?: number;
};

export default function AuditLogPage() {
  const { user } = useAuth();
  const [log, setLog] = useState("# privhelper audit log\n");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const allowed = user?.role === "kin_super_admin";

  const load = useCallback(() => {
    if (busy) return;
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    setBusy(true);
    setMessage("");
    setLog("# Loading /var/log/kin-mail/privhelper.log …\n");
    const es = new EventSource("/api/wizard/deploy/stream?action=audit_log");
    esRef.current = es;
    es.onmessage = (ev) => {
      let parsed: StreamEvent;
      try {
        parsed = JSON.parse(ev.data) as StreamEvent;
      } catch {
        setLog((prev) => prev + ev.data + "\n");
        return;
      }
      if (parsed.type === "stdout" && parsed.data) {
        setLog((prev) => prev + parsed.data);
        return;
      }
      if (parsed.type === "stderr" && parsed.data) {
        setLog((prev) => prev + `[stderr] ${parsed.data}`);
        return;
      }
      if (parsed.type === "error") {
        const msg = parsed.message || parsed.code || "error";
        setLog((prev) => prev + `[error] ${parsed.code || "error"}: ${msg}\n`);
        setMessage(msg);
        setBusy(false);
        es.close();
        return;
      }
      if (parsed.type === "done") {
        setBusy(false);
        es.close();
        esRef.current = null;
      }
    };
    es.onerror = () => {
      setMessage((m) => m || "Stream closed");
      setBusy(false);
      es.close();
      esRef.current = null;
    };
  }, [busy]);

  useEffect(() => {
    if (!allowed) return;
    load();
    return () => {
      esRef.current?.close();
    };
    // initial load only
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allowed]);

  if (!allowed) {
    return <Navigate to="/wizard" replace />;
  }

  return (
    <ConsoleChrome subtitle="Audit log · KIN Super Admin">
      <Page>
        <PageHeader
          icon={<AuditIcon />}
          title="Privhelper audit log"
          subtitle={
            <>
              Read-only tail of <code>/var/log/kin-mail/privhelper.log</code>. Restricted to KIN
              Super Admin (enforced in API and privhelperd).
            </>
          }
          action={
            <Button type="button" variant="primary" loading={busy} onClick={() => load()}>
              {busy ? "Loading…" : "Refresh"}
            </Button>
          }
        />
        {message && <Hint>{message}</Hint>}
        <LogPane aria-label="Audit log">{log}</LogPane>
      </Page>
    </ConsoleChrome>
  );
}
