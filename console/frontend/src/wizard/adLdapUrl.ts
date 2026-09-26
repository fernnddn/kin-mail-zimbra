/** Is this actually an LDAP URL?
 *
 * Until now the only check on this field was that it was not empty. A deploy on
 * 26 September 2026 was configured with
 *
 *   AD_LDAP_URL="daps://10.10.40.200:636"
 *
 * - the leading "l" lost somewhere between the operator's head and the box.
 * It passed the wizard, passed validate_draft, was written into the config,
 * copied onto the domain as zimbraAuthLdapURL, and forty minutes later
 * produced:
 *
 *   Could not parse LDAP URI(s)=daps://10.10.40.200:636 (3)
 *
 * The cost of that one character was the whole feature: no mailbox was created
 * for anyone in the directory, the certificate-trust stage skipped itself
 * because the URL did not start with ldaps://, and nobody could sign in. Three
 * separate symptoms, one typo, and nothing anywhere pointed at it.
 *
 * A scheme is the one part of this field that can be checked without guessing.
 */
export function adLdapUrlError(url: string): string {
  const raw = (url || "").trim();
  if (!raw) return "";

  const scheme = raw.match(/^([A-Za-z][A-Za-z0-9+.-]*):\/\//);
  if (!scheme) {
    // Usually a host typed on its own. Say what to add rather than what is
    // wrong - the operator knows the address, not the URL grammar.
    return (
      "Start the address with ldaps:// (or ldap:// for an unencrypted " +
      `directory). For example: ldaps://${raw.split("/")[0] || "dc.example.test"}`
    );
  }

  const name = scheme[1].toLowerCase();
  if (name !== "ldap" && name !== "ldaps") {
    return `"${scheme[1]}://" is not an LDAP address. Use ldaps:// or ldap://.`;
  }

  const rest = raw.slice(scheme[0].length);
  if (!rest || rest.startsWith("/") || rest.startsWith(":")) {
    return "The address has no host. For example: ldaps://dc.example.test:636";
  }
  if (/\s/.test(raw)) {
    return "The address contains a space.";
  }
  return "";
}

/** True when this URL asks for LDAPS, which is what needs a trusted CA.
 *
 * The certificate-trust stage keys off exactly this, and on the deploy above it
 * silently did nothing because "daps://" is not "ldaps://".
 */
export function adLdapUrlIsSecure(url: string): boolean {
  return /^ldaps:\/\//i.test((url || "").trim());
}
