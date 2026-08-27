import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { useSetup } from "../setup";
import { deriveAlerts, mergeAlerts, type Alert, type AlertSnap } from "./alerts";

/** Cluster alerts, polled once for the whole app.
 *
 * Lives above the chrome so the header bell and the full Activity page show
 * the same list from the same poll, rather than two components fetching the
 * same endpoint and disagreeing about what is currently wrong.
 */
const AlertCtx = createContext<Alert[]>([]);

export function AlertProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const { deployed } = useSetup();
  const [alerts, setAlerts] = useState<Alert[]>([]);

  useEffect(() => {
    if (!user || !deployed) {
      setAlerts([]);
      return;
    }
    let stop = false;
    const load = () => {
      api<{ cluster?: AlertSnap }>("/api/cluster/status")
        .then((res) => {
          if (stop) return;
          setAlerts((prev) => mergeAlerts(prev, deriveAlerts(res.cluster)));
        })
        .catch(() => {
          // A failed poll must not blank real alerts, and must not spam.
        });
    };
    load();
    const id = window.setInterval(load, 30000);
    return () => {
      stop = true;
      window.clearInterval(id);
    };
  }, [user, deployed]);

  return <AlertCtx.Provider value={alerts}>{children}</AlertCtx.Provider>;
}

export function useAlerts(): Alert[] {
  return useContext(AlertCtx);
}
