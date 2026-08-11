import { useNavigate } from "react-router-dom";
import { Button, Choice, ChoiceGrid, Err, Hint, Lede, NavRow, Title } from "../../ui";
import { useWizard } from "../WizardContext";
import type { WizardDraft } from "../types";

export default function TlsStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  function pick(tls_method: WizardDraft["tls_method"]) {
    setLocal({ tls_method });
  }

  async function next() {
    if (!draft.tls_method) return;
    await save({ tls_method: draft.tls_method, current_step: "hybrid" });
    navigate("/wizard/hybrid");
  }

  return (
    <>
      <Title>TLS issuance</Title>
      <Lede>Choose how mail-host certificates will be obtained (TLS_METHOD in config).</Lede>
      <ChoiceGrid>
        <Choice
          type="button"
          selected={draft.tls_method === "cloudflare"}
          onClick={() => pick("cloudflare")}
        >
          <strong>Cloudflare DNS-01 (automated)</strong>
          <span>Requires a Cloudflare API token. Renewals can be automated.</span>
        </Choice>
        <Choice
          type="button"
          selected={draft.tls_method === "manual"}
          onClick={() => pick("manual")}
        >
          <strong>Manual DNS-01</strong>
          <span>
            Any provider — operator creates the TXT record once. Does not auto-renew via cron.
          </span>
        </Choice>
        <Choice
          type="button"
          selected={draft.tls_method === "customer"}
          onClick={() => pick("customer")}
        >
          <strong>Customer-provided certificate</strong>
          <span>Skip certbot; install customer cert/key via zmcertmgr later.</span>
        </Choice>
      </ChoiceGrid>
      {draft.tls_method === "manual" && (
        <Hint>Manual mode needs an operator present for each issue/renew unless an auth-hook is added later.</Hint>
      )}
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/domain")}>
          Back
        </Button>
        <Button type="button" disabled={!draft.tls_method || saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
