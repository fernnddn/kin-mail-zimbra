import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { ConsoleChrome } from "../ConsoleChrome";
import { isOpsRole, useAuth } from "../auth";
import { api } from "../api";
import { theme } from "../styles/theme";
import {
  Button,
  ClusterIcon,
  ConfirmModal,
  Dropdown,
  FieldLabel,
  Hint,
  Input,
  LogPane,
  MenuItem,
  Modal,
  Page,
  PageHeader,
  PasswordInput,
  Skeleton,
  SkeletonCard,
  WarnBox,
} from "../ui";
import { ClusterTopology, type ObservabilitySnap } from "./ClusterTopology";

type ClusterSnap = {
  local_host?: string;
  topology?: string;
  nodes?: string[];
  standby?: string[];
  promoted?: string | null;
  unpromoted?: string[];
  drbd_uptodate?: boolean;
  qdevice_ok?: boolean;
  failcount_ok?: boolean;
  maintenance_active?: boolean;
  offline?: string[];
  stale_peers?: string[];
  observability?: ObservabilitySnap;
  node_ips?: Record<string, string>;
};

type StatusResp = {
  ok: boolean;
  log: string;
  cluster: ClusterSnap;
  error?: string | null;
};

type PreflightCheck = { ok: boolean; detail: string };

type RemoveProbe = {
  mode: "graceful" | "forced";
  target: string;
  survivor: string;
  reachable: boolean;
  errors?: string[];
  notes?: string[];
};

function uniqueNames(...groups: Array<string[] | undefined>): string[] {
  const out: string[] = [];
  for (const group of groups) {
    for (const name of group || []) {
      if (name && !out.includes(name)) out.push(name);
    }
  }
  return out;
}

function ipForNode(
  name: string,
  ips: Record<string, string> | undefined,
  localHost?: string,
): string {
  const map = ips || {};
  if (map[name]) return map[name];
  if (localHost && map[localHost]) return map[localHost];
  const values = Object.values(map).filter(Boolean);
  if (values.length === 1) return values[0];
  return "";
}

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

const CardActions = styled.div`
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 0.85rem 0;
  margin-top: 0.35rem;
  padding-top: 0.85rem;
  border-top: 1px solid ${theme.line};
`;

const PrepCluster = styled.div`
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-width: 0;
  flex: 1 1 12rem;
  padding-right: 1rem;
`;

const PrepButtons = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
`;

const RemoveCluster = styled.div<{ $solo?: boolean }>`
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  flex: 0 1 auto;
  padding-left: ${(p) => (p.$solo ? "0" : "1rem")};
  margin-left: ${(p) => (p.$solo ? "0" : "0.15rem")};
  border-left: ${(p) => (p.$solo ? "0" : `1px solid ${theme.line}`)};

  @media (max-width: 520px) {
    flex-basis: 100%;
    padding-left: 0;
    margin-left: 0;
    padding-top: ${(p) => (p.$solo ? "0" : "0.75rem")};
    border-left: 0;
    border-top: ${(p) => (p.$solo ? "0" : `1px solid ${theme.line}`)};
  }
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
  margin: 0.45rem 0 0;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0;
  font-size: 0.86rem;

  dt:nth-last-child(-n + 2),
  dd:nth-last-child(-n + 2) {
    border-bottom: 0;
  }
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
  margin: 0.35rem 0 0;
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

const CardHint = styled.p`
  margin: 0;
  max-width: 20rem;
  color: ${theme.muted};
  font-size: 0.78rem;
  line-height: 1.45;
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
  if (cluster.topology === "1vm") {
    const name = cluster.local_host || "this server";
    return [
      {
        ok: true,
        label: `Single server: ${name}`,
      },
    ];
  }
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
      ok:
        !cluster.observability
          ? Boolean(cluster.qdevice_ok)
          : cluster.observability.status === "healthy",
      label:
        cluster.observability?.status === "absent"
          ? "Observability absent"
          : cluster.observability?.status === "healthy"
            ? "Observability reachable"
            : cluster.observability
              ? "Observability unreachable"
              : "Observability unknown",
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
  const navigate = useNavigate();
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
  const [pendingRemove, setPendingRemove] = useState<string | null>(null);
  const [removeProbe, setRemoveProbe] = useState<RemoveProbe | null>(null);
  const [probing, setProbing] = useState(false);
  const [pendingObsRemove, setPendingObsRemove] = useState(false);
  const [obsRemoveProbe, setObsRemoveProbe] = useState<{
    errors?: string[];
    notes?: string[];
    configured_ip?: string;
  } | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addIp, setAddIp] = useState("");
  const [addHost, setAddHost] = useState("");
  const [addRoot, setAddRoot] = useState("");
  const [addKin, setAddKin] = useState("");
  const [addErr, setAddErr] = useState("");
  const [pendingObsAdd, setPendingObsAdd] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const probeRef = useRef<EventSource | null>(null);

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
      probeRef.current?.close();
      probeRef.current = null;
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

  function openRemove(target: string) {
    probeRef.current?.close();
    setPendingRemove(target);
    setRemoveProbe(null);
    setProbing(true);
    setMessage("");
    const qs = new URLSearchParams({
      action: "remove_host",
      op: "probe",
      target,
    });
    const es = new EventSource(`/api/wizard/deploy/stream?${qs.toString()}`);
    probeRef.current = es;
    let buf = "";
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as { type?: string; data?: string; exit_code?: number; message?: string };
        if (data.type === "stdout" && data.data) buf += data.data;
        else if (data.type === "stderr" && data.data) buf += data.data;
        else if (data.type === "error") {
          buf += `[error] ${data.message || ""}\n`;
          setMessage(data.message || "Remove-host probe failed");
        } else if (data.type === "done") {
          es.close();
          if (probeRef.current === es) probeRef.current = null;
          const parsed = parseTaggedJson(buf, "REMOVE_PROBE_JSON:");
          if (parsed && typeof parsed.target === "string") {
            setRemoveProbe(parsed as RemoveProbe);
          }
          setProbing(false);
        }
      } catch {
        /* ignore malformed SSE */
      }
    };
    es.onerror = () => {
      if (probeRef.current === es) {
        es.close();
        probeRef.current = null;
        setProbing(false);
        setMessage("Lost connection while probing host reachability.");
      }
    };
  }

  function runRemove(target: string) {
    esRef.current?.close();
    setBusy(true);
    setMessage("");
    setLog("");
    setTab("activity");
    const qs = new URLSearchParams({
      action: "remove_host",
      op: "apply",
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
          setPendingRemove(null);
          setRemoveProbe(null);
          const code = data.exit_code ?? 1;
          setMessage(
            code === 0
              ? `Remove host finished for ${target}.`
              : `Remove host failed for ${target}.`,
          );
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
        setMessage("Lost connection to the remove-host stream.");
      }
    };
  }

  function openObsRemove() {
    probeRef.current?.close();
    setPendingObsRemove(true);
    setObsRemoveProbe(null);
    setProbing(true);
    setMessage("");
    const qs = new URLSearchParams({
      action: "remove_observability",
      op: "probe",
    });
    const es = new EventSource(`/api/wizard/deploy/stream?${qs.toString()}`);
    probeRef.current = es;
    let buf = "";
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as { type?: string; data?: string; message?: string };
        if (data.type === "stdout" && data.data) buf += data.data;
        else if (data.type === "stderr" && data.data) buf += data.data;
        else if (data.type === "error") {
          buf += `[error] ${data.message || ""}\n`;
          setMessage(data.message || "Remove Observability probe failed");
        } else if (data.type === "done") {
          es.close();
          if (probeRef.current === es) probeRef.current = null;
          const parsed = parseTaggedJson(buf, "REMOVE_OBS_PROBE_JSON:");
          if (parsed) setObsRemoveProbe(parsed as { errors?: string[]; notes?: string[]; configured_ip?: string });
          setProbing(false);
        }
      } catch {
        /* ignore malformed SSE */
      }
    };
    es.onerror = () => {
      if (probeRef.current === es) {
        es.close();
        probeRef.current = null;
        setProbing(false);
        setMessage("Lost connection while probing Observability.");
      }
    };
  }

  function runObsRemove() {
    esRef.current?.close();
    setBusy(true);
    setMessage("");
    setLog("");
    setTab("activity");
    const qs = new URLSearchParams({
      action: "remove_observability",
      op: "apply",
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
          setPendingObsRemove(false);
          setObsRemoveProbe(null);
          const code = data.exit_code ?? 1;
          setMessage(
            code === 0
              ? "Remove Observability finished. Add Observability is available if status shows absent."
              : "Remove Observability failed.",
          );
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
        setMessage("Lost connection to the Remove Observability stream.");
      }
    };
  }

  function runObsAdd() {
    esRef.current?.close();
    setBusy(true);
    setMessage("");
    setLog("");
    setTab("activity");
    setPendingObsAdd(false);
    setAddOpen(false);
    const qs = new URLSearchParams({
      action: "add_observability",
      op: "apply",
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
          const code = data.exit_code ?? 1;
          setMessage(code === 0 ? "Add Observability finished." : "Add Observability failed.");
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
        setMessage("Lost connection to the Add Observability stream.");
      }
    };
  }

  async function submitAddForm() {
    setAddErr("");
    if (!addIp.trim()) {
      setAddErr("Enter the new Observability VM IPv4 address.");
      return;
    }
    if (addRoot.length < 8 || addKin.length < 8) {
      setAddErr("Root and kin passwords must be at least 8 characters.");
      return;
    }
    try {
      await api("/api/cluster/observability/secrets", {
        method: "POST",
        body: JSON.stringify({
          ip: addIp.trim(),
          hostname: addHost.trim(),
          host_root_pass: addRoot,
          kin_user_pass: addKin,
        }),
      });
      setPendingObsAdd(true);
    } catch (err: unknown) {
      setAddErr(err instanceof Error ? err.message : "Could not store credentials");
    }
  }

  const topology = cluster.topology === "2vm" ? "2vm" : cluster.topology === "1vm" ? "1vm" : "";
  const nodes = uniqueNames(cluster.nodes, cluster.offline, cluster.stale_peers);
  const displayNodes =
    topology === "1vm"
      ? nodes.length
        ? nodes.slice(0, 1)
        : [cluster.local_host || "this server"]
      : nodes;
  const mailTopo = displayNodes.map((name) => ({
    name,
    healthy:
      !(cluster.offline || []).includes(name) && !(cluster.stale_peers || []).includes(name),
    ip: ipForNode(name, cluster.node_ips, cluster.local_host),
  }));
  const standby = new Set(cluster.standby || []);
  const offline = new Set([...(cluster.offline || []), ...(cluster.stale_peers || [])]);
  const lines = healthLines(cluster);
  const clusterOk = loaded && lines.every((l) => l.ok);

  function nodeCard(node: string) {
    const isStandby = standby.has(node);
    const isPromoted = cluster.promoted === node;
    const isOffline = offline.has(node);
    const pfForThis = preflightTarget === node ? preflight : null;
    const pfFailed = pfForThis ? Object.values(pfForThis).some((c) => !c.ok) : false;
    const pfPassed = !!pfForThis && !pfFailed;
    const statusTone: "ok" | "idle" | "warn" = isOffline
      ? "warn"
      : isStandby
        ? "warn"
        : isPromoted
          ? "ok"
          : "idle";
    const statusLabel = isOffline
      ? "Unreachable"
      : isStandby
        ? "Maintenance"
        : isPromoted
          ? "Promoted"
          : "Unpromoted";
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
          <CardActions>
            {!isOffline ? (
              <PrepCluster>
                <PrepButtons>
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
                </PrepButtons>
                {!isStandby && !pfPassed ? (
                  <CardHint>
                    {pfFailed
                      ? "All checks must pass before you can enter maintenance."
                      : "Run Check first to enable Enter Maintenance."}
                  </CardHint>
                ) : null}
              </PrepCluster>
            ) : null}
            <RemoveCluster $solo={isOffline}>
              <Button
                type="button"
                variant="danger"
                disabled={busy || probing || (!isOffline && !isStandby)}
                onClick={() => openRemove(node)}
              >
                Remove Host
              </Button>
              {!isOffline && !isStandby ? (
                <CardHint>Put this node in maintenance before Remove Host.</CardHint>
              ) : null}
            </RemoveCluster>
          </CardActions>
        ) : (
          <CardActions>
            <CardHint>Maintenance actions require KIN Super Admin or Support-Ops.</CardHint>
          </CardActions>
        )}
      </NodeCard>
    );
  }

  return (
    <ConsoleChrome subtitle="Cluster">
      <Page>
        <PageHeader
          icon={<ClusterIcon />}
          title="Cluster"
          subtitle="Take one mail node offline for planned work. Run Check, then Enter Maintenance. Closing the browser does not exit maintenance; use Exit Maintenance."
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
              {topology === "1vm" ? (
                <>
                  <ClusterTopology
                    topology="1vm"
                    mailNodes={mailTopo}
                    observability={{ status: "absent" }}
                    ops={ops}
                    busy={busy}
                    onAdd={() => navigate("/wizard/topology")}
                    onRemove={() => undefined}
                  />
                  <Grid>
                    {displayNodes.map((node) => {
                      const name = node;
                      return (
                        <NodeCard key={name}>
                          <NodeHead>
                            <NodeName title={name}>{name}</NodeName>
                            <InlineStatus $tone="ok">
                              <i />
                              Healthy
                            </InlineStatus>
                          </NodeHead>
                        </NodeCard>
                      );
                    })}
                  </Grid>
                </>
              ) : (
                <>
              <ClusterTopology
                topology="2vm"
                mailNodes={mailTopo}
                observability={cluster.observability || { status: "absent" }}
                ops={ops}
                busy={busy || probing}
                onAdd={() => {
                  setAddErr("");
                  setAddOpen(true);
                }}
                onRemove={openObsRemove}
              />
              {displayNodes.length === 2 ? (
                <Pair>
                  {nodeCard(displayNodes[0])}
                  <LinkCol>
                    <LinkLine />
                  </LinkCol>
                  {nodeCard(displayNodes[1])}
                </Pair>
              ) : (
                <Grid>{displayNodes.map((node) => nodeCard(node))}</Grid>
              )}
              {topology === "2vm" && displayNodes.length === 0 ? (
                <Hint>No mail nodes are visible in the cluster yet.</Hint>
              ) : null}
                </>
              )}
            </>
          )
        ) : (
          <LogPane aria-label="Maintenance log">{log || "Status and transition output appears here."}</LogPane>
        )}
        {message ? <Hint>{message}</Hint> : null}
        <ConfirmModal
          open={!!pendingRemove}
          title={
            removeProbe?.mode === "forced"
              ? "Remove unreachable host"
              : "Remove host"
          }
          message={
            probing ? (
              <>Probing SSH reachability for <strong>{pendingRemove}</strong>…</>
            ) : removeProbe?.mode === "forced" ? (
              <>
                <strong>{pendingRemove}</strong> does not answer SSH. This will run the
                forced path: clear Corosync membership, the DRBD peer, Pacemaker
                location constraints, and qdevice certs on the survivor (
                {removeProbe.survivor || "this host"}) only. The departing VM will not
                be uninstalled.
              </>
            ) : (
              <>
                Drain <strong>{pendingRemove}</strong> if it is not already in
                maintenance, remove it from the cluster on the survivor, then run
                uninstall on the departing node. Mail on the surviving node keeps
                running.
              </>
            )
          }
          detail={
            probing
              ? undefined
              : removeProbe?.mode === "forced"
                ? "A failed forced remove leaves stale references that also block Add Host. Survivor-side cleanup is the part that must succeed."
                : "This is harder to reverse than maintenance mode. If uninstall on the departing node fails, survivor-side removal still counts as success."
          }
          confirmLabel={probing ? "Probing…" : "Remove host"}
          loading={busy || probing}
          onCancel={() => {
            if (busy || probing) return;
            probeRef.current?.close();
            probeRef.current = null;
            setPendingRemove(null);
            setRemoveProbe(null);
          }}
          onConfirm={() => {
            if (!pendingRemove || !removeProbe || probing || busy) return;
            if ((removeProbe.errors || []).length > 0) return;
            runRemove(pendingRemove);
          }}
        />
        <ConfirmModal
          open={pendingObsRemove}
          title="Remove Observability"
          message={
            probing ? (
              <>Probing whether Observability still answers…</>
            ) : (obsRemoveProbe?.errors || []).length > 0 ? (
              <>{(obsRemoveProbe?.errors || []).join(" ")}</>
            ) : (
              <>
                Observability is unreachable. This will disarm SBD on both mail
                nodes, clear qdevice and iSCSI pointers to
                {" "}
                <strong>{obsRemoveProbe?.configured_ip || "the dead witness"}</strong>,
                and leave mail running without a fencing device until you Add a
                replacement.
              </>
            )
          }
          detail="This is a forced path only. A healthy Observability node cannot be removed. Dual-self-fence risk stays until Add Observability completes."
          confirmLabel={probing ? "Probing…" : "Remove Observability"}
          loading={busy || probing}
          onCancel={() => {
            if (busy || probing) return;
            probeRef.current?.close();
            probeRef.current = null;
            setPendingObsRemove(false);
            setObsRemoveProbe(null);
          }}
          onConfirm={() => {
            if (probing || busy) return;
            if ((obsRemoveProbe?.errors || []).length > 0) return;
            runObsRemove();
          }}
        />
        <Modal
          open={addOpen}
          onClose={() => {
            if (busy) return;
            setAddOpen(false);
          }}
          title="Add Observability"
          footer={
            <>
              <Button type="button" variant="ghost" disabled={busy} onClick={() => setAddOpen(false)}>
                Cancel
              </Button>
              <Button type="button" disabled={busy} onClick={() => void submitAddForm()}>
                Continue
              </Button>
            </>
          }
        >
          <Hint>
            Enter the replacement VM address and credentials. Do not assume the
            previous IP is still available.
          </Hint>
          <FieldLabel htmlFor="obs_ip" required>
            Observability IPv4
          </FieldLabel>
          <Input
            id="obs_ip"
            value={addIp}
            onChange={(e) => setAddIp(e.target.value)}
            placeholder="192.0.2.53"
            autoComplete="off"
          />
          <FieldLabel htmlFor="obs_host">Hostname (optional)</FieldLabel>
          <Input
            id="obs_host"
            value={addHost}
            onChange={(e) => setAddHost(e.target.value)}
            placeholder="obs.example.test"
            autoComplete="off"
          />
          <FieldLabel htmlFor="obs_root" required>
            Root password
          </FieldLabel>
          <PasswordInput
            id="obs_root"
            value={addRoot}
            onChange={(e) => setAddRoot(e.target.value)}
            autoComplete="new-password"
          />
          <FieldLabel htmlFor="obs_kin" required>
            Kin user password
          </FieldLabel>
          <PasswordInput
            id="obs_kin"
            value={addKin}
            onChange={(e) => setAddKin(e.target.value)}
            autoComplete="new-password"
          />
          {addErr ? <Hint>{addErr}</Hint> : null}
        </Modal>
        <ConfirmModal
          open={pendingObsAdd}
          title="Add Observability"
          message={
            <>
              Provision qnetd and the iSCSI SBD target on <strong>{addIp.trim() || "the new VM"}</strong>,
              reconnect both live mail nodes, initialize a fresh SBD device, and
              re-arm fencing. Mail service must stay undisturbed.
            </>
          }
          detail="A mistake here can leave both mail nodes without working fencing. Confirm the new VM is the replacement Observability host."
          confirmLabel="Add Observability"
          loading={busy}
          onCancel={() => {
            if (busy) return;
            setPendingObsAdd(false);
          }}
          onConfirm={() => {
            if (busy) return;
            runObsAdd();
          }}
        />
      </Page>
    </ConsoleChrome>
  );
}
