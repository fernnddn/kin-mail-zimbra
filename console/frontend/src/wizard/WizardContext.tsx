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
import { api } from "../api";
import { emptyDraft, type WizardDraft } from "./types";

type WizardCtx = {
  draft: WizardDraft;
  loading: boolean;
  saving: boolean;
  error: string;
  setLocal: (patch: Partial<WizardDraft>) => void;
  save: (patch?: Partial<WizardDraft>) => Promise<WizardDraft>;
  reload: () => Promise<void>;
};

const Ctx = createContext<WizardCtx | null>(null);

export function WizardProvider({ children }: { children: ReactNode }) {
  const [draft, setDraft] = useState<WizardDraft>(emptyDraft());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const draftRef = useRef(draft);
  draftRef.current = draft;

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api<WizardDraft>("/api/wizard/draft");
      setDraft({ ...emptyDraft(), ...data });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load draft");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const setLocal = useCallback((patch: Partial<WizardDraft>) => {
    setDraft((prev) => ({ ...prev, ...patch }));
  }, []);

  const save = useCallback(async (patch?: Partial<WizardDraft>) => {
    setSaving(true);
    setError("");
    try {
      const body = { ...draftRef.current, ...patch };
      const data = await api<WizardDraft>("/api/wizard/draft", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      const merged = { ...emptyDraft(), ...data };
      setDraft(merged);
      return merged;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to save draft";
      setError(msg);
      throw err;
    } finally {
      setSaving(false);
    }
  }, []);

  const value = useMemo(
    () => ({ draft, loading, saving, error, setLocal, save, reload }),
    [draft, loading, saving, error, setLocal, save, reload],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWizard(): WizardCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useWizard outside WizardProvider");
  return ctx;
}
