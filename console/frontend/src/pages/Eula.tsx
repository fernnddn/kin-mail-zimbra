import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { useEula } from "../eula";
import { useSetup, wizardHomePath } from "../setup";
import { theme } from "../styles/theme";
import { BrandLockup, Button, Card, CheckRow, Err, Lede, Shell, Skeleton, Title } from "../ui";

export default function EulaPage() {
  const { loading, accepted, title, body, accept } = useEula();
  const { deployed, installInProgress, loading: setupLoading } = useSetup();
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!setupLoading && deployed) {
    return <Navigate to="/login" replace />;
  }
  if (!loading && !setupLoading && accepted) {
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
          <div style={{ margin: "0 0 1rem" }}>
            <BrandLockup />
          </div>
          <Title>{title}</Title>
          <Lede>Read and accept before signing in to the admin console.</Lede>
          {loading ? (
            <div
              style={{
                background: theme.surface[50],
                border: `1px solid ${theme.line}`,
                borderRadius: theme.radius.md,
                padding: "0.9rem 1rem",
                margin: "0 0 1rem",
                display: "grid",
                gap: 8,
              }}
            >
              <Skeleton $h="0.7rem" $w="92%" />
              <Skeleton $h="0.7rem" $w="80%" />
              <Skeleton $h="0.7rem" $w="88%" />
            </div>
          ) : (
            <pre
              style={{
                whiteSpace: "pre-wrap",
                background: theme.surface[50],
                border: `1px solid ${theme.line}`,
                borderRadius: theme.radius.md,
                padding: "0.9rem 1rem",
                maxHeight: 280,
                overflow: "auto",
                color: theme.surface[700],
                fontSize: "0.86rem",
                lineHeight: 1.45,
                margin: "0 0 1rem",
                fontFamily: "inherit",
              }}
            >
              {body}
            </pre>
          )}
          <CheckRow>
            <input
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
            />
            <span>I have read and agree to the terms of service</span>
          </CheckRow>
          {error ? <Err>{error}</Err> : null}
          <Button
            type="submit"
            disabled={!agreed || loading}
            loading={busy}
            style={{ width: "100%" }}
          >
            {busy ? "Saving…" : "Accept and continue"}
          </Button>
        </form>
      </Card>
    </Shell>
  );
}
