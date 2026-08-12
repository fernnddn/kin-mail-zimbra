import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { Button, Spinner } from "../../ui";
import { theme } from "../../styles/theme";
import { useDeploySession } from "../DeploySession";

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

export default function DeployLogsPage() {
  const navigate = useNavigate();
  const { log, pipelineBusy, cancelBusy, installProgress } = useDeploySession();
  const preRef = useRef<HTMLPreElement | null>(null);
  const streaming = pipelineBusy || cancelBusy;

  useEffect(() => {
    const el = preRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [log]);

  const { current, total, label, complete, failed } = installProgress;

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
                  : streaming
                    ? "Streaming…"
                    : "Idle — start Deploy from the previous screen"}
          </p>
        </TitleBlock>
        <Meta>
          {streaming ? (
            <Badge $tone="warn">
              <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem" }}>
                <Spinner /> Live
              </span>
            </Badge>
          ) : complete ? (
            <Badge $tone="ok">Complete</Badge>
          ) : failed ? (
            <Badge $tone="warn">Failed</Badge>
          ) : (
            <Badge $tone="muted">Idle</Badge>
          )}
          <Button type="button" variant="ghost" onClick={() => navigate("/wizard/deploy")}>
            Back to progress
          </Button>
        </Meta>
      </Bar>
      <Log ref={preRef} aria-label="Deployment log stream">
        {log}
      </Log>
    </Frame>
  );
}
