import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Choice, ChoiceGrid, Err, FieldLabel, Hint, Lede, NavRow, PasswordInput, Title } from "../../ui";
import { api } from "../../api";
import { useWizard } from "../WizardContext";
import type { WizardDraft } from "../types";

export default function TlsStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");
  const [cfToken, setCfToken] = useState("");
  const [cfSaved, setCfSaved] = useState(false);
  const [cfBusy, setCfBusy] = useState(false);
  const [cfErr, setCfErr] = useState("");

  async function saveCloudflareToken() {
    setCfErr("");
    setCfBusy(true);
    try {
      await api("/api/settings/cloudflare-token", {
        method: "POST",
        body: JSON.stringify({ token: cfToken.trim() }),
      });
      // Never keep the token in component state once it is stored.
      setCfToken("");
      setCfSaved(true);
    } catch (err) {
      setCfErr(err instanceof Error ? err.message : "Could not store the token");
    } finally {
      setCfBusy(false);
    }
  }

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
      {draft.tls_method === "cloudflare" && (
        <>
          <Hint>
            Certificates are issued and renewed automatically over DNS-01, with no operator
            present. To do that certbot needs a Cloudflare API token for this zone.
            <br />
            <br />
            In Cloudflare: <strong>My Profile → API Tokens → Create Token</strong>, use the
            <strong> Edit zone DNS</strong> template, and restrict it to this domain&apos;s zone
            only. Paste the token below; it is written to /etc/letsencrypt/cloudflare.ini as
            root-only and is never stored in the wizard or shown again.
          </Hint>
          <FieldLabel htmlFor="cftoken">Cloudflare API token</FieldLabel>
          <PasswordInput
            id="cftoken"
            value={cfToken}
            onChange={(e) => setCfToken(e.target.value)}
            autoComplete="off"
            placeholder={cfSaved ? "Stored - paste again only to replace it" : ""}
          />
          <NavRow>
            <Button
              type="button"
              variant="secondary"
              loading={cfBusy}
              disabled={!cfToken.trim()}
              onClick={() => void saveCloudflareToken()}
            >
              {cfSaved ? "Replace token" : "Store token"}
            </Button>
          </NavRow>
          {/* Hint tucks up under the element above it. After a row of buttons
              that puts it behind them, so this one gets normal spacing. */}
          {cfSaved ? (
            <Hint style={{ marginTop: "0.9rem" }}>
              Token stored. Deploy can now issue and renew on its own.
            </Hint>
          ) : null}
          <Err>{cfErr}</Err>
          <Hint>
            Without a token the deploy stops at the certificate step rather than issuing a
            broken one. If you cannot create a token now, choose Manual or Customer instead.
          </Hint>
        </>
      )}
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
