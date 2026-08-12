import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { useAuth } from "../auth";
import { useEula } from "../eula";
import { useSetup, wizardHomePath } from "../setup";
import {
  Brand,
  Button,
  Card,
  Err,
  Input,
  Label,
  Lede,
  Shell,
  Title,
} from "../ui";

export default function LoginPage() {
  const { user, loading, login } = useAuth();
  const { accepted, loading: eulaLoading } = useEula();
  const { deployed, installInProgress, loading: setupLoading } = useSetup();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  if (!eulaLoading && !accepted) return <Navigate to="/eula" replace />;
  // Pre-deploy / mid-install: no login gate — send operators to the right wizard step.
  if (!setupLoading && !deployed && !user) {
    return <Navigate to={wizardHomePath(installInProgress)} replace />;
  }
  if (!loading && !setupLoading && user) {
    return <Navigate to={wizardHomePath(installInProgress)} replace />;
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
    <Shell>
      <Card>
        <form onSubmit={onSubmit}>
          <Brand>KIN Mail</Brand>
          <Title>Admin console</Title>
          <Lede>Sign in to continue. Anonymous access to the wizard is disabled.</Lede>
          <Label htmlFor="username">Username</Label>
          <Input
            id="username"
            name="username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          <Label htmlFor="password">Password</Label>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          <Err>{error}</Err>
          <Button type="submit" disabled={busy} style={{ width: "100%" }}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </Card>
    </Shell>
  );
}
