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
      <Title>Z-Push ActiveSync</Title>
      <Lede>
        Not every deployment needs mobile ActiveSync. Full install skips <code>07-zpush.sh</code>{" "}
        when disabled (ZPUSH_ENABLED).
      </Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.zpush_enabled === true}
          onClick={() => setLocal({ zpush_enabled: true })}
        >
          <strong>Enable Z-Push</strong>
          <span>Install ActiveSync for mobile clients during full install.</span>
        </Choice>
        <Choice
          type="button"
          selected={draft.zpush_enabled === false}
          onClick={() => setLocal({ zpush_enabled: false })}
        >
          <strong>Skip Z-Push</strong>
          <span>Leave ActiveSync out of this deployment.</span>
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
