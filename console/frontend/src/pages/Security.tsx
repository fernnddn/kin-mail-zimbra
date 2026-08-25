import { FormEvent, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import {
  Button,
  Hint,
  Label,
  Page,
  PageHeader,
  PasswordInput,
} from "../ui";

const Section = styled.section`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1.05rem 1.1rem 1.15rem;
  margin: 0 0 1rem;
`;

const SectionTitle = styled.h2`
  margin: 0 0 0.35rem;
  font-size: 1.05rem;
  font-weight: 700;
  letter-spacing: -0.02em;
`;

export default function SecurityPage() {
  const { user } = useAuth();
  const [currentPassword, setCurrentPassword] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [pwMsg, setPwMsg] = useState("");
  const [pwErr, setPwErr] = useState("");
  const [pwBusy, setPwBusy] = useState(false);

  async function onChangePassword(e: FormEvent) {
    e.preventDefault();
    setPwErr("");
    setPwMsg("");
    if (password !== password2) {
      setPwErr("Passwords do not match.");
      return;
    }
    setPwBusy(true);
    try {
      await api("/api/me/password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          password,
        }),
      });
      setCurrentPassword("");
      setPassword("");
      setPassword2("");
      setPwMsg("Password updated.");
    } catch (err) {
      setPwErr(err instanceof Error ? err.message : "Could not change password");
    } finally {
      setPwBusy(false);
    }
  }

  const localAccount = user?.auth_type !== "ad";

  return (
    <ConsoleChrome>
      <Page>
        <PageHeader title="Security" subtitle="Manage your console password." />

        {localAccount ? (
          <Section>
            <SectionTitle>Change password</SectionTitle>
            <Hint>Updates the password for this console account only.</Hint>
            {pwErr ? <Hint>{pwErr}</Hint> : null}
            {pwMsg ? <Hint>{pwMsg}</Hint> : null}
            <form onSubmit={(e) => void onChangePassword(e)}>
              <Label htmlFor="cp">Current password</Label>
              <PasswordInput
                id="cp"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                minLength={1}
                required
                autoComplete="current-password"
              />
              <Label htmlFor="np">New password</Label>
              <PasswordInput
                id="np"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={8}
                required
                autoComplete="new-password"
              />
              <Label htmlFor="np2">Confirm password</Label>
              <PasswordInput
                id="np2"
                value={password2}
                onChange={(e) => setPassword2(e.target.value)}
                minLength={8}
                required
                autoComplete="new-password"
              />
              <Button type="submit" loading={pwBusy}>
                {pwBusy ? "Saving..." : "Update password"}
              </Button>
            </form>
          </Section>
        ) : (
          <Section>
            <SectionTitle>Password</SectionTitle>
            <Hint>
              This account signs in with Active Directory. Change the password in AD, not here.
            </Hint>
          </Section>
        )}
      </Page>
    </ConsoleChrome>
  );
}
