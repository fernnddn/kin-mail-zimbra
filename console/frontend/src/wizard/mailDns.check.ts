import {
  aRecordValue,
  dkimPanelName,
  dnsZoneRelativeName,
  dmarcRecord,
  isPublicIPv4,
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
  eq(isPublicIPv4("119.82.245.37"), true, "public wan");
  eq(isPublicIPv4("10.10.40.3"), false, "rfc1918");
  eq(isPublicIPv4("192.168.1.1"), false, "rfc1918-16");
  eq(isPublicIPv4("172.16.0.1"), false, "rfc1918-12");
  eq(isPublicIPv4("127.0.0.1"), false, "loopback");
  eq(isPublicIPv4("169.254.1.1"), false, "link-local");
  eq(isPublicIPv4("100.64.1.1"), false, "cgnat");
  eq(isPublicIPv4("not-an-ip"), false, "garbage");

  eq(dnsZoneRelativeName("mail.gits-it.site", "gits-it.site"), { name: "mail", inZone: true }, "A name");
  eq(dnsZoneRelativeName("gits-it.site", "gits-it.site"), { name: "@", inZone: true }, "apex");
  eq(dnsZoneRelativeName("_dmarc.gits-it.site", "gits-it.site"), { name: "_dmarc", inZone: true }, "dmarc name");
  eq(dnsZoneRelativeName("mail.gits-it.site.", "GITS-IT.SITE"), { name: "mail", inZone: true }, "trailing-dot");
  eq(
    dnsZoneRelativeName("mail.other.example", "gits-it.site"),
    { name: "mail.other.example", inZone: false },
    "outside zone",
  );
  eq(dkimPanelName("D027F21A._domainkey.gits-it.site", "gits-it.site"), "D027F21A._domainkey", "dkim fqdn");
  eq(dkimPanelName("D027F21A._domainkey", "gits-it.site"), "D027F21A._domainkey", "dkim relative");

  eq(spfRecord("", ""), "v=spf1 mx ~all", "spf no ip");
  eq(spfRecord("119.82.245.34", "119.82.245.37"), "v=spf1 ip4:119.82.245.34 ip4:119.82.245.37 mx ~all", "spf nat");
  eq(spfRecord("119.82.245.34", "119.82.245.34"), "v=spf1 ip4:119.82.245.34 mx ~all", "spf same");
  eq(spfRecord("10.10.40.3", ""), "v=spf1 mx ~all", "spf private ignored");
  eq(aRecordValue("10.10.40.3", "", ""), "", "no private A");
  eq(aRecordValue("119.82.245.37", "119.82.245.34", ""), "119.82.245.37", "A prefers http");
  eq(aRecordValue("10.10.40.3", "119.82.245.34", ""), "119.82.245.34", "A falls back to smtp");
  eq(aRecordValue("119.82.245.37", "119.82.245.34", "203.0.113.9"), "203.0.113.9", "A prefers public VIP");
  eq(aRecordValue("119.82.245.37", "", "10.10.40.99"), "119.82.245.37", "private VIP ignored");

  eq(dmarcRecord("gits-it.site", ""), "v=DMARC1; p=none; rua=mailto:admin@gits-it.site", "dmarc default");
  eq(
    dmarcRecord("gits-it.site", "ops@gits-it.site"),
    "v=DMARC1; p=none; rua=mailto:ops@gits-it.site",
    "dmarc rua on zone",
  );
  eq(
    dmarcRecord("gits-it.site", "person@gmail.com"),
    "v=DMARC1; p=none; rua=mailto:admin@gits-it.site",
    "dmarc ignores off-zone le email",
  );

  const rows = preparedMailDnsRecords({
    mailDomain: "gits-it.site",
    mailHost: "mail.gits-it.site",
    httpEgressIp: "119.82.245.37",
    smtpSendIp: "119.82.245.34",
  });
  if (!rows) throw new Error("rows missing");
  const byId = Object.fromEntries(rows.map((r) => [r.id, r]));
  eq(byId.a.name, "mail", "row A name");
  eq(byId.a.value, "119.82.245.37", "row A value");
  eq(byId.a.pending, false, "row A ready");
  eq(byId.mx.name, "@", "row MX name");
  eq(byId.mx.value, "mail.gits-it.site", "row MX target only");
  eq(byId.mx.mxPriority, "10", "row MX priority");
  eq(byId.mx.mxTarget, "mail.gits-it.site", "row MX host");
  eq(byId.spf.name, "@", "row SPF name");
  eq(byId.spf.value, "v=spf1 ip4:119.82.245.34 ip4:119.82.245.37 mx ~all", "row SPF");
  eq(byId.dmarc.name, "_dmarc", "row DMARC name");
  eq(byId.dmarc.value, "v=DMARC1; p=none; rua=mailto:admin@gits-it.site", "row DMARC");

  const pending = preparedMailDnsRecords({
    mailDomain: "gits-it.site",
    mailHost: "mail.gits-it.site",
  });
  if (!pending) throw new Error("pending rows missing");
  eq(pending[0].pending, true, "A pending without public IP");
  eq(pending[0].name, "mail", "A name still copyable");
  eq(pending[2].value, "v=spf1 mx ~all", "SPF mx until IP known");

  const hints = parseMailDnsHintsFromLog(`
info seen by ifconfig.me : 119.82.245.37
ok SMTP sending address is consistent: 119.82.245.34
`);
  eq(hints.httpEgressIp, "119.82.245.37", "log http ip");
  eq(hints.smtpSendIp, "119.82.245.34", "log smtp ip");

  const dkim = parseDkimFromInstallLog(`
  PASTE DKIM RECORD IN DNS PANEL (zone gits-it.site)
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
}

assertMailDnsContract();
