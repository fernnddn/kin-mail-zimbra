import { useNavigate } from "react-router-dom";
import { Button, Choice, ChoiceGrid, Err, Lede, NavRow, Title } from "../../ui";
import { useWizard } from "../WizardContext";

export default function ZpushStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function next(enabled: boolean) {
    setLocal({ zpush_enabled: enabled });
    await save({ zpush_enabled: enabled, current_step: "licensing" });
    navigate("/wizard/licensing");
  }

  return (
    <>
      <Title>Email on phones</Title>
      <Lede>
        Choose whether staff can sync mail on mobile devices (ActiveSync). You can skip this if
        phones are not needed for this customer.
      </Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.zpush_enabled === true}
          onClick={() => setLocal({ zpush_enabled: true })}
        >
          <strong>Enable mobile email access</strong>
          <span>Staff can add company mail on phones and tablets.</span>
        </Choice>
        <Choice
          type="button"
          selected={draft.zpush_enabled === false}
          onClick={() => setLocal({ zpush_enabled: false })}
        >
          <strong>Skip mobile email</strong>
          <span>Web and desktop mail only for this deployment.</span>
        </Choice>
      </ChoiceGrid>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/hybrid")}>
          Back
        </Button>
        <Button
          type="button"
          disabled={saving}
          onClick={() => void next(draft.zpush_enabled)}
        >
          Continue
        </Button>
      </NavRow>
    </>
  );
}
