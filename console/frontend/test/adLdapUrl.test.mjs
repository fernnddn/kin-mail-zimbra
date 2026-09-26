// One character, and the whole directory integration is dead.
//
// A deploy on 26 September 2026 was configured with "daps://10.0.0.200:636".
// The leading "l" was missing. It passed the wizard, passed validate_draft, was
// written into the config, copied onto the Zimbra domain as zimbraAuthLdapURL,
// and forty minutes later produced "Could not parse LDAP URI(s) (3)".
//
// Three symptoms, none of which named the cause: no mailbox was created for
// anyone in the directory; the certificate-trust stage skipped itself silently,
// because it only acts on a URL starting ldaps://; and nobody could sign in.
// Until then the only check on this field was that it was not empty.
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const out = mkdtempSync(join(tmpdir(), "kin-adurl-"));
const bundle = join(out, "adLdapUrl.mjs");
execFileSync(
  join(here, "..", "node_modules", ".bin", "esbuild"),
  [join(here, "..", "src", "wizard", "adLdapUrl.ts"), "--bundle", "--format=esm",
   `--outfile=${bundle}`, "--log-level=error"],
  { stdio: "inherit" },
);
const { adLdapUrlError, adLdapUrlIsSecure } = await import(pathToFileURL(bundle).href);
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
const bad = (u) => adLdapUrlError(u) !== "";

// --- the one that actually happened ------------------------------------------
chk("daps:// is refused", bad("daps://10.0.0.200:636"), true);
chk("and the message names LDAP rather than just 'invalid'",
  /ldaps:\/\//.test(adLdapUrlError("daps://10.0.0.200:636")), true);

// --- other ways to mistype a scheme -------------------------------------------
chk("ldpas:// is refused", bad("ldpas://dc.example.test"), true);
chk("lda:// is refused", bad("lda://dc.example.test"), true);
chk("http:// is refused", bad("http://dc.example.test"), true);
chk("a bare host is refused", bad("dc.example.test:636"), true);
chk("...and is told what to add", /Start the address with ldaps:\/\//.test(
  adLdapUrlError("dc.example.test:636")), true);
chk("a scheme with no host is refused", bad("ldaps://"), true);
chk("a space inside is refused", bad("ldaps://dc.example.test :636"), true);

// --- what must keep working ---------------------------------------------------
chk("ldaps with a hostname is accepted", bad("ldaps://dc.example.test:636"), false);
chk("ldaps with an address is accepted", bad("ldaps://192.0.2.10:636"), false);
chk("plain ldap is still accepted", bad("ldap://dc.example.test:389"), false);
chk("no port is fine", bad("ldaps://dc.example.test"), false);
chk("upper case scheme is fine", bad("LDAPS://dc.example.test"), false);
chk("surrounding space is trimmed, not rejected", bad("  ldaps://dc.example.test  "), false);
// Empty is the other step's problem: the field is already checked for presence,
// and two errors about one box is how an operator stops reading them.
chk("empty is left to the required-fields check", bad(""), false);

// --- the scheme decides whether a CA has to be trusted ------------------------
// 14-ad-trust.sh acts only on ldaps://, which is why "daps://" made it skip
// itself without a word.
chk("ldaps is recognised as needing a trusted CA", adLdapUrlIsSecure("ldaps://dc.example.test"), true);
chk("plain ldap is not", adLdapUrlIsSecure("ldap://dc.example.test"), false);
chk("and neither is the typo", adLdapUrlIsSecure("daps://dc.example.test"), false);

console.log(fail ? `\n${fail} failure(s)` : `\nALL OK (${pass} checks)`);
process.exit(fail ? 1 : 0);
