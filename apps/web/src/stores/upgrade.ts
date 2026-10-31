import { create } from "zustand";
import type { PlanLimitDetail } from "../lib/api";
import { planLimitOffersUpgrade, planLimitTitle } from "../lib/planLimit";
import { useToastStore } from "./toast";

export const TRIAL_CREDIT_TOAST_ID = "plan-limit-trial-credit";

interface UpgradeState {
  /** When non-null, the UpgradePrompt overlay is shown globally. */
  limitDetail: PlanLimitDetail | null;
  show: (detail: PlanLimitDetail) => void;
  dismiss: () => void;
}

export const useUpgradeStore = create<UpgradeState>((set) => ({
  limitDetail: null,
  show: (detail) => {
    // A provider-paid trial stop is not the viewer's plan to upgrade: say
    // what happened and leave the upgrade / buy-credits overlay closed.
    if (!planLimitOffersUpgrade(detail.kind)) {
      useToastStore.getState().addToast({
        id: TRIAL_CREDIT_TOAST_ID,
        type: "warning",
        title: planLimitTitle(detail.kind),
        message: detail.message,
        duration: 8000,
      });
      return;
    }
    set({ limitDetail: detail });
  },
  dismiss: () => set({ limitDetail: null }),
}));
