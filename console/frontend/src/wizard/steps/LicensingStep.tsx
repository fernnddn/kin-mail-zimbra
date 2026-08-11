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
} from "../../ui";
import { useWizard } from "../WizardContext";

export default function LicensingStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function next() {
    let seats = draft.contracted_seats.trim() || "PLACEHOLDER_UNSET";
    if (seats !== "PLACEHOLDER_UNSET" && !/^\d+$/.test(seats)) {
      seats = "PLACEHOLDER_UNSET";
    }
    await save({ contracted_seats: seats, current_step: "firewall" });
    navigate("/wizard/firewall");
  }

  return (
    <>
      <Title>Licensing</Title>
      <Lede>
        CONTRACTED_SEATS limits <strong>new</strong> mailbox creation only (quota gate). Password
        resets and other modify operations are never gated by this value.
      </Lede>
      <FieldRow>
        <Label htmlFor="contracted_seats">Contracted mailbox seats</Label>
        <Input
          id="contracted_seats"
          value={draft.contracted_seats}
          onChange={(e) => setLocal({ contracted_seats: e.target.value })}
          placeholder="PLACEHOLDER_UNSET"
        />
        <Hint>
          Use PLACEHOLDER_UNSET until the operator confirms the real contracted count. Do not invent
          a production number here.
        </Hint>
      </FieldRow>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/zpush")}>
          Back
        </Button>
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
