import { FormEvent, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import styled from "@emotion/styled";
import { api } from "../api";
import { useAuth } from "../auth";
import { ConsoleChrome } from "../ConsoleChrome";
import { theme } from "../styles/theme";
import { Button, Hint, Input, Label, Lede, Title } from "../ui";

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

const Page = styled.div`
  padding: 1.25rem 1.5rem 2rem;
  max-width: 720px;
  width: 100%;
`;

const Table = styled.table`
  width: 100%;
  border-collapse: collapse;
  margin: 1rem 0 1.5rem;
  font-size: 0.9rem;

  th,
  td {
    text-align: left;
    padding: 0.55rem 0.4rem;
    border-bottom: 1px solid ${theme.line};
  }

  th {
    color: ${theme.muted};
    font-weight: 600;
    font-size: 0.75rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
`;

const Select = styled.select`
  width: 100%;
  border: 1px solid ${theme.line};
  background: ${theme.bgPanel};
  color: ${theme.ink};
  border-radius: ${theme.radius};
  padding: 0.7rem 0.8rem;
  font: inherit;
  margin-bottom: 0.9rem;

  &:focus {
    outline: 2px solid color-mix(in srgb, ${theme.accent} 55%, transparent);
    border-color: ${theme.accent};
  }
`;

const FormGrid = styled.form`
  display: grid;
  gap: 0;
  margin-top: 0.5rem;
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
    void refresh().catch((err) =>
      setError(err instanceof Error ? err.message : "Failed to load users"),
    );
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

  async function onDelete(name: string) {
    if (!window.confirm(`Delete user ${name}?`)) return;
    setError("");
    try {
      await api(`/api/users/${encodeURIComponent(name)}`, { method: "DELETE" });
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    }
  }

  const roleLabel = (id: string) => roles.find((r) => r.id === id)?.label || id;

  return (
    <ConsoleChrome subtitle="Console users · KIN Super Admin">
      <Page>
        <Title>Users</Title>
        <Lede>
          Per-account auth: <strong>local</strong> (bcrypt in{" "}
          <code>users.json</code>) or <strong>AD</strong> (LDAP bind using the same{" "}
          <code>/etc/kin-mail/config</code> AD settings as hybrid mail auth). Role is assigned
          here; no AD group mapping in this slice.
        </Lede>
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
        <Table>
          <thead>
            <tr>
              <th>Username</th>
              <th>Auth</th>
              <th>Role</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.username}>
                <td>
                  {row.username}
                  {row.auth_type === "ad" && row.ad_username && row.ad_username !== row.username
                    ? ` (${row.ad_username})`
                    : ""}
                </td>
                <td>{row.auth_type === "ad" ? "AD" : "local"}</td>
                <td>{roleLabel(row.role)}</td>
                <td style={{ textAlign: "right" }}>
                  <Button
                    type="button"
                    variant="ghost"
                    style={{ padding: "0.3rem 0.55rem", fontSize: "0.8rem" }}
                    disabled={row.username === user?.username}
                    onClick={() => void onDelete(row.username)}
                  >
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
        <Title style={{ fontSize: "1.15rem" }}>Add user</Title>
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
              <Input
                id="np"
                type="password"
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
          <Button type="submit" variant="primary" disabled={busy}>
            {busy ? "Creating…" : "Create user"}
          </Button>
        </FormGrid>
      </Page>
    </ConsoleChrome>
  );
}
