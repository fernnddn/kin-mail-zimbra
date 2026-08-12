import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { useEula } from "../eula";
import { useSetup, wizardHomePath } from "../setup";
import {
  Brand,
  Button,
  Card,
  CheckRow,
  Err,
  Lede,
  Shell,
  Title,
} from "../ui";

export default function EulaPage() {
  const { loading, accepted, title, body, accept } = useEula();
  const { deployed, installInProgress, loading: setupLoading } = useSetup();
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!loading && !setupLoading && accepted) {
    if (deployed) return <Navigate to="/login" replace />;
    return <Navigate to={wizardHomePath(installInProgress, deployed)} replace />;
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!agreed) {
      setError("You must agree to the terms of service.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      await accept();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Accept failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <Card style={{ width: "min(640px, 100%)" }}>
        <form onSubmit={onSubmit}>
          <Brand>KIN Mail</Brand>
          <Title>{title}</Title>
          <Lede>Read and accept before signing in to the admin console.</Lede>
          <pre
            style={{
              whiteSpace: "pre-wrap",
              background: "#f8fafc",
              border: "1px solid #e2e8f0",
              borderRadius: 8,
              padding: "0.9rem 1rem",
              maxHeight: 280,
              overflow: "auto",
              color: "#334155",
              fontSize: "0.86rem",
              lineHeight: 1.45,
              margin: "0 0 1rem",
              fontFamily: "inherit",
            }}
          >
            {loading ? "Loading…" : body}
          </pre>
          <CheckRow>
            <input
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
            />
            <span>I have read and agree to the terms of service</span>
          </CheckRow>
          <Err>{error}</Err>
          <Button type="submit" disabled={!agreed || busy || loading} style={{ width: "100%" }}>
            {busy ? "Saving…" : "Accept and continue"}
          </Button>
        </form>
      </Card>
    </Shell>
  );
}
