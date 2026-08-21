import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../../api";
import { Button, Spinner, StatusPill } from "../../ui";
import { theme } from "../../styles/theme";
import { formatDeployLog } from "../ansi";
import { parseInstallProgress } from "../deployPipeline";

const LOG_CACHE_KEY = "kin.deployLastLog";

const Frame = styled.div`
  position: fixed;
  inset: 0;
  z-index: 40;
  display: flex;
  flex-direction: column;
  background: ${theme.bg};
  color: ${theme.ink};
  font-family: ${theme.font};
`;

const Bar = styled.header`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  height: 3.5rem;
  padding: 0 1.5rem;
  border-bottom: 1px solid color-mix(in srgb, ${theme.line} 60%, transparent);
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

const SoftHint = styled.p`
  margin: 0.5rem 1.25rem 0;
  font-size: 0.78rem;
  color: ${theme.muted};
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
  text-align: left;
  background: #0b1220;
  color: #e2e8f0;
`;

type LastLogResponse = {
  text: string;
  missing: boolean;
  install_in_progress?: boolean;
};

function readCachedLog(): string {
  try {
    return sessionStorage.getItem(LOG_CACHE_KEY) || "";
  } catch {
    return "";
  }
}

function writeCachedLog(text: string) {
  try {
    sessionStorage.setItem(LOG_CACHE_KEY, text);
  } catch {
    /* ignore quota */
  }
}

async function fetchLastLog(): Promise<LastLogResponse> {
  return api<LastLogResponse>("/api/wizard/deploy/last-log");
}

export default function DeployLogsPage() {
  const cached = readCachedLog();
  const [log, setLog] = useState(cached);
  const [live, setLive] = useState(false);
  const [softHint, setSoftHint] = useState("");
  const failStreakRef = useRef(0);
  const preRef = useRef<HTMLPreElement | null>(null);
  const stickRef = useRef(true);

  const applyText = useCallback((raw: string, installing?: boolean) => {
    const cleaned = formatDeployLog(raw);
    setLog(cleaned);
    writeCachedLog(cleaned);
    setLive(Boolean(installing));
  }, []);

  const refresh = useCallback(
    async (opts?: { manual?: boolean }) => {
      try {
        const res = await fetchLastLog();
        failStreakRef.current = 0;
        setSoftHint("");
        applyText(res.missing ? res.text || "" : res.text, res.install_in_progress);
      } catch {
        failStreakRef.current += 1;
        // One hiccup while install is healthy must not look like a hard failure.
        if (opts?.manual || failStreakRef.current >= 3) {
          setSoftHint("Could not refresh log, retrying in the background.");
        }
      }
    },
    [applyText],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const id = window.setInterval(() => {
      void refresh();
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
              ? `Failed: ${label}`
              : complete
                ? "Install finished"
                : current > 0
                  ? `Step ${current}/${total}: ${label}`
                  : live
                    ? "Install in progress"
                    : log
                      ? "Last run transcript"
                      : "Waiting for deploy output…"}
          </p>
        </TitleBlock>
        <Meta>
          {live ? (
            <StatusPill tone="warn">
              <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
                <Spinner /> Live
              </span>
            </StatusPill>
          ) : complete ? (
            <StatusPill tone="completed">Complete</StatusPill>
          ) : failed ? (
            <StatusPill tone="critical">Failed</StatusPill>
          ) : (
            <StatusPill tone="neutral">Idle</StatusPill>
          )}
          <Button type="button" variant="secondary" onClick={() => void refresh({ manual: true })}>
            Reload
          </Button>
          <Button type="button" variant="ghost" onClick={() => window.close()}>
            Close tab
          </Button>
        </Meta>
      </Bar>
      {softHint && <SoftHint>{softHint}</SoftHint>}
      <Log
        ref={preRef}
        aria-label="Deployment log"
        onScroll={(e) => {
          const el = e.currentTarget;
          stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
        }}
      >
        {log || " "}
      </Log>
    </Frame>
  );
}
