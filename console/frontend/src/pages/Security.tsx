import { FormEvent, useEffect, useState } from "react";
import styled from "@emotion/styled";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import {
  Button,
  Hint,
  Input,
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

const Mono = styled.code`
  display: block;
  margin: 0.4rem 0 0.85rem;
  padding: 0.55rem 0.7rem;
  border-radius: ${theme.radius.sm};
  background: ${theme.bg};
  border: 1px solid ${theme.line};
  font-size: 0.78rem;
  word-break: break-all;
`;

type BeginResp = {
  status: string;
  enroll_token: string;
  secret: string;
  otpauth_uri: string;
};

export default function SecurityPage() {
  const { user, refresh } = useAuth();
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [pwMsg, setPwMsg] = useState("");
  const [pwErr, setPwErr] = useState("");
  const [pwBusy, setPwBusy] = useState(false);

  const [mfaEnabled, setMfaEnabled] = useState(Boolean(user?.mfa_enabled));
  const [enrollToken, setEnrollToken] = useState("");
  const [secret, setSecret] = useState("");
  const [otpauthUri, setOtpauthUri] = useState("");
  const [code, setCode] = useState("");
  const [mfaMsg, setMfaMsg] = useState("");
  const [mfaErr, setMfaErr] = useState("");
  const [mfaBusy, setMfaBusy] = useState(false);

  useEffect(() => {
    setMfaEnabled(Boolean(user?.mfa_enabled));
  }, [user?.mfa_enabled]);

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
        body: JSON.stringify({ password }),
      });
      setPassword("");
      setPassword2("");
      setPwMsg("Password updated.");
    } catch (err) {
      setPwErr(err instanceof Error ? err.message : "Could not change password");
    } finally {
      setPwBusy(false);
    }
  }

  async function beginMfa() {
    setMfaErr("");
    setMfaMsg("");
    setMfaBusy(true);
    try {
      const resp = await api<BeginResp>("/api/me/mfa/begin", { method: "POST" });
      setEnrollToken(resp.enroll_token);
      setSecret(resp.secret);
      setOtpauthUri(resp.otpauth_uri);
      setCode("");
    } catch (err) {
      setMfaErr(err instanceof Error ? err.message : "Could not start MFA setup");
    } finally {
      setMfaBusy(false);
    }
  }

  async function confirmMfa(e: FormEvent) {
    e.preventDefault();
    setMfaErr("");
    setMfaMsg("");
    setMfaBusy(true);
    try {
      await api("/api/me/mfa/confirm", {
        method: "POST",
        body: JSON.stringify({ enroll_token: enrollToken, code }),
      });
      setEnrollToken("");
      setSecret("");
      setOtpauthUri("");
      setCode("");
      setMfaEnabled(true);
      setMfaMsg("MFA is now enabled for your account.");
      await refresh();
    } catch (err) {
      setMfaErr(err instanceof Error ? err.message : "Could not confirm MFA");
    } finally {
      setMfaBusy(false);
    }
  }

  async function disableMfa(e: FormEvent) {
    e.preventDefault();
    setMfaErr("");
    setMfaMsg("");
    setMfaBusy(true);
    try {
      await api("/api/me/mfa/disable", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      setCode("");
      setMfaEnabled(false);
      setMfaMsg("MFA has been disabled for your account.");
      await refresh();
    } catch (err) {
      setMfaErr(err instanceof Error ? err.message : "Could not disable MFA");
    } finally {
      setMfaBusy(false);
    }
  }

  const localAccount = user?.auth_type !== "ad";

  return (
    <ConsoleChrome>
      <Page>
        <PageHeader
          title="Security"
          subtitle="Manage your console password and optional authenticator (TOTP) MFA."
        />

        {localAccount ? (
          <Section>
            <SectionTitle>Change password</SectionTitle>
            <Hint>Updates the password for this console account only.</Hint>
            {pwErr ? <Hint>{pwErr}</Hint> : null}
            {pwMsg ? <Hint>{pwMsg}</Hint> : null}
            <form onSubmit={(e) => void onChangePassword(e)}>
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
                {pwBusy ? "Saving…" : "Update password"}
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

        <Section>
          <SectionTitle>Authenticator MFA (TOTP)</SectionTitle>
          <Hint>
            Optional second factor after your password. Works the same for local and AD-backed
            console accounts. A Super Admin can reset MFA for another user from the Users page if a
            phone is lost.
          </Hint>
          {mfaErr ? <Hint>{mfaErr}</Hint> : null}
          {mfaMsg ? <Hint>{mfaMsg}</Hint> : null}

          {mfaEnabled ? (
            <form onSubmit={(e) => void disableMfa(e)}>
              <Hint>MFA is enabled on this account.</Hint>
              <Label htmlFor="dis_code">Current authenticator code</Label>
              <Input
                id="dis_code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="123456"
                required
                disabled={mfaBusy}
              />
              <Button type="submit" variant="ghost" loading={mfaBusy}>
                {mfaBusy ? "Disabling…" : "Disable MFA"}
              </Button>
            </form>
          ) : enrollToken ? (
            <form onSubmit={(e) => void confirmMfa(e)}>
              <Hint>
                Add this account in your authenticator app (Google Authenticator, Authy, 1Password,
                etc.), then enter one code to finish. MFA stays off until that code succeeds.
              </Hint>
              <Label>Secret (base32)</Label>
              <Mono>{secret}</Mono>
              <Label>otpauth URI</Label>
              <Mono>{otpauthUri}</Mono>
              <Label htmlFor="en_code">Authentication code</Label>
              <Input
                id="en_code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="123456"
                required
                disabled={mfaBusy}
              />
              <Button type="submit" loading={mfaBusy}>
                {mfaBusy ? "Confirming…" : "Confirm and enable MFA"}
              </Button>
            </form>
          ) : (
            <Button type="button" loading={mfaBusy} onClick={() => void beginMfa()}>
              {mfaBusy ? "Starting…" : "Set up MFA"}
            </Button>
          )}
        </Section>
      </Page>
    </ConsoleChrome>
  );
}
