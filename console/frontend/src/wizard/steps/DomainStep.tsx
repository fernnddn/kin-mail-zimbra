import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Button,
  Err,
  FieldLabel,
  FieldRow,
  Hint,
  Input,
  Lede,
  NavRow,
  PasswordInput,
  Title,
} from "../../ui";
import { useWizard } from "../WizardContext";

export default function DomainStep() {
  const { draft, setLocal, save, error, saving } = useWizard();
  const navigate = useNavigate();
  const [fieldErr, setFieldErr] = useState("");

  async function next() {
    const domain = draft.mail_domain.trim();
    const host =
      draft.mail_host.trim() || (domain ? `mail.${domain}` : "");
    const tz = draft.timezone.trim() || "Asia/Jakarta";
    const le =
      draft.le_email.trim() || (domain ? `admin@${domain}` : "");
    const missing: string[] = [];
    if (!domain) missing.push("Email domain");
    if (!host) missing.push("Mail server hostname");
    if (!tz) missing.push("Timezone");
    const typedPass = draft.admin_pass;
    if (typedPass) {
      if (typedPass.length < 8) missing.push("Admin password (min 8 characters)");
    } else if (!draft.admin_pass_set) {
      missing.push("Admin password (min 8 characters)");
    }
    if (!le) missing.push("Certificate notification email");
    if (missing.length) {
      setFieldErr(`Please fill in: ${missing.join(", ")}.`);
      return;
    }
    setFieldErr("");
    await save({
      mail_domain: domain,
      mail_host: host,
      timezone: tz,
      ...(typedPass ? { admin_pass: typedPass } : {}),
      le_email: le,
      current_step: "tls",
    });
    navigate("/wizard/tls");
  }

  return (
    <>
      <Title>Domain &amp; mail settings</Title>
      <Lede>
        Tell us the customer mail domain and the hostname users will use. Fields marked{" "}
        <span style={{ color: "#ef4444" }}>*</span> are required.
      </Lede>
      <FieldRow>
        <FieldLabel htmlFor="mail_domain" required>
          Email domain (part after @)
        </FieldLabel>
        <Input
          id="mail_domain"
          value={draft.mail_domain}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ mail_domain: e.target.value });
          }}
          placeholder="example.co.id"
        />
      </FieldRow>
      <FieldRow>
        <FieldLabel htmlFor="mail_host" required>
          Mail server hostname
        </FieldLabel>
        <Input
          id="mail_host"
          value={draft.mail_host}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ mail_host: e.target.value });
          }}
          placeholder={draft.mail_domain ? `mail.${draft.mail_domain}` : "mail.example.co.id"}
        />
        <Hint>Usually mail.your-domain. Leave blank only if you want us to suggest mail.&lt;domain&gt;.</Hint>
      </FieldRow>
      <FieldRow>
        <FieldLabel htmlFor="timezone" required>
          System timezone
        </FieldLabel>
        <Input
          id="timezone"
          value={draft.timezone}
          onChange={(e) => setLocal({ timezone: e.target.value })}
          placeholder="Asia/Jakarta"
        />
        <Hint>Default Asia/Jakarta is fine for WIB (UTC+7).</Hint>
      </FieldRow>
      <FieldRow>
        <FieldLabel htmlFor="admin_pass" required>
          Mail admin password
        </FieldLabel>
        <PasswordInput
          id="admin_pass"
          autoComplete="new-password"
          value={draft.admin_pass}
          onChange={(e) => {
            setFieldErr("");
            setLocal({ admin_pass: e.target.value });
          }}
        />
        <Hint>
          {draft.admin_pass_set
            ? "Already stored. Leave blank to keep it, or enter a new value (min 8 characters) to replace."
            : "At least 8 characters. Used for the mail system administrator account."}
        </Hint>
      </FieldRow>
      <FieldRow>
        <FieldLabel htmlFor="le_email" optional>
          Certificate notification email
        </FieldLabel>
        <Input
          id="le_email"
          type="email"
          value={draft.le_email}
          onChange={(e) => setLocal({ le_email: e.target.value })}
          placeholder={draft.mail_domain ? `admin@${draft.mail_domain}` : "admin@example.co.id"}
        />
        <Hint>Optional — defaults to admin@&lt;domain&gt; if left blank.</Hint>
      </FieldRow>
      <Err>{fieldErr || error}</Err>
      <NavRow>
        <Button type="button" variant="ghost" onClick={() => navigate("/wizard/credentials")}>
          Back
        </Button>
        <Button type="button" disabled={saving} onClick={() => void next()}>
          Continue
        </Button>
      </NavRow>
    </>
  );
}
