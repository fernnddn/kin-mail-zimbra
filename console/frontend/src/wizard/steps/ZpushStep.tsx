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
        Staff mail on phones and tablets, over ActiveSync. This is on by default so
        the appliance is usable from a phone the day it is handed over. If the PHP
        packages cannot be fetched during Deploy, mail still installs normally and
        this step is reported as one to re-run.
      </Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.zpush_enabled !== false}
          onClick={() => setLocal({ zpush_enabled: true })}
        >
          <strong>Enable mobile email access</strong>
          <span>
            Recommended. Staff can add company mail on phones and tablets straight
            after handover.
          </span>
        </Choice>
        <Choice
          type="button"
          selected={draft.zpush_enabled === false}
          onClick={() => setLocal({ zpush_enabled: false })}
        >
          <strong>Skip mobile email</strong>
          <span>
            Web and desktop mail only. You can turn it on later with
            sudo ./07-zpush.sh.
          </span>
        </Choice>
      </ChoiceGrid>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/hybrid")}>
          Back
        </Button>
        <Button
          type="button"
          loading={saving}
          onClick={() => void next(draft.zpush_enabled !== false)}
        >
          Continue
        </Button>
      </NavRow>
    </>
  );
}
