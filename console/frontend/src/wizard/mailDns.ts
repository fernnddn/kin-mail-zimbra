import { stripAnsi } from "./ansi";

/** One external DNS row the operator can publish (Cloudflare-style Name + Content). */
export type MailDnsRow = {
  id: "a" | "mx" | "spf" | "dmarc";
  type: "A" | "MX" | "TXT";
  /** Relative Name for the zone panel: `mail`, `@`, `_dmarc`. */
  name: string;
  /** Content / Value to paste. For MX this is the mail-server hostname only. */
  value: string;
  /** True when the value is a placeholder, not paste-ready. */
  pending: boolean;
  note?: string;
  /** Absolute name this record lives at (hint only — do not paste as Name). */
  fqdnHint?: string;
  mxPriority?: string;
  mxTarget?: string;
  /** True when Name is not in the mail zone (create the A in the other zone). */
  nameOutsideZone?: boolean;
};

export type DkimDnsRecord = {
  /** Relative name, e.g. `default._domainkey` */
  name: string;
  /** Single-line TXT content (quoted BIND fragments already joined). */
  value: string;
};

export type MailDnsHints = {
  httpEgressIp: string;
  smtpSendIp: string;
};

export type AcmeTxtRecord = {
  host: string;
  value: string;
};

const IPV4_RE =
  /^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

export function fqdn(raw: string): string {
  return raw.trim().replace(/\.$/, "");
}

export function isPublicIPv4(ip: string): boolean {
  const t = ip.trim();
  if (!IPV4_RE.test(t)) return false;
  const [a, b] = t.split(".").map((n) => Number(n));
  if (a === 10 || a === 127 || a === 0) return false;
  if (a === 169 && b === 254) return false;
  if (a === 192 && b === 168) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a >= 224) return false;
  return true;
}

/**
 * Name field for typical DNS panels (Cloudflare, Route 53, cPanel, Niagahoster).
 * Relative to the zone so `mail.gits-it.site` is pasted as `mail`, not the FQDN.
 */
export function dnsZoneRelativeName(
  recordFqdn: string,
  zone: string,
): { name: string; inZone: boolean } {
  const hostRaw = fqdn(recordFqdn);
  const zoneRaw = fqdn(zone);
  const host = hostRaw.toLowerCase();
  const z = zoneRaw.toLowerCase();
  if (!host || !z) return { name: hostRaw, inZone: false };
  if (host === z) return { name: "@", inZone: true };
  const suffix = `.${z}`;
  if (host.endsWith(suffix)) {
    return { name: hostRaw.slice(0, host.length - suffix.length), inZone: true };
  }
  return { name: hostRaw, inZone: false };
}

export function dkimPanelName(rawName: string, zone: string): string {
  const trimmed = fqdn(rawName);
  if (!trimmed) return trimmed;
  return dnsZoneRelativeName(trimmed, zone).name;
}

export function spfRecord(smtpSendIp: string, httpEgressIp: string): string {
  const ips: string[] = [];
  for (const ip of [smtpSendIp, httpEgressIp]) {
    if (isPublicIPv4(ip) && !ips.includes(ip.trim())) ips.push(ip.trim());
  }
  if (ips.length === 0) return "v=spf1 mx ~all";
  return `v=spf1 ${ips.map((ip) => `ip4:${ip}`).join(" ")} mx ~all`;
}

export function dmarcRecord(domain: string, preferredEmail = ""): string {
  const z = fqdn(domain).toLowerCase();
  const email = preferredEmail.trim();
  let rua = z ? `admin@${z}` : "";
  if (z && /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
    const at = email.toLowerCase();
    if (at.endsWith(`@${z}`)) rua = email;
  }
  return `v=DMARC1; p=none; rua=mailto:${rua}`;
}

export function aRecordValue(
  httpEgressIp: string,
  smtpSendIp: string,
  preferredAIp = "",
): string {
  if (isPublicIPv4(preferredAIp)) return preferredAIp.trim();
  if (isPublicIPv4(httpEgressIp)) return httpEgressIp.trim();
  if (isPublicIPv4(smtpSendIp)) return smtpSendIp.trim();
  return "";
}

/**
 * MX / SPF / DMARC (and A when a public IP is known) as soon as domain + mail
 * host are set. DKIM is not included — it only exists after 04-tls-dkim.sh.
 */
export function preparedMailDnsRecords(opts: {
  mailDomain: string;
  mailHost: string;
  httpEgressIp?: string;
  smtpSendIp?: string;
  preferredAIp?: string;
  ruaEmail?: string;
}): MailDnsRow[] | null {
  const domain = fqdn(opts.mailDomain).toLowerCase();
  if (!domain) return null;
  const host = (fqdn(opts.mailHost) || `mail.${domain}`).toLowerCase();
  const httpIp = (opts.httpEgressIp || "").trim();
  const smtpIp = (opts.smtpSendIp || "").trim();
  const preferred = (opts.preferredAIp || "").trim();
  const aValue = aRecordValue(httpIp, smtpIp, preferred);
  const aName = dnsZoneRelativeName(host, domain);
  const mxName = dnsZoneRelativeName(domain, domain);
  const dmarcName = dnsZoneRelativeName(`_dmarc.${domain}`, domain);
  const spf = spfRecord(smtpIp, httpIp);

  const aNote = (() => {
    if (!aValue) {
      return "IPv4 is the public address hosts use to reach this mail server, not a private 10.x/192.168 address. Fill it when the network team has the WAN IP.";
    }
    if (isPublicIPv4(preferred) && aValue === preferred) {
      return "This is the cluster VIP. Point the A record here so inbound mail follows the floating address.";
    }
    if (isPublicIPv4(smtpIp) && isPublicIPv4(httpIp) && smtpIp !== httpIp) {
      return `HTTPS currently egresses ${httpIp}; SMTP sends from ${smtpIp}. Point A at the IP that should receive mail (usually the SMTP address), and keep ip4:${smtpIp} in SPF.`;
    }
    if (isPublicIPv4(smtpIp) && aValue === smtpIp) {
      return "A matches the SMTP sending address, so SPF mx and ip4 agree.";
    }
    return "This is the public IPv4 seen from this server. Confirm it is the address that should receive inbound mail.";
  })();

  const spfNote = aValue
    ? isPublicIPv4(smtpIp)
      ? `ip4:${smtpIp} authorizes outbound mail even if A and SMTP NAT differ. Do not wrap the TXT in quotes.`
      : "ip4: authorizes this server's public address. Do not wrap the TXT in quotes."
    : "mx alone only works after the A of the MX host is the sending IP. Add ip4: when you know the public send address. Do not wrap the TXT in quotes.";

  return [
    {
      id: "a",
      type: "A",
      name: aName.name,
      value: aValue,
      pending: !aValue,
      fqdnHint: host,
      nameOutsideZone: !aName.inZone,
      note: aName.inZone
        ? aNote
        : `Hostname is outside zone ${domain} — create this A in the zone that owns ${host}.`,
    },
    {
      id: "mx",
      type: "MX",
      name: mxName.name,
      value: host,
      pending: false,
      fqdnHint: domain,
      mxPriority: "10",
      mxTarget: host,
      note: "Priority and mail server are separate fields. Do not paste '10 mail...' into one box. Apex Name is @; some panels want a blank name or the domain itself.",
    },
    {
      id: "spf",
      type: "TXT",
      name: mxName.name,
      value: spf,
      pending: false,
      fqdnHint: domain,
      note: spfNote,
    },
    {
      id: "dmarc",
      type: "TXT",
      name: dmarcName.name,
      value: dmarcRecord(domain, opts.ruaEmail || ""),
      pending: false,
      fqdnHint: `_dmarc.${domain}`,
      note: "p=none is monitor-only for a first install. Do not wrap the TXT in quotes.",
    },
  ];
}

export function parseMailDnsHintsFromLog(log: string): MailDnsHints {
  const text = stripAnsi(log);
  const http =
    text.match(/seen by ifconfig\.me\s*:\s*([0-9.]+)/i)?.[1] ||
    text.match(/seen by ipify\s*:\s*([0-9.]+)/i)?.[1] ||
    text.match(/HTTP egress looks stable at\s+([0-9.]+)/i)?.[1] ||
    "";
  const smtp =
    text.match(/SMTP sending address is consistent:\s*([0-9.]+)/i)?.[1] ||
    text.match(/Consistent:\s*([0-9.]+)/i)?.[1] ||
    "";
  return {
    httpEgressIp: isPublicIPv4(http) ? http : "",
    smtpSendIp: isPublicIPv4(smtp) ? smtp : "",
  };
}

/** Join zone-file / folded DKIM TXT into one Cloudflare-ready string. */
export function joinDkimTxtValue(raw: string): string {
  const quoted = [...raw.matchAll(/"([^"]*)"/g)].map((m) => m[1]);
  if (quoted.length > 0) {
    return quoted.join("").trim();
  }
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("")
    .replace(/[()]/g, "")
    .trim();
}

/**
 * Read Name + Content from the 04-tls-dkim.sh "PASTE DKIM RECORD IN DNS PANEL"
 * block. Uses the last occurrence so a re-run of stage 04 still wins.
 */
export function parseDkimFromInstallLog(log: string): DkimDnsRecord | null {
  const text = stripAnsi(log);
  const marker = "PASTE DKIM RECORD IN DNS PANEL";
  const idx = text.lastIndexOf(marker);
  if (idx < 0) return null;
  const block = text.slice(idx, idx + 8000);
  const nameMatch = block.match(/Name\s*:\s*(\S+)/i);
  const name = (nameMatch?.[1] || "").trim();
  const contentMatch = block.match(/Content\s*:([\s\S]*?)(?:\n\s*-{5,}|\n\s*\[|$)/i);
  if (!name || !contentMatch) return null;
  const value = joinDkimTxtValue(contentMatch[1] || "");
  if (!value || !/v=DKIM1/i.test(value)) return null;
  return { name, value };
}

/** TXT name + value printed by 04-tls-dkim.sh poll-mode ACME hook. */
export function parseAcmeChallengeFromLog(log: string): AcmeTxtRecord | null {
  const text = stripAnsi(log);
  const marker = "KIN Mail ACME DNS-01 challenge";
  const idx = text.lastIndexOf(marker);
  if (idx < 0) return null;
  const block = text.slice(idx, idx + 2000);
  const host =
    block.match(/Host\/Name\s*:\s*(\S+)/i)?.[1]?.trim() ||
    block.match(/_acme-challenge\.[^\s]+/i)?.[0]?.trim() ||
    "";
  const value = (block.match(/Value\s*:\s*(\S+)/i)?.[1] || "").trim();
  if (!host || !value) return null;
  return { host, value };
}
