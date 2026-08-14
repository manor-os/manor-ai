import type { WorkspaceStatDefinition } from "./api";
import { t } from "./i18n";

export interface WorkspaceStatAttentionPresentation {
  type: "warning" | "danger" | "inactive" | "info";
  label: string;
}

export function workspaceStatAttentionPresentation(
  status: string,
): WorkspaceStatAttentionPresentation | null {
  if (status === "fresh") return null;
  if (status === "stale") {
    return { type: "warning", label: t("page.workspace_stats.stale") };
  }
  if (status === "collection_error") {
    return { type: "danger", label: t("page.workspace_stats.error") };
  }
  if (status === "paused") {
    return { type: "inactive", label: t("page.workspace_stats.paused") };
  }
  return { type: "info", label: t("page.workspace_stats.no_data") };
}

export function formatWorkspaceStatValue(
  stat: WorkspaceStatDefinition,
  value = stat.current_value,
): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) {
    return "—";
  }
  const number = Number(value);
  const maximumFractionDigits = Number.isInteger(number) ? 0 : 2;
  const formatted = number.toLocaleString(undefined, { maximumFractionDigits });
  if (stat.value_type === "percent") return `${formatted}%`;
  if (stat.value_type === "currency") return `${stat.unit || ""}${formatted}`;
  return stat.unit ? `${formatted} ${stat.unit}` : formatted;
}
