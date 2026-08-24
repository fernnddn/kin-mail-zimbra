import { useEffect, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { Button, Hint } from "../ui";
import { theme } from "../styles/theme";
import {
  dkimPanelName,
  dnsZoneRelativeName,
  parseAcmeChallengeFromLog,
  parseMailDnsHintsFromLog,
  preparedMailDnsRecords,
  type DkimDnsRecord,
  type MailDnsRow,
} from "./mailDns";

const Panel = styled.section`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1rem 1.05rem;
  margin: 0 0 1rem;
`;

const Heading = styled.h2`
  margin: 0 0 0.35rem;
  font-size: 0.95rem;
  font-weight: 650;
`;

const Intro = styled.p`
  margin: 0 0 0.85rem;
  color: ${theme.muted};
  font-size: 0.86rem;
  line-height: 1.45;
`;

const Rows = styled.div`
  display: grid;
  gap: 0.65rem;
`;

const Row = styled.div`
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  background: ${theme.bgPanel};
  padding: 0.7rem 0.8rem;
`;

const Meta = styled.div`
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.45rem 0.85rem;
  margin-bottom: 0.45rem;
  font-size: 0.78rem;
  color: ${theme.muted};
`;

const TypePill = styled.span`
  font-weight: 700;
  letter-spacing: 0.04em;
  color: ${theme.ink};
`;

const ZoneHint = styled.span`
  font-family: ${theme.mono};
  font-size: 0.78rem;
`;

const Fields = styled.div`
  display: grid;
  gap: 0.4rem;
`;

const Field = styled.div`
  display: grid;
  grid-template-columns: 7.2rem minmax(0, 1fr) auto;
  gap: 0.45rem 0.65rem;
  align-items: start;
  @media (max-width: 640px) {
    grid-template-columns: 1fr auto;
    grid-template-areas:
      "label copy"
      "value value";
  }
`;

const FieldLabel = styled.span`
  font-size: 0.72rem;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: ${theme.muted};
  padding-top: 0.2rem;
  @media (max-width: 640px) {
    grid-area: label;
  }
`;

const FieldValue = styled.pre`
  margin: 0;
  font-family: ${theme.mono};
  font-size: 0.78rem;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-all;
  color: ${theme.ink};
  padding-top: 0.12rem;
  @media (max-width: 640px) {
    grid-area: value;
  }
`;

const Pending = styled.span`
  color: ${theme.warn};
  font-size: 0.82rem;
  @media (max-width: 640px) {
    grid-area: value;
  }
`;

const CopySlot = styled.div`
  @media (max-width: 640px) {
    grid-area: copy;
    justify-self: end;
  }
`;

const RowFoot = styled.div`
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 0.75rem;
  margin-top: 0.55rem;
`;

const Note = styled.p`
  margin: 0;
  font-size: 0.78rem;
  color: ${theme.muted};
  line-height: 1.4;
  flex: 1;
  min-width: 12rem;
`;

const ContinueBox = styled.div`
  margin-top: 1rem;
  padding: 0.9rem 1rem;
  border: 1px solid color-mix(in srgb, ${theme.accent} 40%, ${theme.line});
  background: ${theme.accentSoft};
  border-radius: ${theme.radius.md};
`;

const ContinueHeading = styled.p`
  margin: 0 0 0.35rem;
  font-weight: 650;
  font-size: 0.92rem;
`;

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function CopyValue({
  text,
  label = "Copy",
  disabled,
}: {
  text: string;
  label?: string;
  disabled?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="secondary"
      disabled={disabled || !text}
      style={{ padding: "0.35rem 0.65rem", fontSize: "0.78rem", fontWeight: 600 }}
      onClick={() => {
        void copyText(text).then((ok) => {
          if (!ok) return;
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1600);
        });
      }}
    >
      {copied ? "Copied" : label}
    </Button>
  );
}

function DnsField({
  label,
  value,
  pendingText,
  copyLabel = "Copy",
}: {
  label: string;
  value: string;
  pendingText?: string;
  copyLabel?: string;
}) {
  const pending = Boolean(pendingText);
  return (
    <Field>
      <FieldLabel>{label}</FieldLabel>
      {pending ? <Pending>{pendingText}</Pending> : <FieldValue>{value}</FieldValue>}
      <CopySlot>
        <CopyValue text={value} label={copyLabel} disabled={pending || !value} />
      </CopySlot>
    </Field>
  );
}

function DnsRow({ row, lookingUpIp }: { row: MailDnsRow; lookingUpIp?: boolean }) {
  const aPendingText = lookingUpIp
    ? "Looking up this server's public IPv4…"
    : "Not known here yet - paste the public IPv4 from the network team (not 10.x / 192.168.x).";
  return (
    <Row>
      <Meta>
        <TypePill>{row.type}</TypePill>
        {row.fqdnHint ? <ZoneHint>resolves {row.fqdnHint}</ZoneHint> : null}
      </Meta>
      <Fields>
        <DnsField label="Host / Name" value={row.name} />
        {row.type === "MX" ? (
          <>
            <DnsField label="Priority" value={row.mxPriority || "10"} />
            <DnsField label="Mail server" value={row.mxTarget || row.value} />
          </>
        ) : (
          <DnsField
            label={row.type === "A" ? "IPv4" : "Content"}
            value={row.value}
            pendingText={row.pending ? aPendingText : undefined}
          />
        )}
      </Fields>
      <RowFoot>{row.note ? <Note>{row.note}</Note> : null}</RowFoot>
    </Row>
  );
}

export function DnsRecordsPanel({
  mailDomain,
  mailHost,
  ruaEmail = "",
  preferredAIp = "",
  installLog = "",
  dkimPendingNote = true,
}: {
  mailDomain: string;
  mailHost: string;
  ruaEmail?: string;
  preferredAIp?: string;
  installLog?: string;
  dkimPendingNote?: boolean;
}) {
  const [probedIp, setProbedIp] = useState("");
  const [lookingUpIp, setLookingUpIp] = useState(true);
  useEffect(() => {
    let cancelled = false;
    setLookingUpIp(true);
    void api<{ ipv4?: string }>("/api/wizard/public-ip")
      .then((data) => {
        if (!cancelled) setProbedIp((data.ipv4 || "").trim());
      })
      .catch(() => {
        if (!cancelled) setProbedIp("");
      })
      .finally(() => {
        if (!cancelled) setLookingUpIp(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const fromLog = parseMailDnsHintsFromLog(installLog);
  const httpEgressIp = fromLog.httpEgressIp || probedIp;
  const rows = preparedMailDnsRecords({
    mailDomain,
    mailHost,
    httpEgressIp,
    smtpSendIp: fromLog.smtpSendIp,
    preferredAIp,
    ruaEmail,
  });
  if (!rows) return null;
  const zone = mailDomain.trim().replace(/\.$/, "");
  const aPending = Boolean(rows.find((r) => r.id === "a")?.pending);
  return (
    <Panel>
      <Heading>DNS records to prepare</Heading>
      <Intro>
        Paste these into zone <strong>{zone}</strong>. Host / Name is relative to that zone
        (<code>@</code>, <code>mail</code>, <code>_dmarc</code>) - do not paste the full hostname
        or the panel will create a doubled name. TTL Auto or 300. TXT content has no quotes.
      </Intro>
      <Rows>
        {rows.map((row) => (
          <DnsRow
            key={row.id}
            row={row}
            lookingUpIp={row.id === "a" && row.pending && lookingUpIp}
          />
        ))}
      </Rows>
      {dkimPendingNote ? (
        <Hint style={{ margin: "0.75rem 0 0" }}>
          The DKIM TXT appears here after install reaches Certificates & DKIM. Publish it as
          soon as it shows - you do not have to wait for the last health-check step.
        </Hint>
      ) : null}
      {aPending && !lookingUpIp ? (
        <Hint style={{ margin: "0.55rem 0 0" }}>
          This console could not see a public IPv4 (the host may only have a private NIC). Ask
          the network team for the WAN address that should receive mail, then paste it as the A
          record.
        </Hint>
      ) : null}
    </Panel>
  );
}

export function DkimPublishBox({
  dkim,
  mailDomain = "",
}: {
  dkim: DkimDnsRecord | null;
  mailDomain?: string;
}) {
  if (!dkim) return null;
  const name = mailDomain ? dkimPanelName(dkim.name, mailDomain) : dkim.name;
  return (
    <ContinueBox>
      <ContinueHeading>Publish DKIM in your DNS panel</ContinueHeading>
      <Intro style={{ marginBottom: "0.65rem" }}>
        Certificates & DKIM has generated the key. Host / Name is relative to the zone. Paste
        Content as one TXT value (already joined, no quotes, no BIND line-breaks).
      </Intro>
      <Row>
        <Meta>
          <TypePill>TXT</TypePill>
          <ZoneHint>TTL Auto / 300</ZoneHint>
        </Meta>
        <Fields>
          <DnsField label="Host / Name" value={name} />
          <DnsField label="Content" value={dkim.value} />
        </Fields>
      </Row>
    </ContinueBox>
  );
}

export function AcmeChallengePanel({
  log,
  mailDomain = "",
}: {
  log: string;
  mailDomain?: string;
}) {
  const rec = parseAcmeChallengeFromLog(log);
  if (!rec) return null;
  const relative = mailDomain ? dnsZoneRelativeName(rec.host, mailDomain).name : rec.host;
  return (
    <ContinueBox>
      <ContinueHeading>Publish this ACME TXT record</ContinueHeading>
      <Intro style={{ marginBottom: "0.65rem" }}>
        Let&apos;s Encrypt is waiting for this TXT. Paste Host / Name relative to zone{" "}
        <strong>{mailDomain || "your mail domain"}</strong>, then wait. This page watches DNS
        until the certificate is issued.
      </Intro>
      <Row>
        <Meta>
          <TypePill>TXT</TypePill>
          <ZoneHint>TTL Auto / 300</ZoneHint>
        </Meta>
        <Fields>
          <DnsField label="Host / Name" value={relative} />
          <DnsField label="Content" value={rec.value} />
        </Fields>
      </Row>
    </ContinueBox>
  );
}
