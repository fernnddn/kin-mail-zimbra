import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Err,
  FieldLabel,
  FieldRow,
  Hint,
  Lede,
  NavRow,
  PasswordInput,
  ReadonlyValue,
  Title,
} from "../../ui";
import { useWizard } from "../WizardContext";
import { KIN_OS_USER } from "../types";

export default function CredentialsStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  async function next() {
    const missing: string[] = [];
    if (draft.host_root_pass.length < 8) missing.push("Root password (min 8 characters)");
    if (draft.kin_user_pass.length < 8) missing.push(`Password for user "${KIN_OS_USER}" (min 8 characters)`);
    if (missing.length) {
      setFieldErr(`Please fill in: ${missing.join(", ")}.`);
      return;
    }
    setFieldErr("");
    await save({
      host_root_pass: draft.host_root_pass,
      kin_user_pass: draft.kin_user_pass,
      current_step: "domain",
    });
    navigate("/wizard/domain");
  }

  return (
    <>
      <Title>Default host credentials</Title>
      <Lede>
        Same idea as Arcfra’s default admin account: every KIN Mail host gets a standard OS admin
        user named <strong>{KIN_OS_USER}</strong>, plus a root password you choose. These are saved
        with the draft for later provisioning — this step does not SSH to any host yet.
      </Lede>
      <FieldRow>
        <FieldLabel>Default admin username</FieldLabel>
        <ReadonlyValue aria-label="Default admin username">{KIN_OS_USER}</ReadonlyValue>
        <Hint>
          Fixed identity for KIN Mail (like Arcfra’s “arcfra” user). Not editable.
        </Hint>
      </FieldRow>
      <FieldRow>
        <FieldLabel htmlFor="host_root_pass" required>
          Root password
        </FieldLabel>
        <PasswordInput
          id="host_root_pass"
          autoComplete="new-password"
          value={draft.host_root_pass}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ host_root_pass: e.target.value });
          }}
        />
        <Hint>At least 8 characters. Used for root access on the host(s) in this deployment.</Hint>
      </FieldRow>
      <FieldRow>
        <FieldLabel htmlFor="kin_user_pass" required>
          Password for user &quot;{KIN_OS_USER}&quot;
        </FieldLabel>
        <PasswordInput
          id="kin_user_pass"
          autoComplete="new-password"
          value={draft.kin_user_pass}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ kin_user_pass: e.target.value });
          }}
        />
        <Hint>
          At least 8 characters. This sudo-capable account is what future install/provisioning
          steps will use.
        </Hint>
      </FieldRow>
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/topology")}>
          Back
        </Button>
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
