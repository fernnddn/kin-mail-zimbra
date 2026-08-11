import { useNavigate } from "react-router-dom";
import {
  Button,
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

export default function DomainStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();

  async function next() {
    if (!draft.mail_domain.trim() || !draft.mail_host.trim() || draft.admin_pass.length < 8) {
      return;
    }
    const host =
      draft.mail_host.trim() ||
      (draft.mail_domain ? `mail.${draft.mail_domain.trim()}` : "");
    const le =
      draft.le_email.trim() ||
      (draft.mail_domain ? `admin@${draft.mail_domain.trim()}` : "");
    await save({
      mail_domain: draft.mail_domain.trim(),
      mail_host: host,
      timezone: draft.timezone.trim() || "Asia/Jakarta",
      admin_pass: draft.admin_pass,
      le_email: le,
      current_step: "tls",
    });
    navigate("/wizard/tls");
  }

  const canNext =
    draft.mail_domain.trim().length > 0 &&
    (draft.mail_host.trim().length > 0 || draft.mail_domain.trim().length > 0) &&
    draft.admin_pass.length >= 8;

  return (
    <>
      <Title>Domain &amp; mail settings</Title>
      <Lede>
        Maps to the identity and credential fields collected by <code>00-config.sh</code>. Values
        stay in the console draft store until a later slice applies them.
      </Lede>
      <FieldRow>
        <Label htmlFor="mail_domain">Email domain (part after @)</Label>
        <Input
          id="mail_domain"
          value={draft.mail_domain}
          onChange={(e) => setLocal({ mail_domain: e.target.value })}
          placeholder="example.co.id"
        />
      </FieldRow>
      <FieldRow>
        <Label htmlFor="mail_host">Mail server hostname (FQDN)</Label>
        <Input
          id="mail_host"
          value={draft.mail_host}
          onChange={(e) => setLocal({ mail_host: e.target.value })}
          placeholder={draft.mail_domain ? `mail.${draft.mail_domain}` : "mail.example.co.id"}
        />
      </FieldRow>
      <FieldRow>
        <Label htmlFor="timezone">System timezone</Label>
        <Input
          id="timezone"
          value={draft.timezone}
          onChange={(e) => setLocal({ timezone: e.target.value })}
          placeholder="Asia/Jakarta"
        />
        <Hint>Asia/Jakarta maps to Zimbra Asia/Bangkok (same UTC+7, no DST).</Hint>
      </FieldRow>
      <FieldRow>
        <Label htmlFor="admin_pass">Zimbra admin password (min 8 characters)</Label>
        <Input
          id="admin_pass"
          type="password"
          autoComplete="new-password"
          value={draft.admin_pass}
          onChange={(e) => setLocal({ admin_pass: e.target.value })}
        />
      </FieldRow>
      <FieldRow>
        <Label htmlFor="le_email">Email for Let&apos;s Encrypt notifications</Label>
        <Input
          id="le_email"
          type="email"
          value={draft.le_email}
          onChange={(e) => setLocal({ le_email: e.target.value })}
          placeholder={draft.mail_domain ? `admin@${draft.mail_domain}` : "admin@example.co.id"}
        />
      </FieldRow>
      <Err>{error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/topology")}>
          Back
        </Button>
        <Button type="button" disabled={!canNext || saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
