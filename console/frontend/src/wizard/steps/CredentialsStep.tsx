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
  PairGrid,
  PasswordInput,
  Title,
} from "../../ui";
import { useWizard } from "../WizardContext";

export default function CredentialsStep() {
  const { draft, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [rootPass, setRootPass] = useState("");
  const [rootConfirm, setRootConfirm] = useState("");
  const [adminPass, setAdminPass] = useState("");
  const [adminConfirm, setAdminConfirm] = useState("");
  const [fieldErr, setFieldErr] = useState("");

  function clearErr() {
    setFieldErr("");
  }

  async function next() {
    const replacing = !!(rootPass || adminPass);
    if (!draft.host_credentials_set && (!rootPass || !adminPass)) {
      const missing: string[] = [];
      if (!rootPass) missing.push("Root password");
      if (!adminPass) missing.push("Admin password");
      setFieldErr(`Please fill in: ${missing.join(", ")}.`);
      return;
    }
    if (replacing) {
      if (!rootPass || !adminPass) {
        setFieldErr("Enter both passwords to replace the stored credentials.");
        return;
      }
      if (rootPass.length < 8) {
        setFieldErr("Root password must be at least 8 characters.");
        return;
      }
      if (adminPass.length < 8) {
        setFieldErr("Admin password must be at least 8 characters.");
        return;
      }
      if (rootPass !== rootConfirm) {
        setFieldErr("Root password and confirmation do not match.");
        return;
      }
      if (adminPass !== adminConfirm) {
        setFieldErr("Admin password and confirmation do not match.");
        return;
      }
    }
    setFieldErr("");
    await save({
      ...(replacing ? { host_root_pass: rootPass, kin_user_pass: adminPass } : {}),
      current_step: "domain",
    });
    navigate("/wizard/domain");
  }

  return (
    <>
      <Title>Host credentials</Title>
      <Lede>
        Set the root password and the admin password for the host(s) in this deployment. Both are
        required. They are stored encrypted for later provisioning and are never shown again.
      </Lede>
      {draft.host_credentials_set ? (
        <Hint>
          Credentials are already stored. Leave the fields empty to keep them, or enter new values
          to replace.
        </Hint>
      ) : null}
      <PairGrid>
        <FieldRow>
          <FieldLabel htmlFor="host_root_pass" required>
            Root password
          </FieldLabel>
          <PasswordInput
            id="host_root_pass"
            autoComplete="new-password"
            value={rootPass}
            onChange={(e) => {
              clearErr();
              setRootPass(e.target.value);
            }}
          />
        </FieldRow>
        <FieldRow>
          <FieldLabel htmlFor="host_root_pass_confirm" required>
            Confirm root password
          </FieldLabel>
          <PasswordInput
            id="host_root_pass_confirm"
            autoComplete="new-password"
            value={rootConfirm}
            onChange={(e) => {
              clearErr();
              setRootConfirm(e.target.value);
            }}
          />
        </FieldRow>
        <FieldRow>
          <FieldLabel htmlFor="host_admin_pass" required>
            Admin password
          </FieldLabel>
          <PasswordInput
            id="host_admin_pass"
            autoComplete="new-password"
            value={adminPass}
            onChange={(e) => {
              clearErr();
              setAdminPass(e.target.value);
            }}
          />
        </FieldRow>
        <FieldRow>
          <FieldLabel htmlFor="host_admin_pass_confirm" required>
            Confirm admin password
          </FieldLabel>
          <PasswordInput
            id="host_admin_pass_confirm"
            autoComplete="new-password"
            value={adminConfirm}
            onChange={(e) => {
              clearErr();
              setAdminConfirm(e.target.value);
            }}
          />
        </FieldRow>
      </PairGrid>
      <Hint>At least 8 characters each. Confirmation must match.</Hint>
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/topology")}>
          Back
        </Button>
        <Button type="button" loading={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
