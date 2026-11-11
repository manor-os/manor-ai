import { create } from "zustand";

export type InterfaceMode = "simple" | "professional";

const INTERFACE_MODE_STORAGE_KEY = "manor-interface-mode";

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
    // Storage may be unavailable; the default below still applies.
  }
  // Nobody is asked to pick a mode on their first visit: everyone starts with
  // the full interface, and Settings › Appearance can switch to simple.
  return { interfaceMode: "professional", hasSelectedInterfaceMode: true };
}

// Like the theme preference, interface density belongs to this browser.
export const useDisplayPreferences = create<DisplayPreferences>((set) => {
  const initial = storedDisplayPreferences();
  let hasExplicitChoice = initial.hasSelectedInterfaceMode;

  return {
    ...initial,
    initializeForUser: () => {
      if (hasExplicitChoice) return;
      // An account default, not a browser choice: reset it when a different
      // account signs in, without persisting it for that next account.
      set({ interfaceMode: "professional", hasSelectedInterfaceMode: true });
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
