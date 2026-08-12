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
      <Lede>
        Optional for now. You can leave this blank and restrict which office or home IPs may reach
        admin tools later from the console after mail is up.
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
          Space-separated IPs or networks. Not required to continue setup — configure after
          deployment if you prefer.
        </Hint>
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
