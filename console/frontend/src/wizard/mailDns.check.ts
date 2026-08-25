import {
  aRecordValue,
  dkimPanelName,
  dnsZoneRelativeName,
  dmarcRecord,
  isPublicIPv4,
  parseAcmeChallengeFromLog,
  parseDkimFromInstallLog,
  parseMailDnsHintsFromLog,
  preparedMailDnsRecords,
  spfRecord,
} from "./mailDns";

function eq(actual: unknown, expected: unknown, label: string): void {
  const left = JSON.stringify(actual);
  const right = JSON.stringify(expected);
  if (left !== right) {
    throw new Error(`${label}: got ${left}, expected ${right}`);
  }
}

export function assertMailDnsContract(): void {
  eq(isPublicIPv4("203.0.113.10"), true, "public wan");
  eq(isPublicIPv4("10.10.40.3"), false, "rfc1918");
  eq(isPublicIPv4("192.168.1.1"), false, "rfc1918-16");
  eq(isPublicIPv4("172.16.0.1"), false, "rfc1918-12");
  eq(isPublicIPv4("127.0.0.1"), false, "loopback");
  eq(isPublicIPv4("169.254.1.1"), false, "link-local");
  eq(isPublicIPv4("100.64.1.1"), false, "cgnat");
  eq(isPublicIPv4("not-an-ip"), false, "garbage");

  eq(dnsZoneRelativeName("mail.example.test", "example.test"), { name: "mail", inZone: true }, "A name");
  eq(dnsZoneRelativeName("example.test", "example.test"), { name: "@", inZone: true }, "apex");
  eq(dnsZoneRelativeName("_dmarc.example.test", "example.test"), { name: "_dmarc", inZone: true }, "dmarc name");
  eq(dnsZoneRelativeName("mail.example.test.", "EXAMPLE.TEST"), { name: "mail", inZone: true }, "trailing-dot");
  eq(
    dnsZoneRelativeName("mail.other.example", "example.test"),
    { name: "mail.other.example", inZone: false },
    "outside zone",
  );
  eq(dkimPanelName("D027F21A._domainkey.example.test", "example.test"), "D027F21A._domainkey", "dkim fqdn");
  eq(dkimPanelName("D027F21A._domainkey", "example.test"), "D027F21A._domainkey", "dkim relative");

  eq(spfRecord("", ""), "v=spf1 mx ~all", "spf no ip");
  eq(spfRecord("203.0.113.11", "203.0.113.10"), "v=spf1 ip4:203.0.113.11 ip4:203.0.113.10 mx ~all", "spf nat");
  eq(spfRecord("203.0.113.11", "203.0.113.11"), "v=spf1 ip4:203.0.113.11 mx ~all", "spf same");
  eq(spfRecord("10.10.40.3", ""), "v=spf1 mx ~all", "spf private ignored");
  eq(aRecordValue("10.10.40.3", "", ""), "", "no private A");
  eq(aRecordValue("203.0.113.10", "203.0.113.11", ""), "203.0.113.10", "A prefers http");
  eq(aRecordValue("10.10.40.3", "203.0.113.11", ""), "203.0.113.11", "A falls back to smtp");
  eq(aRecordValue("203.0.113.10", "203.0.113.11", "203.0.113.9"), "203.0.113.9", "A prefers public VIP");
  eq(aRecordValue("203.0.113.10", "", "10.10.40.99"), "203.0.113.10", "private VIP ignored");

  eq(dmarcRecord("example.test", ""), "v=DMARC1; p=none; rua=mailto:admin@example.test", "dmarc default");
  eq(
    dmarcRecord("example.test", "ops@example.test"),
    "v=DMARC1; p=none; rua=mailto:ops@example.test",
    "dmarc rua on zone",
  );
  eq(
    dmarcRecord("example.test", "person@gmail.com"),
    "v=DMARC1; p=none; rua=mailto:admin@example.test",
    "dmarc ignores off-zone le email",
  );

  const rows = preparedMailDnsRecords({
    mailDomain: "example.test",
    mailHost: "mail.example.test",
    httpEgressIp: "203.0.113.10",
    smtpSendIp: "203.0.113.11",
  });
  if (!rows) throw new Error("rows missing");
  const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
  eq(byId.a.name, "mail", "row A name");
  eq(byId.a.value, "203.0.113.10", "row A value");
  eq(byId.a.pending, false, "row A ready");
  eq(byId.mx.name, "@", "row MX name");
  eq(byId.mx.value, "mail.example.test", "row MX target only");
  eq(byId.mx.mxPriority, "10", "row MX priority");
  eq(byId.mx.mxTarget, "mail.example.test", "row MX host");
  eq(byId.spf.name, "@", "row SPF name");
  eq(byId.spf.value, "v=spf1 ip4:203.0.113.11 ip4:203.0.113.10 mx ~all", "row SPF");
  eq(byId.dmarc.name, "_dmarc", "row DMARC name");
  eq(byId.dmarc.value, "v=DMARC1; p=none; rua=mailto:admin@example.test", "row DMARC");

  const pending = preparedMailDnsRecords({
    mailDomain: "example.test",
    mailHost: "mail.example.test",
  });
  if (!pending) throw new Error("pending rows missing");
  eq(pending[0].pending, true, "A pending without public IP");
  eq(pending[0].name, "mail", "A name still copyable");
  eq(pending[2].value, "v=spf1 mx ~all", "SPF mx until IP known");

  const hints = parseMailDnsHintsFromLog(`
info seen by ifconfig.me : 203.0.113.10
ok SMTP sending address is consistent: 203.0.113.11
`);
  eq(hints.httpEgressIp, "203.0.113.10", "log http ip");
  eq(hints.smtpSendIp, "203.0.113.11", "log smtp ip");

  const dkim = parseDkimFromInstallLog(`
  PASTE DKIM RECORD IN DNS PANEL (zone example.test)
  ----------------------------------------------------------------
  Type    : TXT
  Name    : D027F21A._domainkey
  TTL     : Auto / 300
  Content :
    "v=DKIM1; k=rsa; p=MIGf"
    "MA0GC"
  ----------------------------------------------------------------
`);
  eq(dkim?.name, "D027F21A._domainkey", "dkim parse name");
  eq(dkim?.value, "v=DKIM1; k=rsa; p=MIGfMA0GC", "dkim parse joined");

  const acme = parseAcmeChallengeFromLog(`
=== KIN Mail ACME DNS-01 challenge ===
Host/Name : _acme-challenge.mail.example.test
Type      : TXT
Value     : abcdef123
Waiting up to 25 minutes for public DNS
`);
  eq(acme?.host, "_acme-challenge.mail.example.test", "acme host");
  eq(acme?.value, "abcdef123", "acme value");
}

assertMailDnsContract();
