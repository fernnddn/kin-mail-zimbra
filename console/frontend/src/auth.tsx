import type { ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "./api";

export type ConsoleRole = "kin_super_admin" | "kin_support_ops" | "customer_admin";

type User = {
  username: string;
  role: ConsoleRole;
  role_label: string;
  auth_type?: string;
  mfa_enabled?: boolean;
};

export type LoginResult =
  | { status: "ok" }
  | { status: "mfa_required"; mfa_token: string; username: string };

type AuthCtx = {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<LoginResult>;
  completeMfa: (mfaToken: string, code: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
};

const Ctx = createContext<AuthCtx | null>(null);

function userFromPayload(me: User & { status?: string }): User {
  return {
    username: me.username,
    role: me.role,
    role_label: me.role_label,
    auth_type: me.auth_type,
    mfa_enabled: me.mfa_enabled,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const me = await api<User>("/api/me");
      setUser(me);
    } catch {
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (username: string, password: string): Promise<LoginResult> => {
    const me = await api<
      User & { status: string; mfa_token?: string; username: string }
    >("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    if (me.status === "mfa_required") {
      if (!me.mfa_token) {
        throw new Error("MFA required but no challenge token was returned.");
      }
      return { status: "mfa_required", mfa_token: me.mfa_token, username: me.username };
    }
    setUser(userFromPayload(me));
    return { status: "ok" };
  }, []);

  const completeMfa = useCallback(async (mfaToken: string, code: string) => {
    const me = await api<User & { status: string }>("/api/login/mfa", {
      method: "POST",
      body: JSON.stringify({ mfa_token: mfaToken, code }),
    });
    setUser(userFromPayload(me));
  }, []);

  const logout = useCallback(async () => {
    await api("/api/logout", { method: "POST" });
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, loading, login, completeMfa, logout, refresh }),
    [user, loading, login, completeMfa, logout, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

export function isOpsRole(role: string | undefined): boolean {
  return role === "kin_super_admin" || role === "kin_support_ops";
}
