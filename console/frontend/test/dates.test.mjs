import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-dt-"));
const bundle = join(out, "dates.mjs");
execFileSync(join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "lib", "dates.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"], { stdio: "inherit" });
const D = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};
const ok = (n, cond) => chk(n, !!cond, true);

const NOW = Date.parse("2026-08-28T12:00:00Z");

// The licence and certificate both arrive as full ISO 8601 with an offset,
// which is what was being printed straight onto the settings page.
ok("an ISO timestamp stops looking like one", !D.formatDay("2027-08-28T10:35:08+00:00").includes("T"));
ok("the year survives", D.formatDay("2027-08-28T10:35:08+00:00").includes("2027"));

// A date we cannot parse is still more use raw than as "Invalid Date".
chk("unparseable is passed through", D.formatDay("whenever"), "whenever");
chk("empty stays empty", D.formatDay(""), "");
chk("null stays empty", D.formatDay(null), "");
chk("undefined stays empty", D.formatDay(undefined), "");

chk("days until counts whole days", D.daysUntil("2026-09-27T12:00:00Z", NOW), 30);
chk("days until is negative once past", D.daysUntil("2026-08-21T12:00:00Z", NOW), -7);
chk("days until of nothing is null", D.daysUntil(null, NOW), null);
chk("days until of nonsense is null", D.daysUntil("soon", NOW), null);

ok("expiry says how long is left", D.formatExpiry("2026-09-27T12:00:00Z", NOW).includes("30 days left"));
ok("one day is singular", D.formatExpiry("2026-08-29T12:00:00Z", NOW).includes("1 day left"));
ok("today is called out", D.formatExpiry("2026-08-28T18:00:00Z", NOW).includes("expires today"));
ok("past tense once expired", D.formatExpiry("2026-08-21T12:00:00Z", NOW).includes("expired 7 days ago"));

// 30 days is the point where renewal is still comfortable. Renewal needs a
// working DNS-01 challenge and restarts Zimbra on an HA pair, so the warning
// has to arrive while there is still room to fix whatever is broken.
ok("29 days out warns", D.expiringSoon("2026-09-26T12:00:00Z", NOW));
ok("exactly 30 days warns", D.expiringSoon("2026-09-27T12:00:00Z", NOW));
chk("31 days out is quiet", D.expiringSoon("2026-09-28T13:00:00Z", NOW), false);
ok("already expired still warns", D.expiringSoon("2026-08-01T12:00:00Z", NOW));
chk("no date does not warn", D.expiringSoon(null, NOW), false);
chk("the warning window is a month", D.EXPIRY_WARN_DAYS, 30);

console.log(fail ? `\n${fail} FAILED (${pass} ok)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
