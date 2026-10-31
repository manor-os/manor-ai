import type { WorkspaceAppDataItem } from "../../lib/api";

/** project_records uses `${parentProjectId}:${recordIndex}` as its public row ID.
 * Match that identity exactly; titles, platforms and scheduled times are not keys. */
export function calendarRecordProjectId(record: WorkspaceAppDataItem): string | null {
  return record.id.match(/^(.+):\d+$/)?.[1] || null;
}

/** A stopped related run must not leave an unfinished plan looking active.
 * A successful run alone is not proof that the business item was delivered. */
export function calendarDisplayStage(item: WorkspaceAppDataItem, completedStages: readonly string[]): string {
  const stage = item.status || "";
  if (completedStages.includes(stage)) return stage;
  const execution = item.execution?.status;
  if (execution === "failed" || execution === "cancelled") return execution;
  if (["leased", "running", "in_progress", "processing"].includes(stage)
    && (execution === "paused" || execution === "pending")) return execution;
  return stage;
}

/** Adjust only observations we have; keep server totals for unloaded items. */
export function calendarDisplayCounts(counts: Record<string, number>, items: WorkspaceAppDataItem[], completedStages: readonly string[]) {
  const result = { ...counts };
  for (const item of new Map(items.map((item) => [item.id, item])).values()) {
    const stage = item.status || "";
    const display = calendarDisplayStage(item, completedStages);
    if (stage === display || !(result[stage] > 0)) continue;
    result[stage] -= 1;
    result[display] = (result[display] || 0) + 1;
  }
  return result;
}

/**
 * A rollup's counts as shown. When the server already counted each unfinished
 * item by its run they are used as they are, so a card and the open campaign
 * agree and an item is never moved twice; otherwise known items adjust them.
 */
export function calendarSummaryCounts(
  summary: { counts_by_stage?: Record<string, number>; counts_include_execution?: boolean },
  knownItems: WorkspaceAppDataItem[],
  completedStages: readonly string[],
): Record<string, number> {
  const counts = summary.counts_by_stage || {};
  return summary.counts_include_execution ? { ...counts } : calendarDisplayCounts(counts, knownItems, completedStages);
}

/** Calendar grouping uses the same display zone as the rest of the App. */
export function calendarDateKey(value: string | null | undefined, timeZone: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  const parts = new Intl.DateTimeFormat("en-US", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(date);
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

export function calendarMonthDays(month: string): string[] {
  const [year, index] = month.split("-").map(Number);
  const first = new Date(Date.UTC(year, index - 1, 1));
  const offset = (first.getUTCDay() + 6) % 7;
  const length = new Date(Date.UTC(year, index, 0)).getUTCDate();
  const cells = Math.ceil((offset + length) / 7) * 7;
  return Array.from({ length: cells }, (_, day) => new Date(Date.UTC(year, index - 1, 1 - offset + day)).toISOString().slice(0, 10));
}

export function shiftCalendarMonth(month: string, delta: number): string {
  const [year, index] = month.split("-").map(Number);
  return new Date(Date.UTC(year, index - 1 + delta, 1)).toISOString().slice(0, 7);
}

export function calendarItemsByDay(items: WorkspaceAppDataItem[], timeZone: string) {
  const days = new Map<string, WorkspaceAppDataItem[]>();
  const undated: WorkspaceAppDataItem[] = [];
  // A receipt and a later page must not create a second event for one project.
  const unique = [...new Map(items.map((item) => [item.id, item])).values()].sort((a, b) => {
    const left = Date.parse(a.calendar_at || "");
    const right = Date.parse(b.calendar_at || "");
    return (Number.isFinite(left) ? left : Infinity) - (Number.isFinite(right) ? right : Infinity) || a.id.localeCompare(b.id);
  });
  for (const item of unique) {
    const day = calendarDateKey(item.calendar_at, timeZone);
    if (!day) undated.push(item);
    else days.set(day, [...(days.get(day) || []), item]);
  }
  return { days, undated, items: unique };
}

export function calendarAttentionCount(counts: Record<string, number> | undefined, stages: readonly string[]): number {
  return [...new Set(stages)].reduce((total, stage) => total + (counts?.[stage] || 0), 0);
}
