import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { useEula } from "../eula";
import { useSetup, wizardHomePath } from "../setup";
import { theme } from "../styles/theme";
import styled from "@emotion/styled";
import {
  BrandLockup,
  Button,
  CheckRow,
  Err,
  GateAside,
  GateAsideBody,
  GateAsideFoot,
  GateForm,
  GateMain,
  GateShell,
  Lede,
  Skeleton,
  Title,
} from "../ui";

/* The agreement itself, on recessed paper. It is a document, so it is set as
   a document: reading width, generous line height, its own scroll. */
const Terms = styled.pre`
  white-space: pre-wrap;
  background: ${theme.bgPanel};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.sm};
  padding: 1rem 1.15rem;
  max-height: 20rem;
  overflow: auto;
  color: ${theme.ink};
  font-family: inherit;
  font-size: 0.86rem;
  line-height: 1.6;
  margin: 0 0 1.1rem;
`;

const TermsSkeleton = styled.div`
  background: ${theme.bgPanel};
  border: 1px solid ${theme.line};
  border-radius: ${theme.radius.sm};
  padding: 1rem 1.15rem;
  margin: 0 0 1.1rem;
  display: grid;
  gap: 0.5rem;
`;

export default function EulaPage() {
  const { loading, accepted, title, body, accept } = useEula();
  const { deployed, installInProgress, wizardRequiresLogin, loading: setupLoading } =
    useSetup();
  const [agreed, setAgreed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!setupLoading && deployed) {
    return <Navigate to="/login" replace />;
  }
  if (!loading && !setupLoading && accepted) {
    if (wizardRequiresLogin) {
      return <Navigate to="/login" replace />;
    }
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
    <GateShell>
      <GateAside>
        <GateAsideBody>
          <BrandLockup light />
          <p>
            Before this appliance is set up, the terms below have to be accepted. It
            takes a minute and happens once.
          </p>
        </GateAsideBody>
        <GateAsideFoot>{new Date().getFullYear()} Karya Informasi Nusantara</GateAsideFoot>
      </GateAside>
      <GateMain>
        <GateForm>
          <form onSubmit={onSubmit}>
            <Title>{title}</Title>
            <Lede>Read this and accept it to continue.</Lede>
            {loading ? (
              <TermsSkeleton>
                <Skeleton $h="0.7rem" $w="92%" />
                <Skeleton $h="0.7rem" $w="80%" />
                <Skeleton $h="0.7rem" $w="88%" />
              </TermsSkeleton>
            ) : (
              <Terms>{body}</Terms>
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
              {busy ? "Saving" : "Accept and continue"}
            </Button>
          </form>
        </GateForm>
      </GateMain>
    </GateShell>
  );
}
