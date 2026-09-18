import { useNavigate } from "react-router-dom";
import {
  Button,
  Err,
  FieldLabel,
  FieldRow,
  Hint,
  Input,
  Lede,
  NavRow,
  Title,
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
      <Title>Admin access from the internet</Title>
      {/*
        "Leave this blank and restrict it later" used to be untrue in the most
        expensive way available: blank made the firewall stage refuse, so ufw
        never ran and every port stayed open - SSH and this console included -
        on a server the deploy had just reported as finished. The stage now
        firewalls either way, and what blank actually costs is said here rather
        than discovered.
      */}
      <Lede>
        Optional. Leave it blank and the server is still firewalled: only the mail ports are
        public, and SSH, this console and Zimbra admin are reachable from this server&rsquo;s own
        local network. Fill it in and those three are narrowed to the addresses you list. You can
        set it later from the console and re-run the firewall step.
      </Lede>
      <FieldRow>
        <FieldLabel htmlFor="kin_admin_ips" optional>
          Trusted admin IP addresses
        </FieldLabel>
        <Input
          id="kin_admin_ips"
          value={draft.kin_admin_ips}
          onChange={(e) => setLocal({ kin_admin_ips: e.target.value })}
          placeholder="203.0.113.10 198.51.100.0/24"
        />
        <Hint>
          Space-separated IPs or networks. Your office or home address, not the mail server&rsquo;s.
        </Hint>
      </FieldRow>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/licensing")}>
          Back
        </Button>
        <Button type="button" loading={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
