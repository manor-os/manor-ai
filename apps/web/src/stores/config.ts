import { create } from "zustand";

interface AppConfig {
  deployment_mode: "oss" | "cloud";
  environment: string;
  email_enabled: boolean;
  fs_enabled: boolean;
  flows_available: boolean;
  flows_released: boolean;
  ai_credits_unlimited: boolean;
  support_tickets_enabled: boolean;
  loaded: boolean;
  loading: boolean;
  load_error: boolean;
  load: () => Promise<void>;
}

export const useConfigStore = create<AppConfig>((set, get) => ({
  deployment_mode: "oss",
  environment: import.meta.env.DEV ? "local" : "prod",
  email_enabled: false,
  fs_enabled: false,
  flows_available: import.meta.env.DEV,
  flows_released: import.meta.env.DEV,
  ai_credits_unlimited: true,
  support_tickets_enabled: false,
  loaded: false,
  loading: false,
  load_error: false,

  load: async () => {
    if (get().loaded || get().loading) return;
    set({ loading: true, load_error: false });
    try {
      const res = await fetch("/config");
      if (!res.ok) {
        throw new Error(`Config request failed with ${res.status}`);
      }
      const data = await res.json();
      set({ ...data, loaded: true, loading: false, load_error: false });
    } catch {
      set({ loaded: false, loading: false, load_error: true });
    }
  },
}));

