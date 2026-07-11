import type { PlanLimitKind } from "./api";
import { t } from "./i18n";

/**
 * Single source of truth for how a plan-limit reminder presents itself, so the
 * overlay (UpgradePrompt) and the inline chat notice (CreditLimitNotice) stay
 * consistent across limit types — credits exhausted, knowledge-base storage
 * full, workspace/member caps, etc. All flow through the same reminder element;
 * only the title and whether "Buy Credits" makes sense differ by kind.
 */
export function planLimitTitle(kind?: PlanLimitKind): string {
  switch (kind) {
    case "storage":
      return t("component.upgrade_prompt.title_storage");
    case "workspaces":
      return t("component.upgrade_prompt.title_workspaces");
    case "users":
      return t("component.upgrade_prompt.title_users");
    case "generic":
      return t("component.upgrade_prompt.title_generic");
    case "credit":
    default:
      // Legacy payloads (no kind) are credit-exhaustion, the original case.
      return t("component.upgrade_prompt.credits_exhausted");
  }
}

/** Buying credits only resolves the credit limit; other limits need a plan change. */
export function planLimitOffersCredits(kind?: PlanLimitKind): boolean {
  return !kind || kind === "credit";
}
