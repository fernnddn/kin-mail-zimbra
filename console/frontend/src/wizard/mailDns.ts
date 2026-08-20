import { stripAnsi } from "./ansi";

/** One external DNS row the operator can publish (Cloudflare-style Name + Content). */
export type MailDnsRow = {
  id: "a" | "mx" | "spf" | "dmarc";
  type: "A" | "MX" | "TXT";
  name: string;
  value: string;
  /** True when the value is a placeholder, not paste-ready. */
  pending: boolean;
  note?: string;
};

export type DkimDnsRecord = {
  /** Relative name, e.g. `default._domainkey` */
  name: string;
  /** Single-line TXT content (quoted BIND fragments already joined). */
  value: string;
};

function fqdn(raw: string): string {
  return raw.trim().replace(/\.$/, "");
}

/**
 * MX / SPF / DMARC (and the A placeholder) known as soon as domain + mail host
 * are set. DKIM is not included — it only exists after 04-tls-dkim.sh.
 */
export function preparedMailDnsRecords(opts: {
  mailDomain: string;
  mailHost: string;
}): MailDnsRow[] | null {
  const domain = fqdn(opts.mailDomain);
  if (!domain) return null;
  const host = fqdn(opts.mailHost) || `mail.${domain}`;
  return [
    {
      id: "a",
      type: "A",
      name: host,
      value: "",
      pending: true,
      note: "A record needs the public IP from the network team — prepare that in parallel.",
    },
    {
      id: "mx",
      type: "MX",
      name: domain,
      value: `10 ${host}`,
      pending: false,
    },
    {
      id: "spf",
      type: "TXT",
      name: domain,
      value: "v=spf1 mx ~all",
      pending: false,
    },
    {
      id: "dmarc",
      type: "TXT",
      name: `_dmarc.${domain}`,
      value: `v=DMARC1; p=none; rua=mailto:admin@${domain}`,
      pending: false,
    },
  ];
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
