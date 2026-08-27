import { useCallback, useEffect, useRef, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { theme } from "../styles/theme";
import { Hint, Skeleton, SkeletonCard, WarnBox } from "../ui";
import {
  areaPath,
  formatValue,
  latestValue,
  linePaths,
  timeTicks,
  usageTone,
  yBounds,
  type Point,
} from "./chart";

type Series = { instance: string; points: Point[] };
type SeriesResp = { metric: string; label: string; unit: string; range: string; series: Series[] };
type CatalogueResp = { metrics: { metric: string; label: string; unit: string }[]; ranges: string[]; default_range: string };

const RANGE_LABELS: Record<string, string> = {
  "1h": "1 hour",
  "6h": "6 hours",
  "24h": "24 hours",
  "7d": "7 days",
  "30d": "30 days",
  "1y": "1 year",
};

/** Gauges first (current state), then the trend charts. */
const GAUGES = ["cpu", "memory", "disk_mail", "disk_root"];
const CHARTS = ["cpu", "memory", "net_rx", "net_tx", "disk_read", "disk_write", "load1"];

const Bar = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin: 0 0 1.1rem;
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
  color: ${(p) => (p.$on ? "#fff" : theme.surface[600])};
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
  font-size: 1.5rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: ${(p) =>
    p.$tone === "danger" ? theme.danger : p.$tone === "warn" ? theme.warn : theme.surface[800]};
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
  font-size: 0.95rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: ${theme.surface[800]};
`;

const Axis = styled.div`
  position: relative;
  height: 1rem;
  margin-top: 0.2rem;
`;

const Tick = styled.span<{ $at: number }>`
  position: absolute;
  left: ${(p) => p.$at * 100}%;
  transform: translateX(${(p) => (p.$at === 0 ? "0" : p.$at === 1 ? "-100%" : "-50%")});
  font-size: 0.68rem;
  color: ${theme.surface[400]};
  white-space: nowrap;
`;

const Empty = styled.p`
  margin: 0;
  padding: 1.5rem 0;
  text-align: center;
  font-size: 0.8rem;
  color: ${theme.muted};
`;

function Gauge({ label, unit, value }: { label: string; unit: string; value: number | null }) {
  const tone = usageTone(value, unit);
  return (
    <Card>
      <CardLabel>{label}</CardLabel>
      <BigValue $tone={tone}>{formatValue(value, unit)}</BigValue>
      {unit === "percent" ? (
        <Track>
          <Fill $pct={value ?? 0} $tone={tone} />
        </Track>
      ) : null}
    </Card>
  );
}

const W = 520;
const H = 120;

function Chart({ data }: { data: SeriesResp }) {
  const series = data.series[0];
  const points = series?.points || [];
  const bounds = yBounds(points, data.unit);
  const segments = linePaths(points, { width: W, height: H }, bounds);
  const fill = areaPath(points, { width: W, height: H }, bounds);
  const now = latestValue(points);
  const ticks = timeTicks(points, 4);
  const gid = `mon-grad-${data.metric}`;
  const hasData = segments.length > 0;

  return (
    <Card>
      <ChartHead>
        <CardLabel style={{ margin: 0 }}>{data.label}</CardLabel>
        <ChartNow>{formatValue(now, data.unit)}</ChartNow>
      </ChartHead>
      {hasData ? (
        <>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            width="100%"
            height="auto"
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
          </svg>
          <Axis>
            {ticks.map((t, i) => (
              <Tick key={i} $at={t.at}>
                {t.label}
              </Tick>
            ))}
          </Axis>
        </>
      ) : (
        <Empty>No samples in this window yet.</Empty>
      )}
    </Card>
  );
}

export function MonitoringTab() {
  const [range, setRange] = useState("6h");
  const [ranges, setRanges] = useState<string[]>(["1h", "6h", "24h", "7d", "30d", "1y"]);
  const [charts, setCharts] = useState<SeriesResp[]>([]);
  const [gauges, setGauges] = useState<Record<string, { label: string; unit: string; value: number | null }>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    api<CatalogueResp>("/api/monitoring/catalogue")
      .then((res) => {
        if (!alive.current) return;
        if (res.ranges?.length) setRanges(res.ranges);
        if (res.default_range) setRange((r) => (r === "6h" ? res.default_range : r));
      })
      .catch(() => undefined);
  }, []);

  const load = useCallback(async (window: string) => {
    setError("");
    try {
      const results = await Promise.all(
        CHARTS.map((m) =>
          api<SeriesResp>(`/api/monitoring/series?metric=${m}&range=${window}`).catch(
            () => null,
          ),
        ),
      );
      if (!alive.current) return;
      const good = results.filter((r): r is SeriesResp => !!r);
      setCharts(good);
      const next: Record<string, { label: string; unit: string; value: number | null }> = {};
      for (const r of good) {
        next[r.metric] = {
          label: r.label,
          unit: r.unit,
          value: latestValue(r.series[0]?.points || []),
        };
      }
      // Gauges the chart set does not cover need their own short window.
      await Promise.all(
        GAUGES.filter((m) => !next[m]).map(async (m) => {
          const r = await api<SeriesResp>(`/api/monitoring/series?metric=${m}&range=1h`).catch(
            () => null,
          );
          if (r) {
            next[r.metric] = {
              label: r.label,
              unit: r.unit,
              value: latestValue(r.series[0]?.points || []),
            };
          }
        }),
      );
      if (!alive.current) return;
      setGauges(next);
      if (good.length === 0) {
        setError(
          "Metrics are not available on this node yet. They are collected by the " +
            "built-in monitoring stack, which is installed as part of the deploy.",
        );
      }
    } catch (err: unknown) {
      if (alive.current) setError(err instanceof Error ? err.message : "Could not load metrics");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void load(range);
    // Short windows are live; a year of history does not need re-fetching often.
    const period = range === "1h" ? 30000 : range === "6h" ? 60000 : 300000;
    const id = window.setInterval(() => void load(range), period);
    return () => window.clearInterval(id);
  }, [range, load]);

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
          Collected on this server and kept for a year. Metrics never leave the appliance.
        </Hint>
      </Bar>

      {error ? <WarnBox>{error}</WarnBox> : null}

      {Object.keys(gauges).length ? (
        <Grid>
          {GAUGES.filter((m) => gauges[m]).map((m) => (
            <Gauge key={m} label={gauges[m].label} unit={gauges[m].unit} value={gauges[m].value} />
          ))}
        </Grid>
      ) : null}

      <ChartGrid>
        {charts.map((c) => (
          <Chart key={c.metric} data={c} />
        ))}
      </ChartGrid>
    </>
  );
}
