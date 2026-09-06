import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import styled from "@emotion/styled";
import { useAuth } from "../auth";
import { useEula } from "../eula";
import { useSetup, wizardHomePath } from "../setup";
import { theme } from "../styles/theme";
import {
  BrandLockup,
  Button,
  Err,
  GateAside,
  GateAsideBody,
  GateAsideFoot,
  GateForm,
  GateMain,
  GateShell,
  Input,
  Label,
  PasswordInput,
  Title,
} from "../ui";

const Sub = styled.p`
  margin: 0 0 1.6rem;
  font-size: 0.9rem;
  line-height: 1.5;
  color: ${theme.muted};
`;

const Fields = styled.form`
  /* One left edge for the label, the field and the button. Uneven padding
     between them is the thing that makes a form look unfinished. */
  display: block;
`;



export default function LoginPage() {
  const { user, loading, login } = useAuth();
  const { accepted, loading: eulaLoading } = useEula();
  const { deployed, installInProgress, wizardRequiresLogin, loading: setupLoading } =
    useSetup();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (!setupLoading && !deployed && !eulaLoading && !accepted) {
    return <Navigate to="/eula" replace />;
  }
  // Anonymous Host A wizard: bounce /login → Topology. If backend already
  // requires a session (peer / password file gone), stay on login.
  if (!setupLoading && !deployed && !user && !wizardRequiresLogin) {
    return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
  }
  if (!loading && !setupLoading && user) {
    return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(username, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <GateShell>
      <GateAside>
        <GateAsideBody>
          <BrandLockup light />
          <p>
            The admin console for the mail servers on this site. Sign in to check the
            cluster, add mailboxes, or take a node offline for maintenance.
          </p>
        </GateAsideBody>
        <GateAsideFoot>
          {new Date().getFullYear()} Karya Informasi Nusantara
        </GateAsideFoot>
      </GateAside>
      <GateMain>
        <GateForm>
          <Fields onSubmit={onSubmit}>
            <Title>Sign in</Title>
            <Sub>Use your console account. This is not a mailbox password.</Sub>
            {error ? <Err>{error}</Err> : null}
            <Label htmlFor="username">Username</Label>
            <Input
              id="username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoFocus
              disabled={busy}
            />
            <Label htmlFor="password">Password</Label>
            <PasswordInput
              id="password"
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              disabled={busy}
            />
            <Button type="submit" loading={busy} style={{ width: "100%" }}>
              {busy ? "Signing in" : "Sign in"}
            </Button>
          </Fields>
        </GateForm>
      </GateMain>
    </GateShell>
  );
}
