export type PlanUsageResource = "credits" | "storage";
export type PlanUsageSeverity = "low" | "exhausted";

export interface PlanUsageAlert {
  resource: PlanUsageResource;
  severity: PlanUsageSeverity;
  total: number;
  remaining: number;
  used: number;
  percentRemaining: number;
  remainingLabel: string;
  totalLabel: string;
  dismissKey: string;
}

interface BillingLike {
  balance?: {
    total_credits?: unknown;
    used_credits?: unknown;
    remaining_credits?: unknown;
  } | null;
  plan?: {
    credits_total?: unknown;
    credits_used?: unknown;
    credits_remaining?: unknown;
  } | null;
  storage?: {
    used_mb?: unknown;
    limit_mb?: unknown;
    remaining_mb?: unknown;
  } | null;
}

const WARNING_THRESHOLD = 0.1;

function optionalUsageNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const count = Number(value);
  return Number.isFinite(count) ? count : null;
}

function roundForKey(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function formatCredits(value: number): string {
  return Math.max(0, Math.round(value)).toLocaleString();
}

function formatStorageMb(value: number): string {
  const normalized = Math.max(0, value);
  if (normalized >= 1024) {
    const gb = normalized / 1024;
    return `${gb.toFixed(gb >= 10 || Number.isInteger(gb) ? 0 : 1)} GB`;
  }
  return `${normalized.toFixed(normalized >= 10 || Number.isInteger(normalized) ? 0 : 1)} MB`;
}

function buildAlert(
  resource: PlanUsageResource,
  total: number | null,
  remaining: number | null,
  used: number | null,
): PlanUsageAlert | null {
  if (total === null || remaining === null || total <= 0) return null;
  const safeRemaining = Math.max(0, remaining);
  const percentRemaining = safeRemaining / total;
  const severity =
    safeRemaining <= 0
      ? "exhausted"
      : percentRemaining <= WARNING_THRESHOLD
        ? "low"
        : null;
  if (!severity) return null;
  const safeUsed = used ?? Math.max(0, total - safeRemaining);
  const formatter = resource === "credits" ? formatCredits : formatStorageMb;
  return {
    resource,
    severity,
    total,
    remaining: safeRemaining,
    used: safeUsed,
    percentRemaining: Math.max(0, Math.min(100, percentRemaining * 100)),
    remainingLabel: formatter(safeRemaining),
    totalLabel: formatter(total),
    dismissKey: `${resource}:${severity}:${roundForKey(total)}:${roundForKey(safeRemaining)}`,
  };
}

export function buildPlanUsageAlerts(billing: BillingLike | null | undefined): PlanUsageAlert[] {
  const balance = billing?.balance;
  const plan = billing?.plan;
  const storage = billing?.storage;

  const creditsTotal =
    optionalUsageNumber(balance?.total_credits) ??
    optionalUsageNumber(plan?.credits_total);
  const creditsUsed =
    optionalUsageNumber(balance?.used_credits) ??
    optionalUsageNumber(plan?.credits_used);
  const creditsRemaining =
    optionalUsageNumber(balance?.remaining_credits) ??
    optionalUsageNumber(plan?.credits_remaining) ??
    (creditsTotal !== null && creditsUsed !== null ? creditsTotal - creditsUsed : null);

  const storageLimit = optionalUsageNumber(storage?.limit_mb);
  const storageUsed = optionalUsageNumber(storage?.used_mb);
  const storageRemaining =
    optionalUsageNumber(storage?.remaining_mb) ??
    (storageLimit !== null && storageUsed !== null ? storageLimit - storageUsed : null);

  return [
    buildAlert("credits", creditsTotal, creditsRemaining, creditsUsed),
    buildAlert("storage", storageLimit, storageRemaining, storageUsed),
  ].filter((alert): alert is PlanUsageAlert => Boolean(alert));
}

export function filterDismissedPlanUsageAlerts(
  alerts: PlanUsageAlert[],
  dismissedKeys: ReadonlySet<string>,
): PlanUsageAlert[] {
  return alerts.filter((alert) => !dismissedKeys.has(alert.dismissKey));
}
