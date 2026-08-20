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
  busy?: boolean;
  install_in_progress?: boolean;
  full_install_complete?: boolean;
  ha_setup_complete?: boolean;
  marker: string;
  zimbra_tree_present?: boolean;
};

type SetupCtx = {
  deployed: boolean;
  /** Full-install (or stage 03) appears to be running on this host. */
  installInProgress: boolean;
  /** /etc/kin-mail/setup-complete exists. Independent of the live log buffer. */
  fullInstallComplete: boolean;
  loading: boolean;
  refresh: () => Promise<void>;
};

const Ctx = createContext<SetupCtx | null>(null);

export function SetupProvider({ children }: { children: ReactNode }) {
  // Fail closed until loaded: require login if we cannot tell (safer for post-deploy).
  const [deployed, setDeployed] = useState(true);
  const [installInProgress, setInstallInProgress] = useState(false);
  const [fullInstallComplete, setFullInstallComplete] = useState(false);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const st = await api<SetupStatus>("/api/setup/status");
      setDeployed(Boolean(st.deployed));
      setInstallInProgress(Boolean(st.install_in_progress || st.busy));
      setFullInstallComplete(Boolean(st.full_install_complete));
    } catch {
      setDeployed(true);
      setInstallInProgress(false);
      setFullInstallComplete(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = window.setInterval(() => {
      void refresh();
    }, 5000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const value = useMemo(
    () => ({ deployed, installInProgress, fullInstallComplete, loading, refresh }),
    [deployed, installInProgress, fullInstallComplete, loading, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSetup(): SetupCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSetup outside SetupProvider");
  return ctx;
}

/** Where to send the operator after login / EULA / bare /wizard. */
export function wizardHomePath(
  installInProgress: boolean,
  deployed = false,
): string {
  // Fresh hosts start at Topology. Mid-install and already-deployed hosts go
  // straight to Deploy/progress; do not reset the operator to step 1.
  return installInProgress || deployed ? "/wizard/deploy" : "/wizard/topology";
}
