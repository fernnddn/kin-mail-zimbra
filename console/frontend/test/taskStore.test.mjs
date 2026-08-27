// Task centre state. Same esbuild-through-node harness as deployPipeline.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-ts-"));
const bundle = join(out, "taskStore.mjs");
execFileSync(
  join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "tasks", "taskStore.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"],
  { stdio: "inherit" },
);
const S = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};
const mk = (id, state, startedAt = 0) => ({ id, title: id, state, startedAt });

chk("ids are unique", S.nextTaskId() === S.nextTaskId(), false);

let t = S.addTask([], mk("a", "running"));
chk("newest task is first", t.map((x) => x.id), ["a"]);
t = S.addTask(t, mk("b", "running"));
chk("later task goes on top", t.map((x) => x.id), ["b", "a"]);

t = S.endTask(t, "a", "done", "finished");
chk("ending a task sets state and message", [t[1].state, t[1].message], ["done", "finished"]);
chk("ending a task stamps endedAt", typeof t[1].endedAt, "number");
chk("the other task is untouched", t[0].state, "running");

// A late duplicate 'done' must not overwrite a recorded failure.
let f = S.endTask(S.addTask([], mk("c", "running")), "c", "failed", "boom");
f = S.endTask(f, "c", "done", "late");
chk("a finished task is never re-ended", [f[0].state, f[0].message], ["failed", "boom"]);

chk("unknown id is a no-op", S.endTask(t, "zzz", "done").length, t.length);

// History must stay bounded or a long session grows forever.
let many = [];
for (let i = 0; i < S.TASK_HISTORY_LIMIT + 10; i++) many = S.addTask(many, mk(`t${i}`, "done"));
chk("history is capped", many.length, S.TASK_HISTORY_LIMIT);
chk("the cap drops the oldest", many[0].id, `t${S.TASK_HISTORY_LIMIT + 9}`);

const mixed = [mk("r", "running"), mk("d", "done"), mk("x", "failed")];
chk("counts", [S.runningCount(mixed), S.failedCount(mixed)], [1, 1]);
chk("clearFinished keeps only running", S.clearFinished(mixed).map((x) => x.id), ["r"]);

// Badge priority: a failure must be visible even while something else runs.
chk("failed beats running", S.taskBadgeTone(mixed), "failed");
chk("running beats done", S.taskBadgeTone([mk("r", "running"), mk("d", "done")]), "running");
chk("done when all finished ok", S.taskBadgeTone([mk("d", "done")]), "done");
chk("idle with no tasks", S.taskBadgeTone([]), "idle");

chk("elapsed uses endedAt once finished",
  S.taskElapsedMs({ ...mk("e", "done", 1000), endedAt: 4000 }, 99999), 3000);
chk("elapsed uses now while running", S.taskElapsedMs(mk("e", "running", 1000), 3500), 2500);
chk("elapsed never negative", S.taskElapsedMs(mk("e", "running", 5000), 1000), 0);
chk("format seconds", S.formatElapsed(45000), "45s");
chk("format minutes", S.formatElapsed(125000), "2m 5s");
chk("format whole minutes", S.formatElapsed(120000), "2m");

// --- dropdown vs full page (Phase 5 QA: stale tasks cluttered the dropdown) --
const DAY = 24 * 60 * 60 * 1000;
const fin = (id, endedAt) => ({ id, title: id, state: "done", startedAt: 0, endedAt });
const now = 10 * DAY;

chk("a running task is always recent", S.isRecent(mk("r", "running"), now), true);
chk("a task finished an hour ago is recent", S.isRecent(fin("a", now - 3600e3), now), true);
chk("a task finished yesterday is stale", S.isRecent(fin("b", now - DAY - 1), now), false);
chk("exactly one day old is still recent", S.isRecent(fin("c", now - DAY), now), true);

const spread = [mk("run", "running"), fin("fresh", now - 1000), fin("old", now - 3 * DAY)];
chk("dropdown hides stale tasks", S.dropdownTasks(spread, now).map((t) => t.id), ["run", "fresh"]);
chk("dropdown puts running first", S.dropdownTasks([fin("f", now), mk("r", "running")], now)[0].id, "r");
chk("full page still has everything", spread.length, 3);
chk("more-than-dropdown is true when a stale task exists", S.hasMoreThanDropdown(spread, now), true);
chk("more-than-dropdown is false when all fit", S.hasMoreThanDropdown([mk("r", "running")], now), false);

// A long burst must not spill out of the dropdown.
let burst = [];
for (let i = 0; i < 12; i++) burst = S.addTask(burst, fin(`b${i}`, now - 1000));
chk("dropdown is capped", S.dropdownTasks(burst, now).length, S.TASK_DROPDOWN_LIMIT);
chk("capped dropdown reports more available", S.hasMoreThanDropdown(burst, now), true);

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
