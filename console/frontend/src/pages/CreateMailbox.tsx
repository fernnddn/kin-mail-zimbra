import { FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import styled from "@emotion/styled";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import {
  Button,
  ConfirmModal,
  DataTable,
  Hint,
  Input,
  Label,
  MailIcon,
  Page,
  PageHeader,
  PasswordInput,
  Select,
  Skeleton,
  StatusPill,
  TableWrap,
  WarnBox,
} from "../ui";

type Seats = {
  ok?: boolean;
  used?: number | null;
  limit?: number | null;
  remaining?: number | null;
  domain?: string;
  code?: string;
};

type MailboxRow = {
  email: string;
  display_name?: string;
  given_name?: string;
  surname?: string;
  status?: string;
  used_bytes?: number | null;
  quota_bytes?: number | null;
};

type ListResp = {
  ok: boolean;
  message: string;
  can_create: boolean;
  seats: Seats;
  mailboxes: MailboxRow[];
  license?: { status?: string; provisioning_blocked?: boolean };
};

type CreateResp = {
  ok: boolean;
  message?: string;
  email?: string;
  local_part?: string;
  error?: string | null;
};

const FormGrid = styled.form`
  display: grid;
  gap: 0;
  margin: 0;
`;

// Hint carries a negative top margin so it tucks under the field above it.
// After a submit button there is no field to tuck under, so the result text
// slid up behind the button and the first line was unreadable.
const ResultNote = styled.p`
  margin: 0.9rem 0 0;
  color: ${theme.muted};
  font-size: 0.82rem;
  line-height: 1.45;
`;

const DomainSuffix = styled.span`
  color: ${theme.muted};
  font-size: 0.95rem;
  align-self: center;
  margin-bottom: 0.9rem;
  margin-left: 0.35rem;
`;

const LocalRow = styled.div`
  display: flex;
  align-items: baseline;
  gap: 0.15rem;
  margin-bottom: 0;

  input {
    flex: 1;
    margin-bottom: 0.9rem;
  }
`;

const Split = styled.div`
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 0.85rem;
  @media (max-width: 720px) {
    grid-template-columns: 1fr;
  }
`;

const MailPage = styled(Page)`
  max-width: 1120px;
`;

const Columns = styled.div`
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(17rem, 0.85fr);
  gap: 1.25rem;
  align-items: start;
  @media (max-width: 720px) {
    grid-template-columns: 1fr;
  }
`;

const FormCard = styled.div`
  border: 1px solid ${theme.line};
  background: ${theme.bgElev};
  border-radius: ${theme.radius.md};
  padding: 1rem 1.1rem 1.15rem;
  box-shadow: ${theme.shadow.sm};
`;

const FormTitle = styled.h2`
  margin: 0 0 0.65rem;
  font-size: 0.85rem;
  font-weight: 650;
`;

const MB = 1024 * 1024;
const GB = 1024 * MB;

function formatSize(n: number): string {
  if (n >= GB) return `${(n / GB).toFixed(n >= 10 * GB ? 0 : 1)} GB`;
  const mb = n / MB;
  return mb >= 10 ? `${Math.round(mb)} MB` : `${mb.toFixed(1)} MB`;
}

// Typed by hand, so it is checked rather than trusted: an empty box, a minus
// sign or a stray letter must not be sent as a cap. Blank is not zero - zero
// is a deliberate "no cap" the operator has to type.
function quotaValid(amount: string): boolean {
  const n = Number(amount);
  return amount.trim() !== "" && Number.isFinite(n) && n >= 0;
}

function quotaBytes(amount: string, unit: string): number {
  return Math.round(Number(amount) * (unit === "GB" ? GB : MB));
}

// The cap and the bytes in use come from two different places - the cap from
// the directory, the usage from the mail store - so either can be present
// without the other. Showing "n/a" whenever usage was missing hid a cap the
// operator had just set.
function formatUsage(row: MailboxRow): string {
  const capped = !!row.quota_bytes;
  if (row.used_bytes == null) return capped ? `cap ${formatSize(row.quota_bytes!)}` : "no cap";
  const used = formatSize(row.used_bytes);
  return capped ? `${used} / ${formatSize(row.quota_bytes!)}` : `${used} (no cap)`;
}

export default function CreateMailboxPage() {
  const { user } = useAuth();
  const isSuper = user?.role === "kin_super_admin";
  const [domain, setDomain] = useState("");
  const [seats, setSeats] = useState<Seats>({});
  const [statusMessage, setStatusMessage] = useState("");
  const [canCreate, setCanCreate] = useState(false);
  const [rows, setRows] = useState<MailboxRow[]>([]);
  const [localPart, setLocalPart] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [givenName, setGivenName] = useState("");
  const [surname, setSurname] = useState("");
  const [accountStatus, setAccountStatus] = useState("active");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [maintHint, setMaintHint] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<MailboxRow | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameLocal, setRenameLocal] = useState("");
  const [quotaFor, setQuotaFor] = useState<string | null>(null);
  const [quotaAmount, setQuotaAmount] = useState("");
  const [quotaUnit, setQuotaUnit] = useState("GB");

  async function refresh() {
    const st = await api<ListResp>("/api/mailbox");
    setSeats(st.seats || {});
    setStatusMessage(st.message || "");
    setCanCreate(!!st.can_create);
    setRows(st.mailboxes || []);
    if (st.seats?.domain) setDomain(st.seats.domain);
  }

  useEffect(() => {
    void refresh()
      .catch((err) => setMessage(err instanceof Error ? err.message : "Failed to load mailboxes"))
      .finally(() => setLoaded(true));
    void api<{ cluster?: { standby?: string[]; maintenance_active?: boolean } }>("/api/cluster/status")
      .then((st) => {
        const nodes = st.cluster?.standby || [];
        setMaintHint(
          st.cluster?.maintenance_active
            ? `A mail node is in maintenance (${nodes.join(", ") || "unknown"}). New mailboxes are blocked until it leaves maintenance.`
            : "",
        );
      })
      .catch(() => setMaintHint(""));
  }, []);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      const res = await api<CreateResp>("/api/mailbox", {
        method: "POST",
        body: JSON.stringify({
          local_part: localPart.trim().toLowerCase(),
          password,
          display_name: displayName.trim() || `${givenName.trim()} ${surname.trim()}`.trim(),
          given_name: givenName.trim(),
          surname: surname.trim(),
          account_status: accountStatus,
        }),
      });
      if (res.ok) {
        const email = res.email || (domain ? `${res.local_part}@${domain}` : res.local_part);
        setMessage(
          `Created ${email}. Share the password you entered with the user. The console does not store it.`,
        );
        setLocalPart("");
        setPassword("");
        setDisplayName("");
        setGivenName("");
        setSurname("");
        setAccountStatus("active");
      } else {
        setMessage(res.message || "Mailbox was not created.");
      }
      await refresh().catch(() => undefined);
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setMessage("");
    try {
      await api("/api/mailbox/delete", {
        method: "POST",
        body: JSON.stringify({ email: pendingDelete.email }),
      });
      setPendingDelete(null);
      await refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeleting(false);
    }
  }

  function openQuota(row: MailboxRow) {
    setQuotaFor(row.email);
    setRenaming(null);
    // Seed with what is already set, in the unit that reads naturally, so
    // adjusting an existing cap does not start from a blank field.
    if (!row.quota_bytes) {
      setQuotaAmount("");
      setQuotaUnit("GB");
    } else if (row.quota_bytes % GB === 0) {
      setQuotaAmount(String(row.quota_bytes / GB));
      setQuotaUnit("GB");
    } else {
      setQuotaAmount(String(Math.round(row.quota_bytes / MB)));
      setQuotaUnit("MB");
    }
  }

  async function applyQuota(email: string, bytes: number) {
    setBusy(true);
    setMessage("");
    try {
      const res = await api<{ over_cap?: boolean; used_bytes?: number | null }>(
        "/api/mailbox/quota",
        { method: "POST", body: JSON.stringify({ email, quota_bytes: bytes }) },
      );
      setQuotaFor(null);
      setMessage(
        bytes === 0
          ? `${email} now has no storage cap.`
          : res.over_cap
            ? `${email} is capped at ${formatSize(bytes)}, but already holds ${formatSize(res.used_bytes || 0)}. New mail for this account will be refused until it is back under the cap.`
            : `${email} is capped at ${formatSize(bytes)}.`,
      );
      await refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not set the storage cap");
    } finally {
      setBusy(false);
    }
  }

  async function applyRename(email: string) {
    setBusy(true);
    setMessage("");
    try {
      await api("/api/mailbox/rename", {
        method: "POST",
        body: JSON.stringify({ email, new_local_part: renameLocal.trim().toLowerCase() }),
      });
      setRenaming(null);
      setRenameLocal("");
      await refresh();
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConsoleChrome>
      <MailPage>
        <PageHeader
          icon={<MailIcon />}
          title="Mailboxes"
          subtitle="Create, list, rename, delete, and set the storage cap for accounts on the mail domain. Licensing counts seats, not bytes: a cap limits one mailbox's size and does not change how many you may have."
        />

        {maintHint ? <WarnBox>{maintHint}</WarnBox> : null}

        <Hint>
          <strong>{statusMessage || "Seat status unavailable"}</strong>{" "}
          {seats.code === "unset" && isSuper ? (
            <Link to="/settings">Paste a signed license in Settings</Link>
          ) : null}
        </Hint>

        <Columns>
          <TableWrap style={{ marginBottom: 0 }}>
            <DataTable>
              <thead>
                <tr>
                  <th>Email</th>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Usage</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {!loaded ? (
                  <tr>
                    <td colSpan={5}>
                      <Skeleton $h="0.85rem" $w="40%" />
                    </td>
                  </tr>
                ) : rows.length === 0 ? (
                  <tr>
                    <td colSpan={5}>No mailboxes yet.</td>
                  </tr>
                ) : (
                  rows.map((row) => (
                    <tr key={row.email}>
                      <td>
                        {renaming === row.email ? (
                          <LocalRow>
                            <Input
                              value={renameLocal}
                              onChange={(e) => setRenameLocal(e.target.value.replace(/@.*$/, ""))}
                              aria-label="New local part"
                            />
                            <DomainSuffix>@{domain || "..."}</DomainSuffix>
                            <Button
                              type="button"
                              variant="primary"
                              style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                              disabled={busy || !renameLocal.trim()}
                              onClick={() => void applyRename(row.email)}
                            >
                              Save
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                              onClick={() => setRenaming(null)}
                            >
                              Cancel
                            </Button>
                          </LocalRow>
                        ) : (
                          row.email
                        )}
                      </td>
                      <td>{row.display_name || [row.given_name, row.surname].filter(Boolean).join(" ")}</td>
                      <td>
                        <StatusPill tone={row.status === "active" ? "primary" : "neutral"} size="tag">
                          {row.status || "unknown"}
                        </StatusPill>
                      </td>
                      <td>
                        {quotaFor === row.email ? (
                          <LocalRow>
                            <Input
                              type="number"
                              min={0}
                              step="any"
                              value={quotaAmount}
                              onChange={(e) => setQuotaAmount(e.target.value)}
                              aria-label="Storage cap"
                              placeholder="0 = no cap"
                              style={{ width: "6rem" }}
                            />
                            <Select
                              value={quotaUnit}
                              onChange={(e) => setQuotaUnit(e.target.value)}
                              aria-label="Unit"
                              style={{ width: "4.5rem" }}
                            >
                              <option value="MB">MB</option>
                              <option value="GB">GB</option>
                            </Select>
                            <Button
                              type="button"
                              variant="primary"
                              style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                              disabled={busy || !quotaValid(quotaAmount)}
                              onClick={() =>
                                void applyQuota(row.email, quotaBytes(quotaAmount, quotaUnit))
                              }
                            >
                              Save
                            </Button>
                            <Button
                              type="button"
                              variant="ghost"
                              style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                              onClick={() => setQuotaFor(null)}
                            >
                              Cancel
                            </Button>
                          </LocalRow>
                        ) : (
                          formatUsage(row)
                        )}
                      </td>
                      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                        <Button
                          type="button"
                          variant="ghost"
                          style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                          onClick={() => openQuota(row)}
                        >
                          Storage
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                          onClick={() => {
                            setRenaming(row.email);
                            setQuotaFor(null);
                            setRenameLocal(row.email.split("@")[0] || "");
                          }}
                        >
                          Rename
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                          onClick={() => setPendingDelete(row)}
                        >
                          Delete
                        </Button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </DataTable>
          </TableWrap>

          <FormCard>
            <FormTitle>Add account</FormTitle>
            <FormGrid onSubmit={(e) => void onSubmit(e)}>
              <Label htmlFor="lp">Email (local part)</Label>
              <LocalRow>
                <Input
                  id="lp"
                  value={localPart}
                  onChange={(e) => setLocalPart(e.target.value.replace(/@.*$/, ""))}
                  autoComplete="off"
                  required
                  placeholder="jane.doe"
                />
                <DomainSuffix>@{domain || "..."}</DomainSuffix>
              </LocalRow>
              <Split>
                <div>
                  <Label htmlFor="gn">First name</Label>
                  <Input id="gn" value={givenName} onChange={(e) => setGivenName(e.target.value)} autoComplete="off" />
                </div>
                <div>
                  <Label htmlFor="sn">Last name</Label>
                  <Input id="sn" value={surname} onChange={(e) => setSurname(e.target.value)} autoComplete="off" />
                </div>
              </Split>
              <Label htmlFor="dn">Display name (optional)</Label>
              <Input
                id="dn"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="off"
                placeholder="Defaults to first + last if left blank"
              />
              <Label htmlFor="pw">Password (min 8 characters)</Label>
              <PasswordInput
                id="pw"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={8}
                required
                autoComplete="new-password"
              />
              <Label htmlFor="st">Account status</Label>
              <Select id="st" value={accountStatus} onChange={(e) => setAccountStatus(e.target.value)}>
                <option value="active">Active</option>
                <option value="locked">Locked</option>
              </Select>
              <Button type="submit" variant="primary" loading={busy} disabled={!!maintHint || !canCreate}>
                {busy ? "Creating..." : "Create mailbox"}
              </Button>
            </FormGrid>
            {message ? <ResultNote>{message}</ResultNote> : null}
          </FormCard>
        </Columns>

        <ConfirmModal
          open={!!pendingDelete}
          title="Delete mailbox"
          message={
            <>
              Delete mailbox <strong>{pendingDelete?.email}</strong>? Mail in this account will be
              removed.
            </>
          }
          detail="This cannot be undone from the console."
          confirmLabel="Delete mailbox"
          loading={deleting}
          onCancel={() => {
            if (!deleting) setPendingDelete(null);
          }}
          onConfirm={() => void confirmDelete()}
        />
      </MailPage>
    </ConsoleChrome>
  );
}
