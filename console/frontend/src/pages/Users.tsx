import { FormEvent, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import styled from "@emotion/styled";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import {
  Button,
  ConfirmModal,
  DataTable,
  Hint,
  Input,
  Label,
  Page,
  PageHeader,
  PasswordInput,
  Select,
  Skeleton,
  StatusPill,
  TableWrap,
  UsersIcon,
} from "../ui";

type PublicUser = {
  username: string;
  role: string;
  auth_type: string;
  ad_username?: string;
  disabled: boolean;
};

type RoleOpt = { id: string; label: string };

type AdStatus = {
  enabled: boolean;
  ldap_url: string;
  search_base: string;
  search_filter: string;
  bind_dn_template: string;
  search_bind_password_set: boolean;
};

const FormGrid = styled.form`
  display: grid;
  gap: 0;
  margin-top: 0.5rem;
`;

const SectionTitle = styled.h2`
  margin: 0 0 0.5rem;
  font-size: 1.05rem;
  font-weight: 700;
  letter-spacing: -0.02em;
`;

export default function UsersPage() {
  const { user } = useAuth();
  const [rows, setRows] = useState<PublicUser[]>([]);
  const [roles, setRoles] = useState<RoleOpt[]>([]);
  const [ad, setAd] = useState<AdStatus | null>(null);
  const [error, setError] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [adUsername, setAdUsername] = useState("");
  const [authType, setAuthType] = useState<"local" | "ad">("local");
  const [role, setRole] = useState("customer_admin");
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const allowed = user?.role === "kin_super_admin";

  async function refresh() {
    const [u, r, a] = await Promise.all([
      api<{ users: PublicUser[] }>("/api/users"),
      api<{ roles: RoleOpt[] }>("/api/roles"),
      api<{ ad: AdStatus }>("/api/ad/status"),
    ]);
    setRows(u.users);
    setRoles(r.roles);
    setAd(a.ad);
  }

  useEffect(() => {
    if (!allowed) return;
    void refresh()
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load users"))
      .finally(() => setLoaded(true));
  }, [allowed]);

  if (!allowed) {
    return <Navigate to="/wizard" replace />;
  }

  async function onCreate(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/api/users", {
        method: "POST",
        body: JSON.stringify({
          username,
          role,
          auth_type: authType,
          password: authType === "local" ? password : "",
          ad_username: authType === "ad" ? adUsername || username : "",
        }),
      });
      setUsername("");
      setPassword("");
      setAdUsername("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Create failed");
    } finally {
      setBusy(false);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setError("");
    try {
      await api(`/api/users/${encodeURIComponent(pendingDelete)}`, { method: "DELETE" });
      setPendingDelete(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setDeleting(false);
    }
  }

  const roleLabel = (id: string) => roles.find((r) => r.id === id)?.label || id;

  return (
    <ConsoleChrome subtitle="Console users · KIN Super Admin">
      <Page>
        <PageHeader
          icon={<UsersIcon />}
          title="Users"
          subtitle={
            <>
              Per-account auth: <strong>local</strong> (bcrypt in <code>users.json</code>) or{" "}
              <strong>AD</strong> (LDAP bind using the same <code>/etc/kin-mail/config</code> AD
              settings as hybrid mail auth). Role is assigned here; no AD group mapping in this
              slice.
            </>
          }
        />
        {ad && (
          <Hint>
            Appliance AD: {ad.enabled ? "enabled" : "disabled"}
            {ad.ldap_url ? ` · ${ad.ldap_url}` : ""}
            {ad.search_base ? ` · base ${ad.search_base}` : ""}
            {!ad.enabled &&
              " (AD-backed console logins will fail closed until Hybrid AD is configured)."}
          </Hint>
        )}
        {error && <Hint>{error}</Hint>}
        <TableWrap>
          <DataTable>
            <thead>
              <tr>
                <th>Username</th>
                <th>Auth</th>
                <th>Role</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {!loaded ? (
                <tr>
                  <td colSpan={4}>
                    <Skeleton $h="0.85rem" $w="40%" />
                  </td>
                </tr>
              ) : (
                rows.map((row) => (
                  <tr key={row.username}>
                    <td>
                      {row.username}
                      {row.auth_type === "ad" && row.ad_username && row.ad_username !== row.username
                        ? ` (${row.ad_username})`
                        : ""}
                    </td>
                    <td>
                      <StatusPill tone={row.auth_type === "ad" ? "primary" : "neutral"} size="tag">
                        {row.auth_type === "ad" ? "AD" : "local"}
                      </StatusPill>
                    </td>
                    <td>{roleLabel(row.role)}</td>
                    <td style={{ textAlign: "right" }}>
                      <Button
                        type="button"
                        variant="ghost"
                        style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                        disabled={row.username === user?.username}
                        onClick={() => setPendingDelete(row.username)}
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
        <SectionTitle>Add user</SectionTitle>
        <FormGrid onSubmit={(e) => void onCreate(e)}>
          <Label htmlFor="at">Auth type</Label>
          <Select
            id="at"
            value={authType}
            onChange={(e) => setAuthType(e.target.value === "ad" ? "ad" : "local")}
          >
            <option value="local">Local password</option>
            <option value="ad">Active Directory</option>
          </Select>
          <Label htmlFor="nu">Username</Label>
          <Input
            id="nu"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="off"
            required
          />
          {authType === "local" ? (
            <>
              <Label htmlFor="np">Password</Label>
              <PasswordInput
                id="np"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                minLength={8}
                required
              />
            </>
          ) : (
            <>
              <Label htmlFor="adu">AD username / UPN (for bind)</Label>
              <Input
                id="adu"
                value={adUsername}
                onChange={(e) => setAdUsername(e.target.value)}
                placeholder="Defaults to username above"
                autoComplete="off"
              />
            </>
          )}
          <Label htmlFor="nr">Role</Label>
          <Select id="nr" value={role} onChange={(e) => setRole(e.target.value)}>
            {roles.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </Select>
          <Button type="submit" variant="primary" loading={busy}>
            {busy ? "Creating…" : "Create user"}
          </Button>
        </FormGrid>
        <ConfirmModal
          open={!!pendingDelete}
          title="Delete user"
          message={
            <>
              Delete console user <strong>{pendingDelete}</strong>?
            </>
          }
          detail="This removes the account from the console. It cannot be undone."
          confirmLabel="Delete"
          loading={deleting}
          onCancel={() => {
            if (!deleting) setPendingDelete(null);
          }}
          onConfirm={() => void confirmDelete()}
        />
      </Page>
    </ConsoleChrome>
  );
}
