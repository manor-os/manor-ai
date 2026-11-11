import type { WorkspaceAppChartData, WorkspaceAppPresentedField } from "../../lib/api";

/** How an App field's value reads as words, apart from its markup: the same in a card, a table cell and a copy. */

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

function chinese(locale: string): boolean {
  return /^zh\b/i.test(locale);
}

function number(value: number, locale: string, options: Intl.NumberFormatOptions = {}): string {
  try {
    return new Intl.NumberFormat(locale, options).format(value);
  } catch {
    return String(value);
  }
}

/** Seconds as hours, minutes and seconds: "1 分 30 秒" in Chinese, "1m 30s" otherwise. */
export function formatDuration(seconds: number, locale: string): string {
  const whole = Math.max(0, Math.round(seconds));
  const parts: Array<[number, string, string]> = [
    [Math.floor(whole / 3600), " 小时", "h"],
    [Math.floor((whole % 3600) / 60), " 分", "m"],
    [whole % 60, " 秒", "s"],
  ];
  const shown = parts.filter(([amount]) => amount > 0);
  return (shown.length ? shown : [parts[2]]).map(([amount, zh, short]) => `${amount}${chinese(locale) ? zh : short}`).join(" ");
}

/** A 0–1 share as a percentage, to one decimal at most: 0.625 → "62.5%". */
export function formatPercent(share: number, locale: string): string {
  return number(share, locale, { style: "percent", maximumFractionDigits: 1 });
}

/** Bytes in the largest unit that keeps a whole number in front: 2_516_582 → "2.4 MB". */
export function formatFileSize(bytes: number, locale: string): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, bytes);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${number(size, locale, { maximumFractionDigits: unit && size < 10 ? 1 : 0 })} ${units[unit]}`;
}

/** A number with the unit it counts in, if any: "300 g", "2 份". */
export function formatQuantity(value: number, locale: string, unit?: string | null): string {
  const shown = number(value, locale);
  return unit ? `${shown} ${unit}` : shown;
}

/** How much of a progress field is done, 0–1; null when it has no whole to measure against. */
export function progressShare(value?: number | null, max?: number | null): number | null {
  if (typeof value !== "number" || typeof max !== "number" || !(max > 0)) return null;
  return Math.min(1, Math.max(0, value / max));
}

/** A progress field as words: out of 100 it is a percentage, out of anything else "3 / 5". */
export function formatProgress(value: number, max: number, locale: string): string {
  return max === 100 ? formatPercent(Math.min(1, Math.max(0, value / 100)), locale) : `${number(value, locale)} / ${number(max, locale)}`;
}

/** A time as how long ago (or from now) it is: "3 hours ago", "3 小时前", "in 2 days". */
export function formatRelativeTime(iso: string, locale: string, now = Date.now()): string | null {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const seconds = Math.round((at - now) / 1000);
  const steps: Array<[Intl.RelativeTimeFormatUnit, number, number]> = [
    ["second", 1, 60], ["minute", 60, 60], ["hour", 3600, 24], ["day", 86400, 7], ["week", 604800, 4.35],
    ["month", 2629800, 12], ["year", 31557600, Infinity],
  ];
  const [unit, size] = steps.find(([, size, count]) => Math.abs(seconds) < size * count) || steps[steps.length - 1];
  try {
    return new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(Math.round(seconds / size), unit);
  } catch {
    return null;
  }
}

function rangeEnd(value: string): { date: Date; timed: boolean } | null {
  // A plain date is a whole day wherever the reader is; a time reads in the reader's zone.
  if (DATE_ONLY.test(value)) {
    const date = new Date(`${value}T12:00:00Z`);
    // Reject invalid and rolled-over dates before passing them to Intl.
    if (!Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) return null;
    return { date, timed: false };
  }
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : { date: new Date(at), timed: true };
}

/** A date range in one phrase ("Nov 14 – 16", "11月14日至16日"); an open end reads through ``from`` or ``until``. */
export function formatDateRange(start: string | null | undefined, end: string | null | undefined, options: {
  locale: string; timeZone?: string; now?: number; from: (date: string) => string; until: (date: string) => string;
}): string | null {
  const first = start ? rangeEnd(start) : null;
  const last = end ? rangeEnd(end) : null;
  if (!first && !last) return null;
  const ends = [first, last].filter((part): part is NonNullable<typeof first> => part !== null);
  const year = new Date(options.now ?? Date.now()).getUTCFullYear();
  const includeYear = ends.some((part) => part.date.getUTCFullYear() !== year);
  const formatterFor = (timed: boolean) => {
    const format: Intl.DateTimeFormatOptions & { timeZone: string } = {
      month: "short", day: "numeric", ...(includeYear ? { year: "numeric" } : {}),
      ...(timed ? { hour: "2-digit", minute: "2-digit" } : {}),
      timeZone: timed ? options.timeZone || "UTC" : "UTC",
    };
    try {
      return new Intl.DateTimeFormat(options.locale, format);
    } catch {
      return new Intl.DateTimeFormat("en", { ...format, timeZone: "UTC" });
    }
  };
  if (first && last && first.timed !== last.timed) {
    // A date-only endpoint keeps its written day and does not acquire a time.
    return `${formatterFor(first.timed).format(first.date)} – ${formatterFor(last.timed).format(last.date)}`;
  }
  const formatter = formatterFor(Boolean(first?.timed || last?.timed));
  if (first && last) {
    try {
      return formatter.formatRange(first.date, last.date);
    } catch {
      return `${formatter.format(first.date)} – ${formatter.format(last.date)}`;
    }
  }
  return first ? options.from(formatter.format(first.date)) : options.until(formatter.format(last!.date));
}

/** A ``tel:`` link for a phone number written with spaces, dashes or brackets; null for anything else. */
export function phoneHref(value: string): string | null {
  const digits = value.replace(/[\s().-]/g, "");
  return /^\+?\d{3,20}$/.test(digits) ? `tel:${digits}` : null;
}

/** A ``mailto:`` link for one plain address; null for anything else. */
export function emailHref(value: string): string | null {
  const address = value.trim();
  // URI delimiters and escapes can add mail headers instead of addressing one person.
  return /^[a-z0-9.!_+'-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$/i.test(address)
    ? `mailto:${address}` : null;
}

/** A maps search for an address: Amap for a Chinese reader, Google Maps otherwise. */
export function mapsHref(address: string, locale: string): string | null {
  const query = address.trim();
  if (!query) return null;
  return chinese(locale)
    ? `https://uri.amap.com/search?keyword=${encodeURIComponent(query)}`
    : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
}

/** A #rrggbb colour as given, or null. */
export function safeColor(value: unknown): string | null {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value) ? value.toLowerCase() : null;
}

/** What a copy button on this field puts on the clipboard: the words, the address, the tags in a row. */
export function fieldCopyText(field: WorkspaceAppPresentedField): string {
  if (field.format === "tags") return (field.tags || []).join(" ");
  if (field.format === "link") return field.href || "";
  return typeof field.value === "string" || typeof field.value === "number" ? String(field.value) : "";
}

/** A field as one line of plain words, the way a spreadsheet cell holds it: numbers in their format, the
 *  names a record or person field shows, links as their address. Files and players have no words: "". */
export function fieldPlainText(field: WorkspaceAppPresentedField | undefined, locale: string): string {
  if (!field) return "";
  const numberText = fieldNumberText(field, locale);
  if (numberText !== null) return numberText;
  switch (field.format) {
    case "record": return (field.records || []).map((record) => record.label || "").filter(Boolean).join(", ");
    case "member": return (field.members || []).map((member) => member.name).join(", ");
    case "tags": return (field.tags || []).join(", ");
    case "link_list": return (field.links || []).map((link) => link.href).join(" ");
    case "link": return field.href || "";
    case "date_range": return [field.value, field.end].filter(Boolean).join(" – ");
    case "sparkline": return (field.points || []).join(" ");
    case "document": case "image": case "images": case "video": case "audio": case "clips": case "timeline": return "";
    case "rating": return typeof field.value === "number" ? `${field.value}/5` : "";
    default: {
      const value = field.value;
      if (value === null || value === undefined) return "";
      const text = String(value);
      return field.secondary ? `${text} (${field.secondary})` : text;
    }
  }
}

/** A field of a number-like format as words, or null when it is not one (or holds no number). */
export function fieldNumberText(field: WorkspaceAppPresentedField, locale: string): string | null {
  const value = field.value;
  if (typeof value !== "number") return null;
  if (field.format === "percent") return formatPercent(value, locale);
  if (field.format === "duration") return formatDuration(value, locale);
  if (field.format === "file_size") return formatFileSize(value, locale);
  if (field.format === "progress") return typeof field.max === "number" ? formatProgress(value, field.max, locale) : null;
  if (field.format === "money" && field.currency) return number(value, locale, { style: "currency", currency: field.currency });
  if (field.format === "number" || field.format === "money") return formatQuantity(value, locale, field.unit);
  return null;
}

/** A chart's value in its format: money in its currency, a percent on its stored scale, seconds as a duration. */
export function chartValueText(value: number | null | undefined, chart: Pick<WorkspaceAppChartData, "format" | "currency" | "unit" | "scale">,
  locale: string, compact = false): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (chart.format === "percent") return formatPercent(value / (chart.scale || 1), locale);
  if (chart.format === "duration") return formatDuration(value, locale);
  const options: Intl.NumberFormatOptions = compact && Math.abs(value) >= 10_000 ? { notation: "compact", maximumFractionDigits: 1 } : { minimumFractionDigits: 0, maximumFractionDigits: 2 };
  if (chart.format === "money" && chart.currency) {
    try {
      return new Intl.NumberFormat(locale, { style: "currency", currency: chart.currency, ...options,
        ...(compact ? { minimumFractionDigits: 0, maximumFractionDigits: Math.abs(value) >= 10_000 ? 1 : 0 } : {}) }).format(value);
    } catch {
      return `${value} ${chart.currency}`;
    }
  }
  if (compact) {
    try {
      const text = new Intl.NumberFormat(locale, options).format(value);
      return chart.unit ? `${text} ${chart.unit}` : text;
    } catch { /* falls through */ }
  }
  return formatQuantity(Math.round(value * 100) / 100, locale, chart.unit);
}
