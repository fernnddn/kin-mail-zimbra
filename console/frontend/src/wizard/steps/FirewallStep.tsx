import { useNavigate } from "react-router-dom";
import {
  Button,
  Err,
  FieldRow,
  Hint,
  Input,
  Label,
  Lede,
  NavRow,
  Title,
  WarnBox,
} from "../../ui";
import { useWizard } from "../WizardContext";

export default function FirewallStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function next() {
    await save({
      kin_admin_ips: draft.kin_admin_ips.trim(),
      current_step: "review",
    });
    navigate("/wizard/review");
  }

  return (
    <>
      <Title>Firewall — admin source IPs</Title>
      <Lede>
        Space-separated IPs/CIDRs allowed for SSH:22, Zimbra Admin:7071, and this console:9443.
        Cluster LAN /24 from SERVER_IP is always added by the host firewall stage.
      </Lede>
      <WarnBox>
        <strong>Sensitive setting.</strong> Wrong or empty admin IPs can lock operators out of
        SSH, Zimbra Admin, and this console once ufw is applied (default deny incoming). Confirm
        the list with the deployment owner before any future apply step. Empty is allowed only if
        you will set KIN_ADMIN_IPS later — do not apply the firewall stage until this is set.
      </WarnBox>
      <FieldRow>
        <Label htmlFor="kin_admin_ips">Admin source IPs / CIDRs (KIN_ADMIN_IPS)</Label>
        <Input
          id="kin_admin_ips"
          value={draft.kin_admin_ips}
          onChange={(e) => setLocal({ kin_admin_ips: e.target.value })}
          placeholder="203.0.113.10 198.51.100.0/24"
        />
        <Hint>Example: 203.0.113.10 198.51.100.0/24</Hint>
      </FieldRow>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/licensing")}>
          Back
        </Button>
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
