import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  CheckRow,
  Err,
  FieldLabel,
  FieldRow,
  Hint,
  Input,
  Lede,
  NavRow,
  Title,
} from "../../ui";
import { useWizard } from "../WizardContext";

export default function HybridStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  async function go(enabled: boolean) {
    await save({
      ad_auth_enabled: enabled,
      ad_ldap_url: enabled ? draft.ad_ldap_url : "",
      ad_search_base: enabled ? draft.ad_search_base : "",
      ad_search_filter: draft.ad_search_filter || "(sAMAccountName=%u)",
      ad_search_bind_dn: enabled ? draft.ad_search_bind_dn : "",
      ad_search_bind_password: enabled ? draft.ad_search_bind_password : "",
      ad_bind_dn_template: enabled ? draft.ad_bind_dn_template : "",
      ad_test_user: enabled ? draft.ad_test_user : "",
      ad_test_pass: enabled ? draft.ad_test_pass : "",
      current_step: "zpush",
    });
    navigate("/wizard/zpush");
  }

  async function skip() {
    setFieldErr("");
    setLocal({ ad_auth_enabled: false });
    await go(false);
  }

  async function continueWithAd() {
    if (!draft.ad_auth_enabled) {
      setFieldErr('To skip Active Directory, use Skip. Or tick "Configure Active Directory bind now" to fill the fields.');
      return;
    }
    const missing: string[] = [];
    if (!draft.ad_ldap_url.trim()) missing.push("LDAP/AD URL");
    if (!draft.ad_search_base.trim()) missing.push("Search base");
    if (!draft.ad_search_bind_dn.trim()) missing.push("Search bind account");
    if (!draft.ad_search_bind_password) missing.push("Search bind password");
    if (!draft.ad_test_user.trim()) missing.push("Test account");
    if (!draft.ad_test_pass) missing.push("Test account password");
    if (missing.length) {
      setFieldErr(`Fill the required Active Directory fields: ${missing.join(", ")}.`);
      return;
    }
    setFieldErr("");
    await go(true);
  }

  return (
    <>
      <Title>Company directory login (optional)</Title>
      <Lede>
        You can let users sign in with their company Active Directory accounts. Skip this if AD is
        not ready — local mail passwords will be used instead.
      </Lede>
      <CheckRow>
        <input
          type="checkbox"
          checked={draft.ad_auth_enabled}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ ad_auth_enabled: e.target.checked });
          }}
        />
        <span>Configure Active Directory bind now</span>
      </CheckRow>
      {draft.ad_auth_enabled && (
        <>
          <FieldRow>
            <FieldLabel htmlFor="ad_ldap_url" required>
              LDAP/AD URL
            </FieldLabel>
            <Input
              id="ad_ldap_url"
              value={draft.ad_ldap_url}
              onChange={(e) => setLocal({ ad_ldap_url: e.target.value })}
              placeholder="ldap://dc.corp.local:389"
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_search_base" required>
              Search base
            </FieldLabel>
            <Input
              id="ad_search_base"
              value={draft.ad_search_base}
              onChange={(e) => setLocal({ ad_search_base: e.target.value })}
              placeholder="DC=corp,DC=local"
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_search_filter" optional>
              Search filter
            </FieldLabel>
            <Input
              id="ad_search_filter"
              value={draft.ad_search_filter}
              onChange={(e) => setLocal({ ad_search_filter: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_search_bind_dn" required>
              Search service account
            </FieldLabel>
            <Input
              id="ad_search_bind_dn"
              value={draft.ad_search_bind_dn}
              onChange={(e) => setLocal({ ad_search_bind_dn: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_search_bind_password" required>
              Search service account password
            </FieldLabel>
            <Input
              id="ad_search_bind_password"
              type="password"
              value={draft.ad_search_bind_password}
              onChange={(e) => setLocal({ ad_search_bind_password: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_bind_dn_template" optional>
              Optional bind template
            </FieldLabel>
            <Input
              id="ad_bind_dn_template"
              value={draft.ad_bind_dn_template}
              onChange={(e) => setLocal({ ad_bind_dn_template: e.target.value })}
              placeholder="%u@corp.local"
            />
            <Hint>Leave empty to use the search service account.</Hint>
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_test_user" required>
              Test account (full email on the mail domain)
            </FieldLabel>
            <Input
              id="ad_test_user"
              value={draft.ad_test_user}
              onChange={(e) => setLocal({ ad_test_user: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <FieldLabel htmlFor="ad_test_pass" required>
              Test account password
            </FieldLabel>
            <Input
              id="ad_test_pass"
              type="password"
              value={draft.ad_test_pass}
              onChange={(e) => setLocal({ ad_test_pass: e.target.value })}
            />
          </FieldRow>
        </>
      )}
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/tls")}>
          Back
        </Button>
        <div style={{ display: "flex", gap: "0.6rem" }}>
          {!draft.ad_auth_enabled ? (
            <Button type="button" disabled={saving} onClick={() => void skip()}>
              Skip
            </Button>
          ) : (
            <>
              <Button type="button" variant="ghost" disabled={saving} onClick={() => void skip()}>
                Skip instead
              </Button>
              <Button type="button" disabled={saving} onClick={() => void continueWithAd()}>
                Continue
              </Button>
            </>
          )}
        </div>
      </NavRow>
    </>
  );
}
