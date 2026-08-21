import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { ConsoleChrome } from "../ConsoleChrome";
import { isOpsRole, useAuth } from "../auth";
import { api } from "../api";
import { theme } from "../styles/theme";
import {
  Button,
  ClusterIcon,
  Dropdown,
  Hint,
  LogPane,
  MenuItem,
  Page,
  PageHeader,
  Skeleton,
  SkeletonCard,
  WarnBox,
} from "../ui";

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

type HealthLine = { ok: boolean; label: string };

const Tabs = styled.div`
  display: flex;
  align-items: stretch;
  gap: 1.25rem;
  border-bottom: 1px solid ${theme.line};
  margin: 0 0 1.15rem;
`;

const TabBtn = styled.button<{ $on?: boolean }>`
  border: 0;
  background: transparent;
  padding: 0.45rem 0 0.7rem;
  margin: 0;
  cursor: pointer;
  font: inherit;
  font-size: 0.875rem;
  font-weight: 600;
  color: ${(p) => (p.$on ? theme.accent : theme.surface[500])};
  border-bottom: 2px solid ${(p) => (p.$on ? theme.accent : "transparent")};
  margin-bottom: -1px;
`;

const HealthWrap = styled.div`
  position: relative;
  display: inline-flex;
  margin: 0 0 1.1rem;
`;

const HealthBtn = styled.button<{ $ok: boolean }>`
  border: 0;
  background: transparent;
  padding: 0.15rem 0.1rem;
  cursor: pointer;
  font: inherit;
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  color: ${(p) => (p.$ok ? theme.ok : theme.warn)};
  font-size: 0.875rem;
  font-weight: 600;
`;

const HealthDot = styled.span<{ $ok: boolean }>`
  width: 0.45rem;
  height: 0.45rem;
  border-radius: ${theme.radius.full};
  background: ${(p) => (p.$ok ? theme.ok : theme.warn)};
  flex-shrink: 0;
`;

const HealthCaret = styled.span`
  color: ${theme.surface[400]};
  font-weight: 500;
  font-size: 0.75rem;
`;

const Pair = styled.div`
  display: grid;
  grid-template-columns: minmax(0, 1fr) 2.75rem minmax(0, 1fr);
  align-items: stretch;
  margin: 0.25rem 0 1.25rem;

  @media (max-width: 720px) {
    grid-template-columns: 1fr;
    gap: 0.85rem;
  }
`;

const LinkCol = styled.div`
  display: flex;
  align-items: center;
  justify-content: center;

  @media (max-width: 720px) {
    display: none;
  }
`;

const LinkLine = styled.div`
  width: 100%;
  height: 1px;
  background: ${theme.line};
  position: relative;

  &::before,
  &::after {
    content: "";
    position: absolute;
    top: 50%;
    width: 6px;
    height: 6px;
    border-radius: ${theme.radius.full};
    background: ${theme.line};
    transform: translateY(-50%);
  }

  &::before {
    left: 0;
  }

  &::after {
    right: 0;
  }
`;

const Grid = styled.div`
  display: grid;
  gap: 0.85rem;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  margin: 0.25rem 0 1.25rem;
`;

const NodeCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1.1rem 1.15rem 1.15rem;
  box-shadow: ${theme.shadow.sm};
`;

const NodeHead = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
`;

const NodeName = styled.p`
  margin: 0;
  font-weight: 650;
  font-size: 1rem;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const InlineStatus = styled.span<{ $tone: "ok" | "idle" | "warn" }>`
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  flex-shrink: 0;
  font-size: 0.78rem;
  font-weight: 600;
  color: ${(p) =>
    p.$tone === "ok" ? theme.ok : p.$tone === "warn" ? theme.warn : theme.surface[500]};

  i {
    width: 0.4rem;
    height: 0.4rem;
    border-radius: ${theme.radius.full};
    background: currentColor;
    display: inline-block;
  }
`;

const Kv = styled.dl`
  margin: 0.35rem 0 1rem;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0;
  font-size: 0.86rem;
`;

const KvLabel = styled.dt`
  margin: 0;
  padding: 0.55rem 0.75rem 0.55rem 0;
  color: ${theme.muted};
  border-bottom: 1px solid ${theme.line};
`;

const KvValue = styled.dd`
  margin: 0;
  padding: 0.55rem 0;
  font-weight: 650;
  text-align: right;
  color: ${theme.surface[800]};
  border-bottom: 1px solid ${theme.line};
`;

const CheckList = styled.ul`
  margin: 0 0 1rem;
  padding: 0;
  list-style: none;
`;

const CheckItem = styled.li<{ $ok: boolean }>`
  display: grid;
  grid-template-columns: 1.1rem minmax(0, 1fr);
  column-gap: 0.35rem;
  margin: 0.65rem 0;
  color: ${(p) => (p.$ok ? theme.ok : theme.danger)};

  &::before {
    content: ${(p) => (p.$ok ? '"✓"' : '"✗"')};
    font-weight: 700;
    line-height: 1.3;
  }
`;

const CheckBody = styled.div`
  min-width: 0;
`;

const CheckName = styled.strong`
  display: block;
  color: ${theme.surface[800]};
  font-size: 0.86rem;
  font-weight: 650;
  line-height: 1.3;
`;

const CheckDetail = styled.span`
  display: block;
  margin-top: 0.2rem;
  color: ${theme.muted};
  font-size: 0.78rem;
  font-weight: 400;
  line-height: 1.45;
  white-space: pre-wrap;
  word-break: break-word;
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

function healthLines(cluster: ClusterSnap): HealthLine[] {
  return [
    {
      ok: Boolean(cluster.drbd_uptodate),
      label: cluster.drbd_uptodate ? "DRBD UpToDate" : "DRBD not UpToDate",
    },
    {
      ok: Boolean(cluster.qdevice_ok),
      label: cluster.qdevice_ok ? "qdevice voting" : "qdevice unhealthy",
    },
    {
      ok: Boolean(cluster.failcount_ok),
      label: cluster.failcount_ok ? "fail-count 0" : "fail-count nonzero",
    },
    {
      ok: Boolean(cluster.promoted),
      label: cluster.promoted ? `Promoted: ${cluster.promoted}` : "Promoted: unknown",
    },
  ];
}

export default function ClusterPage() {
  const { user } = useAuth();
  const ops = isOpsRole(user?.role);
  const [cluster, setCluster] = useState<ClusterSnap>({});
  const [log, setLog] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [preflight, setPreflight] = useState<Record<string, PreflightCheck> | null>(null);
  const [preflightTarget, setPreflightTarget] = useState("");
  const [tab, setTab] = useState<"status" | "activity">("status");
  const [healthOpen, setHealthOpen] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  const refresh = useCallback(async () => {
    const st = await api<StatusResp>("/api/cluster/status");
    setCluster(st.cluster || {});
    if (!esRef.current) setLog(st.log || "");
  }, []);

  useEffect(() => {
    void refresh()
      .catch((err: unknown) => {
        setMessage(err instanceof Error ? err.message : "Failed to load cluster status");
      })
      .finally(() => setLoaded(true));
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
  const lines = healthLines(cluster);
  const clusterOk = loaded && lines.every((l) => l.ok);

  function nodeCard(node: string) {
    const isStandby = standby.has(node);
    const isPromoted = cluster.promoted === node;
    const pfForThis = preflightTarget === node ? preflight : null;
    const pfFailed = pfForThis ? Object.values(pfForThis).some((c) => !c.ok) : false;
    const pfPassed = !!pfForThis && !pfFailed;
    const statusTone: "ok" | "idle" | "warn" = isStandby ? "warn" : isPromoted ? "ok" : "idle";
    const statusLabel = isStandby ? "Maintenance" : isPromoted ? "Promoted" : "Unpromoted";
    return (
      <NodeCard key={node}>
        <NodeHead>
          <NodeName title={node}>{node}</NodeName>
          <InlineStatus $tone={statusTone}>
            <i />
            {statusLabel}
          </InlineStatus>
        </NodeHead>
        <Kv>
          <KvLabel>Role</KvLabel>
          <KvValue>{isPromoted ? "Serving mail" : "Replica"}</KvValue>
          <KvLabel>Maintenance</KvLabel>
          <KvValue>{isStandby ? "On" : "Off"}</KvValue>
        </Kv>
        {pfForThis ? (
          <CheckList>
            {Object.entries(pfForThis).map(([name, check]) => (
              <CheckItem key={name} $ok={check.ok}>
                <CheckBody>
                  <CheckName>{name}</CheckName>
                  <CheckDetail>{check.detail}</CheckDetail>
                </CheckBody>
              </CheckItem>
            ))}
          </CheckList>
        ) : null}
        {ops ? (
          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            <Button
              type="button"
              variant="secondary"
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
                disabled={busy || !pfPassed || (!!cluster.maintenance_active && !isStandby)}
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
  }

  return (
    <ConsoleChrome subtitle="Cluster">
      <Page>
        <PageHeader
          icon={<ClusterIcon />}
          title="Cluster"
          subtitle="Take one mail node out of service for planned work. Pre-flight must pass before Enter is allowed. Closing this browser does not take the node out of maintenance; use Exit."
        />
        {cluster.maintenance_active ? (
          <WarnBox>
            A node is in maintenance ({(cluster.standby || []).join(", ") || "unknown"}). Deploy
            and mailbox create stay blocked until you exit.
          </WarnBox>
        ) : null}
        <Tabs>
          <TabBtn type="button" $on={tab === "status"} onClick={() => setTab("status")}>
            Status
          </TabBtn>
          <TabBtn type="button" $on={tab === "activity"} onClick={() => setTab("activity")}>
            Activity log
          </TabBtn>
        </Tabs>
        {tab === "status" ? (
          !loaded ? (
            <Grid>
              <SkeletonCard>
                <Skeleton $h="1rem" $w="40%" style={{ marginBottom: 12 }} />
                <Skeleton $h="0.7rem" $w="70%" style={{ marginBottom: 8 }} />
                <Skeleton $h="2rem" $w="55%" />
              </SkeletonCard>
              <SkeletonCard>
                <Skeleton $h="1rem" $w="40%" style={{ marginBottom: 12 }} />
                <Skeleton $h="0.7rem" $w="70%" style={{ marginBottom: 8 }} />
                <Skeleton $h="2rem" $w="55%" />
              </SkeletonCard>
            </Grid>
          ) : (
            <>
              <HealthWrap>
                <HealthBtn
                  type="button"
                  $ok={clusterOk}
                  aria-haspopup="menu"
                  aria-expanded={healthOpen}
                  onClick={() => setHealthOpen((v) => !v)}
                >
                  <HealthDot $ok={clusterOk} />
                  {clusterOk ? "Cluster Healthy" : "Cluster Needs Attention"}
                  <HealthCaret>{healthOpen ? "▴" : "▾"}</HealthCaret>
                </HealthBtn>
                <Dropdown open={healthOpen} onClose={() => setHealthOpen(false)} align="left">
                  {lines.map((line) => (
                    <MenuItem
                      key={line.label}
                      type="button"
                      data-danger={line.ok ? undefined : "true"}
                      onClick={() => setHealthOpen(false)}
                    >
                      <HealthDot $ok={line.ok} />
                      {line.label}
                    </MenuItem>
                  ))}
                </Dropdown>
              </HealthWrap>
              {nodes.length === 2 ? (
                <Pair>
                  {nodeCard(nodes[0])}
                  <LinkCol>
                    <LinkLine />
                  </LinkCol>
                  {nodeCard(nodes[1])}
                </Pair>
              ) : (
                <Grid>{nodes.map((node) => nodeCard(node))}</Grid>
              )}
              {nodes.length === 0 ? (
                <Hint>No Pacemaker nodes reported. Is this host in the HA pair?</Hint>
              ) : null}
            </>
          )
        ) : (
          <LogPane aria-label="Maintenance log">{log || "Status and transition output appears here."}</LogPane>
        )}
        {message ? <Hint>{message}</Hint> : null}
      </Page>
    </ConsoleChrome>
  );
}
