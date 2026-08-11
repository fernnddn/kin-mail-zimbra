import { useNavigate } from "react-router-dom";
import { Button, Err, Lede, NavRow, SummaryTable, Title } from "../../ui";
import { useWizard } from "../WizardContext";

function mask(value: string): string {
  if (!value) return "—";
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
    draft.topology === "1vm" ? "1 VM (single-node)" : draft.topology === "2vm" ? "2 VM (HA)" : "—";
  const tlsLabel =
    draft.tls_method === "cloudflare"
      ? "Cloudflare DNS-01"
      : draft.tls_method === "manual"
        ? "Manual DNS-01"
        : draft.tls_method === "customer"
          ? "Customer-provided"
          : "—";

  return (
    <>
      <Title>Review</Title>
      <Lede>
        Confirm the draft below. Nothing has been applied to the live installer config yet — this
        is review-only storage under the console data directory.
      </Lede>
      <SummaryTable>
        <dt>Topology</dt>
        <dd>{topologyLabel}</dd>
        <dt>Mail domain</dt>
        <dd>{draft.mail_domain || "—"}</dd>
        <dt>Mail host</dt>
        <dd>{draft.mail_host || "—"}</dd>
        <dt>Timezone</dt>
        <dd>{draft.timezone || "—"}</dd>
        <dt>Admin password</dt>
        <dd>{mask(draft.admin_pass)}</dd>
        <dt>LE email</dt>
        <dd>{draft.le_email || "—"}</dd>
        <dt>TLS method</dt>
        <dd>{tlsLabel}</dd>
        <dt>Hybrid AD</dt>
        <dd>{draft.ad_auth_enabled ? `Enabled (${draft.ad_ldap_url || "incomplete"})` : "Skipped"}</dd>
        <dt>Z-Push</dt>
        <dd>{draft.zpush_enabled ? "Enabled" : "Disabled"}</dd>
        <dt>Contracted seats</dt>
        <dd>{draft.contracted_seats || "—"}</dd>
        <dt>Admin IPs</dt>
        <dd>{draft.kin_admin_ips || "(empty — set before firewall apply)"}</dd>
      </SummaryTable>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/firewall")}>
          Back
        </Button>
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue to deployment
        </Button>
      </NavRow>
    </>
  );
}
