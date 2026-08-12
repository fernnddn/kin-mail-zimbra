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
  const { save, error, saving } = useWizard();
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
    const missing: string[] = [];
    if (!rootPass) missing.push("Root password");
    if (!adminPass) missing.push("Admin password");
    if (missing.length) {
      setFieldErr(`Please fill in: ${missing.join(", ")}.`);
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
    setFieldErr("");
    await save({
      host_root_pass: rootPass,
      kin_user_pass: adminPass,
      current_step: "domain",
    });
    navigate("/wizard/domain");
  }

  return (
    <>
      <Title>Host credentials</Title>
      <Lede>
        Set the root password and the admin password for the host(s) in this deployment. Both are
        required.
      </Lede>
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
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
