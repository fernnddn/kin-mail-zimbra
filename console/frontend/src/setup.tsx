import type { ReactNode } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
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
  /** /etc/kin-mail/ha-setup-complete exists (2vm HA apply reached ORCH_DONE). */
  haSetupComplete: boolean;
  loading: boolean;
  /** Last /api/setup/status failure. Empty after a successful fetch. */
  statusError: string;
  refresh: () => Promise<void>;
};

const Ctx = createContext<SetupCtx | null>(null);

export function SetupProvider({ children }: { children: ReactNode }) {
  // Stay on "loading" until the first successful or failed fetch. Do not
  // default deployed=true: a 500 on the 5s poll used to eject the wizard to
  // /login even with a valid session.
  const [deployed, setDeployed] = useState(false);
  const [installInProgress, setInstallInProgress] = useState(false);
  const [fullInstallComplete, setFullInstallComplete] = useState(false);
  const [haSetupComplete, setHaSetupComplete] = useState(false);
  const [loading, setLoading] = useState(true);
  const [statusError, setStatusError] = useState("");
  const loadedOnce = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const st = await api<SetupStatus>("/api/setup/status");
      setDeployed(Boolean(st.deployed));
      setInstallInProgress(Boolean(st.install_in_progress || st.busy));
      setFullInstallComplete(Boolean(st.full_install_complete));
      setHaSetupComplete(Boolean(st.ha_setup_complete));
      setStatusError("");
      loadedOnce.current = true;
    } catch (err) {
      const message = err instanceof Error ? err.message : "Cannot reach setup status";
      if (!loadedOnce.current) {
        // First load failed: do not treat as deployed (that sends a fresh
        // wizard to /login). Show the error and keep the last known defaults.
        setDeployed(false);
        setInstallInProgress(false);
        setFullInstallComplete(false);
        setHaSetupComplete(false);
        setStatusError(message);
      }
      // After a successful load, keep last known status. A transient 500
      // must not flip deployed to true.
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
    () => ({
      deployed,
      installInProgress,
      fullInstallComplete,
      haSetupComplete,
      loading,
      statusError,
      refresh,
    }),
    [
      deployed,
      installInProgress,
      fullInstallComplete,
      haSetupComplete,
      loading,
      statusError,
      refresh,
    ],
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
  if (installInProgress) return "/wizard/deploy";
  if (deployed) return "/cluster";
  return "/wizard/topology";
}
