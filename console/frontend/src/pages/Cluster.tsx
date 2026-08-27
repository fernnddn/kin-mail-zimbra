import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
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
import { useTasks } from "../tasks/TaskProvider";

type ClusterSnap = {
  local_host?: string;
  topology?: string;
  nodes?: string[];
  standby?: string[];
  promoted?: string | null;
  promoted_conflict?: boolean;
  promoted_names?: string[];
  unpromoted?: string[];
  zimbra_node?: string | null;
  drbd_uptodate?: boolean;
  drbd_sync_percent?: number | null;
  vip_ip?: string | null;
  vip_node?: string | null;
  qdevice_ok?: boolean;
  quorate?: boolean | null;
  votes_total?: number | null;
  votes_needed?: number | null;
  quorum_hint?: string;
  no_quorum_policy?: string;
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
  local_is_target?: boolean;
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

function hostsMatch(left?: string, right?: string): boolean {
  const a = (left || "").trim().toLowerCase();
  const b = (right || "").trim().toLowerCase();
  if (!a || !b) return false;
  if (a === b) return true;
  return a.split(".")[0] === b.split(".")[0];
}

function ipForNode(
  name: string,
  ips: Record<string, string> | undefined,
  _localHost?: string,
): string {
  const map = ips || {};
  if (map[name]) return map[name];
  // Never fall back to another node's IP - that mislabels Host B as Host A.
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
  gap: 0.85rem;
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

const KvValue = styled.dd<{ $mono?: boolean }>`
  margin: 0;
  padding: 0.55rem 0;
  font-weight: 650;
  font-family: ${(p) => (p.$mono ? theme.mono : "inherit")};
  font-variant-numeric: tabular-nums;
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

const gaugePulse = keyframes`
  0%, 100% { filter: drop-shadow(0 0 0 rgba(217, 119, 6, 0)); }
  50% { filter: drop-shadow(0 0 6px rgba(217, 119, 6, 0.4)); }
`;

const fadeIn = keyframes`
  from { opacity: 0; transform: translateY(2px); }
  to { opacity: 1; transform: translateY(0); }
`;

type Tone = "ok" | "warn" | "muted" | "accent";

function toneColor(tone?: Tone): string {
  if (tone === "ok") return theme.ok;
  if (tone === "warn") return theme.warn;
  if (tone === "accent") return theme.accent;
  return theme.surface[200];
}

function toneSoftBg(tone?: Tone): string {
  if (tone === "ok") return "rgba(16, 185, 129, 0.12)";
  if (tone === "warn") return "rgba(217, 119, 6, 0.12)";
  if (tone === "accent") return theme.accentSoft;
  return theme.surface[100];
}

function toneFg(tone?: Tone): string {
  if (tone === "ok") return theme.ok;
  if (tone === "warn") return theme.warn;
  if (tone === "accent") return theme.accent;
  return theme.surface[400];
}

const IconChip = styled.div<{ $tone: Tone }>`
  width: 2.6rem;
  height: 2.6rem;
  border-radius: ${theme.radius.md};
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: ${(p) => toneFg(p.$tone)};
  background: ${(p) => toneSoftBg(p.$tone)};

  svg {
    width: 1.35rem;
    height: 1.35rem;
  }
`;

function TopologyGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="5.2" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="5.4" cy="18" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="18.6" cy="18" r="2.2" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M12 7.4L6.5 16M12 7.4L17.5 16M7.6 18h8.8"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function ServerGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="4.5" y="4" width="15" height="6.5" rx="1.4" stroke="currentColor" strokeWidth="1.6" />
      <rect x="4.5" y="13.5" width="15" height="6.5" rx="1.4" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="7.7" cy="7.25" r="0.9" fill="currentColor" />
      <circle cx="7.7" cy="16.75" r="0.9" fill="currentColor" />
    </svg>
  );
}

function VipGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3a6 6 0 00-6 6c0 4.5 6 12 6 12s6-7.5 6-12a6 6 0 00-6-6z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="9" r="2.2" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function RefreshGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M20 12a8 8 0 10-2.3 5.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
      <path
        d="M20 7.5V12h-4.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function FailcountGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M4 19V5M4 19h16"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path
        d="M8 15v-4M12 15V8M16 15v-6"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function QuorumGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 3l6.5 2.6v5.2c0 4.8-2.8 8.3-6.5 9.9-3.7-1.6-6.5-5.1-6.5-9.9V5.6L12 3z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M9 12l2.2 2.2 3.8-4.2" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ObservabilityGlyph() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <circle cx="12" cy="12" r="2.6" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

const OverviewHeader = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem 0.75rem;
  margin: 0 0 0.6rem;
`;

const OverviewTitle = styled.h2`
  margin: 0;
  font-size: 0.8rem;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: ${theme.surface[500]};
`;

const FreshnessRow = styled.div`
  display: flex;
  align-items: center;
  gap: 0.55rem;
  min-width: 0;
`;

const FreshnessText = styled.span`
  font-size: 0.75rem;
  color: ${theme.muted};
  white-space: nowrap;
`;

const RefreshBtn = styled.button`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  color: ${theme.surface[600]};
  border-radius: ${theme.radius.sm};
  width: 1.8rem;
  height: 1.8rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
  transition: background ${theme.motion.fast}, transform ${theme.motion.fast},
    color ${theme.motion.fast};

  &:hover:not(:disabled) {
    background: ${theme.surface[50]};
    color: ${theme.accent};
  }
  &:active:not(:disabled) {
    transform: scale(0.92);
  }
  &:disabled {
    opacity: 0.45;
    cursor: default;
  }

  svg {
    width: 0.95rem;
    height: 0.95rem;
  }
`;

function relativeTime(ms: number): string {
  const secs = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (secs < 3) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  return `${mins}m ago`;
}

function FreshnessBadge({ updatedAt }: { updatedAt: number | null }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      setTick((t) => t + 1);
    };
    const id = window.setInterval(tick, 5000);
    return () => window.clearInterval(id);
  }, []);
  if (!updatedAt) return null;
  return <FreshnessText>Updated {relativeTime(updatedAt)}</FreshnessText>;
}

const OverviewGrid = styled.div`
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 0.9rem;
  margin: 0 0 1.15rem;
  animation: ${fadeIn} ${theme.motion.page} ease;

  @media (min-width: 960px) {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
`;

const OverviewCard = styled.div<{ $tone?: Tone }>`
  border: 1px solid ${theme.line};
  border-left: 3px solid ${(p) => toneColor(p.$tone)};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1.05rem 1.15rem;
  box-shadow: ${theme.shadow.sm};
  display: flex;
  align-items: flex-start;
  gap: 0.9rem;
  min-width: 0;
  transition: border-color ${theme.motion.fast}, box-shadow ${theme.motion.fast};

  &:hover {
    border-color: ${theme.surface[300]};
    box-shadow: ${theme.shadow.md};
  }
`;

const OverviewBody = styled.div`
  min-width: 0;
  flex: 1;
`;

const OverviewLabel = styled.p`
  margin: 0 0 0.3rem;
  font-size: 0.72rem;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: ${theme.muted};
`;

const OverviewValue = styled.p<{ $mono?: boolean; $wrap?: boolean }>`
  margin: 0;
  font-size: 1.05rem;
  font-weight: 650;
  font-family: ${(p) => (p.$mono ? theme.mono : "inherit")};
  font-variant-numeric: tabular-nums;
  color: ${theme.surface[800]};
  overflow: hidden;
  text-overflow: ${(p) => (p.$wrap ? "unset" : "ellipsis")};
  white-space: ${(p) => (p.$wrap ? "normal" : "nowrap")};
  word-break: ${(p) => (p.$wrap ? "break-word" : "normal")};
  line-height: 1.35;
`;

const OverviewSub = styled.p<{ $tone?: "ok" | "warn" | "muted" }>`
  margin: 0.3rem 0 0;
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.78rem;
  font-weight: ${(p) => (p.$tone && p.$tone !== "muted" ? 600 : 400)};
  color: ${(p) =>
    p.$tone === "ok" ? theme.ok : p.$tone === "warn" ? theme.warn : theme.muted};

  &::before {
    content: "";
    display: ${(p) => (p.$tone && p.$tone !== "muted" ? "block" : "none")};
    width: 0.4rem;
    height: 0.4rem;
    border-radius: ${theme.radius.full};
    background: currentColor;
    flex-shrink: 0;
  }
`;

const GaugeWrap = styled.div<{ $pulse?: boolean }>`
  position: relative;
  width: 80px;
  height: 80px;
  flex-shrink: 0;
  animation: ${(p) => (p.$pulse ? gaugePulse : "none")} 2.4s ease-in-out infinite;
`;

const GaugeSvg = styled.svg`
  transform: rotate(-90deg);
`;

const GaugeTrack = styled.circle`
  fill: none;
  stroke: ${theme.surface[200]};
  stroke-width: 7;
`;

const GaugeValue = styled.circle<{ $stroke: string }>`
  fill: none;
  stroke: ${(p) => p.$stroke};
  stroke-width: 7;
  stroke-linecap: round;
  transition:
    stroke-dashoffset 700ms ease,
    stroke 300ms ease;
`;

const GaugeCenter = styled.div`
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
`;

const GaugePct = styled.span`
  font-size: 0.92rem;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
  color: ${theme.surface[800]};
  line-height: 1.1;
`;

const GaugeState = styled.span<{ $tone: "ok" | "warn" | "muted" }>`
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  margin-top: 0.15rem;
  color: ${(p) => (p.$tone === "ok" ? theme.ok : p.$tone === "warn" ? theme.warn : theme.surface[400])};
`;

let gaugeGradientSeq = 0;

function ReplicationGauge({
  percent,
  ok,
  unknown,
}: {
  percent: number;
  ok: boolean;
  unknown?: boolean;
}) {
  const size = 80;
  const stroke = 7;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const pct = unknown ? 0 : Math.max(0, Math.min(100, percent));
  const offset = c - (pct / 100) * c;
  const gradId = useState(() => `repl-grad-${++gaugeGradientSeq}`)[0];
  const tone: "ok" | "warn" | "muted" = unknown ? "muted" : ok ? "ok" : "warn";
  const strokeRef = unknown ? theme.surface[300] : `url(#${gradId})`;
  return (
    <GaugeWrap $pulse={!unknown && !ok} aria-hidden="true">
      <GaugeSvg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
        <defs>
          <linearGradient id={gradId} x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor={ok ? theme.ok : theme.warn} stopOpacity="0.7" />
            <stop offset="100%" stopColor={ok ? theme.ok : theme.warn} stopOpacity="1" />
          </linearGradient>
        </defs>
        <GaugeTrack cx={size / 2} cy={size / 2} r={r} />
        <GaugeValue
          cx={size / 2}
          cy={size / 2}
          r={r}
          $stroke={strokeRef}
          strokeDasharray={c}
          strokeDashoffset={unknown ? c : offset}
        />
      </GaugeSvg>
      <GaugeCenter>
        <GaugePct>{unknown ? "-" : `${pct.toFixed(0)}%`}</GaugePct>
        <GaugeState $tone={tone}>{unknown ? "no data" : ok ? "in sync" : "syncing"}</GaugeState>
      </GaugeCenter>
    </GaugeWrap>
  );
}

type StatusTone = "ok" | "warn" | "muted";

function clusterOverviewCards(cluster: ClusterSnap, topology: string): ReactNode[] {
  const cards: ReactNode[] = [];
  const nodeCount = uniqueNames(cluster.nodes, cluster.offline, cluster.stale_peers).length;

  cards.push(
    <OverviewCard key="topology" $tone="accent">
      <IconChip $tone="accent">
        <TopologyGlyph />
      </IconChip>
      <OverviewBody>
        <OverviewLabel>Topology</OverviewLabel>
        <OverviewValue>{topology === "2vm" ? "2-node HA pair" : "Single server"}</OverviewValue>
        <OverviewSub>
          {topology === "2vm" ? `${nodeCount} mail node${nodeCount === 1 ? "" : "s"} configured` : cluster.local_host || "-"}
        </OverviewSub>
      </OverviewBody>
    </OverviewCard>,
  );

  if (topology !== "2vm") return cards;

  const conflict = Boolean(cluster.promoted_conflict);
  const servingTone: StatusTone = conflict ? "warn" : cluster.promoted ? "ok" : "warn";
  cards.push(
    <OverviewCard key="serving" $tone={servingTone}>
      <IconChip $tone={servingTone}>
        <ServerGlyph />
      </IconChip>
      <OverviewBody>
        <OverviewLabel>Serving mail</OverviewLabel>
        <OverviewValue $wrap>
          {conflict
            ? `Conflict: ${(cluster.promoted_names || []).join(", ") || "multiple"}`
            : cluster.promoted || "Unknown"}
        </OverviewValue>
        <OverviewSub $tone={servingTone}>
          {conflict
            ? "More than one Promoted node - resolve before maintenance"
            : (cluster.unpromoted || []).length
              ? `Replica: ${(cluster.unpromoted || []).join(", ")}`
              : "No confirmed replica"}
        </OverviewSub>
      </OverviewBody>
    </OverviewCard>,
  );

  const vipIp = (cluster.vip_ip || "").trim();
  const vipNode = (cluster.vip_node || "").trim();
  const promoted = (cluster.promoted || "").trim();
  const vipOk = Boolean(vipIp && vipNode && promoted && vipNode === promoted);
  const vipMismatch = Boolean(vipNode && promoted && vipNode !== promoted);
  const vipTone: StatusTone = !vipIp ? "muted" : vipOk ? "ok" : "warn";
  cards.push(
    <OverviewCard key="vip" $tone={vipTone}>
      <IconChip $tone={vipTone}>
        <VipGlyph />
      </IconChip>
      <OverviewBody>
        <OverviewLabel>Mail VIP</OverviewLabel>
        <OverviewValue $mono={Boolean(vipIp)}>{vipIp || "Not configured"}</OverviewValue>
        <OverviewSub $tone={vipTone}>
          {!vipIp
            ? "No floating IP set for this cluster"
            : vipOk
              ? `Active on ${vipNode}`
              : vipNode
                ? vipMismatch
                  ? `Active on ${vipNode} (expected ${promoted})`
                  : `Active on ${vipNode} (Promoted unknown)`
                : "Not currently routed to any node"}
        </OverviewSub>
      </OverviewBody>
    </OverviewCard>,
  );

  const pct = cluster.drbd_sync_percent;
  const uptodate = Boolean(cluster.drbd_uptodate);
  const replTone: StatusTone = uptodate ? "ok" : typeof pct === "number" ? "warn" : "muted";
  cards.push(
    <OverviewCard key="replication" $tone={replTone}>
      <ReplicationGauge percent={typeof pct === "number" ? pct : uptodate ? 100 : 0} ok={uptodate} unknown={!uptodate && typeof pct !== "number"} />
      <OverviewBody>
        <OverviewLabel>DRBD Replication</OverviewLabel>
        <OverviewValue>
          {uptodate ? "UpToDate" : typeof pct === "number" ? `${pct.toFixed(0)}% synced` : "Unknown"}
        </OverviewValue>
        <OverviewSub $tone={replTone}>
          {uptodate
            ? "Both replicas in sync"
            : typeof pct === "number"
              ? "Resync in progress - normal after Build HA or rejoin"
              : "No live sync data"}
        </OverviewSub>
      </OverviewBody>
    </OverviewCard>,
  );

  const fcOk = cluster.failcount_ok !== false;
  const fcTone: StatusTone = cluster.failcount_ok === undefined ? "muted" : fcOk ? "ok" : "warn";
  cards.push(
    <OverviewCard key="failcount" $tone={fcTone}>
      <IconChip $tone={fcTone}>
        <FailcountGlyph />
      </IconChip>
      <OverviewBody>
        <OverviewLabel>Pacemaker fail-count</OverviewLabel>
        <OverviewValue>
          {cluster.failcount_ok === undefined ? "Unknown" : fcOk ? "Clear" : "Nonzero"}
        </OverviewValue>
        <OverviewSub $tone={fcTone}>
          {cluster.failcount_ok === undefined
            ? "Not reported yet"
            : fcOk
              ? "Self-heal monitors healthy"
              : "Ops can re-probe after a transient race"}
        </OverviewSub>
      </OverviewBody>
    </OverviewCard>,
  );

  const qdeviceTone: StatusTone = cluster.qdevice_ok ? "ok" : "warn";
  cards.push(
    <OverviewCard key="qdevice" $tone={qdeviceTone}>
      <IconChip $tone={qdeviceTone}>
        <QuorumGlyph />
      </IconChip>
      <OverviewBody>
        <OverviewLabel>Quorum device</OverviewLabel>
        <OverviewValue>{cluster.qdevice_ok ? "Voting" : "Not voting"}</OverviewValue>
        <OverviewSub $tone={qdeviceTone}>
          {cluster.qdevice_ok ? "qdevice reachable" : "qdevice missing, offline, or not voting"}
        </OverviewSub>
      </OverviewBody>
    </OverviewCard>,
  );

  if (cluster.observability) {
    const obs = cluster.observability;
    const obsHealthy = obs.status === "healthy";
    const obsTone: StatusTone = obsHealthy ? "ok" : obs.status === "absent" ? "muted" : "warn";
    cards.push(
      <OverviewCard key="observability" $tone={obsTone}>
        <IconChip $tone={obsTone}>
          <ObservabilityGlyph />
        </IconChip>
        <OverviewBody>
          <OverviewLabel>Observability</OverviewLabel>
          <OverviewValue>{obs.hostname || obs.ip || (obs.status === "absent" ? "Absent" : "Unknown")}</OverviewValue>
          <OverviewSub $tone={obsTone}>
            {obsHealthy ? "Reachable" : obs.status === "absent" ? "Not configured" : "Unreachable"}
          </OverviewSub>
        </OverviewBody>
      </OverviewCard>,
    );
  }

  return cards;
}

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
  const vipIp = (cluster.vip_ip || "").trim();
  const vipNode = (cluster.vip_node || "").trim();
  const promoted = (cluster.promoted || "").trim();
  const zimbraNode = (cluster.zimbra_node || "").trim();
  const vipOk = Boolean(vipIp && vipNode && promoted && vipNode === promoted);
  const zimbraOk = Boolean(promoted && zimbraNode && zimbraNode === promoted);
  return [
    {
      ok: Boolean(cluster.drbd_uptodate),
      label: cluster.drbd_uptodate
        ? "DRBD UpToDate"
        : typeof cluster.drbd_sync_percent === "number"
          ? `DRBD syncing (${cluster.drbd_sync_percent.toFixed(0)}% - normal after a fresh Build HA pair)`
          : "DRBD not UpToDate",
    },
    {
      ok: Boolean(cluster.qdevice_ok),
      label: cluster.qdevice_ok ? "qdevice voting" : "qdevice unhealthy",
    },
    {
      ok: cluster.observability?.status === "healthy",
      label:
        !cluster.observability
          ? "Observability unknown"
          : cluster.observability.status === "absent"
            ? "Observability absent"
            : cluster.observability.status === "healthy"
              ? "Observability reachable"
              : "Observability unreachable",
    },
    {
      ok: Boolean(cluster.failcount_ok),
      label: cluster.failcount_ok ? "fail-count 0" : "fail-count nonzero",
    },
    {
      ok: Boolean(promoted) && !cluster.promoted_conflict,
      label: cluster.promoted_conflict
        ? `Promoted conflict: ${(cluster.promoted_names || []).join(", ") || "multiple"}`
        : promoted
          ? `Promoted: ${promoted}`
          : "Promoted: unknown",
    },
    {
      ok: vipOk,
      label: !vipIp
        ? "VIP: not configured"
        : !vipNode
          ? "VIP: not Started"
          : vipOk
            ? `VIP on ${vipNode}`
            : `VIP on ${vipNode} (expected ${promoted || "Promoted"})`,
    },
    {
      ok: zimbraOk,
      label: !zimbraNode
        ? "kin-zimbra: not Started"
        : zimbraOk
          ? `kin-zimbra on ${zimbraNode}`
          : `kin-zimbra on ${zimbraNode} (expected ${promoted || "Promoted"})`,
    },
  ];
}

export default function ClusterPage() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const ops = isOpsRole(user?.role);
  const { startTask, endTaskWith } = useTasks();
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
  const [pendingCleanup, setPendingCleanup] = useState(false);
  const [pendingFailback, setPendingFailback] = useState<string | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const probeRef = useRef<EventSource | null>(null);
  const refreshInFlightRef = useRef(false);
  const busyRef = useRef(false);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    try {
      const st = await api<StatusResp>("/api/cluster/status");
      setCluster(st.cluster || {});
      if (!esRef.current) setLog(st.log || "");
      setLastUpdated(Date.now());
    } finally {
      refreshInFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

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

  useEffect(() => {
    if (tab !== "status") return;
    const id = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (esRef.current || busyRef.current || refreshInFlightRef.current) return;
      void refresh().catch(() => undefined);
    }, 12000);
    return () => window.clearInterval(id);
  }, [tab, refresh]);

  async function manualRefresh() {
    setRefreshing(true);
    try {
      await refresh();
    } catch (err: unknown) {
      setMessage(err instanceof Error ? err.message : "Failed to load cluster status");
    } finally {
      setRefreshing(false);
    }
  }

  const TASK_TITLES: Record<string, string> = {
    preflight: "Pre-flight check",
    enter: "Enter maintenance",
    exit: "Exit maintenance",
    cleanup: "Clear fail-counts",
    failback: "Move Master",
  };

  function runStream(op: "preflight" | "enter" | "exit" | "cleanup" | "failback", target: string) {
    esRef.current?.close();
    const taskId = startTask(TASK_TITLES[op] || op, target || undefined);
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
          } else if (op === "cleanup") {
            setMessage(
              code === 0
                ? "Cleared stale Pacemaker fail-counts."
                : "Could not clear fail-counts - see the log below.",
            );
          } else if (op === "failback") {
            setMessage(
              code === 0
                ? `Master moved to ${target}. Mail VIP should be on that node.`
                : `Master move to ${target} did not finish - see the log below. If a temporary ban is still set, on either mail node run: pcs resource clear kin-drbd-clone`,
            );
          } else {
            setMessage(code === 0 ? `${target} left maintenance.` : `Exit maintenance not fully verified for ${target}.`);
          }
          endTaskWith(
            taskId,
            code === 0 ? "done" : "failed",
            code === 0 ? undefined : `exit ${code}`,
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
        setMessage("Lost connection to the maintenance stream.");
        endTaskWith(taskId, "failed", "lost connection to the stream");
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
      setAddRoot("");
      setAddKin("");
      setAddOpen(false);
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
    const isLocal = hostsMatch(node, cluster.local_host);
    const pfForThis = preflightTarget === node ? preflight : null;
    const pfFailed = pfForThis ? Object.values(pfForThis).some((c) => !c.ok) : false;
    const pfPassed = !!pfForThis && !pfFailed;
    const ip = ipForNode(node, cluster.node_ips, cluster.local_host);
    const isVip = Boolean(cluster.vip_ip) && cluster.vip_node === node;
    const canFailback =
      ops &&
      !isOffline &&
      !isStandby &&
      !isPromoted &&
      Boolean(cluster.promoted) &&
      !cluster.promoted_conflict &&
      !cluster.maintenance_active &&
      Boolean(cluster.qdevice_ok);
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
          <KvValue>
            {isOffline
              ? "Unreachable"
              : isPromoted
                ? "Serving mail"
                : "Replica"}
          </KvValue>
          <KvLabel>IP address</KvLabel>
          <KvValue $mono={Boolean(ip)}>{ip || "Unknown"}</KvValue>
          <KvLabel>Mail VIP</KvLabel>
          <KvValue $mono={isVip}>{isVip ? cluster.vip_ip : "-"}</KvValue>
          <KvLabel>Maintenance</KvLabel>
          <KvValue>{isStandby ? "On" : "Off"}</KvValue>
          {isLocal ? (
            <>
              <KvLabel>Console</KvLabel>
              <KvValue>This session</KvValue>
            </>
          ) : null}
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
                      disabled={
                        busy ||
                        isLocal ||
                        !pfPassed ||
                        (!!cluster.maintenance_active && !isStandby)
                      }
                      onClick={() => runStream("enter", node)}
                    >
                      Enter Maintenance
                    </Button>
                  )}
                  {canFailback ? (
                    <Button
                      type="button"
                      variant="secondary"
                      disabled={busy || !cluster.drbd_uptodate}
                      onClick={() => setPendingFailback(node)}
                    >
                      Move Master here
                    </Button>
                  ) : null}
                </PrepButtons>
                {isLocal && !isStandby ? (
                  <CardHint>
                    This console is on this host. Enter Maintenance and Remove Host
                    are only available from the peer console.
                  </CardHint>
                ) : null}
                {!isLocal && !isStandby && !pfPassed ? (
                  <CardHint>
                    {pfFailed
                      ? "All checks must pass before you can enter maintenance."
                      : "Run Check first to enable Enter Maintenance."}
                  </CardHint>
                ) : null}
                {canFailback && !cluster.drbd_uptodate ? (
                  <CardHint>
                    Wait for DRBD to finish syncing
                    {typeof cluster.drbd_sync_percent === "number"
                      ? ` (${cluster.drbd_sync_percent.toFixed(0)}%)`
                      : ""}{" "}
                    before Move Master here.
                  </CardHint>
                ) : null}
              </PrepCluster>
            ) : null}
            <RemoveCluster $solo={isOffline}>
              <Button
                type="button"
                variant="danger"
                disabled={
                  busy ||
                  probing ||
                  isLocal ||
                  (!isOffline && !isStandby)
                }
                onClick={() => openRemove(node)}
              >
                Remove Host
              </Button>
              {isLocal ? (
                <CardHint>Cannot remove the host serving this console.</CardHint>
              ) : !isOffline && !isStandby ? (
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
          subtitle="Take the peer mail node offline for planned work, or move the Master back after failover. You cannot Enter Maintenance or Remove Host on the server serving this console."
        />
        {cluster.quorum_hint ? (
          <WarnBox role="alert">
            <strong>
              {cluster.no_quorum_policy === "ignore"
                ? "Running without redundancy: this node is serving mail alone."
                : "Mail is stopped on this node: the cluster has lost quorum."}
            </strong>
            {typeof cluster.votes_total === "number" &&
            typeof cluster.votes_needed === "number" ? (
              <> This node holds {cluster.votes_total} of the {cluster.votes_needed} votes
              it needs.</>
            ) : null}{" "}
            {cluster.quorum_hint}
          </WarnBox>
        ) : null}
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
            <>
              <Skeleton $h="2rem" $w="12rem" style={{ marginBottom: 14 }} />
              <OverviewGrid>
                {Array.from({ length: 6 }).map((_, i) => (
                  <SkeletonCard key={i}>
                    <Skeleton $h="0.7rem" $w="45%" style={{ marginBottom: 10 }} />
                    <Skeleton $h="1.35rem" $w="70%" style={{ marginBottom: 8 }} />
                    <Skeleton $h="0.75rem" $w="55%" />
                  </SkeletonCard>
                ))}
              </OverviewGrid>
            </>
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
                  {ops && cluster.failcount_ok === false ? (
                    <MenuItem
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setHealthOpen(false);
                        setPendingCleanup(true);
                      }}
                    >
                      Clear fail-counts (re-probe)
                    </MenuItem>
                  ) : null}
                </Dropdown>
              </HealthWrap>
              <OverviewHeader>
                <OverviewTitle>Cluster Overview</OverviewTitle>
                <FreshnessRow>
                  <FreshnessBadge updatedAt={lastUpdated} />
                  <RefreshBtn
                    type="button"
                    aria-label="Refresh cluster status"
                    disabled={refreshing || busy}
                    onClick={() => void manualRefresh()}
                  >
                    <RefreshGlyph />
                  </RefreshBtn>
                </FreshnessRow>
              </OverviewHeader>
              <OverviewGrid>{clusterOverviewCards(cluster, topology)}</OverviewGrid>
              {topology === "1vm" ? (
                <>
                  <ClusterTopology
                    topology="1vm"
                    mailNodes={mailTopo}
                    observability={{ status: "absent" }}
                    ops={ops}
                    busy={busy}
                    onAdd={() => navigate("/cluster/add-second-server")}
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
          open={pendingCleanup}
          title="Clear fail-counts"
          message={
            <>
              Run <strong>pcs resource cleanup</strong> on this cluster. That clears
              sticky fail-count / last-failure history and re-probes every resource.
              A resource that is still broken may stop or restart during the re-probe.
            </>
          }
          detail="Refused automatically if crm_mon shows dual Promoted or the dual-primary assert fails. Prefer this only after a known transient probe race, not to paper over split-brain."
          confirmLabel="Re-probe resources"
          loading={busy}
          onCancel={() => {
            if (busy) return;
            setPendingCleanup(false);
          }}
          onConfirm={() => {
            if (busy) return;
            setPendingCleanup(false);
            runStream("cleanup", "");
          }}
        />
        <ConfirmModal
          open={!!pendingFailback}
          title="Move Master here"
          message={
            <>
              Move mail service (DRBD Master, Zimbra, and VIP) to{" "}
              <strong>{pendingFailback}</strong>. The console waits for DRBD
              sync to finish, then bans the current Promoted node, waits until
              this node is Promoted with VIP and HTTPS healthy, and clears the
              temporary ban.
            </>
          }
          detail="Expect several minutes of mail downtime while Zimbra restarts on the target. This is active-passive: there is no zero-downtime path. Stickiness keeps the new Master after the ban is cleared (no automatic bounce back)."
          confirmLabel="Move Master"
          loading={busy}
          countdownSeconds={5}
          onCancel={() => {
            if (busy) return;
            setPendingFailback(null);
          }}
          onConfirm={() => {
            if (!pendingFailback || busy) return;
            const target = pendingFailback;
            setPendingFailback(null);
            runStream("failback", target);
          }}
        />
        <ConfirmModal
          open={!!pendingRemove}
          title={
            removeProbe?.mode === "forced"
              ? "Remove unreachable host"
              : "Remove host"
          }
          message={
            probing ? (
              <>Probing SSH reachability for <strong>{pendingRemove}</strong>...</>
            ) : (removeProbe?.errors || []).length > 0 ? (
              <>{(removeProbe?.errors || []).join(" ")}</>
            ) : !removeProbe ? (
              <>Probe did not return a result. Cancel and try again.</>
            ) : removeProbe.mode === "forced" ? (
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
              ? "You can cancel while probing."
              : (removeProbe?.errors || []).length > 0
                ? (removeProbe?.notes || []).join(" ") || undefined
                : removeProbe?.mode === "forced"
                  ? "A failed forced remove leaves stale references that also block Add Host. Survivor-side cleanup is the part that must succeed."
                  : "This is harder to reverse than maintenance mode. If uninstall on the departing node fails, survivor-side removal still counts as success."
          }
          confirmLabel={probing ? "Probing..." : "Remove host"}
          loading={busy}
          confirmDisabled={
            probing ||
            !removeProbe ||
            (removeProbe.errors || []).length > 0 ||
            !!removeProbe.local_is_target
          }
          countdownSeconds={
            probing ||
            (removeProbe?.errors || []).length > 0 ||
            !removeProbe ||
            removeProbe.local_is_target
              ? 0
              : 5
          }
          onCancel={() => {
            if (busy) return;
            probeRef.current?.close();
            probeRef.current = null;
            setProbing(false);
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
              <>Probing whether Observability still answers...</>
            ) : (obsRemoveProbe?.errors || []).length > 0 ? (
              <>{(obsRemoveProbe?.errors || []).join(" ")}</>
            ) : !obsRemoveProbe ? (
              <>Probe did not return a result. Cancel and try again.</>
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
          detail={
            probing
              ? "You can cancel while probing."
              : "This is a forced path only. A healthy Observability node cannot be removed. Dual-self-fence risk stays until Add Observability completes."
          }
          confirmLabel={probing ? "Probing..." : "Remove Observability"}
          loading={busy}
          confirmDisabled={
            probing || !obsRemoveProbe || (obsRemoveProbe.errors || []).length > 0
          }
          countdownSeconds={probing || (obsRemoveProbe?.errors || []).length > 0 || !obsRemoveProbe ? 0 : 5}
          onCancel={() => {
            if (busy) return;
            probeRef.current?.close();
            probeRef.current = null;
            setProbing(false);
            setPendingObsRemove(false);
            setObsRemoveProbe(null);
          }}
          onConfirm={() => {
            if (probing || busy) return;
            if (!obsRemoveProbe || (obsRemoveProbe.errors || []).length > 0) return;
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
