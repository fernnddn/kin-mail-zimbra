import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-cl-"));
const bundle = join(out, "clusterLog.mjs");
execFileSync(join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "pages", "clusterLog.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"], { stdio: "inherit" });
const { stripMachineLines } = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};

// The exact shape that buried the transcript on a live pair.
const real = [
  "=== Pre-flight check mail2.example.test - 8:58:38 PM ===",
  "[OK] drbd_uptodate: both replicas UpToDate",
  'PREFLIGHT_JSON:{"pacemaker_nodes":{"ok":true,"detail":"nodes=[...]"},"_all_ok":true}',
  "[OK] qdevice_voting: qdevice reachable and voting",
].join("\n");

chk("machine payload is hidden", stripMachineLines(real), [
  "=== Pre-flight check mail2.example.test - 8:58:38 PM ===",
  "[OK] drbd_uptodate: both replicas UpToDate",
  "[OK] qdevice_voting: qdevice reachable and voting",
].join("\n"));

chk("every tag in the family goes", stripMachineLines([
  "keep me",
  'REMOVE_PROBE_JSON:{"target":"mail2"}',
  'CLUSTER_STATUS_JSON:{"nodes":[]}',
  'ADD_HOST_PROBE_JSON:{"ok":true}',
  "keep me too",
].join("\n")), "keep me\nkeep me too");

// Indented payloads still go: the stream sometimes wraps them in a step body.
chk("leading whitespace does not smuggle one through",
  stripMachineLines('  PREFLIGHT_JSON:{"a":1}\nreal line'), "real line");

// Human lines that merely mention a tag must survive: the operator needs the
// sentence that explains why a payload was unreadable.
chk("a sentence about a payload is not a payload",
  stripMachineLines("could not parse PREFLIGHT_JSON: from the peer"),
  "could not parse PREFLIGHT_JSON: from the peer");

chk("empty input is unchanged", stripMachineLines(""), "");
chk("a log with no payloads is byte-identical",
  stripMachineLines("a\nb\nc"), "a\nb\nc");

console.log(fail === 0 ? `ALL OK (${pass} checks)` : `FAILED ${fail} of ${pass + fail}`);
process.exit(fail === 0 ? 0 : 1);
