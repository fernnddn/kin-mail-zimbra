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
      {/*
        This used to be one paragraph about "mail A, mail B, and the
        Observability VM" regardless of what was being built. Two of those
        machines do not exist on a single appliance and none of them exist
        under those names on a split, so the step that asks for two passwords
        described a deployment the operator was not doing - and the question
        it actually raises ("is this the Zimbra admin password?") went
        unanswered on every topology.
      */}
      {draft.topology === "split" ? (
        <Lede>
          Two Linux accounts, not email accounts: the root password, and the password for Linux
          user kin (sudo). They are set on this server and on the mailbox, which is built from
          here over SSH. The kin password below replaces whatever the mailbox account has now,
          including the one you entered on the previous step - so after the install, log in to
          the mailbox with this one.
        </Lede>
      ) : draft.topology === "2vm" ? (
        <Lede>
          These must already work over SSH on mail A, mail B, and the Observability VM: the root
          password, and the password for Linux user kin (sudo). Build HA pair logs in with those
          accounts; Ubuntu cloud images need PasswordAuthentication enabled first. Mail B does
          not need a console bootstrap; the install tree is copied from this host.
        </Lede>
      ) : (
        <Lede>
          Two Linux accounts on this server, not email accounts: the root password, and the
          password for Linux user kin (sudo). The Zimbra administrator password is set later, on
          the Domain &amp; mail step.
        </Lede>
      )}
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
      {/*
        "Admin password" is kin_user_pass - the Linux sudo account. The label
        has been read as the Zimbra administrator password more than once,
        which is a different password set on a different step, so say so where
        the question gets asked rather than only in the paragraph above.
      */}
      <Hint>
        At least 8 characters each. Confirmation must match. &ldquo;Admin&rdquo; here means the
        Linux user kin, not the Zimbra administrator.
      </Hint>
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
