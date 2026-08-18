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
import { emptyDraft, stepIndex, type WizardDraft } from "./types";

/** Last current_step confirmed by GET/PUT. Never follows setLocal() edits. */
function furthestSavedStep(prev: string, incoming: string | undefined): string {
  if (!incoming) return prev;
  const a = stepIndex(prev);
  const b = stepIndex(incoming);
  if (b < 0) return a < 0 ? "topology" : prev;
  if (a < 0) return incoming;
  // Revisit + Continue from an earlier step still sends that step's "next"
  // id; do not rewind sidebar reach.
  return b > a ? incoming : prev;
}

type WizardCtx = {
  draft: WizardDraft;
  /** Server-confirmed furthest step; ignore in-progress form edits. */
  savedCurrentStep: string;
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
  const [savedCurrentStep, setSavedCurrentStep] = useState("topology");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const savedStepRef = useRef(savedCurrentStep);
  savedStepRef.current = savedCurrentStep;

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api<WizardDraft>("/api/wizard/draft");
      const merged = { ...emptyDraft(), ...data };
      setDraft(merged);
      setSavedCurrentStep(furthestSavedStep(savedStepRef.current, merged.current_step));
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
      body.current_step = furthestSavedStep(savedStepRef.current, body.current_step);
      const data = await api<WizardDraft>("/api/wizard/draft", {
        method: "PUT",
        body: JSON.stringify(body),
      });
      const merged = { ...emptyDraft(), ...data };
      setDraft(merged);
      setSavedCurrentStep(furthestSavedStep(savedStepRef.current, merged.current_step));
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
    () => ({
      draft,
      savedCurrentStep,
      loading,
      saving,
      error,
      setLocal,
      save,
      reload,
    }),
    [draft, savedCurrentStep, loading, saving, error, setLocal, save, reload],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWizard(): WizardCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useWizard outside WizardProvider");
  return ctx;
}
