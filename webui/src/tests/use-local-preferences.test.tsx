import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  DEFAULT_LOCAL_PREFS,
  LOCAL_PREFS_STORAGE_KEY,
  LocalPreferencesProvider,
  readLocalPreferences,
  useLocalPreferences,
} from "@/hooks/useLocalPreferences";

function wrapper({ children }: { children: React.ReactNode }) {
  return <LocalPreferencesProvider>{children}</LocalPreferencesProvider>;
}

describe("useLocalPreferences", () => {
  afterEach(() => {
    window.localStorage.removeItem(LOCAL_PREFS_STORAGE_KEY);
  });

  it("reads stored preferences on mount", () => {
    window.localStorage.setItem(
      LOCAL_PREFS_STORAGE_KEY,
      JSON.stringify({
        density: "compact",
        activityMode: "expanded",
        codeWrap: false,
        brandLogos: false,
      }),
    );

    const { result } = renderHook(() => useLocalPreferences(), { wrapper });

    expect(result.current.prefs).toEqual({
      density: "compact",
      activityMode: "expanded",
      codeWrap: false,
      brandLogos: false,
    });
  });

  it("persists preference updates to localStorage", () => {
    const { result } = renderHook(() => useLocalPreferences(), { wrapper });

    act(() => {
      result.current.setPrefs((prev) => ({ ...prev, codeWrap: false, activityMode: "expanded" }));
    });

    expect(readLocalPreferences()).toEqual({
      ...DEFAULT_LOCAL_PREFS,
      codeWrap: false,
      activityMode: "expanded",
    });
  });
});