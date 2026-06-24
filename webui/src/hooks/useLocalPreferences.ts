import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";

export type LocalDensity = "comfortable" | "compact";
export type LocalActivityMode = "auto" | "expanded";

export interface LocalPreferences {
  density: LocalDensity;
  activityMode: LocalActivityMode;
  codeWrap: boolean;
  brandLogos: boolean;
}

export const LOCAL_PREFS_STORAGE_KEY = "nanobot-webui.settings-preferences";

export const DEFAULT_LOCAL_PREFS: LocalPreferences = {
  density: "comfortable",
  activityMode: "auto",
  codeWrap: true,
  brandLogos: true,
};

export function readLocalPreferences(): LocalPreferences {
  if (typeof window === "undefined") return DEFAULT_LOCAL_PREFS;
  try {
    const raw = window.localStorage.getItem(LOCAL_PREFS_STORAGE_KEY);
    if (!raw) return DEFAULT_LOCAL_PREFS;
    const parsed = JSON.parse(raw) as Partial<LocalPreferences>;
    return {
      density: parsed.density === "compact" ? "compact" : "comfortable",
      activityMode: parsed.activityMode === "expanded" ? "expanded" : "auto",
      codeWrap: parsed.codeWrap !== false,
      brandLogos: parsed.brandLogos !== false,
    };
  } catch {
    return DEFAULT_LOCAL_PREFS;
  }
}

function writeLocalPreferences(prefs: LocalPreferences): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LOCAL_PREFS_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Browser-only preferences should never block the UI.
  }
}

interface LocalPreferencesContextValue {
  prefs: LocalPreferences;
  setPrefs: Dispatch<SetStateAction<LocalPreferences>>;
}

const LocalPreferencesContext = createContext<LocalPreferencesContextValue | null>(null);

export function LocalPreferencesProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefs] = useState<LocalPreferences>(() => readLocalPreferences());

  useEffect(() => {
    writeLocalPreferences(prefs);
  }, [prefs]);

  const value = useMemo(() => ({ prefs, setPrefs }), [prefs]);

  return createElement(LocalPreferencesContext.Provider, { value }, children);
}

export function useLocalPreferences(): LocalPreferencesContextValue {
  const value = useContext(LocalPreferencesContext);
  if (!value) {
    throw new Error("useLocalPreferences must be used within LocalPreferencesProvider");
  }
  return value;
}

export function useLocalPreferenceValue<K extends keyof LocalPreferences>(
  key: K,
): LocalPreferences[K] {
  const value = useContext(LocalPreferencesContext);
  return value?.prefs[key] ?? DEFAULT_LOCAL_PREFS[key];
}

export function useSetLocalPreference<K extends keyof LocalPreferences>(
  key: K,
): (next: LocalPreferences[K]) => void {
  const { setPrefs } = useLocalPreferences();
  return useCallback(
    (next: LocalPreferences[K]) => {
      setPrefs((prev) => (prev[key] === next ? prev : { ...prev, [key]: next }));
    },
    [key, setPrefs],
  );
}