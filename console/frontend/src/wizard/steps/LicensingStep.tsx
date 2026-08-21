import { useState } from "react";
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

export default function LicensingStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  async function next() {
    const raw = draft.contracted_seats.trim();
    if (raw && raw !== "PLACEHOLDER_UNSET" && !/^\d+$/.test(raw)) {
      setFieldErr("Contracted mailboxes must be a whole number, or left blank.");
      return;
    }
    const seats = raw && raw !== "PLACEHOLDER_UNSET" ? raw : "PLACEHOLDER_UNSET";
    setFieldErr("");
    await save({ contracted_seats: seats, current_step: "firewall" });
    navigate("/wizard/firewall");
  }

  const displaySeats =
    draft.contracted_seats === "PLACEHOLDER_UNSET" ? "" : draft.contracted_seats;

  return (
    <>
      <Title>Contracted mailboxes</Title>
      <Lede>
        Optionally set how many mailboxes this customer has purchased. Leave blank to decide later;
        until a number is set, the quota gate blocks new mailbox creates.
      </Lede>
      <FieldRow>
        <FieldLabel htmlFor="contracted_seats" optional>
          Number of contracted mailboxes
        </FieldLabel>
        <Input
          id="contracted_seats"
          value={displaySeats}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ contracted_seats: e.target.value || "PLACEHOLDER_UNSET" });
          }}
          placeholder="Leave blank to set later"
          inputMode="numeric"
        />
        <Hint>
          Optional. Only limits creating <strong>new</strong> mailboxes once a number is set.
          Password resets and other changes are never blocked by this.
        </Hint>
      </FieldRow>
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/zpush")}>
          Back
        </Button>
        <Button type="button" loading={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
