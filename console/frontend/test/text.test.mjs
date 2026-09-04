import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-txt-"));
const bundle = join(out, "text.mjs");
execFileSync(join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "lib", "text.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"], { stdio: "inherit" });
const { truncate, charsThatFit } = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0, fail = 0;
const chk = (n, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; console.log("ok  " + n); }
  else { fail++; console.log(`FAIL ${n}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`); }
};

// The contract that matters: SVG cannot clip, so a label that comes back
// longer than its budget draws over whatever sits next to it.
const within = (s, m) => s.length <= m;
chk("short text is untouched", truncate("Promoted", 24), "Promoted");
chk("exactly at the limit is untouched", truncate("abcdefghij", 10), "abcdefghij");
chk("one over the limit is shortened", truncate("abcdefghijk", 10), "abcdefg...");

// The bug: the old form returned max - 1 characters plus "...", i.e. max + 2.
chk("the result never exceeds the budget", within(truncate("abcdefghijk", 10), 10), true);
{
  let bad = [];
  for (let max = 1; max <= 40; max++) {
    for (const s of ["", "a", "mail2.helgaalan.my.id",
                     "Promoted - VIP 2001:0db8:85a3:0000:0000:8a2e:0370:7334",
                     "x".repeat(200)]) {
      if (!within(truncate(s, max), max)) bad.push(`${max}:${JSON.stringify(s.slice(0, 12))}`);
    }
  }
  chk("no budget from 1 to 40 is ever exceeded", bad, []);
}

// Degenerate budgets must not produce something longer than asked for, and
// must not throw.
chk("a budget of 3 has no room for an ellipsis", truncate("abcdef", 3), "abc");
chk("a budget of 1", truncate("abcdef", 1), "a");
chk("a budget of 0 is empty", truncate("abcdef", 0), "");
chk("a negative budget is empty", truncate("abcdef", -5), "");
chk("empty input survives", truncate("", 10), "");

// charsThatFit states the width instead of hard-coding a character count.
chk("fits by width", charsThatFit(140, 5.6), 25);
chk("no width fits nothing", charsThatFit(0, 5.6), 0);
chk("negative width fits nothing", charsThatFit(-40, 5.6), 0);
chk("a zero glyph width does not divide by zero", charsThatFit(140, 0), 0);

// The two used together must fit the card the topology actually draws.
{
  const CARD_W = 200;
  const budget = charsThatFit(CARD_W - 2 * 20 - 20, 5.6);
  const worst = truncate("Unpromoted - VIP 2001:0db8:85a3:0000:0000:8a2e:0370:7334", budget);
  chk("the widest realistic badge fits the pill", worst.length * 5.6 <= CARD_W - 2 * 20, true);
}

console.log("");
if (fail === 0) { console.log(`ALL OK (${pass} checks)`); process.exit(0); }
console.log(`FAILED ${fail} of ${pass + fail}`); process.exit(1);
