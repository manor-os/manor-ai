/**
 * How an App block presents media, as pure functions: frames and shapes, size labels, version groups,
 * captions and transcripts, chapters and an edit's timeline, and what a file action fills its form with.
 */

/** The frames an App's media takes; ``auto`` is each file's own shape. */
export type WorkspaceAppMediaAspect = "auto" | "9:16" | "4:5" | "3:4" | "1:1" | "4:3" | "16:9";

/** A frame as CSS ``aspect-ratio`` ("9 / 16"); ``auto`` and anything unknown leave the file its own shape. */
export function aspectRatioCss(aspect?: string | null): string | undefined {
  const match = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(aspect || "");
  return match && Number(match[1]) > 0 && Number(match[2]) > 0 ? `${match[1]} / ${match[2]}` : undefined;
}

// The shapes a person names a version by; a file within 3 % of one is that shape.
const NAMED_SHAPES: Array<[string, number]> = [
  ["9:16", 9 / 16], ["2:3", 2 / 3], ["3:4", 3 / 4], ["4:5", 4 / 5], ["1:1", 1], ["5:4", 5 / 4], ["4:3", 4 / 3],
  ["3:2", 3 / 2], ["16:9", 16 / 9], ["21:9", 21 / 9],
];

/** What shape a picture or clip is ("9:16", "1:1"), from its pixels or a declared ratio; null when unknown. */
export function shapeLabel(width?: number | null, height?: number | null, declared?: string | null): string | null {
  const ratio = width && height && width > 0 && height > 0 ? width / height : (() => {
    const match = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(declared || "");
    return match && Number(match[2]) > 0 ? Number(match[1]) / Number(match[2]) : 0;
  })();
  if (!ratio) return null;
  const named = NAMED_SHAPES.find(([, value]) => Math.abs(ratio / value - 1) <= 0.03);
  if (named) return named[0];
  return ratio >= 1 ? `${(Math.round(ratio * 100) / 100).toString()}:1` : `1:${(Math.round(100 / ratio) / 100).toString()}`;
}

/** A picture's pixel size as a poster label ("1080×1350"); null unless both sides are known. */
export function pixelSizeLabel(width?: number | null, height?: number | null): string | null {
  return width && height && width > 0 && height > 0 ? `${Math.round(width)}×${Math.round(height)}` : null;
}

/** Whether a picture may move (GIF, WebP, APNG): its viewer and an animated tile load the file itself. */
export function mayAnimate(mimeType?: string | null): boolean {
  return /^image\/(?:gif|webp|apng)$/i.test(String(mimeType || ""));
}

/** A moment on a player as a clock: "0:07", "1:02:03". */
export function clockLabel(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = String(whole % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

/** Items of a gallery whose ``group`` (one record or result) is the same, in their first item's order. */
export function mediaGroups<T extends { id: string; group?: string | null }>(items: T[]): Array<{ key: string; items: T[] }> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = item.group || item.id;
    groups.set(key, [...(groups.get(key) || []), item]);
  }
  return [...groups].map(([key, members]) => ({ key, items: members }));
}

export type MediaCue = { start: number; end: number; text: string; speaker?: string };

function cueTime(text: string): number | null {
  const match = /^(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})$/.exec(text.trim());
  if (!match) return null;
  return Number(match[1] || 0) * 3600 + Number(match[2]) * 60 + Number(match[3]) + Number(match[4].padEnd(3, "0")) / 1000;
}

/** The cues of a WebVTT or SubRip captions file, in time order; anything else reads as none. */
export function parseCaptions(source: string): MediaCue[] {
  const cues: MediaCue[] = [];
  for (const block of source.replace(/\r\n?/g, "\n").split(/\n{2,}/)) {
    const lines = block.split("\n").filter((line) => line.trim());
    const timing = lines.findIndex((line) => line.includes("-->"));
    if (timing < 0) continue;
    const [from, rest] = lines[timing].split("-->");
    const start = cueTime(from);
    const end = cueTime((rest || "").trim().split(/\s+/)[0] || "");
    const text = lines.slice(timing + 1).join("\n").replace(/<[^>]+>/g, "").trim();
    if (start === null || end === null || end <= start || !text) continue;
    // A WebVTT voice tag (<v Ana>) names who speaks.
    const voice = /<v(?:\.[^\s>]+)?\s+([^>]+)>/.exec(lines.slice(timing + 1).join(" "));
    cues.push({ start, end, text, ...(voice ? { speaker: voice[1].trim() } : {}) });
  }
  return cues.sort((left, right) => left.start - right.start);
}

/** A captions file as WebVTT, which a video's ``<track>`` plays: SubRip becomes WebVTT, WebVTT stays. */
export function captionsAsVtt(source: string): string {
  if (/^﻿?WEBVTT/.test(source)) return source;
  const stamp = (seconds: number) => {
    const millis = Math.round(seconds * 1000);
    const hours = Math.floor(millis / 3_600_000);
    const minutes = Math.floor((millis % 3_600_000) / 60_000);
    const secs = Math.floor((millis % 60_000) / 1000);
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}.${String(millis % 1000).padStart(3, "0")}`;
  };
  return `WEBVTT\n\n${parseCaptions(source).map((cue) => `${stamp(cue.start)} --> ${stamp(cue.end)}\n${cue.text}`).join("\n\n")}\n`;
}

/** The line being said at ``time``: the last one started, unless it has ended; -1 before the first. */
export function activeCueIndex(cues: Array<{ start: number; end?: number }>, time: number): number {
  let found = -1;
  for (let index = 0; index < cues.length && cues[index].start <= time; index += 1) found = index;
  if (found >= 0 && cues[found].end !== undefined && time > cues[found].end! + 0.25) {
    const next = cues[found + 1];
    return next && next.start <= time ? found + 1 : -1;
  }
  return found;
}

export type TimelineSegment = { start: number; end: number; label?: string; track?: string };

/** An edit laid out in time: a lane per track (in first-seen order), each segment as a share of the whole. */
export function timelineLanes(segments: TimelineSegment[]): { total: number; lanes: Array<{ track: string; segments: Array<TimelineSegment & { left: number; width: number }> }> } {
  const total = Math.max(0, ...segments.map((segment) => segment.end));
  const lanes = new Map<string, Array<TimelineSegment & { left: number; width: number }>>();
  if (!total) return { total: 0, lanes: [] };
  for (const segment of segments) {
    const track = segment.track || "";
    lanes.set(track, [...(lanes.get(track) || []), {
      ...segment, left: segment.start / total, width: Math.max(0.004, (segment.end - segment.start) / total),
    }]);
  }
  return { total, lanes: [...lanes].map(([track, members]) => ({ track, segments: members })) };
}

/** What a file action opens its entry with: the file, and the moment or span picked on its player. */
export function fileActionValues(action: { input: string; time_input?: string; start_input?: string; end_input?: string },
  file: { document_id: string; label?: string | null }, moment: { time?: number; start?: number; end?: number } = {}): Record<string, unknown> {
  const seconds = (value?: number) => (typeof value === "number" && Number.isFinite(value) ? Math.round(value * 100) / 100 : undefined);
  const values: Record<string, unknown> = { [action.input]: [{ document_id: file.document_id, ...(file.label ? { label: file.label } : {}) }] };
  if (action.time_input && seconds(moment.time) !== undefined) values[action.time_input] = seconds(moment.time);
  if (action.start_input && action.end_input && seconds(moment.start) !== undefined && seconds(moment.end) !== undefined
    && moment.end! > moment.start!) {
    values[action.start_input] = seconds(moment.start);
    values[action.end_input] = seconds(moment.end);
  }
  return values;
}

/** A comment's place on a picture (a box, as shares of its width and height) or on a player (a moment). */
export type MediaCommentAnchor = { type: "media_region"; x: number; y: number; w: number; h: number; label: string }
  | { type: "media_time"; time: number; label: string };

/** A box drawn from one corner to the other, as shares of the picture, kept inside it; null when it is a click. */
export function regionFromDrag(from: { x: number; y: number }, to: { x: number; y: number }): { x: number; y: number; w: number; h: number } | null {
  const clamp = (value: number) => Math.min(1, Math.max(0, value));
  const x = clamp(Math.min(from.x, to.x));
  const y = clamp(Math.min(from.y, to.y));
  const w = clamp(Math.max(from.x, to.x)) - x;
  const h = clamp(Math.max(from.y, to.y)) - y;
  return w < 0.01 || h < 0.01 ? null : { x: round(x), y: round(y), w: round(w), h: round(h) };
}

function round(value: number): number {
  return Math.round(value * 1000) / 1000;
}

/** A comment anchor this page placed (a box or a moment), or null for any other comment. */
export function mediaAnchor(anchor: unknown): MediaCommentAnchor | null {
  if (!anchor || typeof anchor !== "object") return null;
  const value = anchor as Record<string, unknown>;
  const share = (key: string) => typeof value[key] === "number" && (value[key] as number) >= 0 && (value[key] as number) <= 1;
  if (value.type === "media_region" && ["x", "y", "w", "h"].every(share)) {
    return { type: "media_region", x: value.x as number, y: value.y as number, w: value.w as number, h: value.h as number, label: String(value.label || "") };
  }
  if (value.type === "media_time" && typeof value.time === "number" && value.time >= 0) {
    return { type: "media_time", time: value.time, label: String(value.label || clockLabel(value.time)) };
  }
  return null;
}
