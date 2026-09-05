/** Pure helpers for the Monitoring tab charts.
 *
 * Charts are drawn as plain SVG in the console's own light theme rather than
 * embedded from Grafana: the operator asked for one console, one login, one
 * look, and an iframe would bring a dark theme and a second port with it.
 */

export type Point = [number, number | null];

export function formatValue(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  switch (unit) {
    case "percent":
      return `${value.toFixed(value >= 10 ? 0 : 1)}%`;
    case "bytes_per_sec":
      // Network throughput is quoted in bits per second, with a lower-case b:
      // 1 MB/s of traffic is 8 Mbps, and showing "MBps" for it is simply wrong.
      return formatBitsPerSecond(value * 8);
    case "disk_bytes_per_sec":
      // Disk throughput is quoted in bytes per second, unlike network. Both
      // used the network formatter, so a disk doing 50 MB/s read "400 Mbps".
      return `${formatBytes(value)}/s`;
    case "per_sec":
      return `${value >= 100 ? value.toFixed(0) : value.toFixed(2)}/s`;
    case "per_min":
      // Mail arrives in bursts, and "0.02/s" is not a rate anybody thinks in.
      // Per minute is the unit an operator actually reasons about for mail.
      if (value > 0 && value < 0.1) return "<0.1/min";
      return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)}/min`;
    case "seconds":
      return formatDuration(value);
    case "count":
      // Things you can count. "6.96 messages waiting" and "87.00 connections"
      // are not quantities anyone has; a rate can be fractional, a queue
      // cannot. Averages over a window are rounded rather than shown to two
      // decimals, because the number is a tally either way.
      return Math.round(value).toLocaleString();
    default:
      return value >= 100 ? value.toFixed(0) : value.toFixed(2);
  }
}

export function formatBytes(bytes: number): string {
  const abs = Math.abs(bytes);
  if (abs < 1024) return `${bytes.toFixed(0)} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = bytes / 1024;
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(Math.abs(v) >= 100 ? 0 : 1)} ${units[i]}`;
}

/** bits/s -> "12.4 Mbps". Lower-case b: these are bits, not bytes. */
export function formatBitsPerSecond(bits: number): string {
  const abs = Math.abs(bits);
  if (abs < 1000) return `${bits.toFixed(0)} bps`;
  const units = ["Kbps", "Mbps", "Gbps", "Tbps"];
  let v = bits / 1000;
  let i = 0;
  while (Math.abs(v) >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${v.toFixed(Math.abs(v) >= 100 ? 0 : 1)} ${units[i]}`;
}

/** Bytes as a size, for RAM and disks (binary units, upper-case B). */
export function formatSize(bytes: number): string {
  return formatBytes(bytes);
}

export function formatDuration(seconds: number): string {
  if (seconds < 0) return "-";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return h ? `${d}d ${h}h` : `${d}d`;
  if (h > 0) return m ? `${h}h ${m}m` : `${h}h`;
  return `${Math.max(1, m)}m`;
}

/** Y bounds for a series. Percentages always show the full 0-100 context. */
/** Units whose values can legitimately sit below zero.
 *
 * Everything this appliance charts is a rate, a tally, a size or a duration,
 * and none of those can. Kept as an explicit set so a future signed metric -
 * a clock offset, say - opts in rather than being silently floored at zero.
 */
const CAN_GO_NEGATIVE = new Set<string>([]);

export function yBounds(points: Point[], unit: string): [number, number] {
  if (unit === "percent") return [0, 100];
  const vals = points.map((p) => p[1]).filter((v): v is number => v !== null);
  if (vals.length === 0) return [0, 1];
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  if (hi === lo) {
    // A dead-flat series must not collapse to a zero-height box.
    const pad = Math.abs(hi) || 1;
    // ...but a quiet error counter is the commonest flat series there is, and
    // padding it symmetrically drew an axis reading "-0.50/s". There is no
    // such thing as minus half an error, and an operator reading that has to
    // stop and work out whether the chart is broken.
    const floor = lo >= 0 && !CAN_GO_NEGATIVE.has(unit) ? 0 : lo - pad * 0.5;
    return [floor, hi + pad * 0.5];
  }
  const pad = (hi - lo) * 0.1;
  const lower = CAN_GO_NEGATIVE.has(unit) ? lo - pad : Math.max(0, lo - pad);
  return [lower, hi + pad];
}

export type Geometry = { width: number; height: number };

/** The time window the chart covers, in epoch seconds. */
export type Domain = { start: number; end: number };

/** Horizontal position of a timestamp, 0..1 across the requested window.
 *
 * Positioning by timestamp rather than by array index is what keeps every card
 * on the same axis: a series that only has the last few minutes of samples now
 * draws in the last few minutes of the chart instead of being stretched across
 * the whole width (live Phase 5 QA - the memory card showed a four-minute axis
 * next to a CPU card showing six hours).
 */
export function xFraction(ts: number, domain?: Domain): number | null {
  if (!domain) return null;
  const span = domain.end - domain.start;
  if (span <= 0) return null;
  return Math.max(0, Math.min(1, (ts - domain.start) / span));
}

/** Map a series to SVG path segments, breaking the line wherever data is null. */
export function linePaths(
  points: Point[],
  geo: Geometry,
  bounds: [number, number],
  domain?: Domain,
): string[] {
  const [lo, hi] = bounds;
  const span = hi - lo || 1;
  const n = points.length;
  if (n === 0) return [];
  const x = (i: number) => {
    const frac = xFraction(points[i][0], domain);
    if (frac !== null) return frac * geo.width;
    return n === 1 ? geo.width / 2 : (i / (n - 1)) * geo.width;
  };
  const y = (v: number) => geo.height - ((v - lo) / span) * geo.height;

  const out: string[] = [];
  let current: string[] = [];
  points.forEach((p, i) => {
    const v = p[1];
    if (v === null) {
      // A gap must break the line, not be interpolated across.
      if (current.length > 1) out.push(current.join(" "));
      current = [];
      return;
    }
    current.push(`${current.length === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`);
  });
  if (current.length > 1) out.push(current.join(" "));
  return out;
}

/** Filled area under the first contiguous run, for the soft gradient. */
export function areaPath(
  points: Point[],
  geo: Geometry,
  bounds: [number, number],
  domain?: Domain,
): string {
  const segments = linePaths(points, geo, bounds, domain);
  if (segments.length === 0) return "";
  const first = segments[0];
  const coords = first.replace(/[ML]/g, " ").trim().split(/\s+/);
  if (coords.length < 2) return "";
  const startX = coords[0].split(",")[0];
  const endX = coords[coords.length - 1].split(",")[0];
  return `${first} L${endX},${geo.height} L${startX},${geo.height} Z`;
}

export function latestValue(points: Point[]): number | null {
  for (let i = points.length - 1; i >= 0; i--) {
    const v = points[i][1];
    if (v !== null) return v;
  }
  return null;
}

/** Tick labels along the time axis, in the viewer's locale.
 *
 * Derived from the requested window when one is given, so every card on the
 * page reads the same axis whether or not its series covers all of it.
 */
export function timeTicks(
  points: Point[],
  count = 4,
  domain?: Domain,
): { at: number; label: string }[] {
  const out: { at: number; label: string }[] = [];
  if (domain && domain.end > domain.start) {
    const spanSec = domain.end - domain.start;
    const longRange = spanSec > 3 * 86400;
    for (let i = 0; i < count; i++) {
      const at = i / (count - 1);
      const d = new Date((domain.start + at * spanSec) * 1000);
      out.push({
        at,
        label: longRange
          ? d.toLocaleDateString([], { month: "short", day: "numeric" })
          : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
      });
    }
    return out;
  }
  if (points.length < 2) return [];
  const spanSec = points[points.length - 1][0] - points[0][0];
  const longRange = spanSec > 3 * 86400;
  for (let i = 0; i < count; i++) {
    const idx = Math.round((i / (count - 1)) * (points.length - 1));
    const ts = points[idx][0];
    const d = new Date(ts * 1000);
    out.push({
      at: idx / (points.length - 1),
      label: longRange
        ? d.toLocaleDateString([], { month: "short", day: "numeric" })
        : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    });
  }
  return out;
}

/** Index of the sample nearest a 0..1 position, honouring the time domain. */
export function nearestIndexInDomain(
  points: Point[],
  fraction: number,
  domain?: Domain,
): number {
  if (points.length === 0) return -1;
  if (!domain || domain.end <= domain.start) return nearestIndex(points, fraction);
  const want = domain.start + Math.max(0, Math.min(1, fraction)) * (domain.end - domain.start);
  let best = 0;
  let bestGap = Infinity;
  for (let i = 0; i < points.length; i++) {
    const gap = Math.abs(points[i][0] - want);
    if (gap < bestGap) {
      bestGap = gap;
      best = i;
    }
  }
  return best;
}

/** Index of the sample nearest a 0..1 position across the chart width. */
export function nearestIndex(points: Point[], fraction: number): number {
  if (points.length === 0) return -1;
  if (points.length === 1) return 0;
  const clamped = Math.max(0, Math.min(1, fraction));
  return Math.round(clamped * (points.length - 1));
}

/** Time label for a hovered sample, in the viewer's locale. */
export function pointTimeLabel(ts: number, longRange: boolean): string {
  const d = new Date(ts * 1000);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return longRange
    ? `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`
    : time;
}

/** Peak, low and mean of a series, ignoring gaps. */
export function seriesStats(points: Point[]): {
  min: number | null;
  max: number | null;
  avg: number | null;
} {
  const vals = points.map((p) => p[1]).filter((v): v is number => v !== null);
  if (vals.length === 0) return { min: null, max: null, avg: null };
  let min = vals[0];
  let max = vals[0];
  let sum = 0;
  for (const v of vals) {
    if (v < min) min = v;
    if (v > max) max = v;
    sum += v;
  }
  return { min, max, avg: sum / vals.length };
}

/** The viewer's timezone, so a chart's clock is never ambiguous.
 *
 * Timestamps are rendered in the BROWSER's zone, which is not necessarily the
 * server's. Saying which one avoids the "these times look wrong" reading when
 * the two differ.
 */
export function localZoneLabel(): string {
  try {
    const parts = new Intl.DateTimeFormat([], { timeZoneName: "short" }).formatToParts(new Date());
    const tz = parts.find((p) => p.type === "timeZoneName");
    if (tz?.value) return tz.value;
  } catch {
    /* fall through */
  }
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
  } catch {
    return "local time";
  }
}

/** Tone for a percentage gauge: quiet until it actually matters. */
export function usageTone(percent: number | null, unit: string): "ok" | "warn" | "danger" {
  if (unit !== "percent" || percent === null) return "ok";
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warn";
  return "ok";
}


/** Y-axis labels: the bottom, middle and top of the drawn band.
 *
 * The charts had no y labels at all, so a line could be read for shape but
 * never for magnitude without hovering it. `at` is the fraction DOWN from the
 * top, matching how the gridlines are drawn.
 */
export function yTicks(bounds: [number, number], unit: string): { at: number; label: string }[] {
  const [lo, hi] = bounds;
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi === lo) return [];
  // An axis wants uniform labels. formatValue varies its precision with
  // magnitude, which is right for a readout and wrong for a scale: it renders
  // a percentage axis as 100% / 50.0% / 0.0%.
  const label = (v: number) =>
    unit === "percent" ? `${Math.round(v)}%` : formatValue(v, unit);
  return [
    { at: 0, label: label(hi) },
    { at: 0.5, label: label(lo + (hi - lo) / 2) },
    { at: 1, label: label(lo) },
  ];
}

/** Where a value sits vertically in the plot, 0 (top) .. 1 (bottom). */
export function yFraction(value: number, bounds: [number, number]): number {
  const [lo, hi] = bounds;
  const span = hi - lo;
  if (!Number.isFinite(span) || span === 0) return 0.5;
  return Math.max(0, Math.min(1, 1 - (value - lo) / span));
}

/** Thresholds worth shading behind a series, in the series' own unit.
 *
 * Only for metrics where a number has an agreed meaning. A CPU at 95% is
 * worth seeing as "in the red" without reading the axis; bytes/sec has no
 * such level and gets nothing, because inventing one would be decoration.
 */
export function thresholdsFor(unit: string, metric: string): { from: number; tone: "warn" | "danger" }[] {
  if (unit === "percent") {
    // Disks are the exception: 80% full is a plan, 90% is a problem, whereas
    // 80% CPU on a mail node at 09:00 is just Monday.
    if (metric.startsWith("disk_") && metric !== "disk_busy") {
      return [
        { from: 75, tone: "warn" },
        { from: 90, tone: "danger" },
      ];
    }
    return [
      { from: 80, tone: "warn" },
      { from: 95, tone: "danger" },
    ];
  }
  if (metric === "mail_queue_oldest") {
    // An hour in the queue is worth a look; four is mail nobody has received.
    return [
      { from: 3600, tone: "warn" },
      { from: 14400, tone: "danger" },
    ];
  }
  return [];
}
