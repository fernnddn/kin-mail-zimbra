import { useCallback, useEffect, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { theme } from "../styles/theme";
import {
  Button,
  Err,
  FieldRow,
  Hint,
  Input,
  Label,
  LogPane,
  Mono,
  OkMsg,
  Page,
  PageHeader,
  Select,
  Spinner,
  StatusPill,
  WarnBox,
} from "../ui";

/**
 * The Proxmox Mail Gateway that sits in front of this appliance.
 *
 * From 0.1.10 the gateway is mandatory. It becomes the MX, filters inbound
 * mail before Zimbra ever sees it, and relays outbound mail on its way out.
 * The point is that the machine holding every mailbox stops being the machine
 * an attacker reaches first.
 *
 * This page never sees the API token again after it is submitted. It is
 * encrypted into the privhelper vault, and every later operation runs on the
 * appliance with the console only watching the output.
 *
 * The order on screen is the order of the real work, and nothing skips ahead:
 * connect, confirm the certificate, plan, apply, change DNS, verify.
 */

type Status = {
  enabled?: boolean;
  gateway_host?: string;
  api_port?: string;
  auth_mode?: string;
  token_id?: string;
  credential_present?: boolean;
  certificate_pinned?: boolean;
  phase?: string;
  applied_at?: string;
  configured?: boolean;
  compliant?: boolean;
  detail?: string;
};

type Op = "probe" | "plan" | "apply" | "verify" | "revert";

const Grid = styled.div`
  display: grid;
  gap: 1.15rem;
`;

const Card = styled.section`
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  background: ${theme.bgElev};
  padding: 1.05rem 1.15rem 1.2rem;
`;

const CardHead = styled.div`
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin: 0 0 0.65rem;

  h2 {
    margin: 0;
    font-size: 1rem;
    font-weight: 650;
  }
`;

const Step = styled.span`
  font-family: ${theme.mono};
  font-size: 0.72rem;
  color: ${theme.surface[500]};
`;

/* Owning the spacing at block level rather than with an adjacent-sibling rule:
   a result panel appearing between two rows used to break `Row + Row` and let
   the panel overlap the row beneath it. */
const Actions = styled.div`
  display: flex;
  gap: 0.55rem;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 0.2rem;
`;

const Facts = styled.dl`
  display: grid;
  grid-template-columns: minmax(9rem, max-content) 1fr;
  gap: 0.35rem 0.9rem;
  margin: 0 0 0.85rem;
  font-size: 0.84rem;

  dt {
    color: ${theme.muted};
  }
  dd {
    margin: 0;
    min-width: 0;
    overflow-wrap: anywhere;
  }
`;

const Flow = styled.pre`
  font-family: ${theme.mono};
  font-size: 0.74rem;
  line-height: 1.55;
  color: ${theme.surface[500]};
  background: ${theme.bg};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.sm};
  padding: 0.75rem 0.85rem;
  margin: 0 0 0.9rem;
  overflow-x: auto;
`;

/* The gateway's fingerprint, pulled out of a refusal so the operator can look
   at it and confirm it rather than typing it back character by character. */
function fingerprintFrom(text: string): string {
  const m = text.match(/Fingerprint:\s*([0-9A-Fa-f:]{16,})/);
  return m ? m[1].toUpperCase() : "";
}

function refusalFrom(text: string): string {
  const m = text.match(/KIN_GW_REFUSED reason=(\S+)/);
  return m ? m[1] : "";
}

export default function MailGatewayPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [log, setLog] = useState("");
  const [running, setRunning] = useState<Op | null>(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");

  const [host, setHost] = useState("");
  const [apiPort, setApiPort] = useState("8006");
  const [authMode, setAuthMode] = useState("token");
  const [tokenId, setTokenId] = useState("root@pam!kinmail");
  const [tokenSecret, setTokenSecret] = useState("");
  const [pmgUser, setPmgUser] = useState("root@pam");
  const [password, setPassword] = useState("");
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const st = await api<Status>("/api/mail-gateway/status");
      setStatus(st);
      setErr("");
      if (st.gateway_host) setHost(st.gateway_host);
      if (st.api_port) setApiPort(String(st.api_port));
      if (st.auth_mode) setAuthMode(st.auth_mode);
      if (st.token_id) setTokenId(st.token_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not read the gateway status.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const stream = useCallback(
    (op: Op) => {
      setRunning(op);
      setErr("");
      setNote("");
      let text = "";
      let code: number | null = null;
      const es = new EventSource(`/api/wizard/deploy/stream?action=mail_gateway&op=${op}`);
      const finish = (exit: number) => {
        setRunning(null);
        // A non-zero exit here is usually the helper refusing for a reason
        // worth reading, not a crash. The log already says which, so the page
        // does not add a second, vaguer sentence on top of it.
        if (exit === 0 && (op === "apply" || op === "revert")) void refresh();
        if (exit === 0 && op === "verify") setNote("Everything the link depends on is in place.");
      };
      es.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as {
            type?: string;
            data?: string;
            exit_code?: number;
            message?: string;
          };
          if ((data.type === "stdout" || data.type === "stderr") && data.data) {
            text += data.data;
            setLog(text.slice(-16000));
          } else if (data.type === "error") {
            text += `[error] ${data.message || ""}\n`;
            setLog(text.slice(-16000));
          } else if (data.type === "done") {
            code = typeof data.exit_code === "number" ? data.exit_code : 1;
            es.close();
            finish(code);
          }
        } catch {
          /* ignore malformed SSE */
        }
      };
      es.onerror = () => {
        es.close();
        if (code === null) {
          setLog((prev) => prev + "Lost connection to the appliance.\n");
          setRunning(null);
        }
      };
    },
    [refresh],
  );

  const connect = useCallback(async () => {
    setSaving(true);
    setErr("");
    setNote("");
    try {
      await api("/api/mail-gateway/connect", {
        method: "POST",
        body: JSON.stringify({
          host: host.trim(),
          api_port: Number(apiPort) || 8006,
          auth: authMode,
          token_id: tokenId.trim(),
          token_secret: tokenSecret,
          user: pmgUser.trim(),
          password,
        }),
      });
      // Cleared immediately: there is no reason for the credential to sit in a
      // form field once the appliance has it.
      setTokenSecret("");
      setPassword("");
      setNote("Gateway recorded. Run Probe next.");
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not record the gateway.");
    } finally {
      setSaving(false);
    }
  }, [host, apiPort, authMode, tokenId, tokenSecret, pmgUser, password, refresh]);

  const trust = useCallback(async () => {
    const fp = fingerprintFrom(log);
    if (!fp) return;
    setErr("");
    try {
      await api("/api/mail-gateway/trust", {
        method: "POST",
        body: JSON.stringify({ fingerprint: fp }),
      });
      setNote(`Certificate confirmed: ${fp}`);
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not confirm the certificate.");
    }
  }, [log, refresh]);

  const busy = running !== null || saving;
  const compliant = Boolean(status?.compliant);
  const configured = Boolean(status?.configured);
  const pendingFingerprint = fingerprintFrom(log);
  const showTrust =
    pendingFingerprint !== "" &&
    ["certificate-not-trusted", "certificate-changed"].includes(refusalFrom(log));

  if (loading) {
    return (
      <Page>
        <PageHeader title="Mail Gateway" subtitle="Reading the current state..." />
        <Spinner $size={16} />
      </Page>
    );
  }

  /* A status read that did not happen is not the same as "no gateway". The
     page used to fall through to the not-configured banner here, which told an
     operator their appliance was unprotected on the strength of a 403 or a
     daemon that was not answering. */
  if (!status) {
    return (
      <Page>
        <PageHeader
          title="Mail Gateway"
          subtitle="The current state of the link could not be read."
        />
        <Err>{err || "The appliance did not answer."}</Err>
        <Hint>
          This says nothing about whether a gateway is configured. Check that
          privhelperd is running, and that your role is allowed to manage the
          mail gateway.
        </Hint>
        <Actions>
          <Button type="button" variant="ghost" onClick={() => void refresh()}>
            Try again
          </Button>
        </Actions>
      </Page>
    );
  }

  return (
    <Page>
      <PageHeader
        title="Mail Gateway"
        subtitle="A Proxmox Mail Gateway in front of this appliance. It is the MX, it filters mail before Zimbra sees it, and it relays what leaves."
        action={
          <StatusPill tone={compliant ? "completed" : "warn"}>
            {compliant ? "Linked" : configured ? "Not applied" : "Not configured"}
          </StatusPill>
        }
      />

      {!compliant ? (
        <WarnBox>
          <strong>This appliance is not behind a mail gateway.</strong>
          <p>
            Port 25 still faces the internet on the machine that holds every mailbox. From
            release 0.1.10 the gateway is mandatory, and the healthcheck reports this as a
            failure until the link is applied.
          </p>
        </WarnBox>
      ) : null}

      {err ? <Err>{err}</Err> : null}
      {note ? <OkMsg>{note}</OkMsg> : null}

      <Grid>
        <Card>
          <CardHead>
            <h2>How mail will flow</h2>
            <Step>reference</Step>
          </CardHead>
          <Flow>{`inbound    internet --MX--> gateway :25 --filter--> this appliance :25
outbound   this appliance --relay--> gateway :26 --> internet
internal   mailbox -> mailbox stays here and never reaches the gateway`}</Flow>
          <Hint>
            Mail between two mailboxes in your own domain is delivered locally and does not go
            through the gateway. Forcing it to is how mail loops are built.
          </Hint>
        </Card>

        <Card>
          <CardHead>
            <h2>1. Connect</h2>
            <Step>{configured ? "recorded" : "not recorded"}</Step>
          </CardHead>
          <Hint>
            Install the gateway VM from the Proxmox ISO first and give it a fixed address. Then
            create an API token on it under Configuration &rsaquo; User Management &rsaquo; API
            Tokens. The secret is stored encrypted on this appliance and is never shown again.
          </Hint>
          <FieldRow>
            <Label htmlFor="gw-host">Gateway address</Label>
            <Input
              id="gw-host"
              value={host}
              onChange={(e) => setHost(e.target.value)}
              placeholder="pmg.example.com"
              autoComplete="off"
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="gw-port">API port</Label>
            <Input
              id="gw-port"
              value={apiPort}
              onChange={(e) => setApiPort(e.target.value)}
              inputMode="numeric"
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="gw-auth">Authentication</Label>
            <Select id="gw-auth" value={authMode} onChange={(e) => setAuthMode(e.target.value)}>
              <option value="token">API token (recommended)</option>
              <option value="ticket">Username and password</option>
            </Select>
          </FieldRow>
          {authMode === "token" ? (
            <>
              <FieldRow>
                <Label htmlFor="gw-tokenid">Token id</Label>
                <Input
                  id="gw-tokenid"
                  value={tokenId}
                  onChange={(e) => setTokenId(e.target.value)}
                  placeholder="root@pam!kinmail"
                  autoComplete="off"
                />
              </FieldRow>
              <FieldRow>
                <Label htmlFor="gw-secret">Token secret</Label>
                <Input
                  id="gw-secret"
                  type="password"
                  value={tokenSecret}
                  onChange={(e) => setTokenSecret(e.target.value)}
                  autoComplete="new-password"
                />
              </FieldRow>
              <Hint>
                A token can be revoked on the gateway without changing anyone&rsquo;s password,
                which is why it is preferred over the root login.
              </Hint>
            </>
          ) : (
            <>
              <FieldRow>
                <Label htmlFor="gw-user">User</Label>
                <Input
                  id="gw-user"
                  value={pmgUser}
                  onChange={(e) => setPmgUser(e.target.value)}
                  autoComplete="off"
                />
              </FieldRow>
              <FieldRow>
                <Label htmlFor="gw-pass">Password</Label>
                <Input
                  id="gw-pass"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                />
              </FieldRow>
            </>
          )}
          <Actions>
            <Button
              type="button"
              variant="primary"
              disabled={busy || !host.trim()}
              loading={saving}
              onClick={() => void connect()}
            >
              {configured ? "Update" : "Connect"}
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={busy || !configured}
              loading={running === "probe"}
              onClick={() => stream("probe")}
            >
              Probe
            </Button>
          </Actions>
        </Card>

        {showTrust ? (
          <Card>
            <CardHead>
              <h2>2. Confirm the certificate</h2>
              <Step>action needed</Step>
            </CardHead>
            <Hint>
              The gateway presented a certificate this appliance has not seen before. Check the
              fingerprint against the one shown on the gateway&rsquo;s own console, then confirm
              it. Every operation here refuses until you do, and refuses again if it ever
              changes.
            </Hint>
            <Facts>
              <dt>Fingerprint</dt>
              <dd>
                <Mono>{pendingFingerprint}</Mono>
              </dd>
            </Facts>
            <Actions>
              <Button type="button" variant="primary" disabled={busy} onClick={() => void trust()}>
                This is the right certificate
              </Button>
            </Actions>
          </Card>
        ) : null}

        <Card>
          <CardHead>
            <h2>3. Plan and apply</h2>
            <Step>{status?.phase || "never-applied"}</Step>
          </CardHead>
          <Facts>
            <dt>Gateway</dt>
            <dd>{status?.gateway_host || "not set"}</dd>
            <dt>Credential</dt>
            <dd>{status?.credential_present ? "stored (encrypted)" : "not stored"}</dd>
            <dt>Certificate</dt>
            <dd>{status?.certificate_pinned ? "confirmed" : "not confirmed"}</dd>
            <dt>Last applied</dt>
            <dd>{status?.applied_at || "never"}</dd>
          </Facts>
          <Hint>
            Plan reads both sides and changes nothing. Apply configures the gateway and points
            this appliance&rsquo;s outbound mail at it. Mail keeps flowing while it runs; nothing
            is restarted and no queued message is dropped.
          </Hint>
          <Actions>
            <Button
              type="button"
              variant="ghost"
              disabled={busy || !configured}
              loading={running === "plan"}
              onClick={() => stream("plan")}
            >
              Plan
            </Button>
            <Button
              type="button"
              variant="primary"
              disabled={busy || !configured}
              loading={running === "apply"}
              onClick={() => stream("apply")}
            >
              Apply
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={busy || !configured}
              loading={running === "verify"}
              onClick={() => stream("verify")}
            >
              Verify
            </Button>
          </Actions>
        </Card>

        <Card>
          <CardHead>
            <h2>4. DNS</h2>
            <Step>yours to change</Step>
          </CardHead>
          <Hint>
            These records live at your registrar and no button here can change them. Verify reads
            them back and says which are still wrong.
          </Hint>
          <Flow>{`MX    your-domain.      10   ${host || "<gateway hostname>"}
TXT   your-domain.      "v=spf1 ip4:${host || "<gateway ip>"} ~all"
PTR   ${host || "<gateway ip>"}   ->   the gateway's hostname`}</Flow>
          <Hint>
            DKIM and DMARC do not change. Signing stays on this appliance, and the gateway is
            configured not to sign a second time.
          </Hint>
        </Card>

        <Card>
          <CardHead>
            <h2>Undo</h2>
            <Step>rarely needed</Step>
          </CardHead>
          <Hint>
            Revert points outbound mail straight at the internet again and stops trusting the
            gateway. It leaves the gateway itself configured, so re-applying is instant. You
            still have to move the MX record back yourself.
          </Hint>
          <Actions>
            <Button
              type="button"
              variant="ghost"
              disabled={busy || !configured}
              loading={running === "revert"}
              onClick={() => stream("revert")}
            >
              Revert the appliance side
            </Button>
          </Actions>
        </Card>

        {log ? (
          <Card>
            <CardHead>
              <h2>Output</h2>
              <Step>{running ? "running" : "finished"}</Step>
            </CardHead>
            <LogPane>{log}</LogPane>
          </Card>
        ) : null}
      </Grid>
    </Page>
  );
}
