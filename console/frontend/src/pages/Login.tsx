import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import styled from "@emotion/styled";
import { useAuth } from "../auth";
import { useEula } from "../eula";
import { useSetup, wizardHomePath } from "../setup";
import { theme } from "../styles/theme";
import { BrandLockup, Button, Card, Err, Input, Label, PasswordInput, Shell, Title } from "../ui";

const PageCol = styled.div`
  width: min(360px, 100%);
  min-height: calc(100vh - 4rem);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 1.25rem;
`;

const Sub = styled.p`
  margin: 0 0 1.5rem;
  text-align: center;
  font-size: 0.875rem;
  color: ${theme.surface[400]};
`;

const CenterTitle = styled(Title)`
  text-align: center;
  font-size: 1.25rem;
  margin-bottom: 0.25rem;
`;

const Foot = styled.div`
  padding-top: 1.5rem;
  text-align: center;
  font-size: 0.75rem;
  color: ${theme.surface[400]};

  p {
    margin: 0;
  }

  p + p {
    margin-top: 0.15rem;
    font-size: 10px;
    color: ${theme.surface[300]};
  }
`;

const Logo = styled.div`
  display: flex;
  justify-content: center;
`;

export default function LoginPage() {
  const { user, loading, login, completeMfa } = useAuth();
  const { accepted, loading: eulaLoading } = useEula();
  const { deployed, installInProgress, loading: setupLoading } = useSetup();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [mfaUser, setMfaUser] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (!setupLoading && !deployed && !eulaLoading && !accepted) {
    return <Navigate to="/eula" replace />;
  }
  if (!setupLoading && !deployed && !user) {
    return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
  }
  if (!loading && !setupLoading && user) {
    return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
  }

  async function onPasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const result = await login(username, password);
      if (result.status === "mfa_required") {
        setMfaToken(result.mfa_token);
        setMfaUser(result.username);
        setCode("");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  async function onMfaSubmit(e: FormEvent) {
    e.preventDefault();
    if (!mfaToken) return;
    setError("");
    setBusy(true);
    try {
      await completeMfa(mfaToken, code);
    } catch (err) {
      setError(err instanceof Error ? err.message : "MFA verification failed");
    } finally {
      setBusy(false);
    }
  }

  function backToPassword() {
    setMfaToken(null);
    setMfaUser("");
    setCode("");
    setError("");
  }

  return (
    <Shell>
      <PageCol>
        <Logo>
          <BrandLockup />
        </Logo>
        <Card style={{ width: "100%" }}>
          {mfaToken ? (
            <form onSubmit={(e) => void onMfaSubmit(e)}>
              <CenterTitle>Authenticator code</CenterTitle>
              <Sub>
                Enter the 6-digit code for <strong>{mfaUser || username}</strong>
              </Sub>
              {error ? <Err>{error}</Err> : null}
              <Label htmlFor="mfa_code">Authentication code</Label>
              <Input
                id="mfa_code"
                name="mfa_code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="123456"
                required
                disabled={busy}
                autoFocus
              />
              <Button type="submit" loading={busy} style={{ width: "100%" }}>
                {busy ? "Verifying…" : "Verify"}
              </Button>
              <button
                type="button"
                onClick={backToPassword}
                disabled={busy}
                style={{
                  display: "block",
                  margin: "0.85rem auto 0",
                  padding: 0,
                  border: 0,
                  background: "none",
                  color: "inherit",
                  font: "inherit",
                  fontSize: "0.8rem",
                  cursor: "pointer",
                  textDecoration: "underline",
                  opacity: busy ? 0.6 : 1,
                }}
              >
                Back to password
              </button>
            </form>
          ) : (
            <form onSubmit={(e) => void onPasswordSubmit(e)}>
              <CenterTitle>Welcome Back</CenterTitle>
              <Sub>Sign in to the KIN Mail admin console</Sub>
              {error ? <Err>{error}</Err> : null}
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                name="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="username"
                required
                disabled={busy}
              />
              <Label htmlFor="password">Password</Label>
              <PasswordInput
                id="password"
                name="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Enter your password"
                required
                disabled={busy}
              />
              <Button type="submit" loading={busy} style={{ width: "100%" }}>
                {busy ? "Signing in…" : "Sign In"}
              </Button>
            </form>
          )}
        </Card>
        <Foot>
          <p>KIN Mail Console</p>
          <p>&copy; {new Date().getFullYear()} Karya Informasi Nusantara</p>
        </Foot>
      </PageCol>
    </Shell>
  );
}
