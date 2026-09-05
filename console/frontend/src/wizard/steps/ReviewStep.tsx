import { useNavigate } from "react-router-dom";
import { Button, Err, Lede, NavRow, SummaryTable, Title } from "../../ui";
import { DnsRecordsPanel } from "../DnsRecordsPanel";
import { useWizard } from "../WizardContext";

function mask(value: string): string {
  if (!value) return "-";
  return "••••••••";
}

export default function ReviewStep() {
  const { draft, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function next() {
    await save({ current_step: "deploy" });
    navigate("/wizard/deploy");
  }

  const topologyLabel =
    draft.topology === "1vm" ? "1 server" : draft.topology === "2vm" ? "2 servers (HA)" : "-";
  const tlsLabel =
    draft.tls_method === "cloudflare"
      ? "Automatic (Cloudflare)"
      : draft.tls_method === "manual"
        ? "Manual DNS record"
        : draft.tls_method === "customer"
          ? "Customer-provided"
          : "-";
  const seatsLabel =
    !draft.contracted_seats || draft.contracted_seats === "PLACEHOLDER_UNSET"
      ? "Not set yet (optional)"
      : draft.contracted_seats;

  return (
    <>
      <Title>Review</Title>
      <Lede>
        Confirm these choices. Nothing is installed yet; the next step saves them to this server
        and starts deployment.
      </Lede>
      <SummaryTable>
        <dt>Servers</dt>
        <dd>{topologyLabel}</dd>
        {draft.topology === "2vm" ? (
          <>
            <dt>Second server IP</dt>
            <dd>{draft.peer_host_ip || "-"}</dd>
            <dt>Second server hostname</dt>
            <dd>{draft.peer_host_name || "-"}</dd>
            <dt>Observability VM IP</dt>
            <dd>{draft.observability_vm_ip || "-"}</dd>
            <dt>Cluster VIP</dt>
            <dd>{draft.cluster_vip_ip || "-"}</dd>
          </>
        ) : null}
        <dt>Root password</dt>
        <dd>{draft.host_credentials_set ? "Stored (encrypted)" : mask(draft.host_root_pass)}</dd>
        <dt>Admin password</dt>
        <dd>{draft.host_credentials_set ? "Stored (encrypted)" : mask(draft.kin_user_pass)}</dd>
        <dt>Mail domain</dt>
        <dd>{draft.mail_domain || "-"}</dd>
        <dt>Mail hostname</dt>
        <dd>{draft.mail_host || "-"}</dd>
        <dt>Timezone</dt>
        <dd>{draft.timezone || "-"}</dd>
        <dt>Mail admin password</dt>
        <dd>{draft.admin_pass_set || draft.admin_pass ? mask(draft.admin_pass || "stored") : "-"}</dd>
        <dt>Certificate email</dt>
        <dd>{draft.le_email || "-"}</dd>
        <dt>HTTPS certificates</dt>
        <dd>{tlsLabel}</dd>
        <dt>Company directory</dt>
        <dd>{draft.ad_auth_enabled ? `Enabled (${draft.ad_ldap_url || "incomplete"})` : "Skipped"}</dd>
        <dt>Mobile email</dt>
        <dd>{draft.zpush_enabled !== false ? "Enabled" : "Skipped"}</dd>
        <dt>Contracted mailboxes</dt>
        <dd>{seatsLabel}</dd>
        <dt>Trusted admin IPs</dt>
        <dd>{draft.kin_admin_ips || "Not set (can configure later)"}</dd>
      </SummaryTable>
      <DnsRecordsPanel
        mailDomain={draft.mail_domain}
        mailHost={draft.mail_host}
        ruaEmail={draft.le_email}
        preferredAIp={draft.topology === "2vm" ? draft.cluster_vip_ip : ""}
      />
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/firewall")}>
          Back
        </Button>
        <Button type="button" loading={saving} onClick={() => void next()}>
          Continue to deploy
        </Button>
      </NavRow>
    </>
  );
}
