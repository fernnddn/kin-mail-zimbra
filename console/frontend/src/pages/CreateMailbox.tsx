import { FormEvent, useEffect, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import { Button, Hint, Input, Label, Lede, LogPane, Title, WarnBox } from "../ui";

type StatusResp = {
  ok: boolean;
  exit_code: number;
  error?: string | null;
  log: string;
};

type CreateResp = {
  ok: boolean;
  exit_code: number;
  error?: string | null;
  log: string;
  local_part: string;
};

const Page = styled.div`
  padding: 1.25rem 1.5rem 2rem;
  max-width: 720px;
  width: 100%;
`;

const FormGrid = styled.form`
  display: grid;
  gap: 0;
  margin: 1rem 0;
`;

const DomainSuffix = styled.span`
  color: ${theme.muted};
  font-size: 0.95rem;
  align-self: center;
  margin-bottom: 0.9rem;
  margin-left: 0.35rem;
`;

const LocalRow = styled.div`
  display: flex;
  align-items: baseline;
  gap: 0.15rem;
  margin-bottom: 0;

  input {
    flex: 1;
    margin-bottom: 0.9rem;
  }
`;

function parseDomain(log: string): string {
  const m = log.match(/\bdomain=([^\s]+)/);
  if (m) return m[1];
  const m2 = log.match(/Seat \/ quota status \(([^)]+)\)/);
  if (m2) return m2[1];
  return "";
}

export default function CreateMailboxPage() {
  const [statusLog, setStatusLog] = useState("");
  const [statusOk, setStatusOk] = useState<boolean | null>(null);
  const [domain, setDomain] = useState("");
  const [localPart, setLocalPart] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [resultLog, setResultLog] = useState("");
  const [message, setMessage] = useState("");
  const [createdEmail, setCreatedEmail] = useState("");

  async function refreshStatus() {
    const st = await api<StatusResp>("/api/mailbox/status");
    setStatusLog(st.log || "");
    setStatusOk(st.ok);
    const d = parseDomain(st.log || "");
    if (d) setDomain(d);
  }

  useEffect(() => {
    void refreshStatus().catch((err) =>
      setMessage(err instanceof Error ? err.message : "Failed to load seat status"),
    );
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    setResultLog("");
    setCreatedEmail("");
    try {
      const res = await api<CreateResp>("/api/mailbox", {
        method: "POST",
        body: JSON.stringify({
          local_part: localPart.trim().toLowerCase(),
          password,
          display_name: displayName.trim(),
        }),
      });
      setResultLog(res.log || "");
      if (res.ok) {
        const email = domain ? `${res.local_part}@${domain}` : res.local_part;
        setCreatedEmail(email);
        setMessage(`Created ${email}. Share the password you entered with the user (not stored in the console).`);
        setLocalPart("");
        setPassword("");
        setDisplayName("");
      } else {
        const quota =
          /seat limit|PLACEHOLDER_UNSET|not configured|No zmprov ca/i.test(res.log || "") ||
          res.exit_code === 1;
        setMessage(
          quota
            ? "Mailbox was not created (quota gate). See log — no zmprov ca ran if seats are full or unset."
            : "Create failed — see log.",
        );
      }
      await refreshStatus().catch(() => undefined);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConsoleChrome subtitle="Self-service mailbox create">
      <Page>
        <Title>Create mailbox</Title>
        <Lede>
          Creates one mailbox on the configured mail domain only. Seat limits use the same quota
          gate as the CLI (<code>kin_quota_gate_allow_new_mailbox</code>) — Customer Admin can create
          within the contracted seats, but cannot raise the seat limit.
        </Lede>

        {statusOk === false && (
          <WarnBox>
            <strong>New creates may be blocked.</strong> Seat status below — fix{" "}
            <code>CONTRACTED_SEATS</code> via KIN Super Admin / Support-Ops (wizard apply), not from
            this form.
          </WarnBox>
        )}

        {statusLog && (
          <>
            <Label>Seat status</Label>
            <LogPane aria-label="Seat status">{statusLog}</LogPane>
          </>
        )}

        <FormGrid onSubmit={(e) => void onSubmit(e)}>
          <Label htmlFor="lp">Email (local part)</Label>
          <LocalRow>
            <Input
              id="lp"
              value={localPart}
              onChange={(e) => setLocalPart(e.target.value.replace(/@.*$/, ""))}
              autoComplete="off"
              required
              placeholder="jane.doe"
            />
            <DomainSuffix>@{domain || "…"}</DomainSuffix>
          </LocalRow>
          <Label htmlFor="pw">Password (min 8 characters)</Label>
          <Input
            id="pw"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            minLength={8}
            required
            autoComplete="new-password"
          />
          <Label htmlFor="dn">Display name (optional)</Label>
          <Input
            id="dn"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            autoComplete="off"
          />
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create mailbox"}
          </Button>
        </FormGrid>

        {message && <Hint>{message}</Hint>}
        {createdEmail && (
          <Hint>
            Credentials: <code>{createdEmail}</code> + the password you typed (shown only here as a
            reminder — the console does not store it).
          </Hint>
        )}
        {resultLog && (
          <>
            <Label>Result</Label>
            <LogPane aria-label="Create result">{resultLog}</LogPane>
          </>
        )}
      </Page>
    </ConsoleChrome>
  );
}
