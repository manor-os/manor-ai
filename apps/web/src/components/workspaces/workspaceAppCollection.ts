import type { WorkspaceAppDataItem } from "../../lib/api";

type Field = NonNullable<WorkspaceAppDataItem["fields"]>[number];

/** A board's columns: the stages its source lists (each shown, empty or not), then the stages its labels
 *  name, then any other stage in the order its items came. An item with no stage has a column of its own. */
export function boardColumns(items: WorkspaceAppDataItem[], declared: readonly string[] = [], labelled: readonly string[] = []) {
  const seen = new Set(items.map((item) => item.status || ""));
  const order = [...new Set([...declared, ...labelled.filter((stage) => seen.has(stage)), ...items.map((item) => item.status || "")])];
  return order.map((stage) => ({ stage, items: items.filter((item) => (item.status || "") === stage) }));
}

/** What a table cell sorts by: a number or a date as a number, a yes/no as 0/1, the rest as its words.
 *  An empty cell sorts by null and always goes last. */
export function tableSortValue(field?: Field): number | string | null {
  if (!field) return null;
  const { value } = field;
  if (field.format === "record") return field.records?.map((record) => record.label || "").join(", ") || null;
  if (field.format === "link_list") return field.links?.map((link) => link.label).join(", ") || null;
  if (field.format === "member") return field.members?.map((member) => member.name).join(", ") || null;
  if (field.format === "tags") return field.tags?.join(", ") || null;
  if (field.format === "images") return field.images?.length || null;
  // A range sorts by where it starts, or where it ends when it has no start.
  if (field.format === "date_range") {
    const at = Date.parse(String(value || field.end || ""));
    return Number.isNaN(at) ? null : at;
  }
  // A progress sorts by how much of it is done, whatever its whole.
  if (field.format === "progress") return typeof value === "number" && typeof field.max === "number" && field.max > 0 ? value / field.max : null;
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "number") return value;
  if (typeof value === "boolean") return value ? 1 : 0;
  if (field.format === "date" && !Number.isNaN(Date.parse(value))) return Date.parse(value);
  return value;
}

/** The items sorted by one column, in place of the order they were read in; a tie keeps that order. */
export function sortTableItems<T>(items: T[], valueOf: (item: T) => number | string | null, direction: "asc" | "desc", locale?: string): T[] {
  const collator = new Intl.Collator(locale, { numeric: true, sensitivity: "base" });
  const sign = direction === "asc" ? 1 : -1;
  return items.map((item, index) => ({ item, index, value: valueOf(item) })).sort((left, right) => {
    if (left.value === null || right.value === null) return left.value === right.value ? left.index - right.index : left.value === null ? 1 : -1;
    const order = typeof left.value === "number" && typeof right.value === "number"
      ? left.value - right.value : collator.compare(String(left.value), String(right.value));
    return order * sign || left.index - right.index;
  }).map(({ item }) => item);
}

/** The items of a list in groups, by the nth ``group_by`` value each carries, in the order the groups first come;
 *  items with no value make the last group (key ""). */
export function groupItems<T extends { groups?: string[] }>(items: T[], choice: number): Array<{ key: string; items: T[] }> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = item.groups?.[choice] ?? "";
    groups.set(key, [...(groups.get(key) || []), item]);
  }
  const keyed = [...groups.entries()].map(([key, members]) => ({ key, items: members }));
  return [...keyed.filter((group) => group.key), ...keyed.filter((group) => !group.key)];
}

/** Ticking one row: a plain tick toggles it; with ``range`` every row from the last ticked one to it takes its new state. */
export function toggleSelection(selected: ReadonlySet<string>, id: string, options: { range?: boolean; anchor?: string | null; order?: readonly string[] } = {}): Set<string> {
  const next = new Set(selected);
  const on = !selected.has(id);
  const order = options.order || [];
  const from = options.anchor ? order.indexOf(options.anchor) : -1;
  const to = order.indexOf(id);
  const span = options.range && from >= 0 && to >= 0 ? order.slice(Math.min(from, to), Math.max(from, to) + 1) : [id];
  for (const each of span) {
    if (on) next.add(each); else next.delete(each);
  }
  return next;
}

/** The ticked ids that are still listed, so a row that left the list never stays ticked out of sight. */
export function keepListed(selected: ReadonlySet<string>, listed: readonly string[]): Set<string> {
  const present = new Set(listed);
  return new Set([...selected].filter((id) => present.has(id)));
}

/** Rows as CSV: every cell quoted when it must be, lines ended CRLF, with a BOM so spreadsheet apps read it as UTF-8.
 *  A cell that starts like a formula is kept as text. */
export function toCsv(rows: ReadonlyArray<ReadonlyArray<string>>): string {
  const cell = (value: string) => {
    const text = /^[=+\-@\t\r]/.test(value) && !/^-?\d/.test(value) ? `'${value}` : value;
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return `﻿${rows.map((row) => row.map(cell).join(",")).join("\r\n")}\r\n`;
}

/** A file name from a list's title: its words, safe on every system, and the day it was saved. */
export function csvFileName(title: string, now = new Date()): string {
  const base = title.normalize("NFKC").replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) || "export";
  return `${base} ${now.toISOString().slice(0, 10)}.csv`;
}

/** Reading and writing one viewer's choice in this browser; a blocked or full storage changes nothing. */
export function readStored<T>(key: string, fallback: T, valid: (value: unknown) => value is T): T {
  try {
    const raw = globalThis.localStorage?.getItem(key);
    const value = raw ? JSON.parse(raw) : null;
    return valid(value) ? value : fallback;
  } catch {
    return fallback;
  }
}

export function writeStored(key: string, value: unknown): void {
  try {
    if (value === null || (Array.isArray(value) && !value.length)) globalThis.localStorage?.removeItem(key);
    else globalThis.localStorage?.setItem(key, JSON.stringify(value));
  } catch { /* the choice lasts until the page closes */ }
}

export function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

/** A list a person saved as a tab of their own: its filters, search, grouping and sort. */
export type SavedView = {
  id: string;
  name: string;
  filters: Record<string, string>;
  q: string;
  /** The ``group_by`` choice, or -1 for none. */
  group: number;
  sort: { column: string; direction: "asc" | "desc" } | null;
};

/** The user preference that holds every saved view, keyed by Workspace, view and list. */
export const SAVED_VIEWS_PREFERENCE = "workspace_app_saved_views";
const SAVED_VIEWS_PER_LIST = 12;
const SAVED_VIEWS_LISTS = 200;

export function savedViewsKey(workspaceId: string, componentKey: string, blockKey: string): string {
  return `${workspaceId}:${componentKey}:${blockKey}`;
}

function savedView(value: unknown): SavedView | null {
  if (!value || typeof value !== "object") return null;
  const view = value as Record<string, unknown>;
  const filters = view.filters && typeof view.filters === "object" ? Object.fromEntries(Object.entries(view.filters as Record<string, unknown>)
    .filter((entry): entry is [string, string] => typeof entry[1] === "string" && Boolean(entry[1])).slice(0, 3)) : {};
  const sort = view.sort as SavedView["sort"] | undefined;
  if (typeof view.id !== "string" || typeof view.name !== "string" || !view.name.trim()) return null;
  return {
    id: view.id.slice(0, 40), name: view.name.trim().slice(0, 40), filters, q: typeof view.q === "string" ? view.q.slice(0, 100) : "",
    group: typeof view.group === "number" && Number.isInteger(view.group) && view.group >= -1 && view.group < 3 ? view.group : -1,
    sort: sort && typeof sort.column === "string" && (sort.direction === "asc" || sort.direction === "desc") ? { column: sort.column.slice(0, 120), direction: sort.direction } : null,
  };
}

/** One list's saved views from the stored preference; anything malformed is left out. */
export function savedViewsOf(preference: unknown, key: string): SavedView[] {
  const lists = preference && typeof preference === "object" ? (preference as Record<string, unknown>)[key] : null;
  return Array.isArray(lists) ? lists.map(savedView).filter((view): view is SavedView => view !== null).slice(0, SAVED_VIEWS_PER_LIST) : [];
}

/** The preference with one list's views replaced: this list first, an emptied list removed, the others kept. */
export function withSavedViews(preference: unknown, key: string, views: SavedView[]): Record<string, SavedView[]> {
  const current = preference && typeof preference === "object" && !Array.isArray(preference) ? preference as Record<string, unknown> : {};
  const others = Object.keys(current).filter((other) => other !== key)
    .map((other) => [other, savedViewsOf(current, other)] as const).filter(([, list]) => list.length);
  const kept = views.slice(0, SAVED_VIEWS_PER_LIST);
  return Object.fromEntries([...(kept.length ? [[key, kept] as const] : []), ...others].slice(0, SAVED_VIEWS_LISTS));
}

/** The day key (yyyy-mm-dd in ``timeZone``) a timeline item falls on, or null when it has no readable date. */
export function dayKey(value: string | null | undefined, timeZone: string): string | null {
  if (!value) return null;
  // A date with no time is that day wherever the reader is.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const at = Date.parse(value);
  if (Number.isNaN(at)) return null;
  try {
    const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(at));
    const part = (type: string) => parts.find((item) => item.type === type)?.value || "";
    return `${part("year")}-${part("month")}-${part("day")}`;
  } catch {
    return new Date(at).toISOString().slice(0, 10);
  }
}

function instant(value: string | null | undefined): number {
  if (!value) return NaN;
  return Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
}

/** A timeline: the dated items by day, days and items in time order; undated ones apart at the end. */
export function timelineDays<T extends { time?: { start: string | null } }>(items: T[], timeZone: string) {
  const dated = items.filter((item) => !Number.isNaN(instant(item.time?.start)))
    .sort((left, right) => instant(left.time?.start) - instant(right.time?.start));
  const days = new Map<string, T[]>();
  for (const item of dated) {
    const key = dayKey(item.time!.start, timeZone)!;
    days.set(key, [...(days.get(key) || []), item]);
  }
  return { days: [...days.entries()].map(([day, members]) => ({ day, items: members })), undated: items.filter((item) => Number.isNaN(instant(item.time?.start))) };
}

/** A gantt's bars, each a share of the span from the first start to the last end; an item with no start has no bar.
 *  A bar with no end, or one that ends before it starts, is its start alone. Links join each bar to the bars it waits for. */
export function ganttLayout<T extends { id: string; time?: { start: string | null; end?: string | null; depends_on?: string[] } }>(items: T[]) {
  const timed = items.map((item) => {
    const start = instant(item.time?.start);
    const end = instant(item.time?.end);
    return { item, start, end: Number.isNaN(end) || end < start ? start : end };
  }).filter((row) => !Number.isNaN(row.start)).sort((left, right) => left.start - right.start || left.end - right.end);
  const first = timed.length ? Math.min(...timed.map((row) => row.start)) : 0;
  const last = timed.length ? Math.max(...timed.map((row) => row.end)) : 0;
  const span = Math.max(last - first, 86_400_000);
  const bars = timed.map((row) => ({ item: row.item, start: row.start, end: row.end,
    left: (row.start - first) / span, width: Math.max((row.end - row.start) / span, 0.01) }));
  const index = new Map(bars.map((bar, position) => [bar.item.id, position]));
  const links = bars.flatMap((bar, to) => (bar.item.time?.depends_on || []).filter((id) => index.has(id)).map((id) => ({ from: index.get(id)!, to })));
  return { first, last: first + span, bars, links, untimed: items.filter((item) => Number.isNaN(instant(item.time?.start))) };
}

/** Places: the items grouped by where they are (the same address in any case or spacing is one place), largest first;
 *  items with no address are left apart. */
export function placeGroups<T extends { place?: { address: string | null; name?: string; lat?: number; lng?: number } }>(items: T[]) {
  const groups = new Map<string, { address: string; name?: string; lat?: number; lng?: number; items: T[] }>();
  const unplaced: T[] = [];
  for (const item of items) {
    const place = item.place;
    const address = place?.address?.trim();
    const coordinates = typeof place?.lat === "number" && typeof place?.lng === "number" ? `${place.lat.toFixed(4)},${place.lng.toFixed(4)}` : "";
    const key = (address || "").toLowerCase().replace(/[\s,，.。]+/g, " ").trim() || coordinates;
    if (!key) { unplaced.push(item); continue; }
    const group = groups.get(key) || { address: address || coordinates, name: place?.name, lat: place?.lat, lng: place?.lng, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  return { places: [...groups.values()].sort((left, right) => right.items.length - left.items.length), unplaced };
}

/** A maps search for a place: by its coordinates when it has them, else its address (Amap for a Chinese reader). */
export function placeMapHref(place: { address: string; lat?: number; lng?: number }, locale: string): string {
  const chinese = /^zh\b/i.test(locale);
  if (typeof place.lat === "number" && typeof place.lng === "number") {
    return chinese ? `https://uri.amap.com/marker?position=${place.lng},${place.lat}`
      : `https://www.google.com/maps/search/?api=1&query=${place.lat},${place.lng}`;
  }
  return chinese ? `https://uri.amap.com/search?keyword=${encodeURIComponent(place.address)}`
    : `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(place.address)}`;
}

/** How long a bar may be in a table column: its share of the largest number the column holds. */
export function barShare(value: unknown, values: readonly unknown[]): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const largest = Math.max(0, ...values.map((each) => typeof each === "number" && Number.isFinite(each) ? Math.abs(each) : 0));
  return largest > 0 ? Math.min(1, Math.abs(value) / largest) : 0;
}

/** Sum, average, lowest or highest of the numbers a set of rows holds (the ticked rows' totals). */
export function aggregate(values: readonly unknown[], agg: "sum" | "avg" | "min" | "max"): number | null {
  const numbers = values.filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (!numbers.length) return null;
  if (agg === "sum") return numbers.reduce((sum, value) => sum + value, 0);
  if (agg === "avg") return numbers.reduce((sum, value) => sum + value, 0) / numbers.length;
  return agg === "min" ? Math.min(...numbers) : Math.max(...numbers);
}

/** Which rows of a long list to draw: those on screen (``top`` is how far the list's top is above the screen's top,
 *  ``viewport`` the screen's height) and ``overscan`` more each side, given the rows' average height. */
export function windowRange({ top, viewport, average, count, overscan = 20 }: { top: number; viewport: number; average: number; count: number; overscan?: number }) {
  const row = Math.max(1, average);
  const first = Math.floor(Math.max(0, top) / row);
  const last = Math.ceil((Math.max(0, top) + Math.max(0, viewport)) / row);
  const start = Math.max(0, Math.min(count, first - overscan));
  const end = Math.max(start, Math.min(count, last + overscan));
  return { start, end, before: start * row, after: (count - end) * row };
}
