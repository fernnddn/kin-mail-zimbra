import { useNavigate } from "react-router-dom";
import {
  Button,
  CheckRow,
  Err,
  FieldRow,
  Hint,
  Input,
  Label,
  Lede,
  NavRow,
  Title,
} from "../../ui";
import { useWizard } from "../WizardContext";

export default function HybridStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function next(enabled: boolean) {
    await save({
      ad_auth_enabled: enabled,
      ad_ldap_url: draft.ad_ldap_url,
      ad_search_base: draft.ad_search_base,
      ad_search_filter: draft.ad_search_filter || "(sAMAccountName=%u)",
      ad_search_bind_dn: draft.ad_search_bind_dn,
      ad_search_bind_password: draft.ad_search_bind_password,
      ad_bind_dn_template: draft.ad_bind_dn_template,
      ad_test_user: draft.ad_test_user,
      ad_test_pass: draft.ad_test_pass,
      current_step: "zpush",
    });
    navigate("/wizard/zpush");
  }

  async function skip() {
    setLocal({ ad_auth_enabled: false });
    await next(false);
  }

  return (
    <>
      <Title>Hybrid authentication (Active Directory)</Title>
      <Lede>
        Optional. Domain can bind to customer AD with fallback to local Zimbra passwords. Skip if
        AD is not ready — local auth only.
      </Lede>
      <CheckRow>
        <input
          type="checkbox"
          checked={draft.ad_auth_enabled}
          onChange={(e) => setLocal({ ad_auth_enabled: e.target.checked })}
        />
        <span>Configure Active Directory bind now</span>
      </CheckRow>
      {draft.ad_auth_enabled && (
        <>
          <FieldRow>
            <Label htmlFor="ad_ldap_url">LDAP/AD URL</Label>
            <Input
              id="ad_ldap_url"
              value={draft.ad_ldap_url}
              onChange={(e) => setLocal({ ad_ldap_url: e.target.value })}
              placeholder="ldap://dc.corp.local:389"
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_search_base">Search base</Label>
            <Input
              id="ad_search_base"
              value={draft.ad_search_base}
              onChange={(e) => setLocal({ ad_search_base: e.target.value })}
              placeholder="DC=corp,DC=local"
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_search_filter">Search filter</Label>
            <Input
              id="ad_search_filter"
              value={draft.ad_search_filter}
              onChange={(e) => setLocal({ ad_search_filter: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_search_bind_dn">Bind DN for search (service account)</Label>
            <Input
              id="ad_search_bind_dn"
              value={draft.ad_search_bind_dn}
              onChange={(e) => setLocal({ ad_search_bind_dn: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_search_bind_password">Search bind DN password</Label>
            <Input
              id="ad_search_bind_password"
              type="password"
              value={draft.ad_search_bind_password}
              onChange={(e) => setLocal({ ad_search_bind_password: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_bind_dn_template">Optional bind DN template</Label>
            <Input
              id="ad_bind_dn_template"
              value={draft.ad_bind_dn_template}
              onChange={(e) => setLocal({ ad_bind_dn_template: e.target.value })}
              placeholder="%u@corp.local"
            />
            <Hint>Leave empty to use search bind.</Hint>
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_test_user">AD test account (full email on mail domain)</Label>
            <Input
              id="ad_test_user"
              value={draft.ad_test_user}
              onChange={(e) => setLocal({ ad_test_user: e.target.value })}
            />
          </FieldRow>
          <FieldRow>
            <Label htmlFor="ad_test_pass">AD test account password</Label>
            <Input
              id="ad_test_pass"
              type="password"
              value={draft.ad_test_pass}
              onChange={(e) => setLocal({ ad_test_pass: e.target.value })}
            />
          </FieldRow>
        </>
      )}
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/tls")}>
          Back
        </Button>
        <div style={{ display: "flex", gap: "0.6rem" }}>
          <Button type="button" variant="ghost" disabled={saving} onClick={() => void skip()}>
            Skip
          </Button>
          <Button
            type="button"
            disabled={saving}
            onClick={() => void next(draft.ad_auth_enabled)}
          >
            Continue
          </Button>
        </div>
      </NavRow>
    </>
  );
}
