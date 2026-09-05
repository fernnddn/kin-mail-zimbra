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

// --- time domain (Phase 5 QA: cards showed different axes for one window) ---
const dom = { start: 1000, end: 5000 };
chk("start of window maps to 0", C.xFraction(1000, dom), 0);
chk("end of window maps to 1", C.xFraction(5000, dom), 1);
chk("midpoint maps to 0.5", C.xFraction(3000, dom), 0.5);
chk("outside the window is clamped", [C.xFraction(0, dom), C.xFraction(9999, dom)], [0, 1]);
chk("no domain means no fraction", C.xFraction(3000, undefined), null);
chk("a zero-width window is rejected", C.xFraction(1, { start: 5, end: 5 }), null);

// A series covering only the last quarter must draw in the last quarter,
// not be stretched across the whole card.
const late = [[4000, 1], [4500, 2], [5000, 3]];
const seg = C.linePaths(late, { width: 100, height: 50 }, [0, 10], dom);
ok("a late-starting series starts late", seg[0].startsWith("M75.00,"));
ok("...and still ends at the right edge", seg[0].includes("L100.00,"));

// Ticks come from the window, so every card reads the same axis.
const t = C.timeTicks([[4000, 1]], 4, dom);
chk("ticks span the window even with one sample", [t.length, t[0].at, t[3].at], [4, 0, 1]);
chk("ticks still fall back to the data with no domain", C.timeTicks([[0, 1], [60, 2]], 2).length, 2);

chk("hover finds the sample nearest that TIME", C.nearestIndexInDomain(late, 1, dom), 2);
chk("hover before the data picks the first sample", C.nearestIndexInDomain(late, 0, dom), 0);
chk("hover without a domain falls back to index", C.nearestIndexInDomain(late, 0.5, undefined), 1);

// --- peak / low / average (Phase 6 QA asked to see the extremes) -----------
const st = C.seriesStats([[0, 10], [1, 50], [2, 30]]);
chk("stats find the extremes and the mean", [st.min, st.max, st.avg], [10, 50, 30]);
chk("gaps are ignored, not counted as zero",
  C.seriesStats([[0, 10], [1, null], [2, 30]]), { min: 10, max: 30, avg: 20 });
chk("an all-gap series has no stats",
  C.seriesStats([[0, null]]), { min: null, max: null, avg: null });
chk("an empty series has no stats", C.seriesStats([]), { min: null, max: null, avg: null });
chk("a flat series reports the same value three times",
  C.seriesStats([[0, 7], [1, 7]]), { min: 7, max: 7, avg: 7 });
ok("a zone label is always produced", typeof C.localZoneLabel() === "string" && C.localZoneLabel().length > 0);
// --- per-minute rates -------------------------------------------------------
// Mail arrives in bursts. "0.02/s" is not a rate anyone reasons about.
chk("a mail rate reads per minute", C.formatValue(12, "per_min"), "12.0/min");
chk("a busy rate drops the decimal", C.formatValue(340, "per_min"), "340/min");
chk("a trickle does not read as zero", C.formatValue(0.02, "per_min"), "<0.1/min");
chk("actual zero is zero", C.formatValue(0, "per_min"), "0.0/min");
chk("no samples is a dash", C.formatValue(null, "per_min"), "-");

// --- y axis labels ----------------------------------------------------------
// Without these a line could be read for shape but never for magnitude.
{
  const t = C.yTicks([0, 100], "percent");
  chk("three y labels", t.length, 3);
  chk("top label is the maximum", t[0].label, "100%");
  chk("top label sits at the top", t[0].at, 0);
  chk("middle label is the midpoint", t[1].label, "50%");
  chk("bottom label is the minimum", t[2].label, "0%");
  chk("bottom label sits at the bottom", t[2].at, 1);
  chk("a flat band draws no labels", C.yTicks([5, 5], "number").length, 0);
  chk("a non-finite band draws no labels", C.yTicks([NaN, 1], "number").length, 0);
}

// --- vertical placement -----------------------------------------------------
chk("the maximum sits at the top", C.yFraction(100, [0, 100]), 0);
chk("the minimum sits at the bottom", C.yFraction(0, [0, 100]), 1);
chk("the midpoint sits in the middle", C.yFraction(50, [0, 100]), 0.5);
chk("above the band clamps to the top", C.yFraction(150, [0, 100]), 0);
chk("below the band clamps to the bottom", C.yFraction(-50, [0, 100]), 1);
chk("a zero-height band is centred", C.yFraction(5, [5, 5]), 0.5);

// --- threshold shading ------------------------------------------------------
// Only where a number has an agreed meaning; inventing one is decoration.
chk("cpu percent gets warn and danger bands", C.thresholdsFor("percent", "cpu").length, 2);
chk("cpu warns at 80", C.thresholdsFor("percent", "cpu")[0].from, 80);
chk("a filling disk warns earlier than cpu", C.thresholdsFor("percent", "disk_root")[0].from, 75);
chk("disk danger is 90", C.thresholdsFor("percent", "disk_root")[1].from, 90);
chk("disk busy is not a filling disk", C.thresholdsFor("percent", "disk_busy")[0].from, 80);
chk("throughput has no meaningful level", C.thresholdsFor("bytes_per_sec", "net_rx").length, 0);
chk("a queue age of an hour is worth a look", C.thresholdsFor("seconds", "mail_queue_oldest")[0].from, 3600);
chk("uptime seconds get no bands", C.thresholdsFor("seconds", "uptime").length, 0);


// --- counts -----------------------------------------------------------------
// A rate can be fractional; a queue cannot. "6.96 messages waiting" is not a
// quantity anyone has.
chk("a queue depth is a whole number", C.formatValue(6.96, "count"), "7");
chk("a count rounds down too", C.formatValue(6.2, "count"), "6");
chk("zero is zero", C.formatValue(0, "count"), "0");
chk("large counts get separators", C.formatValue(12345, "count"), (12345).toLocaleString());
chk("no samples is still a dash", C.formatValue(null, "count"), "-");
chk("NaN is a dash", C.formatValue(NaN, "count"), "-");


// --- a quiet series must not imply negative values --------------------------
// An error counter sitting at zero is the commonest flat series there is.
// Padding it symmetrically drew an axis reading "-0.50/s", and there is no
// such thing as minus half an error.
{
  const flatZero = [[1, 0], [2, 0], [3, 0]];
  const b = C.yBounds(flatZero, "per_sec");
  chk("a flat zero series starts at zero", b[0], 0);
  chk("and still has height to draw in", b[1] > 0, true);
  chk("axis labels never go negative", C.yTicks(b, "per_sec")[2].label, "0.00/s");
  const flatCount = C.yBounds([[1, 0], [2, 0]], "count");
  chk("a flat zero count starts at zero", flatCount[0], 0);
  const flatHigh = C.yBounds([[1, 40], [2, 40]], "count");
  chk("a flat non-zero series keeps room below", flatHigh[0], 0);
  chk("a flat non-zero series keeps room above", flatHigh[1], 60);
  const rising = C.yBounds([[1, 10], [2, 90]], "count");
  chk("a normal series is still padded", rising[1], 98);
  chk("and is still floored at zero", rising[0], 2);
}

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
