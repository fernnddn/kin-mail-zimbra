// The seat count is optional, right up until a directory is configured.
//
// QA, 26 September 2026: a screenshot of Active Directory Users and Computers
// showing six people beside the Zimbra admin console showing four accounts,
// none of them from the directory. Same report on 24 and 25 September. Every
// time, the cause was the same field left blank: the AD sync creates a mailbox
// per person, each one spends a seat, and an unset count refuses all of them.
//
// The deploy still finishes. Authentication is still configured. The timer is
// still installed. Nobody can sign in.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-seat-"));
const bundle = join(out, "seatRule.mjs");
execFileSync(
  join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "wizard", "seatRule.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"],
  { stdio: "inherit" },
);
const { seatCountError, normaliseSeats, seatsAreUnlimited } = await import(pathToFileURL(bundle).href);
rmSync(out, { recursive: true, force: true });

let pass = 0;
let fail = 0;
const chk = (name, got, want) => {
  if (JSON.stringify(got) === JSON.stringify(want)) {
    pass++;
    console.log("ok  " + name);
  } else {
    fail++;
    console.log(`FAIL ${name}\n  got  ${JSON.stringify(got)}\n  want ${JSON.stringify(want)}`);
  }
};
const blocked = (seats, adEnabled) => seatCountError({ seats, adEnabled }) !== "";

// --- with a directory, blank is the failure this exists to stop --------------
chk("blank is refused when AD is enabled", blocked("", true), true);
chk("and so is the placeholder the draft starts with", blocked("PLACEHOLDER_UNSET", true), true);
chk("the message says why, not just that it is required",
  /no AD user can sign in/.test(seatCountError({ seats: "", adEnabled: true })), true);
// Zero passes "is it a whole number" and then refuses every create, which is
// the same empty console by a different route.
chk("zero seats is refused when AD is enabled", blocked("0", true), true);
chk("a real number is accepted", blocked("150", true), false);

// --- without a directory it stays optional, exactly as before -----------------
// Mailboxes made by hand are a different trade: the gate refusing until someone
// sets a number is a reasonable thing to defer, and this step must not start
// demanding a commercial figure on the day the appliance is built.
chk("blank is still fine with no directory", blocked("", false), false);
chk("so is the placeholder", blocked("PLACEHOLDER_UNSET", false), false);
chk("zero is still allowed with no directory", blocked("0", false), false);

// --- a number is still a number ----------------------------------------------
chk("text is refused either way", [blocked("ten", true), blocked("ten", false)], [true, true]);
chk("a negative is refused either way", [blocked("-5", true), blocked("-5", false)], [true, true]);
chk("surrounding space does not change the answer", blocked("  150  ", true), false);
chk("the offer to leave it blank only appears when that is true",
  /or left blank/.test(seatCountError({ seats: "ten", adEnabled: true })), false);

// --- what gets stored ---------------------------------------------------------
chk("blank is stored as the placeholder", normaliseSeats(""), "PLACEHOLDER_UNSET");
chk("a number is stored trimmed", normaliseSeats("  150 "), "150");

// --- "no limit" is a third answer, not a blank one ---------------------------
// Asked for on 27 Sep 2026: a customer whose headcount for next year is not
// known. Picking a number there means either refusing real joiners later or
// inventing a figure nobody agreed to.
//
// It has to be a word the operator chooses. Reading an empty box as "no limit"
// would put the gate one typo away from not existing - and empty already means
// "nobody has said yet", which is the opposite answer.
chk("unlimited satisfies the AD requirement", blocked("unlimited", true), false);
chk("and is fine with no directory too", blocked("unlimited", false), false);
chk("any case is accepted", [blocked("UNLIMITED", true), blocked("UnLiMiTeD", true)], [false, false]);
chk("surrounding space does not break it", blocked("  unlimited  ", true), false);
chk("it is stored as one spelling", normaliseSeats("  UNLIMITED "), "unlimited");
chk("blank is still not unlimited", blocked("", true), true);
chk("and the placeholder is still not unlimited", blocked("PLACEHOLDER_UNSET", true), true);
chk("the flag says so", [seatsAreUnlimited("unlimited"), seatsAreUnlimited("UNLIMITED ")], [true, true]);
chk("...and only for that word", [seatsAreUnlimited(""), seatsAreUnlimited("150"),
  seatsAreUnlimited("PLACEHOLDER_UNSET"), seatsAreUnlimited("unlimited-ish")],
  [false, false, false, false]);
// Zero is still refused with AD on. "No cap" and "a cap of none" read alike in
// English and are opposite in effect.
chk("zero is still not a way to say unlimited", blocked("0", true), true);

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
