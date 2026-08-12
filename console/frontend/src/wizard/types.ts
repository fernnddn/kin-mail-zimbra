export type WizardDraft = {
  version: number;
  updated_at: string | null;
  current_step: string;
  topology: "" | "1vm" | "2vm";
  mail_domain: string;
  mail_host: string;
  timezone: string;
  admin_pass: string;
  le_email: string;
  tls_method: "" | "cloudflare" | "manual" | "customer";
  ad_auth_enabled: boolean;
  ad_ldap_url: string;
  ad_search_base: string;
  ad_search_filter: string;
  ad_search_bind_dn: string;
  ad_search_bind_password: string;
  ad_bind_dn_template: string;
  ad_test_user: string;
  ad_test_pass: string;
  zpush_enabled: boolean;
  contracted_seats: string;
  kin_admin_ips: string;
};

export type StepId =
  | "topology"
  | "domain"
  | "tls"
  | "hybrid"
  | "zpush"
  | "licensing"
  | "firewall"
  | "review"
  | "deploy";

export type StepDef = {
  id: StepId;
  label: string;
  crumb: string;
};

export const WIZARD_STEPS: StepDef[] = [
  { id: "topology", label: "Topology", crumb: "Topology" },
  { id: "domain", label: "Domain & mail", crumb: "Domain" },
  { id: "tls", label: "TLS", crumb: "TLS" },
  { id: "hybrid", label: "Hybrid auth", crumb: "Hybrid auth" },
  { id: "zpush", label: "Mobile email", crumb: "Mobile email" },
  { id: "licensing", label: "Mailbox seats", crumb: "Mailbox seats" },
  { id: "firewall", label: "Admin access (optional)", crumb: "Admin access" },
  { id: "review", label: "Review", crumb: "Review" },
  { id: "deploy", label: "Deploy", crumb: "Deploy" },
];

export function emptyDraft(): WizardDraft {
  return {
    version: 1,
    updated_at: null,
    current_step: "topology",
    topology: "",
    mail_domain: "",
    mail_host: "",
    timezone: "Asia/Jakarta",
    admin_pass: "",
    le_email: "",
    tls_method: "",
    ad_auth_enabled: false,
    ad_ldap_url: "",
    ad_search_base: "",
    ad_search_filter: "(sAMAccountName=%u)",
    ad_search_bind_dn: "",
    ad_search_bind_password: "",
    ad_bind_dn_template: "",
    ad_test_user: "",
    ad_test_pass: "",
    zpush_enabled: true,
    contracted_seats: "PLACEHOLDER_UNSET",
    kin_admin_ips: "",
  };
}

export function stepIndex(id: string): number {
  return WIZARD_STEPS.findIndex((s) => s.id === id);
}
