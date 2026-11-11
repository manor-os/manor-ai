import type { WorkspaceAppDataItem } from "../../lib/api";

/** The decision a held post's publish run offers to publish it after its time has passed. */
export const CALENDAR_LATE_PUBLISH_CHOICE = "publish_late";
/** The variable that decision is stored in, as the publish Workflow's wait names it. */
export const CALENDAR_LATE_PUBLISH_VARIABLE = "late_publication_decision";

/** A scheduled item whose time has passed, so it waits for a person instead of posting on time. */
export function calendarItemOverdue(item: Pick<WorkspaceAppDataItem, "calendar_at">, now: number): boolean {
  const due = item.calendar_at ? Date.parse(item.calendar_at) : Number.NaN;
  return Number.isFinite(due) && due < now;
}

/** A post whose time has passed and that is still waiting to publish, with no publish run in progress. */
export function calendarCanContinuePublication(item: Pick<WorkspaceAppDataItem, "calendar_at" | "status" | "execution">, now: number): boolean {
  if (!calendarItemOverdue(item, now)) return false;
  const waiting = item.status === "ready" || item.status === "publication_blocked";
  const running = item.execution?.status === "running" || item.execution?.status === "paused";
  return waiting && !running;
}

/** Whether a run's pending decision is an approval that offers to publish a post past its time. */
export function calendarOffersLatePublish(action: { kind?: string; options?: string[] } | null | undefined): boolean {
  return action?.kind === "workflow_approval" && Boolean(action.options?.includes(CALENDAR_LATE_PUBLISH_CHOICE));
}

/** project_records uses `${parentProjectId}:${recordIndex}` as its public row ID.
 * Match that identity exactly; titles, platforms and scheduled times are not keys. */
export function calendarRecordProjectId(record: WorkspaceAppDataItem): string | null {
  return record.id.match(/^(.+):\d+$/)?.[1] || null;
}

/** Stages an item is still producing in; the server reads the same list when it counts them. */
export const CALENDAR_IN_PROGRESS_STAGES = ["leased", "running", "in_progress", "processing", "producing"] as const;

/** A stopped related run must not leave an unfinished plan looking active.
 * A successful run alone is not proof that the business item was delivered. */
export function calendarDisplayStage(item: WorkspaceAppDataItem, completedStages: readonly string[]): string {
  const stage = item.status || "";
  if (completedStages.includes(stage)) return stage;
  const execution = item.execution?.status;
  if (execution === "failed" || execution === "cancelled") return execution;
  if (CALENDAR_IN_PROGRESS_STAGES.includes(stage as (typeof CALENDAR_IN_PROGRESS_STAGES)[number])
    && (execution === "paused" || execution === "pending")) return execution;
  return stage;
}

/** The step a live item is on, shown beside its stage so the card says what is being done. */
export function calendarCurrentStep(item: WorkspaceAppDataItem): string | null {
  return item.execution?.status === "running" ? item.execution.current_step_label || null : null;
}

/** A live item's phase is the workflow it runs. A block's stage label is shared by several workflows (publishing,
 * cover and video making all lease), so the label alone names the wrong work. */
export function calendarPhaseLabel(item: WorkspaceAppDataItem, stage: string, stageLabel: string): string {
  const workflow = item.execution?.status === "running" ? item.execution.workflow_name : null;
  return workflow && CALENDAR_IN_PROGRESS_STAGES.includes(stage as (typeof CALENDAR_IN_PROGRESS_STAGES)[number]) ? workflow : stageLabel;
}

/** The phase, then the step it is on. A sub-flow's step already leads with its workflow, so the phase is not repeated. */
export function calendarStatusText(phase: string, step: string | null): string {
  if (!step) return phase;
  const lead = `${phase} · `;
  return `${phase} · ${step.startsWith(lead) ? step.slice(lead.length) : step}`;
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
  // A plain date is a whole day where it was planned, not midnight UTC moved into the viewer's zone.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
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

/** The seven days (Monday first, as the month grid) of the week a day is in. */
export function calendarWeekDays(day: string): string[] {
  const date = new Date(`${day}T00:00:00Z`);
  const monday = date.getUTCDate() - (date.getUTCDay() + 6) % 7;
  return Array.from({ length: 7 }, (_, index) => new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), monday + index)).toISOString().slice(0, 10));
}

export function shiftCalendarDay(day: string, delta: number): string {
  const date = new Date(`${day}T00:00:00Z`);
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate() + delta)).toISOString().slice(0, 10);
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
