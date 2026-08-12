import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { Button, Spinner } from "../../ui";
import { theme } from "../../styles/theme";
import { parseInstallProgress } from "../deployPipeline";

const Frame = styled.div`
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  background: ${theme.bg};
  color: ${theme.ink};
  font-family: ${theme.font};
  animation: kin-fade-in ${theme.motion} ease-out;
`;

const Bar = styled.header`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: 0.85rem 1.25rem;
  border-bottom: 1px solid ${theme.line};
  background: ${theme.bgElev};
  flex-shrink: 0;
`;

const TitleBlock = styled.div`
  min-width: 0;

  h1 {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 650;
    letter-spacing: -0.02em;
  }

  p {
    margin: 0.15rem 0 0;
    font-size: 0.8rem;
    color: ${theme.muted};
  }
`;

const Meta = styled.div`
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-shrink: 0;
`;

const Badge = styled.span<{ $tone?: "ok" | "warn" | "muted" }>`
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.25rem 0.55rem;
  border-radius: 999px;
  border: 1px solid ${theme.line};
  color: ${(p) =>
    p.$tone === "ok" ? theme.ok : p.$tone === "warn" ? theme.warn : theme.muted};
  background: ${(p) =>
    p.$tone === "ok"
      ? "rgba(5, 150, 105, 0.08)"
      : p.$tone === "warn"
        ? theme.warnSoft
        : theme.bgPanel};
`;

const Log = styled.pre`
  flex: 1;
  margin: 0;
  padding: 1rem 1.25rem 1.5rem;
  overflow: auto;
  font-family: ${theme.mono};
  font-size: 0.8rem;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
  background: #0b1220;
  color: #e2e8f0;
`;

type StreamEvent = {
  type: string;
  data?: string;
  message?: string;
  code?: string;
  exit_code?: number;
};

function loadDeployLogOnce(): Promise<string> {
  return new Promise((resolve, reject) => {
    const es = new EventSource("/api/wizard/deploy/stream?action=deploy_log");
    let buf = "";
    const timer = window.setTimeout(() => {
      es.close();
      reject(new Error("Timed out loading deploy log"));
    }, 60_000);

    es.onmessage = (ev) => {
      let parsed: StreamEvent;
      try {
        parsed = JSON.parse(ev.data) as StreamEvent;
      } catch {
        buf += ev.data + "\n";
        return;
      }
      if (parsed.type === "stdout" && parsed.data) {
        buf += parsed.data;
        return;
      }
      if (parsed.type === "stderr" && parsed.data) {
        buf += `[stderr] ${parsed.data}`;
        return;
      }
      if (parsed.type === "error") {
        window.clearTimeout(timer);
        es.close();
        reject(new Error(parsed.message || parsed.code || "error"));
        return;
      }
      if (parsed.type === "done") {
        window.clearTimeout(timer);
        es.close();
        resolve(buf);
      }
    };
    es.onerror = () => {
      window.clearTimeout(timer);
      es.close();
      if (buf.trim()) resolve(buf);
      else reject(new Error("Failed to load deploy log"));
    };
  });
}

export default function DeployLogsPage() {
  const [log, setLog] = useState("Loading last deploy log…\n");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [live, setLive] = useState(false);
  const preRef = useRef<HTMLPreElement | null>(null);
  const stickRef = useRef(true);

  const refresh = useCallback(async (opts?: { quiet?: boolean }) => {
    if (!opts?.quiet) setLoading(true);
    setError("");
    try {
      const text = await loadDeployLogOnce();
      const cleaned = text.replace(/^=== last deploy log:.*===\n/, "");
      setLog(cleaned.trim() ? cleaned : "(no deploy log yet)\n");
      const progress = parseInstallProgress(cleaned);
      setLive(!progress.complete && !progress.failed && /Running /i.test(cleaned));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load log");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll so a second tab keeps up while install streams on the server.
  useEffect(() => {
    const id = window.setInterval(() => {
      void refresh({ quiet: true });
    }, 2500);
    return () => window.clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    const el = preRef.current;
    if (!el || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [log]);

  const progress = parseInstallProgress(log);
  const { current, total, label, complete, failed } = progress;

  return (
    <Frame>
      <Bar>
        <TitleBlock>
          <h1>Deployment logs</h1>
          <p>
            {failed
              ? `Failed — ${label}`
              : complete
                ? "Install finished"
                : current > 0
                  ? `Step ${current}/${total} — ${label}`
                  : loading
                    ? "Loading…"
                    : "Last run transcript (server-persisted)"}
          </p>
        </TitleBlock>
        <Meta>
          {loading ? (
            <Badge $tone="muted">
              <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
                <Spinner /> Loading
              </span>
            </Badge>
          ) : live ? (
            <Badge $tone="warn">
              <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
                <Spinner /> Refreshing
              </span>
            </Badge>
          ) : complete ? (
            <Badge $tone="ok">Complete</Badge>
          ) : failed ? (
            <Badge $tone="warn">Failed</Badge>
          ) : (
            <Badge $tone="muted">Idle</Badge>
          )}
          <Button type="button" variant="ghost" onClick={() => void refresh()}>
            Reload
          </Button>
          <Button type="button" variant="ghost" onClick={() => window.close()}>
            Close tab
          </Button>
        </Meta>
      </Bar>
      {error && (
        <p style={{ margin: "0.75rem 1.25rem", color: theme.danger, fontSize: "0.85rem" }}>
          {error}
        </p>
      )}
      <Log
        ref={preRef}
        aria-label="Deployment log"
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        {log}
      </Log>
    </Frame>
  );
}
