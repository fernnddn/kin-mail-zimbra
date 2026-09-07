/** What the operator sees in the Cluster Activity log.
 *
 * The maintenance stream carries two audiences on one channel: a human
 * transcript ("Pre-flight check ...", "[OK] drbd_uptodate: ...") and machine
 * payloads the page parses for its own state. The payloads are single
 * unwrapped lines thousands of characters long, and printing them buried the
 * transcript the operator is trying to read (live QA, 7 Sep 2026: "I cant see
 * the log clearly").
 *
 * Parsing reads the raw stream buffer, never this, so hiding them costs the
 * page nothing.
 */

/** `PREFLIGHT_JSON:`, `REMOVE_PROBE_JSON:`, `CLUSTER_STATUS_JSON:`, and any
 *  future tag in the same SHOUTY_CASE_JSON: shape. */
const MACHINE_LINE = /^[A-Z][A-Z0-9_]*_JSON:/;

export function stripMachineLines(log: string): string {
  if (!log) return log;
  return log
    .split("\n")
    .filter((line) => !MACHINE_LINE.test(line.trimStart()))
    .join("\n");
}
