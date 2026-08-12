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

type SetupStatus = {
  deployed: boolean;
  marker: string;
};

type SetupCtx = {
  deployed: boolean;
  loading: boolean;
  refresh: () => Promise<void>;
};

const Ctx = createContext<SetupCtx | null>(null);

export function SetupProvider({ children }: { children: ReactNode }) {
  const [deployed, setDeployed] = useState(true); // fail closed until loaded
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const st = await api<SetupStatus>("/api/setup/status");
      setDeployed(Boolean(st.deployed));
    } catch {
      // If status cannot be read, require login (safer default).
      setDeployed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const value = useMemo(
    () => ({ deployed, loading, refresh }),
    [deployed, loading, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSetup(): SetupCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSetup outside SetupProvider");
  return ctx;
}
