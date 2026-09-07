import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { isOpsRole, useAuth } from "../auth";
import { theme } from "../styles/theme";
import { Button, Hint, LogPane, Skeleton, SkeletonCard, WarnBox } from "../ui";
import {
  areaPath,
  formatBytes,
  formatDuration,
  formatValue,
  latestValue,
  linePaths,
  localZoneLabel,
  nearestIndexInDomain,
  pointTimeLabel,
  seriesStats,
  xFraction,
  timeTicks,
  usageTone,
  yBounds,
  type Point,
  yTicks,
  yFraction,
  thresholdsFor,
} from "./chart";

type Series = { instance: string; points: Point[] };
type SeriesResp = {
  metric: string;
  label: string;
  unit: string;
  range: string;
  start?: number;
  end?: number;
  series: Series[];
};
type CatalogueResp = { metrics: { metric: string; label: string; unit: string }[]; ranges: string[]; default_range: string };
// One request for every chart, sharing a single clock so the cards cannot
// drift onto slightly different x-axes.
type SeriesBatchResp = {
  range: string;
  start?: number;
  end?: number;
  step?: number;
  charts: { metric: string; label: string; unit: string; series: Series[] }[];
};

const RANGE_LABELS: Record<string, string> = {
  now: "Now (live)",
  "1h": "1 hour",
  "6h": "6 hours",
  "24h": "24 hours",
  "7d": "7 days",
  "30d": "30 days",
  "1y": "1 year",
};

// Ordered so the pair you compare sits side by side: CPU next to what it is
// waiting on, disk throughput next to how busy the disk actually is, and
// network throughput next to the errors that show the link giving up.
// Grouped the way an operator triages: is mail moving, is the machine coping,
// is the link healthy, is there room on disk. A flat wall of thirteen
// identical cards made every one of them equally easy to miss.
type ChartGroup = { title: string; blurb: string; metrics: string[] };

const CHART_GROUPS: ChartGroup[] = [
  {
    title: "Mail flow",
    blurb:
      "What the mail server is actually doing. Everything below this describes " +
      "the machine; this describes the mail.",
    metrics: [
      "mail_received",
      "mail_delivered_in",
      "mail_delivered_out",
      "mail_rejected",
      "mail_deferred",
      "mail_bounced",
      "mail_queue",
      "mail_queue_oldest",
    ],
  },
  {
    title: "Processor and memory",
    blurb: "CPU next to what it is waiting on, so a busy machine reads differently from a stuck one.",
    metrics: ["cpu", "cpu_iowait", "memory", "swap_used", "load1", "load5"],
  },
  {
    title: "Network",
    blurb: "Throughput next to the errors that show a link giving up before DRBD notices.",
    metrics: ["net_rx", "net_tx", "net_errors", "tcp_established"],
  },
  {
    title: "Storage",
    blurb: "Throughput next to how busy the disk actually is.",
    metrics: ["disk_read", "disk_write", "disk_busy"],
  },
];

const CHARTS = CHART_GROUPS.flatMap((g) => g.metrics);

const Bar = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin: 0 0 1.1rem;
`;

/* Pushed to the end of the bar so the range buttons keep their position when
   the hint text wraps at a narrow width. */
const ExportSlot = styled.div`
  margin-left: auto;
`;

const RangeGroup = styled.div`
  display: inline-flex;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  overflow: hidden;
  background: ${theme.bgElev};
`;

const RangeBtn = styled.button<{ $on: boolean }>`
  border: 0;
  border-right: 1px solid ${theme.line};
  background: ${(p) => (p.$on ? theme.accent : "transparent")};
  color: ${(p) => (p.$on ? theme.paper : theme.surface[600])};
  font: inherit;
  font-size: 0.78rem;
  font-weight: 600;
  padding: 0.4rem 0.75rem;
  cursor: pointer;

  &:last-of-type {
    border-right: 0;
  }
  &:hover {
    background: ${(p) => (p.$on ? theme.accent : theme.surface[50])};
  }
`;

const Grid = styled.div`
  display: grid;
  gap: 0.85rem;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  margin: 0 0 1.15rem;
`;

const ChartGrid = styled.div`
  display: grid;
  gap: 0.85rem;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
`;

const Card = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1rem 1.15rem;
  box-shadow: ${theme.shadow.sm};
  min-width: 0;
`;

const CardLabel = styled.p`
  margin: 0 0 0.35rem;
  font-size: 0.72rem;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: ${theme.muted};
`;

const BigValue = styled.p<{ $tone: "ok" | "warn" | "danger" }>`
  margin: 0;
  font-family: ${theme.mono};
  font-size: 1.4rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: ${(p) =>
    p.$tone === "danger" ? theme.oxide : p.$tone === "warn" ? theme.warn : theme.ink};
`;

const Track = styled.div`
  margin-top: 0.6rem;
  height: 0.4rem;
  border-radius: ${theme.radius.full};
  background: ${theme.surface[200]};
  overflow: hidden;
`;

const Fill = styled.div<{ $pct: number; $tone: "ok" | "warn" | "danger" }>`
  height: 100%;
  width: ${(p) => Math.max(0, Math.min(100, p.$pct))}%;
  border-radius: ${theme.radius.full};
  background: ${(p) =>
    p.$tone === "danger" ? theme.danger : p.$tone === "warn" ? theme.warn : theme.ok};
  transition: width 500ms ease;
`;

const ChartHead = styled.div`
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
  margin: 0 0 0.5rem;
`;

const ChartNow = styled.span`
  font-family: ${theme.mono};
  font-size: 0.88rem;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  color: ${theme.ink};
`;

const Stats = styled.div`
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
  margin-top: 0.35rem;
  font-size: 0.72rem;
  color: ${theme.muted};

  b {
    color: ${theme.ink};
    font-family: ${theme.mono};
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }
`;

const Section = styled.section`
  margin-top: 26px;
  &:first-of-type {
    margin-top: 4px;
  }
`;

const SectionHead = styled.div`
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin: 0 0 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid ${theme.line};
`;

const SectionTitle = styled.h3`
  margin: 0;
  font-size: 0.9rem;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: ${theme.ink};
`;

const SectionBlurb = styled.p`
  margin: 0;
  font-size: 0.75rem;
  color: ${theme.muted};
`;

/* Wraps the svg ALONE. An earlier version put the y labels over the whole
   Plot, which also contains the time axis and the peak/low/avg row - so the
   bottom label landed on top of the statistics. */
const PlotArea = styled.div`
  position: relative;
  line-height: 0;
`;

const YAxis = styled.div`
  position: absolute;
  inset: 0;
  pointer-events: none;
`;

/* Left edge, not right: these sit over the oldest few pixels of the trace
   rather than over the newest, which is the part being read. */
const YTick = styled.span<{ $at: number }>`
  position: absolute;
  left: 0;
  top: ${(p) => p.$at * 100}%;
  transform: translateY(${(p) => (p.$at === 0 ? "0" : p.$at === 1 ? "-100%" : "-50%")});
  font-size: 0.62rem;
  font-variant-numeric: tabular-nums;
  color: ${theme.muted};
  background: ${theme.bgElev};
  padding: 0 3px;
  border-radius: 3px;
  line-height: 1.15;
  opacity: 0.92;
`;

const Axis = styled.div`
  position: relative;
  height: 1rem;
  margin-top: 0.2rem;
`;

/* The install action sits inside the warning that explains why it is there,
   so the button and its consequences read as one thing. */
const InstallRow = styled.div`
  display: flex;
  align-items: flex-start;
  gap: 0.85rem;
  margin-top: 0.75rem;
  flex-wrap: wrap;

  span {
    flex: 1 1 22rem;
    min-width: 0;
    font-size: 0.85rem;
    line-height: 1.5;
    color: ${theme.surface[600]};
  }
`;

const Tick = styled.span<{ $at: number }>`
  position: absolute;
  left: ${(p) => p.$at * 100}%;
  transform: translateX(${(p) => (p.$at === 0 ? "0" : p.$at === 1 ? "-100%" : "-50%")});
  font-size: 0.68rem;
  /* Axis times are read, not decoration. surface[400] measures 2.82 on a
     card and misses AA for text this small. */
  color: ${theme.muted};
  white-space: nowrap;
`;

const Empty = styled.p`
  margin: 0;
  padding: 1.5rem 0;
  text-align: center;
  font-size: 0.8rem;
  color: ${theme.muted};
`;

const HostGrid = styled.div`
  display: grid;
  gap: 0.85rem;
  grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
  margin: 0 0 1.15rem;
`;

const Kv = styled.dl`
  margin: 0.4rem 0 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.3rem 0.75rem;
  font-size: 0.8rem;
`;

const K = styled.dt`
  margin: 0;
  color: ${theme.muted};
  white-space: nowrap;
`;

const V = styled.dd`
  margin: 0;
  text-align: right;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  color: ${theme.surface[800]};
`;

const Model = styled.p`
  margin: 0.3rem 0 0;
  font-size: 0.76rem;
  line-height: 1.4;
  color: ${theme.muted};
  word-break: break-word;
`;

function HostPanel({ host }: { host: HostFacts }) {
  const mail = host.disks.find((d) => d.mount !== "/");
  const system = host.disks.find((d) => d.mount === "/");
  const mem = host.memory;
  return (
    <HostGrid>
      <Card>
        <CardLabel>Processor</CardLabel>
        <BigValue $tone="ok">
          {host.cpu.cores || host.cpu.threads || "-"} core
          {(host.cpu.cores || host.cpu.threads) === 1 ? "" : "s"}
        </BigValue>
        {host.cpu.threads && host.cpu.threads !== host.cpu.cores ? (
          <Kv>
            <K>Threads</K>
            <V>{host.cpu.threads}</V>
          </Kv>
        ) : null}
        {host.cpu.model ? <Model>{host.cpu.model}</Model> : null}
      </Card>

      <Card>
        <CardLabel>Memory</CardLabel>
        <BigValue $tone={usageTone(mem.percent, "percent")}>
          {mem.percent === null ? "-" : `${mem.percent.toFixed(1)}%`}
        </BigValue>
        <Kv>
          <K>Used</K>
          <V>{formatBytes(mem.used_bytes)}</V>
          <K>Total</K>
          <V>{formatBytes(mem.total_bytes)}</V>
          <K>Available</K>
          <V>{formatBytes(mem.available_bytes)}</V>
        </Kv>
      </Card>

      {[
        { label: "Mail storage", d: mail },
        { label: "System disk", d: system },
      ].map(({ label, d }) =>
        d ? (
          <Card key={label}>
            <CardLabel>{label}</CardLabel>
            <BigValue $tone={usageTone(d.percent, "percent")}>
              {d.percent.toFixed(1)}%
            </BigValue>
            <Track>
              <Fill $pct={d.percent} $tone={usageTone(d.percent, "percent")} />
            </Track>
            <Kv>
              <K>Used</K>
              <V>{formatBytes(d.used_bytes)}</V>
              <K>Free</K>
              <V>{formatBytes(d.free_bytes)}</V>
              <K>Total</K>
              <V>{formatBytes(d.total_bytes)}</V>
            </Kv>
            <Model>{d.mount}</Model>
          </Card>
        ) : null,
      )}

      <Card>
        <CardLabel>Uptime</CardLabel>
        <BigValue $tone="ok">
          {host.uptime_seconds === null ? "-" : formatDuration(host.uptime_seconds)}
        </BigValue>
        {host.cpu.load.length === 3 ? (
          <Kv>
            <K>Load 1 / 5 / 15m</K>
            <V>{host.cpu.load.join("  ")}</V>
          </Kv>
        ) : null}
      </Card>
    </HostGrid>
  );
}

const W = 520;
const H = 120;

type HostFacts = {
  cpu: { model: string; threads: number; cores: number; load: number[] };
  memory: { total_bytes: number; used_bytes: number; available_bytes: number; percent: number | null };
  disks: { mount: string; total_bytes: number; used_bytes: number; free_bytes: number; percent: number }[];
  uptime_seconds: number | null;
};

const Tip = styled.div<{ $left: number }>`
  position: absolute;
  top: 0;
  left: ${(p) => p.$left * 100}%;
  transform: translateX(${(p) => (p.$left > 0.75 ? "-100%" : p.$left < 0.25 ? "0" : "-50%")});
  pointer-events: none;
  background: ${theme.ink};
  color: ${theme.paper};
  border-radius: ${theme.radius.sm};
  padding: 0.3rem 0.5rem;
  font-size: 0.72rem;
  line-height: 1.35;
  white-space: nowrap;
  z-index: 3;
  box-shadow: ${theme.shadow.md};
`;

const Plot = styled.div`
  position: relative;
`;

const Crosshair = styled.div<{ $left: number }>`
  position: absolute;
  top: 0;
  bottom: 0;
  left: ${(p) => p.$left * 100}%;
  width: 1px;
  background: ${theme.surface[300]};
  pointer-events: none;
`;

function Chart({ data }: { data: SeriesResp }) {
  const series = data.series[0];
  const points = series?.points || [];
  const domain =
    typeof data.start === "number" && typeof data.end === "number"
      ? { start: data.start, end: data.end }
      : undefined;
  const bounds = yBounds(points, data.unit);
  const segments = linePaths(points, { width: W, height: H }, bounds, domain);
  const fill = areaPath(points, { width: W, height: H }, bounds, domain);
  const now = latestValue(points);
  const ticks = timeTicks(points, 4, domain);
  const stats = seriesStats(points);
  const gid = `mon-grad-${data.metric}`;
  const yLabels = yTicks(bounds, data.unit);
  const bands = thresholdsFor(data.unit, data.metric);
  const hasData = segments.length > 0;
  const [hover, setHover] = useState<number | null>(null);
  const longRange = data.range === "7d" || data.range === "30d" || data.range === "1y";
  const hoverIdx = hover === null ? -1 : nearestIndexInDomain(points, hover, domain);
  const hoverPoint = hoverIdx >= 0 ? points[hoverIdx] : null;
  const hoverAt = hoverPoint
    ? (xFraction(hoverPoint[0], domain) ?? hoverIdx / Math.max(1, points.length - 1))
    : 0;

  // The point the operator is reading: the hovered sample, or the latest one.
  const markerValue = hoverPoint ? hoverPoint[1] : now;
  const markerX = hoverPoint
    ? hoverAt
    : points.length
      ? (xFraction(points[points.length - 1][0], domain) ?? 1)
      : 1;

  return (
    <Card>
      <ChartHead>
        <CardLabel style={{ margin: 0 }}>{data.label}</CardLabel>
        <ChartNow>
          {hoverPoint ? formatValue(hoverPoint[1], data.unit) : formatValue(now, data.unit)}
        </ChartNow>
      </ChartHead>
      {hasData ? (
        <Plot
          onMouseMove={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            if (r.width > 0) setHover((e.clientX - r.left) / r.width);
          }}
          onMouseLeave={() => setHover(null)}
        >
          {hoverPoint ? (
            <>
              <Crosshair $left={hoverAt} />
              <Tip $left={hoverAt}>
                <strong>{formatValue(hoverPoint[1], data.unit)}</strong>
                {"  "}
                {pointTimeLabel(hoverPoint[0], longRange)}
              </Tip>
            </>
          ) : null}
          <PlotArea>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            width="100%"
            preserveAspectRatio="none"
            role="img"
            aria-label={`${data.label} over the last ${RANGE_LABELS[data.range] || data.range}`}
          >
            <defs>
              <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={theme.accent} stopOpacity="0.22" />
                <stop offset="100%" stopColor={theme.accent} stopOpacity="0" />
              </linearGradient>
            </defs>
            {/* Threshold context, drawn behind the trace. A CPU at 95% should
                read as "in the red" without anyone parsing the axis. */}
            {bands.map((b) => {
              const top = yFraction(b.from, bounds) * H;
              if (top >= H) return null;
              return (
                <rect
                  key={`${b.tone}-${b.from}`}
                  x={0}
                  y={0}
                  width={W}
                  height={Math.max(0, top)}
                  fill={b.tone === "danger" ? theme.danger : theme.warn}
                  opacity={b.tone === "danger" ? 0.09 : 0.07}
                />
              );
            })}
            {[0.25, 0.5, 0.75].map((f) => (
              <line
                key={f}
                x1={0}
                x2={W}
                y1={H * f}
                y2={H * f}
                stroke={theme.surface[200]}
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {fill ? <path d={fill} fill={`url(#${gid})`} /> : null}
            {segments.map((d, i) => (
              <path
                key={i}
                d={d}
                fill="none"
                stroke={theme.accent}
                strokeWidth={1.75}
                strokeLinejoin="round"
                strokeLinecap="round"
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {/* A dot on the value being read. Without it the crosshair says
                which moment, and nothing says which point on the line. */}
            {markerValue !== null && markerValue !== undefined ? (
              <circle
                cx={markerX * W}
                cy={yFraction(markerValue, bounds) * H}
                r={3}
                fill={theme.accent}
                stroke={theme.bgElev}
                strokeWidth={1.5}
                vectorEffect="non-scaling-stroke"
              />
            ) : null}
          </svg>
            <YAxis aria-hidden="true">
              {yLabels.map((t) => (
                <YTick key={t.at} $at={t.at}>
                  {t.label}
                </YTick>
              ))}
            </YAxis>
          </PlotArea>
          <Axis>
            {ticks.map((t, i) => (
              <Tick key={i} $at={t.at}>
                {t.label}
              </Tick>
            ))}
          </Axis>
          <Stats>
            <span>
              Peak <b>{formatValue(stats.max, data.unit)}</b>
            </span>
            <span>
              Low <b>{formatValue(stats.min, data.unit)}</b>
            </span>
            <span>
              Avg <b>{formatValue(stats.avg, data.unit)}</b>
            </span>
          </Stats>
        </Plot>
      ) : (
        <Empty>No samples in this window yet.</Empty>
      )}
    </Card>
  );
}

export function MonitoringTab() {
  const [range, setRange] = useState("1h");
  const [ranges, setRanges] = useState<string[]>(["now", "1h", "6h", "24h", "7d", "30d", "1y"]);
  const [charts, setCharts] = useState<SeriesResp[]>([]);
  const [host, setHost] = useState<HostFacts | null>(null);
  const [loading, setLoading] = useState(true);
  const { user } = useAuth();
  // Install is a privhelper command gated to the ops roles. Showing the button
  // to a Customer Admin would only earn them a 403 from the stream.
  const canInstall = isOpsRole(user?.role);
  // The loader is a useCallback with no deps, so it cannot close over `user`
  // without going stale on the first sign-in.
  const userRef = useRef(user);
  userRef.current = user;
  const [error, setError] = useState("");
  // Installing the metrics stack from here. A single-server appliance never
  // got it during deploy before 7 Sep 2026, and there was no way to add it
  // afterwards without a full redeploy the appliance refuses to run.
  const [installing, setInstalling] = useState(false);
  const [installLog, setInstallLog] = useState("");
  const [installDone, setInstallDone] = useState<"" | "ok" | "failed">("");
  const [unavailable, setUnavailable] = useState(false);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    const loadHost = () =>
      api<HostFacts>("/api/monitoring/host")
        .then((h) => {
          if (alive.current) setHost(h);
        })
        .catch(() => undefined);
    loadHost();
    // Point-in-time facts: cheap, and disk usage is the one people watch.
    const id = window.setInterval(loadHost, 30000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    api<CatalogueResp>("/api/monitoring/catalogue")
      .then((res) => {
        if (!alive.current) return;
        if (res.ranges?.length) setRanges(res.ranges);
        if (res.default_range) setRange((r) => (r === "1h" ? res.default_range : r));
      })
      .catch(() => undefined);
  }, []);

  const load = useCallback(async (window: string) => {
    setError("");
    try {
      const batch = await api<SeriesBatchResp>(
        `/api/monitoring/series-batch?metrics=${encodeURIComponent(CHARTS.join(","))}` +
          `&range=${encodeURIComponent(window)}`,
      );
      if (!alive.current) return;
      const good: SeriesResp[] = (batch.charts || [])
        .filter((c) => (c.series || []).length > 0)
        .map((c) => ({
          metric: c.metric,
          label: c.label,
          unit: c.unit,
          range: batch.range,
          start: batch.start,
          end: batch.end,
          series: c.series,
        }));
      setCharts(good);
      setUnavailable(good.length === 0);
      if (good.length === 0) {
        setError(
          isOpsRole(userRef.current?.role)
            ? "This appliance is not collecting metrics yet. Install monitoring " +
              "below adds Prometheus and the mail flow collector on this node. " +
              "Figures start from the moment it runs; nothing is back-filled."
            : "This appliance is not collecting metrics yet. Ask KIN Support-Ops " +
              "or a KIN Super Admin to add monitoring from this tab.",
        );
      }
    } catch (err: unknown) {
      if (alive.current) setError(err instanceof Error ? err.message : "Could not load metrics");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  /* Streams the same privhelper command Build HA pair runs as its metrics
     step, against this node only. Additive: it installs packages and writes
     loopback config, and touches no cluster or mail state. */
  const installMonitoring = useCallback(() => {
    setInstalling(true);
    setInstallDone("");
    setInstallLog("");
    const es = new EventSource("/api/wizard/deploy/stream?action=install_monitoring");
    let code: number | null = null;
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as {
          type?: string;
          data?: string;
          exit_code?: number;
          message?: string;
        };
        if ((data.type === "stdout" || data.type === "stderr") && data.data) {
          setInstallLog((prev) => (prev + data.data).slice(-8000));
        } else if (data.type === "error") {
          setInstallLog((prev) => prev + `[error] ${data.message || ""}\n`);
        } else if (data.type === "done") {
          code = typeof data.exit_code === "number" ? data.exit_code : 1;
          es.close();
          setInstalling(false);
          setInstallDone(code === 0 ? "ok" : "failed");
        }
      } catch {
        /* ignore malformed SSE */
      }
    };
    es.onerror = () => {
      es.close();
      setInstalling(false);
      if (code === null) {
        setInstallDone("failed");
        setInstallLog((prev) => prev + "Lost connection to the install stream.\n");
      }
    };
  }, []);

  useEffect(() => {
    setLoading(true);
    void load(range);
    // Short windows are live; a year of history does not need re-fetching often.
    // When there is no Prometheus at all, back right off: polling every 30s
    // fires a dozen failing requests a minute for as long as the tab is open.
    // "Now" is a live view: refresh on the same cadence as its step so the
    // trace actually moves while you watch it.
    const period = unavailable
      ? 300000
      : range === "now"
        ? 10000
        : range === "1h"
          ? 30000
          : range === "6h"
            ? 60000
            : 300000;
    const id = window.setInterval(() => void load(range), period);
    return () => window.clearInterval(id);
  }, [range, load, unavailable]);

  if (loading && charts.length === 0 && !error) {
    return (
      <Grid>
        <SkeletonCard>
          <Skeleton $h="0.7rem" $w="45%" style={{ marginBottom: 12 }} />
          <Skeleton $h="1.6rem" $w="35%" />
        </SkeletonCard>
        <SkeletonCard>
          <Skeleton $h="0.7rem" $w="45%" style={{ marginBottom: 12 }} />
          <Skeleton $h="1.6rem" $w="35%" />
        </SkeletonCard>
      </Grid>
    );
  }

  return (
    <>
      <Bar>
        <RangeGroup role="group" aria-label="Time range">
          {ranges.map((r) => (
            <RangeBtn key={r} type="button" $on={r === range} onClick={() => setRange(r)}>
              {RANGE_LABELS[r] || r}
            </RangeBtn>
          ))}
        </RangeGroup>
        <Hint style={{ margin: 0 }}>
          Times shown in {localZoneLabel()}. Collected on this server and kept for a year;
          metrics never leave the appliance.
        </Hint>
        <ExportSlot>
          <Button
            type="button"
            variant="ghost"
            disabled={charts.length === 0}
            onClick={() => {
              // Plain navigation: the browser handles the download and takes
              // the filename from Content-Disposition.
              window.location.href =
                `/api/monitoring/series.csv?metrics=${encodeURIComponent(CHARTS.join(","))}` +
                `&range=${encodeURIComponent(range)}`;
            }}
          >
            Export CSV
          </Button>
        </ExportSlot>
      </Bar>

      {error ? (
        <WarnBox>
          {error}
          {unavailable && canInstall ? (
            <InstallRow>
              <Button
                type="button"
                variant="primary"
                loading={installing}
                disabled={installing}
                onClick={installMonitoring}
              >
                Install monitoring
              </Button>
              <span>
                Adds Prometheus, node_exporter and the mail flow collector on
                this node. All three listen on loopback only and are read
                through this console, so nothing new is exposed. Mail keeps
                running throughout.
              </span>
            </InstallRow>
          ) : null}
        </WarnBox>
      ) : null}

      {installLog ? (
        <>
          <LogPane>{installLog}</LogPane>
          {installDone === "ok" ? (
            <Hint>
              Monitoring installed. Charts fill in as samples arrive, so give it
              a minute and then reload this tab.
            </Hint>
          ) : null}
          {installDone === "failed" ? (
            <Hint>
              The install did not finish. Mail is unaffected. The output above is
              the whole run.
            </Hint>
          ) : null}
        </>
      ) : null}

      {host ? <HostPanel host={host} /> : null}

      {CHART_GROUPS.map((group) => {
        const inGroup = charts.filter((c) => group.metrics.includes(c.metric));
        // A group with nothing behind it is hidden rather than shown empty:
        // mail flow metrics only exist once the collector has run, and an
        // appliance mid-deploy should not display eight blank cards.
        if (inGroup.length === 0) return null;
        return (
          <Section key={group.title}>
            <SectionHead>
              <SectionTitle>{group.title}</SectionTitle>
              <SectionBlurb>{group.blurb}</SectionBlurb>
            </SectionHead>
            <ChartGrid>
              {inGroup.map((c) => (
                <Chart key={c.metric} data={c} />
              ))}
            </ChartGrid>
          </Section>
        );
      })}
    </>
  );
}
