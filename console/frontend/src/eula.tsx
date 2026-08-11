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

type EulaInfo = {
  accepted: boolean;
  title: string;
  body: string;
};

type EulaCtx = {
  loading: boolean;
  accepted: boolean;
  title: string;
  body: string;
  accept: () => Promise<void>;
  refresh: () => Promise<void>;
};

const Ctx = createContext<EulaCtx | null>(null);

export function EulaProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [accepted, setAccepted] = useState(false);
  const [title, setTitle] = useState("Terms of Service");
  const [body, setBody] = useState("");

  const refresh = useCallback(async () => {
    try {
      const info = await api<EulaInfo>("/api/eula");
      setAccepted(info.accepted);
      setTitle(info.title);
      setBody(info.body);
    } catch {
      setAccepted(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const accept = useCallback(async () => {
    await api<{ accepted: boolean }>("/api/eula/accept", {
      method: "POST",
      body: JSON.stringify({ accepted: true }),
    });
    setAccepted(true);
  }, []);

  const value = useMemo(
    () => ({ loading, accepted, title, body, accept, refresh }),
    [loading, accepted, title, body, accept, refresh],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useEula(): EulaCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useEula outside EulaProvider");
  return ctx;
}
