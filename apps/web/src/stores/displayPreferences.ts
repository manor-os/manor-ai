import { create } from "zustand";

export type InterfaceMode = "simple" | "professional";

const INTERFACE_MODE_STORAGE_KEY = "manor-interface-mode";
const INTERFACE_MODE_RELEASE_AT = Date.parse("2026-09-18T08:11:13Z");

interface DisplayPreferences {
  interfaceMode: InterfaceMode;
  hasSelectedInterfaceMode: boolean;
  initializeForUser: (createdAt: string | null | undefined) => void;
  setInterfaceMode: (mode: InterfaceMode) => void;
}

function storedDisplayPreferences(): Pick<DisplayPreferences, "interfaceMode" | "hasSelectedInterfaceMode"> {
  try {
    const savedMode = typeof window !== "undefined"
      ? window.localStorage.getItem(INTERFACE_MODE_STORAGE_KEY)
      : null;
    if (savedMode === "simple" || savedMode === "professional") {
      return { interfaceMode: savedMode, hasSelectedInterfaceMode: true };
    }
  } catch {
    // A default is not an explicit selection when storage cannot be read.
  }
  return { interfaceMode: "simple", hasSelectedInterfaceMode: false };
}

// Like the theme preference, interface density belongs to this browser.
export const useDisplayPreferences = create<DisplayPreferences>((set) => {
  const initial = storedDisplayPreferences();
  let hasExplicitChoice = initial.hasSelectedInterfaceMode;

  return {
    ...initial,
    initializeForUser: (createdAt) => {
      if (hasExplicitChoice) return;
      const createdAtMs = typeof createdAt === "string" ? Date.parse(createdAt) : NaN;
      // Older API responses may omit the date. Preserve their existing UI too.
      const existingUser = !Number.isFinite(createdAtMs) || createdAtMs < INTERFACE_MODE_RELEASE_AT;
      // This is an account default, not a browser choice: recompute it when a
      // different account signs in, without persisting it for that next account.
      set({
        interfaceMode: existingUser ? "professional" : "simple",
        hasSelectedInterfaceMode: existingUser,
      });
    },
    setInterfaceMode: (interfaceMode) => {
      hasExplicitChoice = true;
      set({ interfaceMode, hasSelectedInterfaceMode: true });
      try {
        window.localStorage.setItem(INTERFACE_MODE_STORAGE_KEY, interfaceMode);
      } catch {
        // Keep the explicit choice ahead of account defaults for this session.
      }
    },
  };
});
