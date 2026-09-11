import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import styled from "@emotion/styled";
import { keyframes } from "@emotion/react";
import { api } from "../api";
import { ConsoleChrome } from "../ConsoleChrome";
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
  Switch,
  WarnBox,
} from "../ui";

/**
 * The Proxmox Mail Gateway that sits in front of this appliance.
 *
 * Named in full, everywhere. "Mail Gateway" on its own reads like a feature we
 * wrote and therefore support end to end; this is a Proxmox appliance the
 * customer runs, and the difference matters the first time somebody opens a
 * ticket about a spam rule.
 *
 * Two things this page is careful about.
 *
 * The gateway has two identities and confusing them breaks mail. The address
 * this console talks to is usually a LAN address. What goes into MX and SPF is
 * what the internet sees. They are asked for separately, and the DNS panel
 * refuses to print a record until it has the public one - the first version
 * printed the LAN address into an SPF record, which authorises nobody.
 *
 * And the raw output of the helper is an implementation detail: internal
 * markers, file paths, shell reasons. It is genuinely useful when something is
 * wrong, so it is kept - behind a disclosure, under a plain-language summary
 * that says what actually happened.
 */

type Status = {
  enabled?: boolean;
  gateway_host?: string;
  api_port?: string;
  auth_mode?: string;
  token_id?: string;
  public_host?: string;
  public_ip?: string;
  greylist?: boolean;
  public_identity_complete?: boolean;
  credential_present?: boolean;
  certificate_pinned?: boolean;
  phase?: string;
  applied_at?: string;
  configured?: boolean;
  compliant?: boolean;
};

type Op = "probe" | "plan" | "apply" | "verify" | "revert";

const rise = keyframes`
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: none; }
`;

const Grid = styled.div`
  display: grid;
  gap: 1.1rem;
`;

/* Cards arrive in sequence rather than all at once, which makes the page read
   as a set of steps instead of a wall. The delay is capped so a slow reader
   never waits on decoration. */
const Card = styled.section<{ $i?: number }>`
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  background: ${theme.bgElev};
  padding: 1rem 1.1rem 1.15rem;
  animation: ${rise} ${theme.motion.slow} ${theme.motion.ease.standard} both;
  animation-delay: ${(p) => Math.min((p.$i ?? 0) * 45, 270)}ms;

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }
`;

const CardHead = styled.div`
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin: 0 0 0.6rem;

  h2 {
    margin: 0;
    font-size: 0.98rem;
    font-weight: 650;
    letter-spacing: -0.01em;
  }
`;

const StepTag = styled.span<{ $done?: boolean }>`
  font-family: ${theme.mono};
  font-size: 0.7rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: ${(p) => (p.$done ? theme.surface[600] : theme.surface[400])};
  border: 1px solid ${theme.line};
  border-radius: 999px;
  padding: 0.12rem 0.5rem;
  transition: color ${theme.motion.base} ${theme.motion.ease.standard};
`;

const Actions = styled.div`
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 0.75rem;
`;

const Facts = styled.dl`
  display: grid;
  grid-template-columns: minmax(8.5rem, max-content) 1fr;
  gap: 0.32rem 0.9rem;
  margin: 0 0 0.8rem;
  font-size: 0.83rem;

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
  font-size: 0.73rem;
  line-height: 1.6;
  color: ${theme.surface[600]};
  background: ${theme.bg};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.sm};
  padding: 0.7rem 0.8rem;
  margin: 0 0 0.8rem;
  overflow-x: auto;
`;

/* Steps, with the one that needs attention lit. A row of identical buttons
   tells an operator nothing about what to press next. */
const Trail = styled.ol`
  list-style: none;
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin: 0 0 1.1rem;
  padding: 0;
  font-size: 0.78rem;
`;

const TrailStep = styled.li<{ $state: "done" | "now" | "todo" }>`
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.3rem 0.65rem;
  border-radius: 999px;
  border: 1px solid
    ${(p) => (p.$state === "now" ? theme.ink : theme.line)};
  background: ${(p) => (p.$state === "now" ? theme.ink : "transparent")};
  color: ${(p) =>
    p.$state === "now"
      ? theme.paper
      : p.$state === "done"
        ? theme.surface[600]
        : theme.surface[400]};
  transition:
    background ${theme.motion.base} ${theme.motion.ease.standard},
    color ${theme.motion.base} ${theme.motion.ease.standard},
    border-color ${theme.motion.base} ${theme.motion.ease.standard};

  &::before {
    content: ${(p) => (p.$state === "done" ? '"\\2713"' : '""')};
    font-size: 0.72rem;
  }
`;

const Disclosure = styled.details`
  margin-top: 0.8rem;
  border-top: 1px solid ${theme.line};
  padding-top: 0.7rem;

  summary {
    cursor: pointer;
    font-size: 0.8rem;
    color: ${theme.muted};
    list-style: none;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    user-select: none;
  }
  summary::-webkit-details-marker {
    display: none;
  }
  summary::before {
    content: "▸";
    transition: transform ${theme.motion.fast} ${theme.motion.ease.standard};
  }
  &[open] summary::before {
    transform: rotate(90deg);
  }
  summary:hover {
    color: ${theme.ink};
  }
`;

/* Plain-language lines lifted out of the helper's output. The markers are how
   the helper talks to us; they are not how we talk to an operator. */
const Line = styled.p<{ $tone: "ok" | "warn" | "bad" | "step" }>`
  margin: 0.18rem 0;
  font-size: 0.84rem;
  line-height: 1.5;
  padding-left: 1.2rem;
  position: relative;
  font-weight: ${(p) => (p.$tone === "step" ? 650 : 400)};
  color: ${(p) =>
    p.$tone === "bad"
      ? theme.danger
      : p.$tone === "warn"
        ? theme.warn
        : p.$tone === "step"
          ? theme.ink
          : theme.surface[700]};

  &::before {
    position: absolute;
    left: 0;
    content: ${(p) =>
      p.$tone === "ok"
        ? '"\\2713"'
        : p.$tone === "warn"
          ? '"\\0021"'
          : p.$tone === "bad"
            ? '"\\00d7"'
            : '""'};
  }
`;

type Readable = { tone: "ok" | "warn" | "bad" | "step"; text: string };

/**
 * Turn the helper's transcript into something an operator reads.
 *
 * Drops the machine markers entirely, keeps their message, and never shows a
 * line it cannot classify - an unexplained internal string in a customer's
 * console is exactly the "rahasia dapur" this is meant to keep out of sight.
 * The raw text is still one click away.
 */
function readable(log: string): Readable[] {
  const out: Readable[] = [];
  for (const raw of log.split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    if (line.startsWith("KIN_GW_OK ")) {
      out.push({ tone: "ok", text: line.slice("KIN_GW_OK ".length) });
    } else if (line.startsWith("KIN_GW_WARN ")) {
      out.push({ tone: "warn", text: line.slice("KIN_GW_WARN ".length) });
    } else if (line.startsWith("KIN_GW_REFUSED")) {
      out.push({ tone: "bad", text: "Stopped, and nothing further was changed." });
    } else if (line.startsWith("KIN_GW_PROBLEM ")) {
      out.push({ tone: "bad", text: line.slice("KIN_GW_PROBLEM ".length) });
    } else if (line.startsWith("KIN_GW_CHANGE ")) {
      const [what, from, to] = line.slice("KIN_GW_CHANGE ".length).split(/\s+/);
      out.push({
        tone: "warn",
        text: `${what}: ${(from || "").replace("from=", "")} → ${(to || "").replace("to=", "")}`,
      });
    } else if (/^\s*\d+\.\s/.test(line)) {
      out.push({ tone: "step", text: line.trim().replace(/^\d+\.\s*/, "") });
    } else if (line.startsWith("KIN_GW_")) {
      // TLS mode, auth mode, plan counts, status JSON: ours, not theirs.
      continue;
    } else if (line.startsWith("    ")) {
      out.push({ tone: "ok", text: line.trim() });
    }
  }
  return out;
}

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
  const esRef = useRef<EventSource | null>(null);

  const [host, setHost] = useState("");
  const [apiPort, setApiPort] = useState("8006");
  const [authMode, setAuthMode] = useState("token");
  const [tokenId, setTokenId] = useState("root@pam!kinmail");
  const [tokenSecret, setTokenSecret] = useState("");
  const [pmgUser, setPmgUser] = useState("root@pam");
  const [password, setPassword] = useState("");
  const [publicHost, setPublicHost] = useState("");
  const [publicIp, setPublicIp] = useState("");
  const [greylist, setGreylist] = useState(false);
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
      if (st.public_host) setPublicHost(st.public_host);
      if (st.public_ip) setPublicIp(st.public_ip);
      setGreylist(Boolean(st.greylist));
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not read the gateway status.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A stream left open after the page unmounts keeps a privhelper slot busy.
  useEffect(() => () => esRef.current?.close(), []);

  const stream = useCallback(
    (op: Op) => {
      setRunning(op);
      setErr("");
      setNote("");
      setLog("");
      let text = "";
      let code: number | null = null;
      const es = new EventSource(`/api/wizard/deploy/stream?action=mail_gateway&op=${op}`);
      esRef.current = es;
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
            setLog(text.slice(-24000));
          } else if (data.type === "error") {
            text += `[error] ${data.message || ""}\n`;
            setLog(text.slice(-24000));
          } else if (data.type === "done") {
            code = typeof data.exit_code === "number" ? data.exit_code : 1;
            es.close();
            esRef.current = null;
            setRunning(null);
            if (code === 0 && (op === "apply" || op === "revert")) void refresh();
            if (code === 0 && op === "verify") {
              setNote("Everything the link depends on is in place.");
            }
          }
        } catch {
          /* ignore malformed SSE */
        }
      };
      es.onerror = () => {
        es.close();
        esRef.current = null;
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
          public_host: publicHost.trim().replace(/\.$/, ""),
          public_ip: publicIp.trim(),
          greylist,
        }),
      });
      // Cleared immediately: no reason for a credential to sit in a form field
      // once the appliance has it.
      setTokenSecret("");
      setPassword("");
      setNote("Gateway recorded. Run Probe next.");
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not record the gateway.");
    } finally {
      setSaving(false);
    }
  }, [host, apiPort, authMode, tokenId, tokenSecret, pmgUser, password, publicHost, publicIp, greylist, refresh]);

  const trust = useCallback(async () => {
    const fp = fingerprintFrom(log);
    if (!fp) return;
    setErr("");
    try {
      await api("/api/mail-gateway/trust", {
        method: "POST",
        body: JSON.stringify({ fingerprint: fp }),
      });
      setNote("Certificate confirmed. Run Probe again.");
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not confirm the certificate.");
    }
  }, [log, refresh]);

  const lines = useMemo(() => readable(log), [log]);
  const busy = running !== null || saving;
  const compliant = Boolean(status?.compliant);
  const configured = Boolean(status?.configured);
  const pinned = Boolean(status?.certificate_pinned);
  const hasPublic = Boolean(status?.public_identity_complete);
  const pendingFingerprint = fingerprintFrom(log);
  const showTrust =
    pendingFingerprint !== "" &&
    ["certificate-not-trusted", "certificate-changed"].includes(refusalFrom(log));

  const stepState = (done: boolean, now: boolean) =>
    done ? "done" : now ? "now" : "todo";

  if (loading) {
    return (
      <ConsoleChrome>
        <Page>
          <PageHeader title="Proxmox Mail Gateway" subtitle="Reading the current state..." />
          <Spinner $size={16} />
        </Page>
      </ConsoleChrome>
    );
  }

  /* A status read that did not happen is not the same as "no gateway". The
     page used to fall through to the not-configured banner here, which told an
     operator their appliance was unprotected on the strength of a 403. */
  if (!status) {
    return (
      <ConsoleChrome>
        <Page>
          <PageHeader
            title="Proxmox Mail Gateway"
            subtitle="The current state of the link could not be read."
          />
          <Err>{err || "The appliance did not answer."}</Err>
          <Hint>
            This says nothing about whether a gateway is configured. Check that the
            appliance is healthy, and that your role is allowed to manage the mail
            gateway.
          </Hint>
          <Actions>
            <Button type="button" variant="ghost" onClick={() => void refresh()}>
              Try again
            </Button>
          </Actions>
        </Page>
      </ConsoleChrome>
    );
  }

  return (
    <ConsoleChrome subtitle="Proxmox Mail Gateway">
      <Page>
        <PageHeader
          title="Proxmox Mail Gateway"
          subtitle="A Proxmox Mail Gateway in front of this appliance. It is the MX, it filters mail before Zimbra sees it, and it relays what leaves."
          action={
            <StatusPill tone={compliant ? "completed" : "warn"}>
              {compliant ? "Linked" : configured ? "Not applied" : "Not configured"}
            </StatusPill>
          }
        />

        <Trail>
          <TrailStep $state={stepState(configured, !configured)}>Connect</TrailStep>
          <TrailStep $state={stepState(pinned, configured && !pinned)}>Certificate</TrailStep>
          <TrailStep $state={stepState(compliant, configured && pinned && !compliant)}>
            Apply
          </TrailStep>
          <TrailStep $state={stepState(compliant && hasPublic, compliant && !hasPublic)}>
            DNS
          </TrailStep>
          <TrailStep $state={stepState(false, compliant)}>Verify</TrailStep>
        </Trail>

        {!compliant ? (
          <WarnBox>
            <strong>This appliance is not behind a mail gateway.</strong>
            <p>
              Port 25 still faces the internet on the machine that holds every mailbox.
              From release 0.1.10 the gateway is part of the product, and the healthcheck
              reports this until the link is applied.
            </p>
          </WarnBox>
        ) : null}

        {err ? <Err>{err}</Err> : null}
        {note ? <OkMsg>{note}</OkMsg> : null}

        <Grid>
          <Card $i={0}>
            <CardHead>
              <h2>How mail will flow</h2>
              <StepTag>Reference</StepTag>
            </CardHead>
            <Flow>{`inbound    internet ──MX──▶ gateway :25 ──filter──▶ this appliance :25
outbound   this appliance ──relay──▶ gateway :26 ──▶ internet
internal   mailbox ──▶ mailbox stays here and never reaches the gateway`}</Flow>
            <Hint>
              Mail between two mailboxes in your own domain is delivered locally and does
              not go through the gateway. Forcing it to is how mail loops are built.
            </Hint>
          </Card>

          <Card $i={1}>
            <CardHead>
              <h2>1. Connect to the gateway</h2>
              <StepTag $done={configured}>{configured ? "Recorded" : "Not recorded"}</StepTag>
            </CardHead>
            <Hint>
              Install the Proxmox Mail Gateway VM first and give it a fixed address. Then
              create an API token on it under Configuration &rsaquo; User Management
              &rsaquo; API Tokens. The secret is stored encrypted on this appliance and is
              never shown again.
            </Hint>
            <FieldRow>
              <Label htmlFor="gw-host">Address this console uses</Label>
              <Input
                id="gw-host"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                placeholder="the gateway's address on your network"
                autoComplete="off"
              />
            </FieldRow>
            <Hint>Usually its LAN address. This is not what goes into DNS.</Hint>
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
                  A token can be revoked on the gateway without changing anyone&rsquo;s
                  password, which is why it is preferred over the root login.
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

          <Card $i={2}>
            <CardHead>
              <h2>2. What the internet sees</h2>
              <StepTag $done={hasPublic}>{hasPublic ? "Recorded" : "Needed for DNS"}</StepTag>
            </CardHead>
            <Hint>
              These are the gateway&rsquo;s public identity, and they are the only values
              that belong in DNS. An MX record can only name a host, never an address, and
              an SPF record listing a private address authorises nobody.
            </Hint>
            <FieldRow>
              <Label htmlFor="gw-pub-host">Public hostname</Label>
              <Input
                id="gw-pub-host"
                value={publicHost}
                onChange={(e) => setPublicHost(e.target.value)}
                placeholder="relay.your-domain.com"
                autoComplete="off"
              />
            </FieldRow>
            <FieldRow>
              <Label htmlFor="gw-pub-ip">Public address</Label>
              <Input
                id="gw-pub-ip"
                value={publicIp}
                onChange={(e) => setPublicIp(e.target.value)}
                placeholder="the address the internet reaches it on"
                autoComplete="off"
              />
            </FieldRow>
            <Hint>
              Both are saved with Connect above. Without them this page cannot print your
              DNS records, and will say so rather than guess.
            </Hint>
          </Card>

          {showTrust ? (
            <Card $i={3}>
              <CardHead>
                <h2>Confirm the gateway&rsquo;s certificate</h2>
                <StepTag>Action needed</StepTag>
              </CardHead>
              <Hint>
                The gateway presented a certificate this appliance has not seen before.
                Check the fingerprint against the one shown on the gateway&rsquo;s own
                console, then confirm it. Every operation here refuses until you do, and
                refuses again if it ever changes.
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

          <Card $i={4}>
            <CardHead>
              <h2>3. Apply the link</h2>
              <StepTag $done={compliant}>{compliant ? "Applied" : "Not applied"}</StepTag>
            </CardHead>
            <Facts>
              <dt>Gateway</dt>
              <dd>{status.gateway_host || "not set"}</dd>
              <dt>Credential</dt>
              <dd>{status.credential_present ? "stored, encrypted" : "not stored"}</dd>
              <dt>Certificate</dt>
              <dd>{pinned ? "confirmed" : "not confirmed"}</dd>
              <dt>Last applied</dt>
              <dd>{status.applied_at || "never"}</dd>
            </Facts>
            <FieldRow>
              <Switch
                checked={greylist}
                onChange={setGreylist}
                label="Greylist unknown senders"
              />
            </FieldRow>
            <Hint>
              Greylisting turns away the first message from any sender it has not seen and
              waits for the retry. It stops a lot of cheap spam, and it delays the first
              message from a new correspondent by a few minutes. Off by default: spam is
              judged on content instead. Saved with Connect.
            </Hint>
            <Hint>
              Plan reads both sides and changes nothing. Apply configures the gateway,
              tunes its spam and virus detection, and points this appliance&rsquo;s
              outbound mail at it. Mail keeps flowing throughout.
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

          <Card $i={5}>
            <CardHead>
              <h2>4. DNS</h2>
              <StepTag $done={hasPublic}>Yours to change</StepTag>
            </CardHead>
            {hasPublic ? (
              <>
                <Hint>
                  These records live at your registrar and no button here can change them.
                  Verify reads them back and says which are still wrong.
                </Hint>
                <Flow>{`A     ${status.public_host}.        →  ${status.public_ip}
MX    your-domain.       10    ${status.public_host}.
TXT   your-domain.             "v=spf1 ip4:${status.public_ip} ~all"
PTR   ${status.public_ip}   →  ${status.public_host}   (ask your ISP)`}</Flow>
              </>
            ) : (
              <Hint>
                Record the gateway&rsquo;s public hostname and address above and these
                records will be spelled out here. Printing them from the address this
                console uses would publish something the internet cannot route to, and
                every outbound message would fail SPF.
              </Hint>
            )}
            <Hint>
              DKIM and DMARC do not change. Signing stays on this appliance, and the
              gateway is configured not to sign a second time.
            </Hint>
          </Card>

          <Card $i={6}>
            <CardHead>
              <h2>Undo</h2>
              <StepTag>Rarely needed</StepTag>
            </CardHead>
            <Hint>
              Revert points outbound mail straight at the internet again and stops trusting
              the gateway. It leaves the gateway itself configured, so re-applying is
              instant. You still have to move the MX record back and reopen port 25.
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
            <Card $i={7}>
              <CardHead>
                <h2>What happened</h2>
                <StepTag>{running ? "Running" : "Finished"}</StepTag>
              </CardHead>
              {lines.length ? (
                <div>
                  {lines.map((l, i) => (
                    <Line key={`${i}-${l.text}`} $tone={l.tone}>
                      {l.text}
                    </Line>
                  ))}
                </div>
              ) : (
                <Hint>No output yet.</Hint>
              )}
              <Disclosure>
                <summary>Technical output</summary>
                <LogPane>{log}</LogPane>
              </Disclosure>
            </Card>
          ) : null}
        </Grid>
      </Page>
    </ConsoleChrome>
  );
}
