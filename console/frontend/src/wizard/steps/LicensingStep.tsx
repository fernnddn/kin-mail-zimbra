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
import { normaliseSeats, seatCountError } from "../seatRule";

export default function LicensingStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  // Optional in general, required the moment a directory is configured: every
  // account the sync creates spends a seat, so leaving this blank means it
  // creates nobody. The deploy still finishes, authentication is still set up,
  // and the admin console lists nobody from AD - which is how three deploys in
  // a row arrived at a customer-facing demo with the headline feature dead
  // (24-26 Sep 2026). The step used to call itself optional and offer "Leave
  // blank to set later" as its placeholder, so that is exactly what happened.
  const adOn = Boolean(draft.ad_auth_enabled);

  async function next() {
    const problem = seatCountError({
      seats: draft.contracted_seats,
      adEnabled: adOn,
    });
    if (problem) {
      setFieldErr(problem);
      return;
    }
    setFieldErr("");
    await save({
      contracted_seats: normaliseSeats(draft.contracted_seats),
      current_step: "firewall",
    });
    navigate("/wizard/firewall");
  }

  const displaySeats =
    draft.contracted_seats === "PLACEHOLDER_UNSET" ? "" : draft.contracted_seats;

  return (
    <>
      <Title>Contracted mailboxes</Title>
      <Lede>
        {adOn ? (
          <>
            How many mailboxes this customer has purchased. You enabled Active Directory, so
            this is required: every person the directory sync brings across spends a seat, and
            with no number set it creates <strong>nobody</strong>.
          </>
        ) : (
          <>
            Optionally set how many mailboxes this customer has purchased. Leave blank to decide
            later; until a number is set, the quota gate blocks new mailbox creates.
          </>
        )}
      </Lede>
      <FieldRow>
        <FieldLabel htmlFor="contracted_seats" optional={!adOn}>
          Number of contracted mailboxes
        </FieldLabel>
        <Input
          id="contracted_seats"
          value={displaySeats}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ contracted_seats: e.target.value || "PLACEHOLDER_UNSET" });
          }}
          placeholder={adOn ? "e.g. 150" : "Leave blank to set later"}
          inputMode="numeric"
        />
        <Hint>
          {adOn ? (
            <>
              Set it to at least the number of people in the directory who need a mailbox.
              Only limits creating <strong>new</strong> mailboxes - password resets and other
              changes are never blocked by this.
            </>
          ) : (
            <>
              Optional. Only limits creating <strong>new</strong> mailboxes once a number is set.
              Password resets and other changes are never blocked by this.
            </>
          )}
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
