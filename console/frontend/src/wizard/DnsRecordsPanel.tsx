import { useState } from "react";
import styled from "@emotion/styled";
import { Button, Hint } from "../ui";
import { theme } from "../styles/theme";
import {
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
  margin-bottom: 0.35rem;
  font-size: 0.78rem;
  color: ${theme.muted};
`;

const TypePill = styled.span`
  font-weight: 700;
  letter-spacing: 0.04em;
  color: ${theme.ink};
`;

const Name = styled.span`
  font-family: ${theme.mono};
  font-size: 0.8rem;
  color: ${theme.ink};
  word-break: break-all;
`;

const Value = styled.pre`
  margin: 0;
  font-family: ${theme.mono};
  font-size: 0.78rem;
  line-height: 1.4;
  white-space: pre-wrap;
  word-break: break-all;
  color: ${theme.ink};
`;

const Pending = styled.span`
  color: ${theme.warn};
  font-size: 0.82rem;
`;

const RowFoot = styled.div`
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 0.75rem;
  margin-top: 0.5rem;
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

function DnsRow({ row }: { row: MailDnsRow }) {
  return (
    <Row>
      <Meta>
        <TypePill>{row.type}</TypePill>
        <Name>{row.name}</Name>
      </Meta>
      {row.pending ? (
        <Pending>Value not known here — wait for the public IP from the network team.</Pending>
      ) : (
        <Value>{row.value}</Value>
      )}
      <RowFoot>
        {!row.pending ? <CopyValue text={row.value} /> : null}
        {row.note ? <Note>{row.note}</Note> : null}
      </RowFoot>
    </Row>
  );
}

export function DnsRecordsPanel({
  mailDomain,
  mailHost,
  dkimPendingNote = true,
}: {
  mailDomain: string;
  mailHost: string;
  dkimPendingNote?: boolean;
}) {
  const rows = preparedMailDnsRecords({ mailDomain, mailHost });
  if (!rows) return null;
  return (
    <Panel>
      <Heading>DNS records to prepare</Heading>
      <Intro>
        Publish these at your DNS provider while install runs. Mail works after they are live —
        you do not need to wait until the last health-check step to look them up.
      </Intro>
      <Rows>
        {rows.map((row) => (
          <DnsRow key={row.id} row={row} />
        ))}
      </Rows>
      {dkimPendingNote ? (
        <Hint style={{ margin: "0.75rem 0 0" }}>
          The DKIM record will appear here after install reaches the Certificates & DKIM stage.
        </Hint>
      ) : null}
    </Panel>
  );
}

export function DkimPublishBox({ dkim }: { dkim: DkimDnsRecord | null }) {
  if (!dkim) return null;
  return (
    <ContinueBox>
      <ContinueHeading>Publish DKIM in your DNS panel</ContinueHeading>
      <Intro style={{ marginBottom: "0.65rem" }}>
        Certificates & DKIM has generated the key. Paste this as one TXT value (already joined —
        do not keep BIND line-breaks).
      </Intro>
      <Row>
        <Meta>
          <TypePill>TXT</TypePill>
          <Name>{dkim.name}</Name>
        </Meta>
        <Value>{dkim.value}</Value>
        <RowFoot>
          <CopyValue text={dkim.value} label="Copy value" />
          <CopyValue text={dkim.name} label="Copy name" />
        </RowFoot>
      </Row>
    </ContinueBox>
  );
}
