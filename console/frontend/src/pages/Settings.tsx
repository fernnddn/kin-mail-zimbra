import { FormEvent, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import styled from "@emotion/styled";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import { AcmeChallengePanel } from "../wizard/DnsRecordsPanel";
import {
  Button,
  ConfirmModal,
  Hint,
  Input,
  Label,
  LogPane,
  Page,
  PageHeader,
  PasswordInput,
  SettingsIcon,
  Switch,
  WarnBox,
} from "../ui";

type LicenseState = {
  present?: boolean;
  status?: string;
  type?: string;
  seats?: number | null;
  expires_at?: string | null;
  grace_until?: string | null;
  provisioning_blocked?: boolean;
  server_id?: string;
  error?: string;
};

type AdState = {
  enabled: boolean;
  ldap_url: string;
  search_base: string;
  search_filter: string;
  search_bind_dn: string;
  bind_dn_template: string;
  search_bind_password_set: boolean;
};

type TlsState = {
  path?: string;
  not_after?: string;
  days_left?: number | null;
  method?: string;
};

type SettingsResp = {
  server_id?: string;
  admin_ips?: string;
  license?: LicenseState;
  ad?: AdState;
  tls?: TlsState;
};

const Section = styled.section`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1.05rem 1.1rem 1.15rem;
  margin: 0 0 1rem;
`;

const SectionTitle = styled.h2`
  margin: 0 0 0.35rem;
  font-size: 1.05rem;
  font-weight: 700;
  letter-spacing: -0.02em;
`;

const Area = styled.textarea`
  width: 100%;
  min-height: 6.5rem;
  margin: 0 0 0.9rem;
  padding: 0.55rem 0.7rem;
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.md};
  font-family: ${theme.mono};
  font-size: 0.82rem;
  resize: vertical;
`;

const Row = styled.div`
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem;
  align-items: center;
  margin-top: 0.35rem;
`;

function streamAction(
  qs: URLSearchParams,
  onChunk: (text: string) => void,
): Promise<number> {
  return new Promise((resolve, reject) => {
    const es = new EventSource(`/api/wizard/deploy/stream?${qs.toString()}`);
    let buf = "";
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as {
          type?: string;
          data?: string;
          exit_code?: number;
          message?: string;
        };
        if ((data.type === "stdout" || data.type === "stderr") && data.data) {
          buf += data.data;
          onChunk(buf);
        } else if (data.type === "error") {
          buf += `${data.message || "stream error"}\n`;
          onChunk(buf);
        } else if (data.type === "done") {
          es.close();
          resolve(data.exit_code ?? 1);
        }
      } catch {
        /* ignore malformed SSE */
      }
    };
    es.onerror = () => {
      es.close();
      reject(new Error("Lost connection to the settings stream."));
    };
  });
}

export default function SettingsPage() {
  const { user } = useAuth();
  const allowed = user?.role === "kin_super_admin";
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [serverId, setServerId] = useState("");
  const [license, setLicense] = useState<LicenseState>({});
  const [licenseToken, setLicenseToken] = useState("");
  const [adminIps, setAdminIps] = useState("");
  const [ipConfirm, setIpConfirm] = useState(false);
  const [adEnabled, setAdEnabled] = useState(false);
  const [ldapUrl, setLdapUrl] = useState("");
  const [searchBase, setSearchBase] = useState("");
  const [searchFilter, setSearchFilter] = useState("(sAMAccountName=%u)");
  const [searchBindDn, setSearchBindDn] = useState("");
  const [bindDnTemplate, setBindDnTemplate] = useState("");
  const [bindPassword, setBindPassword] = useState("");
  const [bindPasswordSet, setBindPasswordSet] = useState(false);
  const [tls, setTls] = useState<TlsState>({});
  const [busy, setBusy] = useState("");
  const [fwLog, setFwLog] = useState("");
  const [tlsLog, setTlsLog] = useState("");
  const [deadmanOpen, setDeadmanOpen] = useState(false);
  const [mailDomain, setMailDomain] = useState("");

  async function refresh() {
    const st = await api<SettingsResp>("/api/settings");
    setServerId(st.server_id || st.license?.server_id || "");
    setLicense(st.license || {});
    setAdminIps(st.admin_ips || "");
    const ad = st.ad;
    if (ad) {
      setAdEnabled(!!ad.enabled);
      setLdapUrl(ad.ldap_url || "");
      setSearchBase(ad.search_base || "");
      setSearchFilter(ad.search_filter || "(sAMAccountName=%u)");
      setSearchBindDn(ad.search_bind_dn || "");
      setBindDnTemplate(ad.bind_dn_template || "");
      setBindPasswordSet(!!ad.search_bind_password_set);
    }
    setTls(st.tls || {});
  }

  useEffect(() => {
    if (!allowed) return;
    void refresh()
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load settings"))
      .finally(() => setLoaded(true));
    void api<{ cluster?: { mail_domain?: string }; log?: string }>("/api/cluster/status")
      .then((st) => {
        const fromCluster = String(st.cluster?.mail_domain || "");
        if (fromCluster) setMailDomain(fromCluster);
      })
      .catch(() => undefined);
    return undefined;
  }, [allowed]);

  if (!allowed) {
    return <Navigate to="/cluster" replace />;
  }

  async function saveAd(e: FormEvent) {
    e.preventDefault();
    setBusy("ad");
    setError("");
    try {
      await api("/api/settings/ad", {
        method: "POST",
        body: JSON.stringify({
          enabled: adEnabled,
          ldap_url: ldapUrl,
          search_base: searchBase,
          search_filter: searchFilter,
          search_bind_dn: searchBindDn,
          bind_dn_template: bindDnTemplate,
          search_bind_password: bindPassword,
        }),
      });
      setBindPassword("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save directory settings");
    } finally {
      setBusy("");
    }
  }

  async function saveLicense(e: FormEvent) {
    e.preventDefault();
    setBusy("license");
    setError("");
    try {
      await api("/api/settings/license", {
        method: "POST",
        body: JSON.stringify({ token: licenseToken.trim() }),
      });
      setLicenseToken("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not apply license");
    } finally {
      setBusy("");
    }
  }

  async function applyFirewall() {
    if (!ipConfirm) {
      setError("Confirm that the list includes the IPv4 you are using to open this console.");
      return;
    }
    setBusy("firewall");
    setError("");
    setFwLog("");
    try {
      const qs = new URLSearchParams({
        action: "appliance_settings",
        section: "firewall",
        admin_ips: adminIps.trim(),
      });
      const code = await streamAction(qs, setFwLog);
      if (code !== 0) {
        setError("Trusted IP apply did not finish cleanly. A safety timer will disable the firewall if you cannot reconnect.");
      } else {
        setDeadmanOpen(true);
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not apply trusted IPs");
    } finally {
      setBusy("");
    }
  }

  async function keepFirewall() {
    setBusy("deadman");
    setError("");
    try {
      const qs = new URLSearchParams({ action: "cancel_firewall_deadman" });
      const code = await streamAction(qs, setFwLog);
      if (code !== 0) {
        setError("Could not confirm the firewall change. If you can still open this page, try again.");
      } else {
        setDeadmanOpen(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not confirm firewall access");
    } finally {
      setBusy("");
    }
  }

  async function renewTls() {
    setBusy("tls");
    setError("");
    setTlsLog("");
    try {
      const qs = new URLSearchParams({
        action: "appliance_settings",
        section: "tls_renew",
      });
      const code = await streamAction(qs, setTlsLog);
      if (code !== 0) {
        setError("Certificate renewal did not finish. Check the live log and DNS TXT record.");
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start certificate renewal");
    } finally {
      setBusy("");
    }
  }

  const days = tls.days_left;
  const tlsLabel =
    tls.not_after && days != null
      ? `Expires ${tls.not_after} (${days} day${days === 1 ? "" : "s"} left).`
      : tls.not_after
        ? `Expires ${tls.not_after}.`
        : "No certificate expiry could be read on this host yet.";

  return (
    <ConsoleChrome>
      <Page>
        <PageHeader
          icon={<SettingsIcon />}
          title="Settings"
          subtitle="Directory sign-in, trusted console IPs, TLS, and the signed license for this Email Server."
        />
        {error ? <WarnBox>{error}</WarnBox> : null}
        {!loaded ? <Hint>Loading settings…</Hint> : null}

        <Section>
          <SectionTitle>License</SectionTitle>
          <Hint>
            Email Server ID (give this to KIN when requesting a license):{" "}
            <code>{serverId || "not issued yet"}</code>
          </Hint>
          <Hint>
            Status: {license.status || "none"}
            {license.type ? ` · ${license.type}` : ""}
            {license.seats ? ` · ${license.seats} seats` : ""}
            {license.expires_at ? ` · expires ${license.expires_at}` : license.type === "perpetual" ? " · no expiry" : ""}
            {license.grace_until ? ` · grace until ${license.grace_until}` : ""}
          </Hint>
          {license.provisioning_blocked ? (
            <WarnBox>
              New mailboxes and console users cannot be created until a current license is
              applied. Mail already delivered keeps working.
            </WarnBox>
          ) : null}
          <form onSubmit={(e) => void saveLicense(e)}>
            <Label htmlFor="lic">Paste a signed license</Label>
            <Area
              id="lic"
              value={licenseToken}
              onChange={(e) => setLicenseToken(e.target.value)}
              spellCheck={false}
              autoComplete="off"
              placeholder="payload.signature"
            />
            <Button type="submit" variant="primary" loading={busy === "license"} disabled={!licenseToken.trim()}>
              Apply license
            </Button>
          </form>
        </Section>

        <Section>
          <SectionTitle>Active Directory</SectionTitle>
          <Hint>
            Same <code>/etc/kin-mail/config</code> fields the Hybrid Auth wizard uses. Console
            and mail directory bind stay on one source of truth. Bind password is write-only.
          </Hint>
          <form onSubmit={(e) => void saveAd(e)}>
            <Switch
              checked={adEnabled}
              onChange={setAdEnabled}
              label="Allow Active Directory sign-in"
            />
            <Label htmlFor="ldap">LDAP / AD URL</Label>
            <Input
              id="ldap"
              value={ldapUrl}
              onChange={(e) => setLdapUrl(e.target.value)}
              placeholder="ldaps://dc.example.com:636"
              disabled={!adEnabled}
            />
            <Label htmlFor="base">Search base</Label>
            <Input
              id="base"
              value={searchBase}
              onChange={(e) => setSearchBase(e.target.value)}
              placeholder="DC=example,DC=com"
              disabled={!adEnabled}
            />
            <Label htmlFor="filter">Search filter</Label>
            <Input
              id="filter"
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              disabled={!adEnabled}
            />
            <Label htmlFor="binddn">Search bind account</Label>
            <Input
              id="binddn"
              value={searchBindDn}
              onChange={(e) => setSearchBindDn(e.target.value)}
              disabled={!adEnabled}
            />
            <Label htmlFor="bindpw">
              Search bind password {bindPasswordSet ? "(set, leave blank to keep)" : ""}
            </Label>
            <PasswordInput
              id="bindpw"
              value={bindPassword}
              onChange={(e) => setBindPassword(e.target.value)}
              autoComplete="new-password"
              disabled={!adEnabled}
            />
            <Label htmlFor="tpl">Bind DN template (optional)</Label>
            <Input
              id="tpl"
              value={bindDnTemplate}
              onChange={(e) => setBindDnTemplate(e.target.value)}
              placeholder="EXAMPLE\\%u"
              disabled={!adEnabled}
            />
            <Button type="submit" variant="primary" loading={busy === "ad"}>
              Save directory settings
            </Button>
          </form>
        </Section>

        <Section>
          <SectionTitle>Trusted console IPs</SectionTitle>
          <Hint>
            IPv4 addresses or networks allowed to reach this console. Applying this re-runs the
            host firewall. A safety timer turns the firewall off if you cannot reconnect. Always
            include the address you are using right now.
          </Hint>
          <Label htmlFor="ips">Allowed IPs or CIDR ranges</Label>
          <Input
            id="ips"
            value={adminIps}
            onChange={(e) => setAdminIps(e.target.value)}
            placeholder="203.0.113.10 198.51.100.0/24"
          />
          <label style={{ display: "flex", gap: "0.5rem", alignItems: "flex-start", margin: "0.4rem 0 0.85rem" }}>
            <input
              type="checkbox"
              checked={ipConfirm}
              onChange={(e) => setIpConfirm(e.target.checked)}
              style={{ marginTop: "0.2rem" }}
            />
            <span>
              I have included the IPv4 I am using to open this console, and I can still reach it
              after apply.
            </span>
          </label>
          <Row>
            <Button
              type="button"
              variant="primary"
              loading={busy === "firewall"}
              disabled={!adminIps.trim() || !ipConfirm}
              onClick={() => void applyFirewall()}
            >
              Apply trusted IPs
            </Button>
            {deadmanOpen ? (
              <Button type="button" loading={busy === "deadman"} onClick={() => void keepFirewall()}>
                I can still open the console, keep this allowlist
              </Button>
            ) : null}
          </Row>
          {fwLog ? <LogPane aria-label="Firewall apply log">{fwLog}</LogPane> : null}
        </Section>

        <Section>
          <SectionTitle>TLS certificate</SectionTitle>
          <Hint>
            {tlsLabel} Method: {tls.method || "unknown"}. Renewal uses the same DNS-01 flow as
            install. When a TXT record is required, it appears below in copy-paste form.
          </Hint>
          <Button type="button" variant="primary" loading={busy === "tls"} onClick={() => void renewTls()}>
            Renew certificate
          </Button>
          <AcmeChallengePanel log={tlsLog} mailDomain={mailDomain} />
          {tlsLog ? <LogPane aria-label="Certificate renewal log">{tlsLog}</LogPane> : null}
        </Section>
      </Page>
      <ConfirmModal
        open={deadmanOpen}
        title="Confirm console access"
        message="The new trusted-IP list is live, with a safety timer. If this page still loads, keep the change. If you cannot reconnect, wait for the timer to disable the firewall."
        detail="Keeping the allowlist cancels the safety timer so the firewall stays on."
        confirmLabel="Keep this allowlist"
        variant="warn"
        loading={busy === "deadman"}
        onCancel={() => setDeadmanOpen(false)}
        onConfirm={() => void keepFirewall()}
      />
    </ConsoleChrome>
  );
}
