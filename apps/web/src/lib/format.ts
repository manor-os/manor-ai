/**
 * Shared date/time formatting and status helpers.
 * Centralised to avoid per-page duplication.
 */
import { getLocale, t } from "./i18n";

const DATE_LOCALES = {
  en: "en-US",
  zh: "zh-CN",
  es: "es-ES",
  de: "de-DE",
} as const;

function dateLocale() {
  return DATE_LOCALES[getLocale()] || "en-US";
}

let preferredTimeZone: string | undefined;

function normalizeTimeZone(timeZone?: string | null): string | undefined {
  const candidate = (timeZone || "").trim();
  if (!candidate) return undefined;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: candidate }).format(new Date());
    return candidate;
  } catch {
    return undefined;
  }
}

function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

export function setPreferredTimeZone(timeZone?: string | null) {
  preferredTimeZone = normalizeTimeZone(timeZone) || browserTimeZone();
}

export function getPreferredTimeZone(): string {
  return preferredTimeZone || browserTimeZone();
}

function withTimeZone(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormatOptions {
  return { ...options, timeZone: getPreferredTimeZone() };
}

const DATE_ONLY_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
const MIDNIGHT_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})T00:00(?::00(?:\.0{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?$/;

function dateOnlyParts(dateStr: string): [number, number, number] | null {
  const match = dateStr.match(DATE_ONLY_RE) || dateStr.match(MIDNIGHT_DATE_RE);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function zonedPartsFor(date: Date, timeZone = getPreferredTimeZone()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(date);
  const values: Record<string, number> = {};
  for (const part of parts) {
    if (part.type !== "literal") values[part.type] = Number(part.value);
  }
  return values as { year: number; month: number; day: number; hour: number; minute: number; second: number };
}

function zonedDateFromParts(parts: [number, number, number], endOfDay = false): Date {
  const [year, month, day] = parts;
  const target = {
    year,
    month,
    day,
    hour: endOfDay ? 23 : 0,
    minute: endOfDay ? 59 : 0,
    second: endOfDay ? 59 : 0,
    ms: endOfDay ? 999 : 0,
  };
  const targetAsUtc = Date.UTC(
    target.year,
    target.month - 1,
    target.day,
    target.hour,
    target.minute,
    target.second,
    target.ms,
  );
  let utcTime = targetAsUtc;
  const timeZone = getPreferredTimeZone();
  for (let i = 0; i < 3; i += 1) {
    const actual = zonedPartsFor(new Date(utcTime), timeZone);
    const actualAsUtc = Date.UTC(
      actual.year,
      actual.month - 1,
      actual.day,
      actual.hour,
      actual.minute,
      actual.second,
      target.ms,
    );
    const diff = targetAsUtc - actualAsUtc;
    if (diff === 0) break;
    utcTime += diff;
  }
  return new Date(utcTime);
}

function parseDisplayDate(dateStr?: string): { date: Date; dateOnly: boolean } | null {
  if (!dateStr) return null;
  const parts = dateOnlyParts(dateStr);
  if (parts) return { date: zonedDateFromParts(parts), dateOnly: true };
  const date = new Date(dateStr);
  return Number.isFinite(date.getTime()) ? { date, dateOnly: false } : null;
}

export function deadlineEndTime(deadline?: string | null): number | null {
  if (!deadline) return null;
  const parts = dateOnlyParts(deadline);
  if (parts) return zonedDateFromParts(parts, true).getTime();
  const time = new Date(deadline).getTime();
  return Number.isFinite(time) ? time : null;
}

export function isDeadlineOverdue(deadline?: string | null, status?: string, now: Date = new Date()): boolean {
  if (!deadline || ["completed", "cancelled", "failed"].includes(status || "")) return false;
  const end = deadlineEndTime(deadline);
  return end !== null && end < now.getTime();
}

/** Short date+time  →  "Apr 21, 2:35 PM" */
export function formatDate(dateStr?: string, fallback = "--"): string {
  const parsed = parseDisplayDate(dateStr);
  if (!parsed) return fallback;
  if (parsed.dateOnly) return formatDateShort(dateStr, fallback);
  return parsed.date.toLocaleString(dateLocale(), withTimeZone({
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }));
}

/** Long date+time (includes year)  →  "Apr 21, 2026, 2:35 PM" */
export function formatDateLong(dateStr?: string, fallback = "--"): string {
  const parsed = parseDisplayDate(dateStr);
  if (!parsed) return fallback;
  if (parsed.dateOnly) return formatDateOnly(dateStr, fallback);
  return parsed.date.toLocaleString(dateLocale(), withTimeZone({
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }));
}

/** Weekday + short date+time  →  "Mon, Apr 21, 2:35 PM" */
export function formatDateFull(dateStr?: string, fallback = "--"): string {
  const parsed = parseDisplayDate(dateStr);
  if (!parsed) return fallback;
  if (parsed.dateOnly) {
    return parsed.date.toLocaleDateString(dateLocale(), withTimeZone({
      weekday: "short",
      month: "short",
      day: "numeric",
      year: "numeric",
    }));
  }
  return parsed.date.toLocaleString(dateLocale(), withTimeZone({
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }));
}

/** Date only (no time)  →  "Apr 21, 2026" */
export function formatDateOnly(dateStr?: string, fallback = "--"): string {
  const parsed = parseDisplayDate(dateStr);
  if (!parsed) return fallback;
  return parsed.date.toLocaleDateString(dateLocale(), withTimeZone({
    year: "numeric",
    month: "short",
    day: "numeric",
  }));
}

/** Short date only (no year)  →  "Apr 21" */
export function formatDateShort(dateStr?: string, fallback = ""): string {
  const parsed = parseDisplayDate(dateStr);
  if (!parsed) return fallback;
  return parsed.date.toLocaleDateString(dateLocale(), withTimeZone({
    month: "short",
    day: "numeric",
  }));
}

export function formatTodayFull(now: Date = new Date()): string {
  return now.toLocaleDateString(dateLocale(), withTimeZone({
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  }));
}

export function currentZonedHour(now: Date = new Date()): number {
  const hour = new Intl.DateTimeFormat("en-US", {
    timeZone: getPreferredTimeZone(),
    hourCycle: "h23",
    hour: "2-digit",
  }).formatToParts(now).find((part) => part.type === "hour")?.value;
  return Number(hour ?? now.getHours());
}

/** Relative time  →  "3h ago", "just now" */
export function relativeTime(dateStr?: string, fallback = t("lib.format.never")): string {
  if (!dateStr) return fallback;
  const diff = Date.now() - new Date(dateStr).getTime();
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return t("lib.format.just_now");
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return t("lib.format.minutes_ago").replace("{count}", String(minutes));
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return t("lib.format.hours_ago").replace("{count}", String(hours));
  const days = Math.floor(hours / 24);
  return t("lib.format.days_ago").replace("{count}", String(days));
}

/** Human-readable file size  →  "1.2 MB" */
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

/** Canonical vector_status values — single source of truth for the frontend. */
export const VectorStatus = {
  PENDING: "pending",
  PROCESSING: "processing",
  GENERATING: "generating",
  READY: "ready",
  INDEXED: "indexed",   // legacy alias for READY
  FAILED: "failed",
  SKIPPED: "skipped",
} as const;

export type VectorStatusValue = (typeof VectorStatus)[keyof typeof VectorStatus];

/** Statuses that mean "work in progress" — used for animations & polling. */
const IN_PROGRESS: ReadonlySet<string> = new Set([
  VectorStatus.PENDING,
  VectorStatus.PROCESSING,
  VectorStatus.GENERATING,
]);

/** True when the document is still being processed (generating, indexing, or queued). */
export function isVectorInProgress(status: string): boolean {
  return IN_PROGRESS.has(status);
}

/** True when the document is being indexed (pending or processing). */
export function isVectorIndexing(status: string): boolean {
  return status === VectorStatus.PENDING || status === VectorStatus.PROCESSING;
}

/** Map vector_status → StatusBadge props */
export function getVectorStatusBadge(status: string): { type: "success" | "warning" | "danger" | "info"; label: string } {
  switch (status) {
    case VectorStatus.INDEXED:
      return { type: "success", label: t("lib.format.vector_indexed") };
    case VectorStatus.PROCESSING:
      return { type: "warning", label: t("lib.format.vector_indexing") };
    case VectorStatus.PENDING:
      return { type: "info", label: t("lib.format.vector_queued") };
    case VectorStatus.GENERATING:
      return { type: "warning", label: t("lib.format.vector_generating") };
    case VectorStatus.READY:
      return { type: "success", label: t("lib.format.vector_ready") };
    case VectorStatus.SKIPPED:
      return { type: "info", label: t("lib.format.vector_skipped") };
    case VectorStatus.FAILED:
      return { type: "danger", label: t("component.status.failed") };
    default:
      return { type: "info", label: status || t("component.status.pending") };
  }
}

/** Map API status string → StatusBadge type */
export function statusBadgeType(
  status: string,
): "active" | "inactive" | "warning" | "danger" {
  switch (status) {
    case "active":
    case "running":
      return "active";
    case "paused":
    case "archived":
      return "warning";
    case "error":
    case "revoked":
    case "failed":
      return "danger";
    default:
      return "inactive";
  }
}

/** Format integer cents as a dollar string. usd-only in v1. */
export function formatPriceUsd(cents?: number | null): string {
  return `$${((cents ?? 0) / 100).toFixed(2)}`;
}

/** True when a blueprint carries a nonzero marketplace price. */
export function isPaidBlueprint(bp: { price_cents?: number | null }): boolean {
  return (bp.price_cents ?? 0) > 0;
}
