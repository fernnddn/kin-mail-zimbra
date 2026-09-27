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
import { normaliseSeats, seatCountError, seatsAreUnlimited } from "../seatRule";

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

  const noLimit = seatsAreUnlimited(draft.contracted_seats);
  const displaySeats =
    draft.contracted_seats === "PLACEHOLDER_UNSET" || noLimit
      ? ""
      : draft.contracted_seats;

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
          disabled={noLimit}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ contracted_seats: e.target.value || "PLACEHOLDER_UNSET" });
          }}
          placeholder={noLimit ? "No limit" : adOn ? "e.g. 150" : "Leave blank to set later"}
          inputMode="numeric"
        />
        {/* A word the operator picks, never an inference from an empty box:
            "nobody has said yet" and "there is no cap" are opposite answers,
            and a gate that reads blank as permission is one typo from being no
            gate. Asked for on 27 Sep 2026 by an operator who cannot know next
            year's headcount and did not want to invent a figure. */}
        <label
          htmlFor="seats_unlimited"
          style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginTop: "0.6rem" }}
        >
          <input
            id="seats_unlimited"
            type="checkbox"
            checked={noLimit}
            onChange={(e) => {
              setFieldErr("");
              setLocal({ contracted_seats: e.target.checked ? "unlimited" : "PLACEHOLDER_UNSET" });
            }}
          />
          <span>No limit - do not cap how many mailboxes can be created</span>
        </label>
        <Hint>
          {noLimit ? (
            <>
              Nothing will stop mailboxes being created, including by the
              directory sync. Worth knowing: the seat count is also what limits
              the damage from a wrong AD search base, so check that it points at
              the OU you meant.
            </>
          ) : adOn ? (
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
