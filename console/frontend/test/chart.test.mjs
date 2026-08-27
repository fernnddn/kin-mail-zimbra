import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-ch-"));
const bundle = join(out, "chart.mjs");
execFileSync(join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "monitoring", "chart.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"], { stdio: "inherit" });
const C = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};
const ok = (n, cond) => chk(n, !!cond, true);

// --- formatting -------------------------------------------------------------
chk("percent rounds sensibly", [C.formatValue(42.4, "percent"), C.formatValue(3.27, "percent")], ["42%", "3.3%"]);
chk("missing value is a dash", C.formatValue(null, "percent"), "-");
chk("undefined value is a dash", C.formatValue(undefined, "percent"), "-");
chk("NaN is a dash", C.formatValue(NaN, "percent"), "-");
// Throughput is quoted in BITS per second with a lower-case b: 1.5 KB/s of
// traffic is 12.3 Kbps, and calling it "1.5 KB/s" is a different quantity.
chk("throughput converts bytes to bits", C.formatValue(1536, "bytes_per_sec"), "12.3 Kbps");
chk("1 MB/s reads as 8.4 Mbps", C.formatValue(1024 * 1024, "bytes_per_sec"), "8.4 Mbps");
chk("bits use lower-case b", /[KMG]bps$/.test(C.formatValue(5e6, "bytes_per_sec")), true);
chk("tiny throughput stays in bps", C.formatBitsPerSecond(120), "120 bps");
chk("throughput scales to Gbps", C.formatBitsPerSecond(2.5e9), "2.5 Gbps");
chk("zero throughput", C.formatBitsPerSecond(0), "0 bps");

// Hover: the operator wants Grafana-style "value at this time".
chk("nearest index at the left edge", C.nearestIndex([[0, 1], [1, 2], [2, 3]], 0), 0);
chk("nearest index at the right edge", C.nearestIndex([[0, 1], [1, 2], [2, 3]], 1), 2);
chk("nearest index clamps past the edges", C.nearestIndex([[0, 1], [1, 2]], 9), 1);
chk("nearest index on an empty series", C.nearestIndex([], 0.5), -1);
chk("hover label is time only on a short range", /^\d{1,2}:\d{2}/.test(C.pointTimeLabel(1.7e9, false)), true);
chk("hover label adds the date on a long range", C.pointTimeLabel(1.7e9, true).length > C.pointTimeLabel(1.7e9, false).length, true);
chk("small bytes stay bytes", C.formatBytes(512), "512 B");
chk("big bytes scale to GB", C.formatBytes(3 * 1024 ** 3), "3.0 GB");
chk("uptime in days", C.formatValue(200000, "seconds"), "2d 7h");
chk("uptime under an hour", C.formatValue(300, "seconds"), "5m");
chk("sub-minute uptime never shows 0m", C.formatValue(10, "seconds"), "1m");

// --- y bounds ---------------------------------------------------------------
chk("percent always shows full scale", C.yBounds([[0, 5]], "percent"), [0, 100]);
chk("empty series gets a safe box", C.yBounds([], "number"), [0, 1]);
// A flat line must not collapse into a zero-height box (division by zero).
const flat = C.yBounds([[0, 7], [1, 7]], "number");
ok("flat series still has height", flat[1] > flat[0]);
chk("bounds never go negative for rates", C.yBounds([[0, 1], [1, 2]], "bytes_per_sec")[0], 0.9);

// --- paths ------------------------------------------------------------------
const geo = { width: 100, height: 50 };
chk("empty series draws nothing", C.linePaths([], geo, [0, 100]), []);
const solid = C.linePaths([[0, 0], [1, 50], [2, 100]], geo, [0, 100]);
chk("a full series is one segment", solid.length, 1);
ok("path starts at the left edge", solid[0].startsWith("M0.00,"));
ok("y is inverted (100% at the top)", solid[0].includes("M0.00,50.00"));

// A gap must BREAK the line, never be drawn straight through.
const gapped = C.linePaths([[0, 10], [1, null], [2, 30], [3, 40]], geo, [0, 100]);
chk("a gap splits the line into segments", gapped.length, 1);
const twoRuns = C.linePaths([[0, 1], [1, 2], [2, null], [3, 4], [4, 5]], geo, [0, 100]);
chk("two runs give two segments", twoRuns.length, 2);
chk("an all-null series draws nothing", C.linePaths([[0, null], [1, null]], geo, [0, 100]), []);
chk("a single point draws no line", C.linePaths([[0, 5]], geo, [0, 100]), []);

const area = C.areaPath([[0, 10], [1, 20], [2, 30]], geo, [0, 100]);
ok("area closes back to the baseline", area.endsWith("Z") && area.includes(`,${geo.height}`));
chk("no area without a line", C.areaPath([], geo, [0, 100]), "");

// --- latest + ticks ---------------------------------------------------------
chk("latest skips trailing gaps", C.latestValue([[0, 1], [1, 9], [2, null]]), 9);
chk("latest of an empty series is null", C.latestValue([]), null);
chk("latest of an all-null series is null", C.latestValue([[0, null]]), null);
chk("no ticks for a single point", C.timeTicks([[0, 1]]), []);
chk("tick count is honoured", C.timeTicks([[0, 1], [60, 2], [120, 3], [180, 4]], 4).length, 4);
ok("ticks span 0..1", (() => { const t = C.timeTicks([[0, 1], [60, 2], [120, 3]], 3); return t[0].at === 0 && t[t.length - 1].at === 1; })());

// --- tone -------------------------------------------------------------------
chk("quiet under 75%", C.usageTone(50, "percent"), "ok");
chk("warn at 75%", C.usageTone(80, "percent"), "warn");
chk("danger at 90%", C.usageTone(95, "percent"), "danger");
chk("non-percent units are never alarmed", C.usageTone(99999, "bytes_per_sec"), "ok");
chk("null is not alarmed", C.usageTone(null, "percent"), "ok");

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
