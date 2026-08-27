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
      return `${formatBytes(value)}/s`;
    case "seconds":
      return formatDuration(value);
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
export function yBounds(points: Point[], unit: string): [number, number] {
  if (unit === "percent") return [0, 100];
  const vals = points.map((p) => p[1]).filter((v): v is number => v !== null);
  if (vals.length === 0) return [0, 1];
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  if (hi === lo) {
    // A dead-flat series must not collapse to a zero-height box.
    const pad = Math.abs(hi) || 1;
    return [lo - pad * 0.5, hi + pad * 0.5];
  }
  const pad = (hi - lo) * 0.1;
  return [Math.max(0, lo - pad), hi + pad];
}

export type Geometry = { width: number; height: number };

/** Map a series to SVG path segments, breaking the line wherever data is null. */
export function linePaths(points: Point[], geo: Geometry, bounds: [number, number]): string[] {
  const [lo, hi] = bounds;
  const span = hi - lo || 1;
  const n = points.length;
  if (n === 0) return [];
  const x = (i: number) => (n === 1 ? geo.width / 2 : (i / (n - 1)) * geo.width);
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
export function areaPath(points: Point[], geo: Geometry, bounds: [number, number]): string {
  const segments = linePaths(points, geo, bounds);
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

/** Tick labels along the time axis, in the viewer's locale. */
export function timeTicks(points: Point[], count = 4): { at: number; label: string }[] {
  if (points.length < 2) return [];
  const out: { at: number; label: string }[] = [];
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

/** Tone for a percentage gauge: quiet until it actually matters. */
export function usageTone(percent: number | null, unit: string): "ok" | "warn" | "danger" {
  if (unit !== "percent" || percent === null) return "ok";
  if (percent >= 90) return "danger";
  if (percent >= 75) return "warn";
  return "ok";
}
