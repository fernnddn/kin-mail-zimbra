import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Choice, ChoiceGrid, Err, Hint, Lede, NavRow, Title } from "../../ui";
import { useWizard } from "../WizardContext";
import type { WizardDraft } from "../types";

export default function TlsStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  function pick(tls_method: WizardDraft["tls_method"]) {
    setFieldErr("");
    setLocal({ tls_method });
  }

  async function next() {
    if (!draft.tls_method) {
      setFieldErr("Choose how HTTPS certificates will be obtained.");
      return;
    }
    setFieldErr("");
    await save({ tls_method: draft.tls_method, current_step: "hybrid" });
    navigate("/wizard/hybrid");
  }

  return (
    <>
      <Title>HTTPS certificates</Title>
      <Lede>Choose how the mail hostname gets a trusted certificate for HTTPS and mail clients.</Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.tls_method === "cloudflare"}
          onClick={() => pick("cloudflare")}
        >
          <strong>Automatic (Cloudflare DNS)</strong>
          <span>Requires a Cloudflare API token. Renewals can be automated.</span>
        </Choice>
        <Choice
          type="button"
          selected={draft.tls_method === "manual"}
          onClick={() => pick("manual")}
        >
          <strong>Manual DNS record</strong>
          <span>
            Works with any DNS provider; someone creates a one-time TXT record when issuing the
            certificate.
          </span>
        </Choice>
        <Choice
          type="button"
          selected={draft.tls_method === "customer"}
          onClick={() => pick("customer")}
        >
          <strong>Customer-provided certificate</strong>
          <span>Skip automatic issuance; install the customer&apos;s certificate files later.</span>
        </Choice>
      </ChoiceGrid>
      {draft.tls_method === "manual" && (
        <Hint>Manual mode needs an operator available each time a certificate is issued or renewed.</Hint>
      )}
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/domain")}>
          Back
        </Button>
        <Button type="button" loading={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
