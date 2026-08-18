import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { ConsoleChrome } from "../ConsoleChrome";
import { isOpsRole, useAuth } from "../auth";
import { api } from "../api";
import { theme } from "../styles/theme";
import { Button, Hint, Lede, LogPane, Title, WarnBox } from "../ui";

type ClusterSnap = {
  local_host?: string;
  nodes?: string[];
  standby?: string[];
  promoted?: string | null;
  unpromoted?: string[];
  drbd_uptodate?: boolean;
  qdevice_ok?: boolean;
  failcount_ok?: boolean;
  maintenance_active?: boolean;
};

type StatusResp = {
  ok: boolean;
  log: string;
  cluster: ClusterSnap;
  error?: string | null;
};

type PreflightCheck = { ok: boolean; detail: string };

const Page = styled.div`
  padding: 1.25rem 1.5rem 2rem;
  max-width: 960px;
  width: 100%;
`;

const Grid = styled.div`
  display: grid;
  gap: 0.85rem;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  margin: 1rem 0 1.25rem;
`;

const NodeCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius};
  padding: 1rem 1.05rem;
`;

const NodeName = styled.p`
  margin: 0 0 0.35rem;
  font-weight: 650;
  font-size: 1rem;
`;

const Meta = styled.p`
  margin: 0 0 0.85rem;
  color: ${theme.muted};
  font-size: 0.86rem;
  line-height: 1.4;
`;

const Badge = styled.span<{ $ok?: boolean }>`
  display: inline-block;
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  margin-right: 0.35rem;
  color: ${(p) => (p.$ok ? theme.ok : theme.warn)};
  background: ${(p) => (p.$ok ? "rgba(5, 150, 105, 0.08)" : theme.warnSoft)};
  border: 1px solid ${theme.line};
`;

const CheckList = styled.ul`
  margin: 0 0 1rem;
  padding: 0;
  list-style: none;
  font-size: 0.86rem;
`;

const CheckItem = styled.li<{ $ok: boolean }>`
  margin: 0.25rem 0;
  color: ${(p) => (p.$ok ? theme.ink : theme.danger)};
  &::before {
    content: ${(p) => (p.$ok ? '"✓ "' : '"✗ "')};
  }
`;

function parseTaggedJson(log: string, prefix: string): Record<string, unknown> | null {
  for (const line of log.split("\n")) {
    if (line.startsWith(prefix)) {
      try {
        const data = JSON.parse(line.slice(prefix.length)) as unknown;
        if (data && typeof data === "object") return data as Record<string, unknown>;
      } catch {
        /* ignore */
      }
    }
  }
  return null;
}

export default function ClusterPage() {
  const { user } = useAuth();
  const ops = isOpsRole(user?.role);
  const [cluster, setCluster] = useState<ClusterSnap>({});
  const [log, setLog] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [preflight, setPreflight] = useState<Record<string, PreflightCheck> | null>(null);
  const [preflightTarget, setPreflightTarget] = useState("");
  const esRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    const st = await api<StatusResp>("/api/cluster/status");
    setCluster(st.cluster || {});
    if (!esRef.current) setLog(st.log || "");
  }, []);

  useEffect(() => {
    void refresh().catch((err: unknown) => {
      setMessage(err instanceof Error ? err.message : "Failed to load cluster status");
    });
    return () => {
      esRef.current?.close();
      esRef.current = null;
    };
  }, [refresh]);

  function runStream(op: "preflight" | "enter" | "exit", target: string) {
    esRef.current?.close();
    setBusy(true);
    setMessage("");
    setLog("");
    if (op === "preflight") {
      setPreflight(null);
      setPreflightTarget(target);
    }
    const qs = new URLSearchParams({
      action: "maintenance",
      op,
      target,
    });
    const es = new EventSource(`/api/wizard/deploy/stream?${qs.toString()}`);
    esRef.current = es;
    let buf = "";
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as { type?: string; data?: string; exit_code?: number; message?: string };
        if (data.type === "stdout" && data.data) {
          buf += data.data;
          setLog(buf);
        } else if (data.type === "stderr" && data.data) {
          buf += data.data;
          setLog(buf);
        } else if (data.type === "error") {
          buf += `[error] ${data.message || ""}\n`;
          setLog(buf);
        } else if (data.type === "done") {
          es.close();
          esRef.current = null;
          setBusy(false);
          const pf = parseTaggedJson(buf, "PREFLIGHT_JSON:");
          if (pf) {
            const checks: Record<string, PreflightCheck> = {};
            for (const [k, v] of Object.entries(pf)) {
              if (k.startsWith("_")) continue;
              if (v && typeof v === "object" && "ok" in v) {
                checks[k] = v as PreflightCheck;
              }
            }
            setPreflight(checks);
            setPreflightTarget(target);
          }
          const code = data.exit_code ?? 1;
          if (op === "preflight") {
            setMessage(code === 0 ? `Pre-flight passed for ${target}.` : `Pre-flight failed for ${target}.`);
          } else if (op === "enter") {
            setMessage(code === 0 ? `${target} is in maintenance.` : `Enter maintenance failed for ${target}.`);
          } else {
            setMessage(code === 0 ? `${target} left maintenance.` : `Exit maintenance not fully verified for ${target}.`);
          }
          void refresh();
        }
      } catch {
        /* ignore malformed SSE */
      }
    };
    es.onerror = () => {
      if (esRef.current === es) {
        es.close();
        esRef.current = null;
        setBusy(false);
        setMessage("Lost connection to the maintenance stream.");
      }
    };
  }

  const nodes = cluster.nodes || [];
  const standby = new Set(cluster.standby || []);

  return (
    <ConsoleChrome subtitle="Cluster">
      <Page>
        <Title>Cluster</Title>
        <Lede>
          Take one mail node out of service for planned work. Pre-flight must pass before Enter
          is allowed. Closing this browser does not take the node out of maintenance; use Exit.
        </Lede>
        {cluster.maintenance_active ? (
          <WarnBox>
            A node is in maintenance ({(cluster.standby || []).join(", ") || "unknown"}). Deploy
            and mailbox create stay blocked until you exit.
          </WarnBox>
        ) : null}
        <Meta as="div" style={{ marginBottom: "0.5rem" }}>
          <Badge $ok={!!cluster.drbd_uptodate}>DRBD {cluster.drbd_uptodate ? "UpToDate" : "not UpToDate"}</Badge>
          <Badge $ok={!!cluster.qdevice_ok}>qdevice {cluster.qdevice_ok ? "voting" : "unhealthy"}</Badge>
          <Badge $ok={!!cluster.failcount_ok}>fail-count {cluster.failcount_ok ? "0" : "nonzero"}</Badge>
          <Badge $ok={!cluster.maintenance_active}>
            {cluster.promoted ? `Promoted: ${cluster.promoted}` : "Promoted: unknown"}
          </Badge>
        </Meta>
        <Grid>
          {nodes.map((node) => {
            const isStandby = standby.has(node);
            const isPromoted = cluster.promoted === node;
            const pfForThis = preflightTarget === node ? preflight : null;
            const pfFailed = pfForThis
              ? Object.values(pfForThis).some((c) => !c.ok)
              : false;
            const pfPassed = !!pfForThis && !pfFailed;
            return (
              <NodeCard key={node}>
                <NodeName>{node}</NodeName>
                <Meta>
                  {isPromoted ? "Promoted (serving mail)" : "Unpromoted"}
                  {isStandby ? " · in maintenance" : ""}
                </Meta>
                {pfForThis ? (
                  <CheckList>
                    {Object.entries(pfForThis).map(([name, check]) => (
                      <CheckItem key={name} $ok={check.ok}>
                        {name}: {check.detail}
                      </CheckItem>
                    ))}
                  </CheckList>
                ) : null}
                {ops ? (
                  <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                    <Button
                      type="button"
                      variant="ghost"
                      disabled={busy}
                      onClick={() => runStream("preflight", node)}
                    >
                      Check
                    </Button>
                    {isStandby ? (
                      <Button type="button" disabled={busy} onClick={() => runStream("exit", node)}>
                        Exit Maintenance
                      </Button>
                    ) : (
                      <Button
                        type="button"
                        disabled={
                          busy ||
                          !pfPassed ||
                          (!!cluster.maintenance_active && !isStandby)
                        }
                        onClick={() => runStream("enter", node)}
                      >
                        Enter Maintenance
                      </Button>
                    )}
                  </div>
                ) : (
                  <Hint>Maintenance actions require KIN Super Admin or Support-Ops.</Hint>
                )}
                {ops && !isStandby && !pfPassed ? (
                  <Hint>
                    {pfFailed
                      ? "Enter is blocked until every pre-flight check is green."
                      : "Run Check first. Enter stays disabled until pre-flight passes."}
                  </Hint>
                ) : null}
              </NodeCard>
            );
          })}
        </Grid>
        {nodes.length === 0 ? <Hint>No Pacemaker nodes reported. Is this host in the HA pair?</Hint> : null}
        {message ? <Hint>{message}</Hint> : null}
        <LogPane aria-label="Maintenance log">{log || "Status and transition output appears here."}</LogPane>
      </Page>
    </ConsoleChrome>
  );
}
