import { create } from "zustand";
import type { PlanLimitDetail } from "../lib/api";

interface UpgradeState {
  /** When non-null, the UpgradePrompt overlay is shown globally. */
  limitDetail: PlanLimitDetail | null;
  show: (detail: PlanLimitDetail) => void;
  dismiss: () => void;
}

export const useUpgradeStore = create<UpgradeState>((set) => ({
  limitDetail: null,
  show: (detail) => {
    set({ limitDetail: detail });
  },
  dismiss: () => set({ limitDetail: null }),
}));
