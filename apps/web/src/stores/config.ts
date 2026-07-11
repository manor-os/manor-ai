import { create } from "zustand";

interface AppConfig {
  deployment_mode: "oss" | "cloud";
  environment: string;
  email_enabled: boolean;
  fs_enabled: boolean;
  support_tickets_enabled: boolean;
  loaded: boolean;
  load: () => Promise<void>;
}

export const useConfigStore = create<AppConfig>((set, get) => ({
  deployment_mode: "oss",
  environment: import.meta.env.DEV ? "local" : "prod",
  email_enabled: false,
  fs_enabled: false,
  support_tickets_enabled: false,
  loaded: false,

  load: async () => {
    if (get().loaded) return;
    try {
      const res = await fetch("/config");
      if (res.ok) {
        const data = await res.json();
        set({ ...data, loaded: true });
      }
    } catch {
      set({ loaded: true }); // fallback to defaults
    }
  },
}));

