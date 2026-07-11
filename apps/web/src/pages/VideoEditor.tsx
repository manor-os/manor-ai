import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  CSSProperties,
  ChangeEvent,
  DragEvent as ReactDragEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { formatFileSize } from "../lib/format";
import type { Document, DocumentFolderInfo } from "../lib/types";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import InfoPopover from "../components/ui/InfoPopover";
import Modal from "../components/ui/Modal";
import Select from "../components/ui/Select";
import AiEditButton from "../components/ui/AiEditButton";
import {
  IconArrowLeft,
  IconCheck,
  IconChevronLeft,
  IconChevronRight,
  IconClock,
  IconCopy,
  IconDocument,
  IconDownload,
  IconDragHandle,
  IconEdit,
  IconEye,
  IconEyeOff,
  IconFolder,
  IconLock,
  IconPause,
  IconPlay,
  IconPlus,
  IconRedo,
  IconRefresh,
  IconSearch,
  IconSparkles,
  IconStop,
  IconText,
  IconTrash,
  IconUndo,
  IconUpload,
} from "../components/icons";
import { useToastStore } from "../stores/toast";
import { openEditorLiveChat } from "../lib/editorLiveChat";

type ClipSegment = {
  id: string;
  label: string;
  sourceStart: number;
  sourceEnd: number;
  muted: boolean;
  color: string;
  assetDocumentId?: string | null;
  assetName?: string | null;
  assetMimeType?: string | null;
  assetDuration?: number | null;
  replacementPrompt?: string | null;
  editNotes?: string | null;
};

type CaptionCue = {
  id: string;
  speaker?: string | null;
  emotion?: string | null;
  style: "subtitle" | "speechBubble" | "narrationBox";
  text: string;
  start: number;
  end: number;
  x: number;
  y: number;
  size: number;
  color: string;
  background: string;
  backgroundColor: string;
  backgroundOpacity: number;
  align: CanvasTextAlign;
};

type ShotBeat = {
  id: string;
  title: string;
  scene: string;
  shot: string;
  start: number;
  end: number;
  location: string;
  camera: string;
  action: string;
  dialogue: string;
  notes: string;
};

type AudioCueType = "dialogue" | "narration" | "music" | "ambience" | "sfx";

type AudioCue = {
  id: string;
  type: AudioCueType;
  label: string;
  start: number;
  end: number;
  volumeDb: number;
  fadeIn: number;
  fadeOut: number;
  loop: boolean;
  duckUnderDialogue: boolean;
  muted: boolean;
  assetDocumentId?: string | null;
  assetName?: string | null;
  assetMimeType?: string | null;
  sourcePlan: string;
  prompt: string;
};

type TimelineMarker = {
  id: string;
  time: number;
  label: string;
  color: string;
  notes: string;
};

type Selection =
  | { type: "clip"; id: string }
  | { type: "shot"; id: string }
  | { type: "caption"; id: string }
  | { type: "audio"; id: string }
  | { type: "marker"; id: string }
  | null;

type TimelineMap = {
  clip: ClipSegment;
  index: number;
  timelineStart: number;
  timelineEnd: number;
  sourceTime: number;
};

type ClipTimelineSpan = {
  clip: ClipSegment;
  index: number;
  start: number;
  end: number;
  duration: number;
};

type TimedTrackItem = {
  start: number;
  end: number;
};

type EditorTrackState = {
  clips: ClipSegment[];
  shotBeats: ShotBeat[];
  captions: CaptionCue[];
  audioCues: AudioCue[];
  markers: TimelineMarker[];
};

type EditorHistoryTransaction = {
  before: EditorTrackState;
  closing: boolean;
};

type TimelineTrackId = "markers" | "shots" | "video" | "captions" | "audio";

type TimelineTrackState = {
  locked: boolean;
  muted: boolean;
  visible: boolean;
};

type WorkAreaState = {
  enabled: boolean;
  start: number;
  end: number;
};

type ClipReorderResult = {
  clips: ClipSegment[];
  shotBeats: ShotBeat[];
  captions: CaptionCue[];
  audioCues: AudioCue[];
  markers: TimelineMarker[];
};

type ClipDropPreview = {
  time: number;
  index: number;
};

type PreviewAudioEntry = {
  audio: HTMLAudioElement;
  url: string;
  generated?: boolean;
};

type MediaSourceTab = "project" | "knowledge";
type MediaKindFilter = "all" | "video" | "audio" | "project";

type RenderIssue = {
  id: string;
  tone: "blocker" | "warning" | "info";
  label: string;
  detail: string;
};

type VideoEditRecipe = {
  kind?: string;
  version?: number;
  source_document?: {
    id?: string | null;
    name?: string | null;
    folder_id?: string | null;
    fs_path?: string | null;
    mime_type?: string | null;
  };
  canvas?: {
    width?: number;
    height?: number;
  };
  timeline?: {
    duration?: number;
    clips?: Partial<ClipSegment>[];
    shots?: Partial<ShotBeat>[];
    captions?: Partial<CaptionCue>[];
    audio_cues?: Partial<AudioCue>[];
    markers?: Partial<TimelineMarker>[];
  };
  manual_edits?: {
    clip_id: string;
    label: string;
    timeline_start: number;
    timeline_end: number;
    source_start: number;
    source_end: number;
    replacement_document?: {
      id?: string | null;
      name?: string | null;
      mime_type?: string | null;
      duration?: number | null;
    } | null;
    replacement_prompt?: string | null;
    edit_notes?: string | null;
  }[];
  editor_settings?: {
    track_states?: Partial<Record<TimelineTrackId, Partial<TimelineTrackState>>>;
    work_area?: Partial<WorkAreaState>;
  };
};

type BuildRecipeOptions = {
  finalDocument?: Document | null;
  createdBy?: string;
};

type NormalizedVideoEditRecipe = {
  mediaSize: { width: number; height: number } | null;
  trackStates: Record<TimelineTrackId, TimelineTrackState>;
  workArea: WorkAreaState;
  state: EditorTrackState;
  duration: number;
};

type AiEditFocus = {
  selection: NonNullable<Selection>;
  time: number;
};

type AiEditNotice = {
  id: string;
  title: string;
  detail: string;
  highlights: NonNullable<Selection>[];
  focus: AiEditFocus | null;
};

const CLIP_COLORS = ["#436b65", "#4869ac", "#9333ea", "#b66a3c", "#be123c"];
const MARKER_COLORS = ["#cf9b44", "#5f928a", "#5f84bd", "#a07fc0", "#d65f59"];
const MEDIA_DRAG_MIME = "application/x-manor-video-editor-media";
const AUDIO_TYPE_LABELS: Record<AudioCueType, string> = {
  dialogue: "Dialogue",
  narration: "Narration",
  music: "Music bed",
  ambience: "Ambience",
  sfx: "SFX",
};
const AUDIO_TYPE_COLORS: Record<AudioCueType, string> = {
  dialogue: "#436b65",
  narration: "#6f4ba8",
  music: "#4869ac",
  ambience: "#4f7e87",
  sfx: "#c14a44",
};
const DEFAULT_TIMELINE_TRACK_STATES: Record<TimelineTrackId, TimelineTrackState> = {
  markers: { locked: false, muted: false, visible: true },
  shots: { locked: false, muted: false, visible: true },
  video: { locked: false, muted: false, visible: true },
  captions: { locked: false, muted: false, visible: true },
  audio: { locked: false, muted: false, visible: true },
};
const CAPTION_STYLE_LABELS: Record<CaptionCue["style"], string> = {
  subtitle: "Subtitle",
  speechBubble: "Speech bubble",
  narrationBox: "Narration box",
};
const VIDEO_EXTENSIONS = new Set(["mp4", "webm", "mov", "avi", "mkv", "ogg"]);
const AUDIO_EXTENSIONS = new Set(["mp3", "wav", "ogg", "aac", "flac", "m4a", "wma"]);
const PLAYBACK_BOUNDARY_EPSILON = 0.001;
const PLAYBACK_CLOCK_STALE_MS = 750;
const PREVIEW_END_FRAME_EPSILON = 0.05;
const TIMELINE_LABEL_COLUMN_WIDTH = 144;
const TIMELINE_PLAYHEAD_HITBOX_WIDTH = 28;
const MIN_TIMELINE_LANE_WIDTH = 560;
const MIN_USABLE_MEDIA_ASSET_BYTES = 8 * 1024;
const mediaThumbnailUrlCache = new Map<string, string>();
const mediaThumbnailFailureCache = new Set<string>();
const mediaThumbnailInflight = new Map<string, Promise<string>>();
const mediaVideoFrameUrlCache = new Map<string, string>();
const mediaVideoFrameFailureCache = new Set<string>();
const mediaVideoFrameInflight = new Map<string, Promise<string>>();
const mediaVideoPreviewUrlCache = new Map<string, string>();
const mediaVideoPreviewFailureCache = new Set<string>();
const mediaVideoPreviewInflight = new Map<string, Promise<string>>();
const generatedAudioPreviewUrlCache = new Map<AudioCueType, string>();
const GENERATED_AUDIO_PREVIEW_SAMPLE_RATE = 44100;
const GENERATED_AUDIO_PREVIEW_SECONDS = 4;
const TRACK_HELP_KEYS: Record<TimelineTrackId, { titleKey: string; bodyKey: string; itemKeys: string[] }> = {
  markers: {
    titleKey: "help.track.markers.title",
    bodyKey: "help.track.markers.body",
    itemKeys: ["help.track.markers.item1", "help.track.markers.item2"],
  },
  shots: {
    titleKey: "help.track.shots.title",
    bodyKey: "help.track.shots.body",
    itemKeys: ["help.track.shots.item1", "help.track.shots.item2"],
  },
  video: {
    titleKey: "help.track.video.title",
    bodyKey: "help.track.video.body",
    itemKeys: ["help.track.video.item1", "help.track.video.item2", "help.track.video.item3"],
  },
  captions: {
    titleKey: "help.track.captions.title",
    bodyKey: "help.track.captions.body",
    itemKeys: ["help.track.captions.item1", "help.track.captions.item2"],
  },
  audio: {
    titleKey: "help.track.audio.title",
    bodyKey: "help.track.audio.body",
    itemKeys: ["help.track.audio.item1", "help.track.audio.item2", "help.track.audio.item3"],
  },
};

function veText(key: string, vars?: Record<string, string | number>): string {
  return t(`page.video_editor.${key}`, vars);
}

function VideoEditorHelp({
  titleKey,
  bodyKey,
  itemKeys = [],
  align = "right",
}: {
  titleKey: string;
  bodyKey: string;
  itemKeys?: string[];
  align?: "right" | "left";
}) {
  return (
    <span className="ve-help-anchor">
      <InfoPopover ariaLabel={veText("help.aria")} align={align} width={300} size={13}>
        <div className="ve-help-popover">
          <strong>{veText(titleKey)}</strong>
          <p>{veText(bodyKey)}</p>
          {itemKeys.length > 0 && (
            <ul>
              {itemKeys.map((key) => (
                <li key={key}>{veText(key)}</li>
              ))}
            </ul>
          )}
        </div>
      </InfoPopover>
    </span>
  );
}

function audioTypeDisplayLabel(type: AudioCueType): string {
  return veText(`audio_type.${type}`);
}

function captionStyleDisplayLabel(style: CaptionCue["style"]): string {
  return veText(`caption_style.${style}`);
}

function makeId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 9)}`;
}

function baseName(name: string): string {
  return name.replace(/\.[^.]+$/, "") || "video";
}

function clamp(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, value));
}

function numberOr(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function createDefaultTimelineTrackStates(): Record<TimelineTrackId, TimelineTrackState> {
  return {
    markers: { ...DEFAULT_TIMELINE_TRACK_STATES.markers },
    shots: { ...DEFAULT_TIMELINE_TRACK_STATES.shots },
    video: { ...DEFAULT_TIMELINE_TRACK_STATES.video },
    captions: { ...DEFAULT_TIMELINE_TRACK_STATES.captions },
    audio: { ...DEFAULT_TIMELINE_TRACK_STATES.audio },
  };
}

function normalizeTimelineTrackStates(
  value: Partial<Record<TimelineTrackId, Partial<TimelineTrackState>>> | undefined,
): Record<TimelineTrackId, TimelineTrackState> {
  const defaults = createDefaultTimelineTrackStates();
  const source = (value && typeof value === "object" ? value : {}) as Partial<Record<TimelineTrackId, Partial<TimelineTrackState>>>;
  (Object.keys(defaults) as TimelineTrackId[]).forEach((track) => {
    const next = source[track];
    if (!next || typeof next !== "object") return;
    defaults[track] = {
      locked: typeof next.locked === "boolean" ? next.locked : defaults[track].locked,
      muted: typeof next.muted === "boolean" ? next.muted : defaults[track].muted,
      visible: typeof next.visible === "boolean" ? next.visible : defaults[track].visible,
    };
  });
  return defaults;
}

function createDefaultWorkArea(duration = 0): WorkAreaState {
  return { enabled: false, start: 0, end: Math.max(0, duration) };
}

function normalizeWorkArea(value: Partial<WorkAreaState> | undefined, duration: number): WorkAreaState {
  const safeDuration = Math.max(0, duration);
  if (safeDuration <= 0) return createDefaultWorkArea(0);
  const start = clamp(numberOr(value?.start, 0), 0, Math.max(0, safeDuration - 0.05));
  const end = clamp(numberOr(value?.end, safeDuration), start + 0.05, safeDuration);
  return {
    enabled: Boolean(value?.enabled),
    start,
    end,
  };
}

function trackForSelectionType(type: NonNullable<Selection>["type"]): TimelineTrackId {
  if (type === "marker") return "markers";
  if (type === "clip") return "video";
  if (type === "shot") return "shots";
  if (type === "caption") return "captions";
  return "audio";
}

function dbToGain(db: number): number {
  return Math.pow(10, db / 20);
}

function hexToRgba(hex: string, opacity: number): string {
  const normalized = hex.trim();
  const short = normalized.match(/^#([0-9a-f]{3})$/i);
  const long = normalized.match(/^#([0-9a-f]{6})$/i);
  const value = long?.[1] ?? short?.[1]?.split("").map((char) => `${char}${char}`).join("");
  if (!value) return normalized;
  const red = Number.parseInt(value.slice(0, 2), 16);
  const green = Number.parseInt(value.slice(2, 4), 16);
  const blue = Number.parseInt(value.slice(4, 6), 16);
  return `rgba(${red},${green},${blue},${clamp(opacity, 0, 1).toFixed(2)})`;
}

function defaultAudioFade(type: AudioCueType): number {
  return type === "music" || type === "ambience" ? 0.5 : 0;
}

function defaultAudioLoop(type: AudioCueType): boolean {
  return type === "music" || type === "ambience";
}

function defaultDuckUnderDialogue(type: AudioCueType): boolean {
  return type === "music" || type === "ambience";
}

function defaultAudioVolumeDb(type: AudioCueType): number {
  if (type === "dialogue" || type === "narration") return -3;
  if (type === "sfx") return -7;
  return -10;
}

function inferAudioCueType(name: string): AudioCueType {
  const lowerName = name.toLowerCase();
  if (lowerName.includes("dialogue") || lowerName.includes("voice") || lowerName.includes("line") || lowerName.includes("tts")) return "dialogue";
  if (lowerName.includes("sfx") || lowerName.includes("effect") || lowerName.includes("impact") || lowerName.includes("hit")) return "sfx";
  if (lowerName.includes("ambience") || lowerName.includes("ambient") || lowerName.includes("room") || lowerName.includes("wind") || lowerName.includes("crowd")) return "ambience";
  return "music";
}

function formatTime(seconds: number): string {
  const safe = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  const minutes = Math.floor(safe / 60);
  const secs = safe - minutes * 60;
  return `${minutes}:${secs.toFixed(2).padStart(5, "0")}`;
}

function getClipMaxDuration(clip: ClipSegment, sourceDuration: number): number {
  return clip.assetDuration && clip.assetDuration > 0 ? clip.assetDuration : sourceDuration;
}

function clipHasManualEdit(clip: ClipSegment, sourceDuration: number): boolean {
  const maxDuration = getClipMaxDuration(clip, sourceDuration);
  const hasTrim = maxDuration > 0
    ? clip.sourceStart > 0.001 || Math.abs(clip.sourceEnd - maxDuration) > 0.001
    : clip.sourceStart > 0.001;
  return Boolean(
    hasTrim ||
    clip.muted ||
    clip.assetDocumentId ||
    clip.replacementPrompt?.trim() ||
    clip.editNotes?.trim()
  );
}

function parseSubtitleTimestamp(value: string): number {
  const match = value.trim().replace(",", ".").match(/^(?:(\d+):)?(\d{1,2}):(\d{1,2})(?:\.(\d{1,3}))?$/);
  if (!match) return 0;
  const hours = Number(match[1] || 0);
  const minutes = Number(match[2] || 0);
  const seconds = Number(match[3] || 0);
  const millis = Number((match[4] || "0").padEnd(3, "0").slice(0, 3));
  return hours * 3600 + minutes * 60 + seconds + millis / 1000;
}

function formatSubtitleTimestamp(seconds: number): string {
  const totalMillis = Math.max(0, Math.round(seconds * 1000));
  const hours = Math.floor(totalMillis / 3600000);
  const minutes = Math.floor((totalMillis % 3600000) / 60000);
  const secs = Math.floor((totalMillis % 60000) / 1000);
  const millis = totalMillis % 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}

function parseSrtCaptions(content: string, duration: number): CaptionCue[] {
  return content
    .replace(/^\uFEFF/, "")
    .replace(/\r/g, "")
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block): CaptionCue | null => {
      const lines = block.split("\n").map((line) => line.trim());
      const timeIndex = lines.findIndex((line) => line.includes("-->"));
      if (timeIndex < 0) return null;
      const [rawStart, rawEnd] = lines[timeIndex].split("-->").map((part) => part.trim().split(/\s+/)[0]);
      const start = clamp(parseSubtitleTimestamp(rawStart || "0:00:00,000"), 0, duration);
      const end = clamp(parseSubtitleTimestamp(rawEnd || "0:00:02,000"), start + 0.05, Math.max(start + 0.05, duration));
      const text = lines.slice(timeIndex + 1).join("\n").trim();
      if (!text) return null;
      return {
        id: makeId("caption"),
        speaker: null,
        emotion: null,
        style: "subtitle" as const,
        text,
        start,
        end,
        x: 50,
        y: 84,
        size: 32,
        color: "#ffffff",
        background: "rgba(28,25,23,0.72)",
        backgroundColor: "#1c1917",
        backgroundOpacity: 0.72,
        align: "center" as CanvasTextAlign,
      };
    })
    .filter((caption): caption is CaptionCue => Boolean(caption));
}

function captionsToSrt(captions: CaptionCue[]): string {
  return [...captions]
    .sort((a, b) => a.start - b.start)
    .map((caption, index) => [
      String(index + 1),
      `${formatSubtitleTimestamp(caption.start)} --> ${formatSubtitleTimestamp(caption.end)}`,
      caption.text,
    ].join("\n"))
    .join("\n\n")
    .concat("\n");
}

function captionDisplay(caption: CaptionCue): { color: string; background: string } {
  const opacity = Number.isFinite(caption.backgroundOpacity) ? clamp(caption.backgroundOpacity, 0, 1) : null;
  if (caption.style === "speechBubble") {
    return {
      color: caption.color === "#ffffff" ? "#1c1917" : caption.color,
      background: hexToRgba(caption.backgroundColor || "#ffffff", opacity ?? 0.94),
    };
  }
  if (caption.style === "narrationBox") {
    return {
      color: caption.color,
      background: hexToRgba(caption.backgroundColor || "#1c1917", opacity ?? 0.86),
    };
  }
  return {
    color: caption.color,
    background: caption.backgroundColor ? hexToRgba(caption.backgroundColor, opacity ?? 0.72) : caption.background,
  };
}

function isVideoDocument(doc: Document | undefined): doc is Document {
  if (!doc) return false;
  const ext = (doc.name || "").split(".").pop()?.toLowerCase() || "";
  const mime = doc.mime_type || doc.file_type || "";
  return VIDEO_EXTENSIONS.has(ext) || mime.startsWith("video/");
}

function isAudioDocument(doc: Document | undefined): doc is Document {
  if (!doc) return false;
  const ext = (doc.name || "").split(".").pop()?.toLowerCase() || "";
  const mime = doc.mime_type || doc.file_type || "";
  return AUDIO_EXTENSIONS.has(ext) || mime.startsWith("audio/");
}

function projectAssetKind(doc: Document): "video" | "audio" | "project" | null {
  if (isVideoEditRecipeDocument(doc)) return "project";
  if (
    typeof doc.file_size === "number"
    && doc.file_size > 0
    && doc.file_size < MIN_USABLE_MEDIA_ASSET_BYTES
    && (isVideoDocument(doc) || isAudioDocument(doc))
  ) {
    return null;
  }
  if (isVideoDocument(doc)) return "video";
  if (isAudioDocument(doc)) return "audio";
  return null;
}

function preferredMediaFilter(
  counts: Record<MediaKindFilter, number>,
  current: MediaKindFilter,
): MediaKindFilter {
  if (current !== "all" && counts[current] > 0) return current;
  if (counts.video > 0) return "video";
  if (counts.audio > 0) return "audio";
  if (counts.project > 0) return "project";
  return "all";
}

function mediaAssetMatchesFilter(doc: Document, filter: MediaKindFilter): boolean {
  const kind = projectAssetKind(doc);
  return Boolean(kind && (filter === "all" || kind === filter));
}

function mediaAssetMatchesSearch(doc: Document, search: string): boolean {
  if (!search) return true;
  return doc.name.toLowerCase().includes(search.toLowerCase());
}

function countMediaKinds(docs: Document[]): Record<MediaKindFilter, number> {
  return docs.reduce<Record<MediaKindFilter, number>>((counts, doc) => {
    const kind = projectAssetKind(doc);
    if (!kind) return counts;
    counts.all += 1;
    counts[kind] += 1;
    return counts;
  }, { all: 0, video: 0, audio: 0, project: 0 });
}

function loadMediaThumbnailUrl(assetId: string): Promise<string> {
  const cachedUrl = mediaThumbnailUrlCache.get(assetId);
  if (cachedUrl) return Promise.resolve(cachedUrl);
  if (mediaThumbnailFailureCache.has(assetId)) return Promise.reject(new Error("Thumbnail unavailable"));
  const inflight = mediaThumbnailInflight.get(assetId);
  if (inflight) return inflight;

  const request = api.documents.videoThumbnail(assetId, { cache: true })
    .then((url) => {
      mediaThumbnailUrlCache.set(assetId, url);
      return url;
    })
    .catch((error) => {
      mediaThumbnailFailureCache.add(assetId);
      throw error;
    })
    .finally(() => {
      mediaThumbnailInflight.delete(assetId);
    });
  mediaThumbnailInflight.set(assetId, request);
  return request;
}

function waitForMediaElement(
  element: HTMLMediaElement,
  events: string[],
  timeoutMs = 8000,
): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false;
    let timer: number | null = null;
    const cleanup = () => {
      if (timer !== null) window.clearTimeout(timer);
      events.forEach((eventName) => element.removeEventListener(eventName, handleSuccess));
      element.removeEventListener("error", handleError);
    };
    const finish = (callback: () => void) => {
      if (settled) return;
      settled = true;
      cleanup();
      callback();
    };
    const handleSuccess = () => finish(resolve);
    const handleError = () => finish(() => reject(new Error("Media preview failed")));
    events.forEach((eventName) => element.addEventListener(eventName, handleSuccess, { once: true }));
    element.addEventListener("error", handleError, { once: true });
    timer = window.setTimeout(() => finish(() => reject(new Error("Media preview timed out"))), timeoutMs);
  });
}

function clearMediaElementSource(element: HTMLMediaElement | null | undefined, url?: string) {
  if (!element) return;
  const current = element.currentSrc || element.src;
  if (!url || current === url || current.startsWith(`${url}#`)) {
    element.pause();
    element.removeAttribute("src");
    element.load();
  }
}

function revokeObjectUrlSoon(url: string, delayMs = 15000) {
  if (!url.startsWith("blob:")) return;
  window.setTimeout(() => {
    URL.revokeObjectURL(url);
  }, delayMs);
}

function noiseSample(index: number): number {
  const seed = Math.sin(index * 12.9898) * 43758.5453;
  return (seed - Math.floor(seed)) * 2 - 1;
}

function generatedAudioSample(type: AudioCueType, time: number, index: number): number {
  const sine = (frequency: number, gain = 1) => Math.sin(2 * Math.PI * frequency * time) * gain;
  if (type === "music") {
    const tremolo = 0.72 + Math.sin(2 * Math.PI * 0.28 * time) * 0.18;
    return (sine(196, 0.36) + sine(246.94, 0.25) + sine(329.63, 0.18)) * 0.28 * tremolo;
  }
  if (type === "ambience") {
    const drift = sine(72 + sine(0.08, 8), 0.08) + sine(118, 0.04);
    return drift + noiseSample(index) * 0.07;
  }
  if (type === "sfx") {
    const phase = time % 1.1;
    if (phase > 0.28) return 0;
    const sweep = 720 - phase * 1200;
    return sine(Math.max(220, sweep), 0.72) * Math.exp(-phase * 10);
  }
  const pulse = Math.sin(2 * Math.PI * 3.2 * time) > -0.2 ? 1 : 0.18;
  return (sine(type === "dialogue" ? 210 : 260, 0.28) + sine(type === "dialogue" ? 315 : 390, 0.12)) * pulse;
}

function writeAscii(view: DataView, offset: number, value: string) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

function createGeneratedAudioPreviewUrl(type: AudioCueType): string {
  const cached = generatedAudioPreviewUrlCache.get(type);
  if (cached) return cached;
  const sampleRate = GENERATED_AUDIO_PREVIEW_SAMPLE_RATE;
  const sampleCount = sampleRate * GENERATED_AUDIO_PREVIEW_SECONDS;
  const dataSize = sampleCount * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);
  writeAscii(view, 0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(view, 8, "WAVE");
  writeAscii(view, 12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, "data");
  view.setUint32(40, dataSize, true);

  for (let index = 0; index < sampleCount; index += 1) {
    const time = index / sampleRate;
    const intro = clamp(time / 0.025, 0, 1);
    const outro = clamp((GENERATED_AUDIO_PREVIEW_SECONDS - time) / 0.025, 0, 1);
    const value = clamp(generatedAudioSample(type, time, index) * Math.min(intro, outro), -0.9, 0.9);
    view.setInt16(44 + index * 2, Math.round(value * 32767), true);
  }

  const url = URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
  generatedAudioPreviewUrlCache.set(type, url);
  return url;
}

async function captureVideoFrame(videoUrl: string): Promise<string> {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.src = videoUrl;
  video.load();

  if (video.readyState < HTMLMediaElement.HAVE_METADATA) {
    await waitForMediaElement(video, ["loadedmetadata", "loadeddata"]);
  }

  const duration = Number.isFinite(video.duration) ? video.duration : 0;
  const targetTime = duration > 0.2 ? Math.min(0.8, Math.max(0.08, duration * 0.12)) : 0;
  if (targetTime > 0) {
    await new Promise<void>((resolve) => {
      let timer: number | null = null;
      const cleanup = () => {
        if (timer !== null) window.clearTimeout(timer);
        video.removeEventListener("seeked", finish);
        video.removeEventListener("error", finish);
      };
      const finish = () => {
        cleanup();
        resolve();
      };
      video.addEventListener("seeked", finish, { once: true });
      video.addEventListener("error", finish, { once: true });
      timer = window.setTimeout(finish, 2500);
      try {
        video.currentTime = targetTime;
      } catch {
        finish();
      }
    });
  }

  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !video.videoWidth || !video.videoHeight) {
    await waitForMediaElement(video, ["loadeddata", "canplay"], 5000);
  }

  const width = video.videoWidth || 640;
  const height = video.videoHeight || 360;
  const maxWidth = 640;
  const scale = Math.min(1, maxWidth / width);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(2, Math.round(width * scale));
  canvas.height = Math.max(2, Math.round(height * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas is not available");
  context.drawImage(video, 0, 0, canvas.width, canvas.height);

  video.removeAttribute("src");
  video.load();
  return canvas.toDataURL("image/jpeg", 0.82);
}

function loadMediaVideoFrameUrl(assetId: string): Promise<string> {
  const cachedUrl = mediaVideoFrameUrlCache.get(assetId);
  if (cachedUrl) return Promise.resolve(cachedUrl);
  if (mediaVideoFrameFailureCache.has(assetId)) return Promise.reject(new Error("Video preview unavailable"));
  const inflight = mediaVideoFrameInflight.get(assetId);
  if (inflight) return inflight;

  const request = api.documents.download(assetId, { cache: true })
    .then(async (url) => {
      try {
        const frameUrl = await captureVideoFrame(url);
        mediaVideoFrameUrlCache.set(assetId, frameUrl);
        return frameUrl;
      } finally {
        revokeObjectUrlSoon(url);
      }
    })
    .catch((error) => {
      mediaVideoFrameFailureCache.add(assetId);
      throw error;
    })
    .finally(() => {
      mediaVideoFrameInflight.delete(assetId);
    });
  mediaVideoFrameInflight.set(assetId, request);
  return request;
}

function loadMediaVideoPreviewUrl(assetId: string): Promise<string> {
  const cachedUrl = mediaVideoPreviewUrlCache.get(assetId);
  if (cachedUrl) return Promise.resolve(cachedUrl);
  if (mediaVideoPreviewFailureCache.has(assetId)) return Promise.reject(new Error("Video preview unavailable"));
  const inflight = mediaVideoPreviewInflight.get(assetId);
  if (inflight) return inflight;

  const request = api.documents.download(assetId, { cache: true })
    .then((url) => {
      mediaVideoPreviewUrlCache.set(assetId, url);
      return url;
    })
    .catch((error) => {
      mediaVideoPreviewFailureCache.add(assetId);
      throw error;
    })
    .finally(() => {
      mediaVideoPreviewInflight.delete(assetId);
    });
  mediaVideoPreviewInflight.set(assetId, request);
  return request;
}

function MediaAssetThumbnail({ asset, kind, label }: { asset: Document; kind: "video" | "audio" | "project"; label: string }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [isVisible, setIsVisible] = useState(false);
  const [thumbnailUrl, setThumbnailUrl] = useState<string | null>(null);
  const [thumbnailFailed, setThumbnailFailed] = useState(false);
  const [videoFrameUrl, setVideoFrameUrl] = useState<string | null>(null);
  const [videoFrameFailed, setVideoFrameFailed] = useState(false);
  const [videoPreviewUrl, setVideoPreviewUrl] = useState<string | null>(null);
  const [videoPreviewFailed, setVideoPreviewFailed] = useState(false);
  const shouldLoadThumbnail = kind === "video";

  useEffect(() => {
    const node = containerRef.current;
    if (!node || !shouldLoadThumbnail) return undefined;
    if (typeof IntersectionObserver === "undefined") {
      setIsVisible(true);
      return undefined;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setIsVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "180px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [shouldLoadThumbnail]);

  useEffect(() => {
    const cachedUrl = mediaThumbnailUrlCache.get(asset.id) ?? null;
    const cachedFailure = mediaThumbnailFailureCache.has(asset.id);
    setThumbnailUrl(cachedUrl);
    setThumbnailFailed(cachedFailure);
    setVideoFrameUrl(mediaVideoFrameUrlCache.get(asset.id) ?? null);
    setVideoFrameFailed(mediaVideoFrameFailureCache.has(asset.id));
    setVideoPreviewUrl(mediaVideoPreviewUrlCache.get(asset.id) ?? null);
    setVideoPreviewFailed(mediaVideoPreviewFailureCache.has(asset.id));
    if (!shouldLoadThumbnail || !isVisible) return undefined;
    if (cachedUrl || cachedFailure) return undefined;

    let cancelled = false;
    loadMediaThumbnailUrl(asset.id)
      .then((url) => {
        if (!cancelled) setThumbnailUrl(url);
      })
      .catch(() => {
        if (!cancelled) setThumbnailFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [asset.id, isVisible, shouldLoadThumbnail]);

  useEffect(() => {
    const cachedUrl = mediaVideoFrameUrlCache.get(asset.id) ?? null;
    const cachedFailure = mediaVideoFrameFailureCache.has(asset.id);
    setVideoFrameUrl(cachedUrl);
    setVideoFrameFailed(cachedFailure);
    if (!shouldLoadThumbnail || !isVisible || thumbnailUrl || cachedUrl || cachedFailure) return undefined;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      loadMediaVideoFrameUrl(asset.id)
      .then((url) => {
        if (!cancelled) setVideoFrameUrl(url);
      })
      .catch(() => {
        if (!cancelled) setVideoFrameFailed(true);
      });
    }, thumbnailFailed ? 0 : 700);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [asset.id, isVisible, shouldLoadThumbnail, thumbnailFailed, thumbnailUrl]);

  useEffect(() => {
    const cachedUrl = mediaVideoPreviewUrlCache.get(asset.id) ?? null;
    const cachedFailure = mediaVideoPreviewFailureCache.has(asset.id);
    setVideoPreviewUrl(cachedUrl);
    setVideoPreviewFailed(cachedFailure);
    if (!shouldLoadThumbnail || !isVisible || thumbnailUrl || videoFrameUrl || cachedUrl || cachedFailure) return undefined;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      loadMediaVideoPreviewUrl(asset.id)
        .then((url) => {
          if (!cancelled) setVideoPreviewUrl(url);
        })
        .catch(() => {
          if (!cancelled) setVideoPreviewFailed(true);
        });
    }, thumbnailFailed && videoFrameFailed ? 0 : 1100);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [asset.id, isVisible, shouldLoadThumbnail, thumbnailFailed, thumbnailUrl, videoFrameFailed, videoFrameUrl]);

  const handleImageError = () => {
    if (thumbnailUrl) {
      mediaThumbnailUrlCache.delete(asset.id);
      mediaThumbnailFailureCache.add(asset.id);
    }
    setThumbnailUrl(null);
    setThumbnailFailed(true);
  };

  const handleVideoPreviewError = () => {
    mediaVideoPreviewFailureCache.add(asset.id);
    setVideoPreviewUrl(null);
    setVideoPreviewFailed(true);
  };

  const hasVisualPreview = Boolean(thumbnailUrl || videoFrameUrl || videoPreviewUrl);
  const hasExhaustedVideoPreview = shouldLoadThumbnail && thumbnailFailed && videoFrameFailed && videoPreviewFailed;
  const isLoadingVideoPreview = shouldLoadThumbnail && isVisible && !hasVisualPreview && !hasExhaustedVideoPreview;

  return (
    <div
      ref={containerRef}
      className={[
        "ve-media-thumb",
        hasVisualPreview ? "has-thumbnail" : "",
        isLoadingVideoPreview ? "is-loading" : "",
      ].filter(Boolean).join(" ")}
    >
      {thumbnailUrl && <img src={thumbnailUrl} alt="" loading="lazy" onError={handleImageError} />}
      {!thumbnailUrl && videoFrameUrl && <img src={videoFrameUrl} alt="" loading="lazy" />}
      {!thumbnailUrl && !videoFrameUrl && videoPreviewUrl && (
        <video
          src={`${videoPreviewUrl}#t=0.35`}
          muted
          preload="auto"
          playsInline
          onLoadedMetadata={(event) => {
            const video = event.currentTarget;
            const targetTime = Number.isFinite(video.duration) && video.duration > 0.2
              ? Math.min(0.8, Math.max(0.08, video.duration * 0.12))
              : 0;
            if (targetTime > 0 && Math.abs(video.currentTime - targetTime) > 0.05) {
              try {
                video.currentTime = targetTime;
              } catch {
                // Browser preview still shows the first available frame.
              }
            }
          }}
          onError={handleVideoPreviewError}
        />
      )}
      <span className="ve-media-type-badge">{label}</span>
      {kind === "audio" ? (
        <div className="ve-audio-wave" aria-hidden="true">
          <i /><i /><i /><i /><i />
        </div>
      ) : kind === "project" ? (
        <IconFolder size={30} />
      ) : !hasVisualPreview ? (
        <div className="ve-video-glyph" aria-hidden="true">
          <span /><span /><span />
        </div>
      ) : null}
    </div>
  );
}

function fileMediaKind(file: File): "video" | "audio" | null {
  const mime = file.type.toLowerCase();
  const ext = file.name.toLowerCase().split(".").pop() || "";
  if (mime.startsWith("video/") || VIDEO_EXTENSIONS.has(ext)) return "video";
  if (mime.startsWith("audio/") || AUDIO_EXTENSIONS.has(ext)) return "audio";
  return null;
}

function isVideoEditRecipeDocument(doc: Document | undefined): boolean {
  if (!doc) return false;
  return doc.name.toLowerCase().endsWith(".video-edit.json");
}

function normalizeRecipePath(value: unknown): string {
  if (typeof value !== "string") return "";
  return value
    .replace(/^\/?api\/v1\/fs\/[^/]+\//, "")
    .replace(/^\/+/, "")
    .replace(/\\/g, "/")
    .toLowerCase();
}

function recipePathBaseName(value: unknown): string {
  const normalized = normalizeRecipePath(value);
  if (!normalized) return "";
  const parts = normalized.split("/");
  return parts[parts.length - 1] || normalized;
}

function timelineItemsFromRecipePayload(payload: unknown): Record<string, unknown>[] {
  if (!payload || typeof payload !== "object") return [];
  const source = payload as Record<string, unknown>;
  for (const key of ["clips", "video_clips", "shot_clips", "shots", "shot_beats", "storyboards", "storyboard"]) {
    const value = source[key];
    if (Array.isArray(value)) return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"));
  }
  const scenes = source.scenes;
  if (!Array.isArray(scenes)) return [];
  return scenes.flatMap((scene) => {
    if (!scene || typeof scene !== "object") return [];
    const rawShots = (scene as Record<string, unknown>).shots
      ?? (scene as Record<string, unknown>).clips
      ?? (scene as Record<string, unknown>).beats;
    return Array.isArray(rawShots)
      ? rawShots.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object"))
      : [];
  });
}

function recipeReferencesDocument(recipe: VideoEditRecipe, doc: Document): boolean {
  const docPath = normalizeRecipePath(doc.fs_path);
  const docBaseName = recipePathBaseName(doc.name);
  const source = recipe.source_document;
  if (source?.id === doc.id) return true;
  if (docPath && normalizeRecipePath(source?.fs_path) === docPath) return true;
  if (docBaseName && recipePathBaseName(source?.name) === docBaseName) return true;

  const aiComposition = (recipe as unknown as { ai_composition?: Record<string, unknown> }).ai_composition;
  if (docPath && normalizeRecipePath(aiComposition?.final_video_path) === docPath) return true;
  if (docPath && normalizeRecipePath(aiComposition?.clean_picture_master) === docPath) return true;

  const clips = Array.isArray(recipe.timeline?.clips) ? recipe.timeline.clips : [];
  const clipMatches = clips.some((clip) => {
    const candidate = clip as Partial<ClipSegment>;
    if (candidate.assetDocumentId === doc.id) return true;
    return Boolean(docBaseName && recipePathBaseName(candidate.assetName) === docBaseName);
  });
  if (clipMatches) return true;

  const rawItems = timelineItemsFromRecipePayload((recipe as unknown as { source_timeline?: unknown }).source_timeline);
  return rawItems.some((item) => {
    const candidateId = item.document_id ?? item.doc_id ?? item.asset_document_id ?? item.assetDocumentId;
    if (candidateId === doc.id) return true;
    const candidatePath = item.path ?? item.file ?? item.video_path ?? item.media_path ?? item.output_path ?? item.fs_path;
    if (docPath && normalizeRecipePath(candidatePath) === docPath) return true;
    return Boolean(docBaseName && recipePathBaseName(candidatePath) === docBaseName);
  });
}

function recipeSearchFolderIds(folderId: string | null | undefined, folders: DocumentFolderInfo[]): string[] {
  if (!folderId) return [];
  const current = folders.find((folder) => folder.id === folderId);
  const ids = new Set<string>([folderId]);
  if (current?.parent_id) ids.add(current.parent_id);

  const queue = Array.from(ids);
  for (let index = 0; index < queue.length; index += 1) {
    const parentId = queue[index];
    for (const folder of folders) {
      if (folder.parent_id !== parentId || ids.has(folder.id)) continue;
      ids.add(folder.id);
      queue.push(folder.id);
    }
  }
  return Array.from(ids);
}

function recipeTimelineClipCount(recipe: VideoEditRecipe): number {
  const clips = Array.isArray(recipe.timeline?.clips) ? recipe.timeline?.clips ?? [] : [];
  if (clips.length > 0) return clips.length;
  return timelineItemsFromRecipePayload((recipe as unknown as { source_timeline?: unknown }).source_timeline).length;
}

function recipeFallbackScore(doc: Document, recipe: VideoEditRecipe): number {
  const clipCount = recipeTimelineClipCount(recipe);
  if (clipCount <= 1) return 0;
  let score = Math.min(clipCount, 20) * 10;
  if (/final|master|edit|timeline|compose|composition/i.test(doc.name)) score += 50;
  const aiComposition = (recipe as unknown as { ai_composition?: Record<string, unknown> }).ai_composition;
  if (numberOr(aiComposition?.clip_count, 0) > 1) score += 30;
  if ((recipe.timeline?.audio_cues ?? []).length > 0) score += 8;
  if ((recipe.timeline?.captions ?? []).length > 0) score += 8;
  if ((recipe.timeline?.shots ?? []).length > 1) score += 6;
  return score;
}

function projectClipOrder(doc: Document): number {
  const name = doc.name.toLowerCase();
  const match = name.match(/(?:^|[-_\s])(clip|shot|scene|c)[-_\s]?0*(\d{1,3})(?:[-_\s.]|$)/);
  return match ? Number(match[2]) : Number.MAX_SAFE_INTEGER;
}

function sortProjectVideoDocuments(docs: Document[]): Document[] {
  return [...docs].sort((a, b) => (
    projectClipOrder(a) - projectClipOrder(b)
    || a.name.localeCompare(b.name)
    || (a.created_at || "").localeCompare(b.created_at || "")
  ));
}

function isAudioCueType(value: unknown): value is AudioCueType {
  return typeof value === "string" && value in AUDIO_TYPE_LABELS;
}

function getKnowledgeReturnTo(state: unknown): string | null {
  if (!state || typeof state !== "object") return null;
  const value = (state as { chatReturnTo?: unknown; knowledgeReturnTo?: unknown; returnTo?: unknown }).chatReturnTo
    ?? (state as { knowledgeReturnTo?: unknown; returnTo?: unknown }).knowledgeReturnTo
    ?? (state as { returnTo?: unknown }).returnTo;
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : null;
}

function mapTimelineTime(time: number, clips: ClipSegment[]): TimelineMap | null {
  let cursor = 0;
  for (let index = 0; index < clips.length; index += 1) {
    const clip = clips[index];
    const duration = Math.max(0, clip.sourceEnd - clip.sourceStart);
    const timelineEnd = cursor + duration;
    if (time < timelineEnd || index === clips.length - 1) {
      const offset = clamp(time - cursor, 0, duration);
      return {
        clip,
        index,
        timelineStart: cursor,
        timelineEnd,
        sourceTime: clip.sourceStart + offset,
      };
    }
    cursor = timelineEnd;
  }
  return null;
}

function previewSourceTime(mapped: TimelineMap): number {
  const duration = Math.max(0, mapped.clip.sourceEnd - mapped.clip.sourceStart);
  if (duration <= 0) return mapped.sourceTime;
  if (mapped.sourceTime >= mapped.clip.sourceEnd - PLAYBACK_BOUNDARY_EPSILON) {
    return Math.max(
      mapped.clip.sourceStart,
      mapped.clip.sourceEnd - Math.min(PREVIEW_END_FRAME_EPSILON, duration / 2),
    );
  }
  return mapped.sourceTime;
}

function getClipTimelineSpans(clips: ClipSegment[]): ClipTimelineSpan[] {
  let cursor = 0;
  return clips.map((clip, index) => {
    const duration = Math.max(0, clip.sourceEnd - clip.sourceStart);
    const span = {
      clip,
      index,
      start: cursor,
      end: cursor + duration,
      duration,
    };
    cursor += duration;
    return span;
  });
}

function timedItemMidpoint(item: TimedTrackItem): number {
  return item.start + (item.end - item.start) / 2;
}

function midpointInSpan(item: TimedTrackItem, span: ClipTimelineSpan): boolean {
  const midpoint = timedItemMidpoint(item);
  return midpoint >= span.start - 0.001 && midpoint <= span.end + 0.001;
}

function shiftTimedItem<T extends TimedTrackItem>(item: T, shift: number): T {
  return {
    ...item,
    start: Math.max(0, item.start + shift),
    end: Math.max(0.05, item.end + shift),
  };
}

function duplicateTimedRange(item: TimedTrackItem, duration: number): Pick<TimedTrackItem, "start" | "end"> {
  const itemDuration = Math.max(0.05, item.end - item.start);
  const start = clamp(item.end, 0, Math.max(0, duration - itemDuration));
  return { start, end: start + itemDuration };
}

function sortTimedItems<T extends TimedTrackItem>(items: T[]): T[] {
  return [...items].sort((a, b) => a.start - b.start || a.end - b.end);
}

function moveArrayItem<T>(items: T[], fromIndex: number, toIndex: number): T[] {
  if (fromIndex === toIndex) return [...items];
  const next = [...items];
  const [item] = next.splice(fromIndex, 1);
  next.splice(toIndex, 0, item);
  return next;
}

function timelineTimeFromLaneClientX(lane: HTMLElement, clientX: number, duration: number): number {
  const rect = lane.getBoundingClientRect();
  if (rect.width <= 0 || duration <= 0) return 0;
  return clamp(((clientX - rect.left) / rect.width) * duration, 0, duration);
}

function timelineTimeFromContentClientX(content: HTMLElement, clientX: number, duration: number): number {
  const rect = content.getBoundingClientRect();
  const laneLeft = rect.left + TIMELINE_LABEL_COLUMN_WIDTH;
  const laneWidth = Math.max(1, rect.width - TIMELINE_LABEL_COLUMN_WIDTH);
  if (duration <= 0) return 0;
  return clamp(((clientX - laneLeft) / laneWidth) * duration, 0, duration);
}

function clipInsertIndexAtTime(clips: ClipSegment[], time: number): number {
  const spans = getClipTimelineSpans(clips);
  const index = spans.findIndex((span) => time < span.start + span.duration / 2);
  return index === -1 ? clips.length : index;
}

function clipBoundaryTimeForIndex(clips: ClipSegment[], index: number): number {
  if (index <= 0) return 0;
  const spans = getClipTimelineSpans(clips);
  return spans[Math.min(index, spans.length) - 1]?.end ?? 0;
}

function insertClipIntoTrackState(baseState: EditorTrackState, clip: ClipSegment, index: number): EditorTrackState {
  const safeIndex = clamp(Math.round(index), 0, baseState.clips.length);
  const spans = getClipTimelineSpans(baseState.clips);
  const insertTime = spans[safeIndex]?.start ?? editorTrackDuration(baseState);
  const duration = Math.max(0.05, clip.sourceEnd - clip.sourceStart);
  const nextDuration = editorTrackDuration(baseState) + duration;
  const shiftRangeAfterInsert = <T extends TimedTrackItem>(item: T): T => (
    item.start >= insertTime - 0.001
      ? {
        ...item,
        start: clamp(item.start + duration, 0, Math.max(0, nextDuration - 0.05)),
        end: clamp(item.end + duration, 0.05, nextDuration),
      }
      : item
  );

  return {
    clips: [
      ...baseState.clips.slice(0, safeIndex),
      clip,
      ...baseState.clips.slice(safeIndex),
    ],
    shotBeats: sortTimedItems(baseState.shotBeats.map(shiftRangeAfterInsert)),
    captions: sortTimedItems(baseState.captions.map(shiftRangeAfterInsert)),
    audioCues: sortTimedItems(baseState.audioCues.map(shiftRangeAfterInsert)),
    markers: baseState.markers
      .map((marker) => marker.time >= insertTime - 0.001
        ? { ...marker, time: clamp(marker.time + duration, 0, nextDuration) }
        : marker)
      .sort((a, b) => a.time - b.time),
  };
}

function findSpanForMidpoint(item: TimedTrackItem, spans: ClipTimelineSpan[]): ClipTimelineSpan | null {
  return spans.find((span) => midpointInSpan(item, span)) ?? null;
}

function findSpanForMarker(marker: TimelineMarker, spans: ClipTimelineSpan[]): ClipTimelineSpan | null {
  return spans.find((span) => marker.time >= span.start - 0.001 && marker.time <= span.end + 0.001) ?? null;
}

function remapTimedItemsForClipOrder<T extends TimedTrackItem>(
  items: T[],
  originalSpans: ClipTimelineSpan[],
  nextSpansByClipId: Map<string, ClipTimelineSpan>,
  nextDuration: number,
): T[] {
  return sortTimedItems(items.map((item) => {
    const originalSpan = findSpanForMidpoint(item, originalSpans);
    const nextSpan = originalSpan ? nextSpansByClipId.get(originalSpan.clip.id) : null;
    if (!originalSpan || !nextSpan) return item;
    const shift = nextSpan.start - originalSpan.start;
    return {
      ...item,
      start: clamp(item.start + shift, 0, Math.max(0, nextDuration - 0.05)),
      end: clamp(item.end + shift, 0.05, Math.max(0.05, nextDuration)),
    };
  }));
}

function remapMarkersForClipOrder(
  markers: TimelineMarker[],
  originalSpans: ClipTimelineSpan[],
  nextSpansByClipId: Map<string, ClipTimelineSpan>,
  nextDuration: number,
): TimelineMarker[] {
  return markers.map((marker) => {
    const originalSpan = findSpanForMarker(marker, originalSpans);
    const nextSpan = originalSpan ? nextSpansByClipId.get(originalSpan.clip.id) : null;
    if (!originalSpan || !nextSpan) return marker;
    const offset = marker.time - originalSpan.start;
    return {
      ...marker,
      time: clamp(nextSpan.start + offset, 0, nextDuration),
    };
  }).sort((a, b) => a.time - b.time);
}

function buildClipReorderResult(baseState: EditorTrackState, nextClips: ClipSegment[]): ClipReorderResult {
  const originalSpans = getClipTimelineSpans(baseState.clips);
  const nextSpans = getClipTimelineSpans(nextClips);
  const nextSpansByClipId = new Map(nextSpans.map((span) => [span.clip.id, span]));
  const nextDuration = nextSpans.length ? nextSpans[nextSpans.length - 1].end : 0;
  return {
    clips: nextClips,
    shotBeats: remapTimedItemsForClipOrder(baseState.shotBeats, originalSpans, nextSpansByClipId, nextDuration),
    captions: remapTimedItemsForClipOrder(baseState.captions, originalSpans, nextSpansByClipId, nextDuration),
    audioCues: remapTimedItemsForClipOrder(baseState.audioCues, originalSpans, nextSpansByClipId, nextDuration),
    markers: remapMarkersForClipOrder(baseState.markers, originalSpans, nextSpansByClipId, nextDuration),
  };
}

function cloneEditorTrackState(state: EditorTrackState): EditorTrackState {
  return {
    clips: state.clips.map((clip) => ({ ...clip })),
    shotBeats: state.shotBeats.map((shot) => ({ ...shot })),
    captions: state.captions.map((caption) => ({ ...caption })),
    audioCues: state.audioCues.map((cue) => ({ ...cue })),
    markers: state.markers.map((marker) => ({ ...marker })),
  };
}

function serializeEditorTrackState(state: EditorTrackState): string {
  return JSON.stringify(state);
}

function editorTrackDuration(state: EditorTrackState): number {
  return state.clips.reduce((total, clip) => total + Math.max(0, clip.sourceEnd - clip.sourceStart), 0);
}

function normalizeVideoEditRecipe(recipe: VideoEditRecipe, sourceDuration: number): NormalizedVideoEditRecipe {
  const rawClips = Array.isArray(recipe.timeline?.clips) ? recipe.timeline?.clips ?? [] : [];
  if (rawClips.length === 0) throw new Error("Recipe has no video clips");
  const maxSourceDuration = sourceDuration || Math.max(...rawClips.map((clip) => numberOr(clip.sourceEnd, 0)), 1);
  const nextClips = rawClips.map((clip, index) => {
    const clipMaxDuration = typeof clip.assetDuration === "number" && clip.assetDuration > 0 ? clip.assetDuration : maxSourceDuration;
    const start = clamp(numberOr(clip.sourceStart, 0), 0, Math.max(0, clipMaxDuration - 0.05));
    const end = clamp(numberOr(clip.sourceEnd, clipMaxDuration), start + 0.05, clipMaxDuration);
    return {
      id: typeof clip.id === "string" ? clip.id : makeId("clip"),
      label: typeof clip.label === "string" ? clip.label : `Clip ${index + 1}`,
      sourceStart: start,
      sourceEnd: end,
      muted: Boolean(clip.muted),
      color: typeof clip.color === "string" ? clip.color : CLIP_COLORS[index % CLIP_COLORS.length],
      assetDocumentId: typeof clip.assetDocumentId === "string" ? clip.assetDocumentId : null,
      assetName: typeof clip.assetName === "string" ? clip.assetName : null,
      assetMimeType: typeof clip.assetMimeType === "string" ? clip.assetMimeType : null,
      assetDuration: typeof clip.assetDuration === "number" ? clip.assetDuration : null,
      replacementPrompt: typeof clip.replacementPrompt === "string" ? clip.replacementPrompt : "",
      editNotes: typeof clip.editNotes === "string" ? clip.editNotes : "",
    } satisfies ClipSegment;
  });
  const recipeDuration = nextClips.reduce((total, clip) => total + Math.max(0, clip.sourceEnd - clip.sourceStart), 0);
  const nextShotBeats = (Array.isArray(recipe.timeline?.shots) ? recipe.timeline?.shots ?? [] : []).map((shot, index) => {
    const start = clamp(numberOr(shot.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(shot.end, start + 3), start + 0.05, Math.max(start + 0.05, recipeDuration));
    return {
      id: typeof shot.id === "string" ? shot.id : makeId("shot"),
      title: typeof shot.title === "string" ? shot.title : `Beat ${index + 1}`,
      scene: typeof shot.scene === "string" ? shot.scene : `Scene ${index + 1}`,
      shot: typeof shot.shot === "string" ? shot.shot : `Shot ${index + 1}`,
      start,
      end,
      location: typeof shot.location === "string" ? shot.location : "",
      camera: typeof shot.camera === "string" ? shot.camera : "",
      action: typeof shot.action === "string" ? shot.action : "",
      dialogue: typeof shot.dialogue === "string" ? shot.dialogue : "",
      notes: typeof shot.notes === "string" ? shot.notes : "",
    } satisfies ShotBeat;
  });
  const nextCaptions = (Array.isArray(recipe.timeline?.captions) ? recipe.timeline?.captions ?? [] : []).map((caption) => {
    const start = clamp(numberOr(caption.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(caption.end, start + 2), start + 0.05, Math.max(start + 0.05, recipeDuration));
    const style = caption.style === "speechBubble" || caption.style === "narrationBox" || caption.style === "subtitle" ? caption.style : "subtitle";
    return {
      id: typeof caption.id === "string" ? caption.id : makeId("caption"),
      speaker: typeof caption.speaker === "string" ? caption.speaker : null,
      emotion: typeof caption.emotion === "string" ? caption.emotion : null,
      style,
      text: typeof caption.text === "string" ? caption.text : "",
      start,
      end,
      x: clamp(numberOr(caption.x, 50), 0, 100),
      y: clamp(numberOr(caption.y, 84), 0, 100),
      size: clamp(numberOr(caption.size, 32), 10, 96),
      color: typeof caption.color === "string" ? caption.color : "#ffffff",
      background: typeof caption.background === "string" ? caption.background : "rgba(28,25,23,0.72)",
      backgroundColor: typeof caption.backgroundColor === "string"
        ? caption.backgroundColor
        : style === "speechBubble"
          ? "#ffffff"
          : "#1c1917",
      backgroundOpacity: clamp(numberOr(caption.backgroundOpacity, style === "speechBubble" ? 0.94 : style === "narrationBox" ? 0.86 : 0.72), 0, 1),
      align: caption.align === "left" || caption.align === "right" || caption.align === "center" ? caption.align : "center",
    } satisfies CaptionCue;
  });
  const nextAudioCues = (Array.isArray(recipe.timeline?.audio_cues) ? recipe.timeline?.audio_cues ?? [] : []).map((cue) => {
    const start = clamp(numberOr(cue.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(cue.end, start + 2), start + 0.05, Math.max(start + 0.05, recipeDuration));
    const type = isAudioCueType(cue.type) ? cue.type : "ambience";
    const duration = Math.max(0.05, end - start);
    return {
      id: typeof cue.id === "string" ? cue.id : makeId("audio"),
      type,
      label: typeof cue.label === "string" ? cue.label : AUDIO_TYPE_LABELS[type],
      start,
      end,
      volumeDb: clamp(numberOr(cue.volumeDb, defaultAudioVolumeDb(type)), -48, 6),
      fadeIn: clamp(numberOr(cue.fadeIn, defaultAudioFade(type)), 0, Math.min(10, duration)),
      fadeOut: clamp(numberOr(cue.fadeOut, defaultAudioFade(type)), 0, Math.min(10, duration)),
      loop: typeof cue.loop === "boolean" ? cue.loop : defaultAudioLoop(type),
      duckUnderDialogue: typeof cue.duckUnderDialogue === "boolean" ? cue.duckUnderDialogue : defaultDuckUnderDialogue(type),
      muted: Boolean(cue.muted),
      assetDocumentId: typeof cue.assetDocumentId === "string" ? cue.assetDocumentId : null,
      assetName: typeof cue.assetName === "string" ? cue.assetName : null,
      assetMimeType: typeof cue.assetMimeType === "string" ? cue.assetMimeType : null,
      sourcePlan: typeof cue.sourcePlan === "string" ? cue.sourcePlan : "",
      prompt: typeof cue.prompt === "string" ? cue.prompt : "",
    } satisfies AudioCue;
  });
  const nextMarkers = (Array.isArray(recipe.timeline?.markers) ? recipe.timeline?.markers ?? [] : []).map((marker, index) => ({
    id: typeof marker.id === "string" ? marker.id : makeId("marker"),
    time: clamp(numberOr(marker.time, 0), 0, recipeDuration),
    label: typeof marker.label === "string" ? marker.label : `Marker ${index + 1}`,
    color: typeof marker.color === "string" ? marker.color : MARKER_COLORS[index % MARKER_COLORS.length],
    notes: typeof marker.notes === "string" ? marker.notes : "",
  } satisfies TimelineMarker));

  return {
    mediaSize: recipe.canvas?.width && recipe.canvas?.height
      ? { width: Number(recipe.canvas.width), height: Number(recipe.canvas.height) }
      : null,
    trackStates: normalizeTimelineTrackStates(recipe.editor_settings?.track_states),
    workArea: normalizeWorkArea(recipe.editor_settings?.work_area, recipeDuration),
    state: {
      clips: nextClips,
      shotBeats: nextShotBeats,
      captions: nextCaptions,
      audioCues: nextAudioCues,
      markers: nextMarkers,
    },
    duration: recipeDuration,
  };
}

function addedItems<T extends { id: string }>(before: T[], after: T[]): T[] {
  const beforeIds = new Set(before.map((item) => item.id));
  return after.filter((item) => !beforeIds.has(item.id));
}

function modifiedItems<T extends { id: string }>(before: T[], after: T[]): T[] {
  const beforeById = new Map(before.map((item) => [item.id, JSON.stringify(item)]));
  return after.filter((item) => {
    const previous = beforeById.get(item.id);
    return previous !== undefined && previous !== JSON.stringify(item);
  });
}

function formatAiEditCount(count: number, label: string): string {
  return `+${count} ${label}${count === 1 ? "" : "s"}`;
}

function timelineTimeForSelection(selection: NonNullable<Selection>, state: EditorTrackState): number {
  if (selection.type === "clip") {
    return getClipTimelineSpans(state.clips).find((span) => span.clip.id === selection.id)?.start ?? 0;
  }
  if (selection.type === "shot") return state.shotBeats.find((shot) => shot.id === selection.id)?.start ?? 0;
  if (selection.type === "caption") return state.captions.find((caption) => caption.id === selection.id)?.start ?? 0;
  if (selection.type === "audio") return state.audioCues.find((cue) => cue.id === selection.id)?.start ?? 0;
  return state.markers.find((marker) => marker.id === selection.id)?.time ?? 0;
}

function buildAiEditNotice(before: EditorTrackState, after: EditorTrackState): AiEditNotice {
  const addedClips = addedItems(before.clips, after.clips);
  const addedShots = addedItems(before.shotBeats, after.shotBeats);
  const addedCaptions = addedItems(before.captions, after.captions);
  const addedAudio = addedItems(before.audioCues, after.audioCues);
  const addedMarkers = addedItems(before.markers, after.markers);
  const changedClips = modifiedItems(before.clips, after.clips);
  const changedShots = modifiedItems(before.shotBeats, after.shotBeats);
  const changedCaptions = modifiedItems(before.captions, after.captions);
  const changedAudio = modifiedItems(before.audioCues, after.audioCues);
  const changedMarkers = modifiedItems(before.markers, after.markers);
  const details = [
    addedClips.length ? formatAiEditCount(addedClips.length, "clip") : "",
    addedShots.length ? formatAiEditCount(addedShots.length, "shot") : "",
    addedCaptions.length ? formatAiEditCount(addedCaptions.length, "caption") : "",
    addedAudio.length ? formatAiEditCount(addedAudio.length, "audio cue") : "",
    addedMarkers.length ? formatAiEditCount(addedMarkers.length, "marker") : "",
  ].filter(Boolean);
  const changedCount = changedClips.length + changedShots.length + changedCaptions.length + changedAudio.length + changedMarkers.length;
  if (changedCount > 0) details.push(`${changedCount} updated`);

  const highlights: NonNullable<Selection>[] = [
    ...addedAudio.map((cue) => ({ type: "audio" as const, id: cue.id })),
    ...addedCaptions.map((caption) => ({ type: "caption" as const, id: caption.id })),
    ...addedClips.map((clip) => ({ type: "clip" as const, id: clip.id })),
    ...addedShots.map((shot) => ({ type: "shot" as const, id: shot.id })),
    ...addedMarkers.map((marker) => ({ type: "marker" as const, id: marker.id })),
  ];
  if (highlights.length === 0) {
    highlights.push(
      ...changedAudio.slice(0, 2).map((cue) => ({ type: "audio" as const, id: cue.id })),
      ...changedCaptions.slice(0, 2).map((caption) => ({ type: "caption" as const, id: caption.id })),
      ...changedClips.slice(0, 2).map((clip) => ({ type: "clip" as const, id: clip.id })),
      ...changedShots.slice(0, 2).map((shot) => ({ type: "shot" as const, id: shot.id })),
      ...changedMarkers.slice(0, 2).map((marker) => ({ type: "marker" as const, id: marker.id })),
    );
  }
  const focusSelection = highlights[0] ?? null;
  return {
    id: makeId("ai-edit"),
    title: "AI edit applied",
    detail: details.length > 0 ? details.join(" · ") : "Timeline updated",
    highlights: highlights.slice(0, 12),
    focus: focusSelection
      ? { selection: focusSelection, time: timelineTimeForSelection(focusSelection, after) }
      : null,
  };
}

function isTextEditingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
}

function getTimelineSnapPoints(
  clips: ClipSegment[],
  shots: ShotBeat[],
  captions: CaptionCue[],
  audioCues: AudioCue[],
  markers: TimelineMarker[],
  duration: number,
): number[] {
  const points = new Set<number>([0, duration]);
  getClipTimelineSpans(clips).forEach((span) => {
    points.add(span.start);
    points.add(span.end);
  });
  shots.forEach((shot) => {
    points.add(shot.start);
    points.add(shot.end);
  });
  captions.forEach((caption) => {
    points.add(caption.start);
    points.add(caption.end);
  });
  audioCues.forEach((cue) => {
    points.add(cue.start);
    points.add(cue.end);
  });
  markers.forEach((marker) => points.add(marker.time));
  return [...points]
    .filter((point) => Number.isFinite(point))
    .map((point) => clamp(point, 0, duration))
    .sort((a, b) => a - b);
}

function isDialogueLikeCue(cue: AudioCue): boolean {
  return cue.type === "dialogue" || cue.type === "narration";
}

function hasActiveDialogue(audioCues: AudioCue[], timelineTime: number, exceptId?: string): boolean {
  return audioCues.some((cue) => (
    cue.id !== exceptId &&
    !cue.muted &&
    isDialogueLikeCue(cue) &&
    timelineTime >= cue.start &&
    timelineTime <= cue.end
  ));
}

function hasActiveSourceReplacingAudioCue(audioCues: AudioCue[], timelineTime: number): boolean {
  return audioCues.some((cue) => (
    !cue.muted &&
    isDialogueLikeCue(cue) &&
    timelineTime >= cue.start &&
    timelineTime <= cue.end
  ));
}

function shouldMuteSourceVideoAudio(
  clip: ClipSegment,
  timelineTime: number,
  audioCues: AudioCue[],
  trackStates: Record<TimelineTrackId, TimelineTrackState>,
): boolean {
  if (trackStates.video.muted || clip.muted) return true;
  if (trackStates.audio.muted) return false;
  // Dialogue/narration cues are the edited voice track, so keep the source audio
  // quiet underneath them to avoid doubled speech in preview and export.
  return hasActiveSourceReplacingAudioCue(audioCues, timelineTime);
}

function getAudioCueGain(cue: AudioCue, timelineTime: number, audioCues: AudioCue[]): number {
  const cueDuration = Math.max(0.05, cue.end - cue.start);
  const offset = timelineTime - cue.start;
  let gain = dbToGain(cue.volumeDb);
  if (cue.fadeIn > 0 && offset < cue.fadeIn) {
    gain *= clamp(offset / cue.fadeIn, 0, 1);
  }
  const remaining = cueDuration - offset;
  if (cue.fadeOut > 0 && remaining < cue.fadeOut) {
    gain *= clamp(remaining / cue.fadeOut, 0, 1);
  }
  if (cue.duckUnderDialogue && !isDialogueLikeCue(cue) && hasActiveDialogue(audioCues, timelineTime, cue.id)) {
    gain *= 0.35;
  }
  return clamp(gain, 0, 2);
}

function getAudioCueSourceTime(cue: AudioCue, cueOffset: number, mediaDuration: number): number {
  if (cue.loop && Number.isFinite(mediaDuration) && mediaDuration > 0.05) {
    return cueOffset % mediaDuration;
  }
  return Math.max(0, cueOffset);
}

function waitForVideoEvent(video: HTMLVideoElement, eventName: keyof HTMLMediaElementEventMap) {
  return new Promise<void>((resolve, reject) => {
    const onEvent = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("Video failed to load"));
    };
    const cleanup = () => {
      video.removeEventListener(eventName, onEvent);
      video.removeEventListener("error", onError);
    };
    video.addEventListener(eventName, onEvent, { once: true });
    video.addEventListener("error", onError, { once: true });
  });
}

function waitForAudioReady(audio: HTMLAudioElement) {
  return new Promise<void>((resolve, reject) => {
    if (audio.readyState >= 2) {
      resolve();
      return;
    }
    const onReady = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("Audio failed to load"));
    };
    const cleanup = () => {
      audio.removeEventListener("canplay", onReady);
      audio.removeEventListener("loadedmetadata", onReady);
      audio.removeEventListener("error", onError);
    };
    audio.addEventListener("canplay", onReady, { once: true });
    audio.addEventListener("loadedmetadata", onReady, { once: true });
    audio.addEventListener("error", onError, { once: true });
  });
}

function readVideoFileDuration(file: File) {
  return new Promise<number>((resolve) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    const cleanup = () => {
      clearMediaElementSource(video, url);
      revokeObjectUrlSoon(url);
      video.remove();
    };
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const duration = Number.isFinite(video.duration) ? video.duration : 0;
      cleanup();
      resolve(duration);
    };
    video.onerror = () => {
      cleanup();
      resolve(0);
    };
    video.src = url;
  });
}

function readVideoUrlDuration(url: string) {
  return new Promise<number>((resolve) => {
    const video = document.createElement("video");
    const cleanup = () => {
      clearMediaElementSource(video, url);
      video.remove();
    };
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const duration = Number.isFinite(video.duration) ? video.duration : 0;
      cleanup();
      resolve(duration);
    };
    video.onerror = () => {
      cleanup();
      resolve(0);
    };
    video.src = url;
  });
}

function readAudioUrlDuration(url: string) {
  return new Promise<number>((resolve) => {
    const audio = new Audio();
    const cleanup = () => {
      clearMediaElementSource(audio, url);
      audio.remove();
    };
    audio.preload = "metadata";
    audio.onloadedmetadata = () => {
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      cleanup();
      resolve(duration);
    };
    audio.onerror = () => {
      cleanup();
      resolve(0);
    };
    audio.src = url;
  });
}

function seekVideo(video: HTMLVideoElement, time: number) {
  return new Promise<void>((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      video.removeEventListener("seeked", finish);
      window.clearTimeout(timer);
      resolve();
    };
    const timer = window.setTimeout(finish, 700);
    video.addEventListener("seeked", finish, { once: true });
    video.currentTime = time;
  });
}

function nextFrame() {
  return new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
}

function wrapCanvasText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const words = text.split(/\s+/).filter(Boolean);
  if (words.length === 0) return [""];
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (ctx.measureText(candidate).width <= maxWidth || !line) {
      line = candidate;
    } else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function drawRoundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + width, y, x + width, y + height, r);
  ctx.arcTo(x + width, y + height, x, y + height, r);
  ctx.arcTo(x, y + height, x, y, r);
  ctx.arcTo(x, y, x + width, y, r);
  ctx.closePath();
}

function FieldLabel({ children }: { children: React.ReactNode }) {
  return <label className="ve-field-label">{children}</label>;
}

function NumberField({
  label,
  value,
  min,
  max,
  step = 0.1,
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <div className="ve-field">
      <FieldLabel>{label}</FieldLabel>
      <input
        className="ve-input"
        type="number"
        value={Number.isFinite(value) ? value : 0}
        min={min}
        max={max}
        step={step}
        onChange={(event) => onChange(Number(event.currentTarget.value))}
      />
    </div>
  );
}

export default function VideoEditor() {
  const { docId = "" } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const toastSuccess = useToastStore((s) => s.success);
  const toastError = useToastStore((s) => s.error);
  const toastWarning = useToastStore((s) => s.warning);
  const toast = useMemo(() => ({
    success: toastSuccess,
    error: toastError,
    warning: toastWarning,
  }), [toastError, toastSuccess, toastWarning]);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const timelineScrollRef = useRef<HTMLDivElement | null>(null);
  const mediaFileInputRef = useRef<HTMLInputElement | null>(null);
  const audioFileInputRef = useRef<HTMLInputElement | null>(null);
  const replacementVideoInputRef = useRef<HTMLInputElement | null>(null);
  const subtitleFileInputRef = useRef<HTMLInputElement | null>(null);
  const previewAudioRef = useRef<Map<string, PreviewAudioEntry>>(new Map());
  const mediaAssetPreviewRef = useRef<{ assetId: string; audio: HTMLAudioElement; url: string } | null>(null);
  const mediaAssetPreviewRequestRef = useRef(0);
  const clipAssetUrlRef = useRef<Map<string, string>>(new Map());
  const playheadRef = useRef(0);
  const activePlaybackMapRef = useRef<TimelineMap | null>(null);
  const playbackClockRef = useRef<{
    activeClipId: string | null;
    lastTickAt: number;
    rafId: number | null;
    switching: boolean;
  } | null>(null);
  const suppressPreviewPauseRef = useRef(false);
  const playbackAdvancePendingRef = useRef(false);
  const initializedDocRef = useRef<string | null>(null);
  const autoProjectTimelineRef = useRef<string | null>(null);
  const undoStackRef = useRef<EditorTrackState[]>([]);
  const redoStackRef = useRef<EditorTrackState[]>([]);
  const lastHistoryStateRef = useRef<EditorTrackState | null>(null);
  const latestTrackStateRef = useRef<EditorTrackState | null>(null);
  const historyTransactionRef = useRef<EditorHistoryTransaction | null>(null);
  const restoringHistoryRef = useRef(false);

  const [downloadUrl, setDownloadUrl] = useState("");
  const [previewSourceUrl, setPreviewSourceUrl] = useState("");
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceDuration, setSourceDuration] = useState(0);
  const [mediaSize, setMediaSize] = useState({ width: 1920, height: 1080 });
  const [clips, setClips] = useState<ClipSegment[]>([]);
  const [shotBeats, setShotBeats] = useState<ShotBeat[]>([]);
  const [captions, setCaptions] = useState<CaptionCue[]>([]);
  const [audioCues, setAudioCues] = useState<AudioCue[]>([]);
  const [markers, setMarkers] = useState<TimelineMarker[]>([]);
  const [selection, setSelection] = useState<Selection>(null);
  const [draggingClipId, setDraggingClipId] = useState<string | null>(null);
  const [previewingMediaAssetId, setPreviewingMediaAssetId] = useState<string | null>(null);
  const [mediaAssetPreviewLoadingId, setMediaAssetPreviewLoadingId] = useState<string | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [nudgeStep, setNudgeStep] = useState(0.1);
  const [timelinePixelsPerSecond, setTimelinePixelsPerSecond] = useState(48);
  const [timelineViewportWidth, setTimelineViewportWidth] = useState(0);
  const [trackStates, setTrackStates] = useState<Record<TimelineTrackId, TimelineTrackState>>(() => createDefaultTimelineTrackStates());
  const [workArea, setWorkArea] = useState<WorkAreaState>(() => createDefaultWorkArea());
  const [isPlaying, setIsPlaying] = useState(false);
  const [historyVersion, setHistoryVersion] = useState(0);
  const [mediaSearch, setMediaSearch] = useState("");
  const [mediaSourceTab, setMediaSourceTab] = useState<MediaSourceTab>("project");
  const [mediaKindFilter, setMediaKindFilter] = useState<MediaKindFilter>("all");
  const [mediaPickerOpen, setMediaPickerOpen] = useState(false);
  const [mediaPickerSelectedIds, setMediaPickerSelectedIds] = useState<string[]>([]);
  const [mediaDropActive, setMediaDropActive] = useState(false);
  const [clipDropPreview, setClipDropPreview] = useState<ClipDropPreview | null>(null);
  const [importingMedia, setImportingMedia] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadingRecipe, setLoadingRecipe] = useState(false);
  const [uploadingAudio, setUploadingAudio] = useState(false);
  const [uploadingVideo, setUploadingVideo] = useState(false);
  const [savingSubtitles, setSavingSubtitles] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState(0);
  const [lastExportDoc, setLastExportDoc] = useState<Document | null>(null);
  const [recipeDoc, setRecipeDoc] = useState<Document | null>(null);
  const [sourceDocOverride, setSourceDocOverride] = useState<Document | null>(null);
  const [pendingRouteRecipe, setPendingRouteRecipe] = useState<VideoEditRecipe | null>(null);
  const [aiEditNotice, setAiEditNotice] = useState<AiEditNotice | null>(null);
  const [scanningProjectRecipe, setScanningProjectRecipe] = useState(false);

  useEffect(() => setAiEditNotice(null), [docId]);
  useEffect(() => {
    if (!aiEditNotice) return undefined;
    const timer = window.setTimeout(() => setAiEditNotice(null), 9000);
    return () => window.clearTimeout(timer);
  }, [aiEditNotice]);
  const autoLoadedRecipeRef = useRef<string | null>(null);

  useEffect(() => {
    document.body.classList.add("video-editor-page-active");
    return () => {
      document.body.classList.remove("video-editor-page-active");
    };
  }, []);

  useEffect(() => {
    playheadRef.current = playhead;
  }, [playhead]);

  const docQuery = useQuery({
    queryKey: ["document", docId],
    queryFn: () => api.documents.get(docId),
    enabled: Boolean(docId),
  });

  const routeDoc = docQuery.data;
  const routeIsRecipe = isVideoEditRecipeDocument(routeDoc);
  const doc = sourceDocOverride ?? routeDoc;
  const sourceDocId = isVideoDocument(doc) ? doc.id : "";
  const recipeFileName = useMemo(() => doc ? `${baseName(doc.name)}.video-edit.json` : "", [doc]);
  const recipeQuery = useQuery({
    queryKey: ["video-edit-recipe", doc?.folder_id ?? null, recipeFileName],
    queryFn: async () => {
      if (!doc || !recipeFileName) return null;
      const result = await api.documents.list({
        folder_id: doc.folder_id ?? undefined,
        search: recipeFileName,
        include_generated_assets: true,
        limit: 25,
      });
      return result.items.find((item) => item.name === recipeFileName) ?? null;
    },
    enabled: Boolean(doc && recipeFileName && isVideoDocument(doc)),
  });
  const projectAssetsQuery = useQuery({
    queryKey: ["video-editor-assets", doc?.folder_id ?? null],
    queryFn: async () => {
      if (!doc?.folder_id) return [];
      const result = await api.documents.list({
        folder_id: doc.folder_id,
        include_generated_assets: true,
        limit: 120,
      });
      return result.items;
    },
    enabled: Boolean(doc?.folder_id),
  });
  const projectRecipeCandidatesQuery = useQuery({
    queryKey: ["video-editor-recipe-candidates", routeDoc?.folder_id ?? null],
    queryFn: async () => {
      if (!routeDoc?.folder_id) return [];
      const folders = await api.folders.list();
      const folderIds = recipeSearchFolderIds(routeDoc.folder_id, folders);
      const docsById = new Map<string, Document>();
      await Promise.all(folderIds.map(async (folderId) => {
        const result = await api.documents.list({
          folder_id: folderId,
          include_generated_assets: true,
          limit: 200,
        });
        for (const item of result.items) {
          if (item.id !== routeDoc.id && isVideoEditRecipeDocument(item)) {
            docsById.set(item.id, item);
          }
        }
      }));
      return Array.from(docsById.values());
    },
    enabled: Boolean(routeDoc?.folder_id && isVideoDocument(routeDoc) && !routeIsRecipe),
  });
  const linkedRecipeQuery = useQuery({
    queryKey: ["video-editor-linked-recipe", routeDoc?.editor_recipe_document_id ?? null],
    queryFn: async () => {
      if (!routeDoc?.editor_recipe_document_id) return null;
      const linked = await api.documents.get(routeDoc.editor_recipe_document_id);
      return isVideoEditRecipeDocument(linked) ? linked : null;
    },
    enabled: Boolean(routeDoc?.editor_recipe_document_id && isVideoDocument(routeDoc) && !routeIsRecipe),
  });
  const projectRecipeDocs = useMemo(() => (
    ([linkedRecipeQuery.data, ...(projectRecipeCandidatesQuery.data ?? [])].filter(Boolean) as Document[])
      .filter((item) => item.id !== routeDoc?.id && isVideoEditRecipeDocument(item))
      .filter((item, index, items) => items.findIndex((candidate) => candidate.id === item.id) === index)
      .sort((a, b) => {
        if (a.id === routeDoc?.editor_recipe_document_id) return -1;
        if (b.id === routeDoc?.editor_recipe_document_id) return 1;
        const aLooksFinal = /final|master|edit|timeline/i.test(a.name) ? 0 : 1;
        const bLooksFinal = /final|master|edit|timeline/i.test(b.name) ? 0 : 1;
        if (aLooksFinal !== bLooksFinal) return aLooksFinal - bLooksFinal;
        return (b.created_at || "").localeCompare(a.created_at || "");
      })
  ), [linkedRecipeQuery.data, projectRecipeCandidatesQuery.data, routeDoc?.editor_recipe_document_id, routeDoc?.id]);
  const projectMediaAssets = useMemo(() => (
    (projectAssetsQuery.data ?? [])
      .filter((item) => item.id !== doc?.id && Boolean(projectAssetKind(item)))
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
  ), [doc?.id, projectAssetsQuery.data]);
  const trimmedMediaSearch = mediaSearch.trim();
  const knowledgeMediaQuery = useQuery({
    queryKey: ["video-editor-knowledge-media", trimmedMediaSearch, doc?.id ?? null],
    queryFn: async () => {
      const result = await api.documents.list({
        search: trimmedMediaSearch || undefined,
        include_generated_assets: true,
        limit: 120,
      });
      return result.items
        .filter((item) => item.id !== doc?.id && Boolean(projectAssetKind(item)))
        .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    },
    enabled: mediaSourceTab === "knowledge" && (mediaPickerOpen || trimmedMediaSearch.length >= 2),
    staleTime: 30_000,
  });
  const knowledgeMediaAssets = knowledgeMediaQuery.data ?? [];
  const searchedProjectMediaAssets = useMemo(() => (
    projectMediaAssets.filter((asset) => mediaAssetMatchesSearch(asset, trimmedMediaSearch))
  ), [projectMediaAssets, trimmedMediaSearch]);
  const searchedKnowledgeMediaAssets = useMemo(() => (
    knowledgeMediaAssets.filter((asset) => mediaAssetMatchesSearch(asset, trimmedMediaSearch))
  ), [knowledgeMediaAssets, trimmedMediaSearch]);
  const projectMediaKindCounts = useMemo(() => countMediaKinds(searchedProjectMediaAssets), [searchedProjectMediaAssets]);
  const knowledgeMediaKindCounts = useMemo(() => countMediaKinds(searchedKnowledgeMediaAssets), [searchedKnowledgeMediaAssets]);
  const activeMediaKindCounts = mediaSourceTab === "project" ? projectMediaKindCounts : knowledgeMediaKindCounts;
  const filteredProjectMediaAssets = useMemo(() => (
    searchedProjectMediaAssets
      .filter((asset) => mediaAssetMatchesFilter(asset, mediaKindFilter))
  ), [mediaKindFilter, searchedProjectMediaAssets]);
  const filteredKnowledgeMediaAssets = useMemo(() => (
    searchedKnowledgeMediaAssets
      .filter((asset) => mediaAssetMatchesFilter(asset, mediaKindFilter))
  ), [mediaKindFilter, searchedKnowledgeMediaAssets]);
  const activeMediaAssets = mediaSourceTab === "project" ? filteredProjectMediaAssets : filteredKnowledgeMediaAssets;
  const activeMediaSourceLabel = mediaSourceTab === "project" ? veText("project_media") : veText("knowledge_media");
  const activeMediaKindLabel = veText(`media_filter.${mediaKindFilter}`);
  const mediaPickerEmptyMessage = useMemo(() => {
    if (mediaKindFilter !== "all") {
      return veText("media_picker_empty_filter", {
        kind: activeMediaKindLabel,
        source: activeMediaSourceLabel,
      });
    }
    if (mediaSourceTab === "knowledge") {
      return trimmedMediaSearch ? veText("no_knowledge_media") : veText("media_picker_no_recent_knowledge");
    }
    return trimmedMediaSearch ? veText("no_matching_project_media") : veText("bin_empty");
  }, [activeMediaKindLabel, activeMediaSourceLabel, mediaKindFilter, mediaSourceTab, trimmedMediaSearch]);

  useEffect(() => {
    if (mediaKindFilter === "all") return;
    if (activeMediaKindCounts[mediaKindFilter] > 0) return;
    if (mediaSourceTab === "project" && projectAssetsQuery.isLoading) return;
    if (mediaSourceTab === "knowledge" && knowledgeMediaQuery.isLoading) return;

    const fallbackFilter: MediaKindFilter =
      activeMediaKindCounts.video > 0 ? "video"
      : activeMediaKindCounts.audio > 0 ? "audio"
      : activeMediaKindCounts.project > 0 ? "project"
      : "all";

    if (fallbackFilter !== mediaKindFilter) {
      setMediaKindFilter(fallbackFilter);
      setMediaPickerSelectedIds([]);
    }
  }, [
    activeMediaKindCounts,
    knowledgeMediaQuery.isLoading,
    mediaKindFilter,
    mediaSourceTab,
    projectAssetsQuery.isLoading,
  ]);

  const mediaAssetsById = useMemo(() => {
    const assets = new Map<string, Document>();
    [...projectMediaAssets, ...knowledgeMediaAssets].forEach((asset) => assets.set(asset.id, asset));
    return assets;
  }, [knowledgeMediaAssets, projectMediaAssets]);
  const mediaPickerSelectedAssets = useMemo(() => (
    mediaPickerSelectedIds
      .map((id) => mediaAssetsById.get(id))
      .filter((asset): asset is Document => Boolean(asset))
  ), [mediaAssetsById, mediaPickerSelectedIds]);
  const timelineDuration = useMemo(
    () => clips.reduce((total, clip) => total + Math.max(0, clip.sourceEnd - clip.sourceStart), 0),
    [clips],
  );
  const timelineViewportLaneWidth = Math.max(
    MIN_TIMELINE_LANE_WIDTH,
    timelineViewportWidth > 0 ? timelineViewportWidth - TIMELINE_LABEL_COLUMN_WIDTH : 0,
  );
  const timelineLaneWidth = useMemo(
    () => Math.max(
      MIN_TIMELINE_LANE_WIDTH,
      timelineViewportLaneWidth,
      timelineDuration * timelinePixelsPerSecond,
    ),
    [timelineDuration, timelinePixelsPerSecond, timelineViewportLaneWidth],
  );
  const timelineEffectivePixelsPerSecond = timelineDuration > 0
    ? timelineLaneWidth / timelineDuration
    : timelinePixelsPerSecond;
  const timelineTrackWidth = timelineLaneWidth + TIMELINE_LABEL_COLUMN_WIDTH;
  const timelineTrackStyle = useMemo(
    () => ({
      "--ve-lane-width": `${timelineLaneWidth}px`,
      "--ve-second-width": `${timelineEffectivePixelsPerSecond}px`,
      width: `${timelineTrackWidth}px`,
    }) as CSSProperties,
    [timelineEffectivePixelsPerSecond, timelineLaneWidth, timelineTrackWidth],
  );
  const timelinePlayheadLeft = timelineDuration
    ? TIMELINE_LABEL_COLUMN_WIDTH + (clamp(playhead, 0, timelineDuration) / timelineDuration) * timelineLaneWidth
    : TIMELINE_LABEL_COLUMN_WIDTH;
  const timelinePlayheadHitLeft = clamp(
    timelinePlayheadLeft - TIMELINE_PLAYHEAD_HITBOX_WIDTH / 2,
    TIMELINE_LABEL_COLUMN_WIDTH,
    Math.max(TIMELINE_LABEL_COLUMN_WIDTH, timelineTrackWidth - TIMELINE_PLAYHEAD_HITBOX_WIDTH),
  );
  const timelinePlayheadLineOffset = clamp(
    timelinePlayheadLeft - timelinePlayheadHitLeft,
    0,
    TIMELINE_PLAYHEAD_HITBOX_WIDTH,
  );
  const timelinePlayheadStyle = useMemo(
    () => ({
      left: `${timelinePlayheadHitLeft}px`,
      width: `${TIMELINE_PLAYHEAD_HITBOX_WIDTH}px`,
      "--ve-playhead-line-offset": `${timelinePlayheadLineOffset}px`,
    }) as CSSProperties,
    [timelinePlayheadHitLeft, timelinePlayheadLineOffset],
  );
  useEffect(() => {
    const scroller = timelineScrollRef.current;
    if (!scroller) return undefined;

    const updateTimelineViewport = () => {
      setTimelineViewportWidth(Math.floor(scroller.clientWidth || 0));
    };

    updateTimelineViewport();

    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(updateTimelineViewport);
      observer.observe(scroller);
      return () => observer.disconnect();
    }

    window.addEventListener("resize", updateTimelineViewport);
    return () => window.removeEventListener("resize", updateTimelineViewport);
  }, [timelineDuration]);
  const timelineRulerTicks = useMemo(() => {
    if (timelineDuration <= 0) return [];
    const intervals = [0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
    const interval = intervals.find((candidate) => candidate * timelineEffectivePixelsPerSecond >= 72) ?? intervals[intervals.length - 1];
    const ticks: { time: number; major: boolean }[] = [];
    let index = 0;
    for (let time = 0; time <= timelineDuration + 0.001; time += interval) {
      ticks.push({ time: clamp(Number(time.toFixed(3)), 0, timelineDuration), major: index % 2 === 0 });
      index += 1;
    }
    const lastTick = ticks[ticks.length - 1];
    if (!lastTick || Math.abs(lastTick.time - timelineDuration) > 0.05) {
      ticks.push({ time: timelineDuration, major: true });
    }
    return ticks;
  }, [timelineDuration, timelineEffectivePixelsPerSecond]);
  const videoClipSpans = useMemo(() => getClipTimelineSpans(clips), [clips]);
  const normalizedWorkArea = useMemo(() => normalizeWorkArea(workArea, timelineDuration), [timelineDuration, workArea]);
  const exportRangeStart = normalizedWorkArea.enabled ? normalizedWorkArea.start : 0;
  const exportRangeEnd = normalizedWorkArea.enabled ? normalizedWorkArea.end : timelineDuration;
  const exportRangeDuration = Math.max(0, exportRangeEnd - exportRangeStart);
  const workAreaLeft = timelineDuration ? TIMELINE_LABEL_COLUMN_WIDTH + (normalizedWorkArea.start / timelineDuration) * timelineLaneWidth : TIMELINE_LABEL_COLUMN_WIDTH;
  const workAreaWidth = timelineDuration ? ((normalizedWorkArea.end - normalizedWorkArea.start) / timelineDuration) * timelineLaneWidth : 0;
  const currentTrackState = useMemo<EditorTrackState>(() => ({
    clips,
    shotBeats,
    captions,
    audioCues,
    markers,
  }), [audioCues, captions, clips, markers, shotBeats]);
  const serializedTrackState = useMemo(() => serializeEditorTrackState(currentTrackState), [currentTrackState]);
  const canUndo = useMemo(() => undoStackRef.current.length > 0, [historyVersion]);
  const canRedo = useMemo(() => redoStackRef.current.length > 0, [historyVersion]);
  const selectedClip = selection?.type === "clip" ? clips.find((clip) => clip.id === selection.id) ?? null : null;
  const selectedClipIndex = selectedClip ? clips.findIndex((clip) => clip.id === selectedClip.id) : -1;
  const selectedShot = selection?.type === "shot" ? shotBeats.find((shot) => shot.id === selection.id) ?? null : null;
  const selectedCaption = selection?.type === "caption" ? captions.find((caption) => caption.id === selection.id) ?? null : null;
  const selectedAudio = selection?.type === "audio" ? audioCues.find((cue) => cue.id === selection.id) ?? null : null;
  const selectedMarker = selection?.type === "marker" ? markers.find((marker) => marker.id === selection.id) ?? null : null;
  const selectedTrackId = selection ? trackForSelectionType(selection.type) : null;
  const selectedTrackLocked = selectedTrackId ? trackStates[selectedTrackId].locked : false;
  const timelineSnapPoints = useMemo(
    () => getTimelineSnapPoints(clips, shotBeats, captions, audioCues, markers, timelineDuration),
    [audioCues, captions, clips, markers, shotBeats, timelineDuration],
  );
  const snapTimelineTime = useCallback((value: number, threshold = 0.12) => {
    const safe = clamp(value, 0, timelineDuration);
    if (!snapEnabled || timelineSnapPoints.length === 0) return safe;
    let nearest = safe;
    let distance = threshold;
    for (const point of timelineSnapPoints) {
      const nextDistance = Math.abs(point - safe);
      if (nextDistance <= distance) {
        nearest = point;
        distance = nextDistance;
      }
    }
    return clamp(nearest, 0, timelineDuration);
  }, [snapEnabled, timelineDuration, timelineSnapPoints]);
  const activeShot = useMemo(
    () => trackStates.shots.visible ? shotBeats.find((shot) => playhead >= shot.start && playhead <= shot.end) ?? null : null,
    [playhead, shotBeats, trackStates.shots.visible],
  );
  const activeCaption = useMemo(
    () => trackStates.captions.visible ? captions.find((caption) => playhead >= caption.start && playhead <= caption.end) ?? null : null,
    [captions, playhead, trackStates.captions.visible],
  );
  const activeAudioCue = useMemo(
    () => trackStates.audio.muted ? null : audioCues.find((cue) => !cue.muted && playhead >= cue.start && playhead <= cue.end) ?? null,
    [audioCues, playhead, trackStates.audio.muted],
  );
  const aiHighlightKeys = useMemo(
    () => new Set((aiEditNotice?.highlights ?? []).map((item) => `${item.type}:${item.id}`)),
    [aiEditNotice],
  );
  const hasAiHighlight = useCallback(
    (type: NonNullable<Selection>["type"], id: string) => aiHighlightKeys.has(`${type}:${id}`),
    [aiHighlightKeys],
  );
  const renderIssues = useMemo<RenderIssue[]>(() => {
    const issues: RenderIssue[] = [];
    if (clips.length === 0 || timelineDuration <= 0) {
      issues.push({
        id: "timeline-empty",
        tone: "blocker",
        label: veText("issue.timeline_empty.label"),
        detail: veText("issue.timeline_empty.detail"),
      });
    }
    if (timelineDuration > 0 && exportRangeDuration <= 0.05) {
      issues.push({
        id: "range-empty",
        tone: "blocker",
        label: veText("issue.range_empty.label"),
        detail: veText("issue.range_empty.detail"),
      });
    }
    if (normalizedWorkArea.enabled && exportRangeDuration > 0.05) {
      issues.push({
        id: "range-enabled",
        tone: "info",
        label: veText("issue.range_enabled.label", { start: formatTime(exportRangeStart), end: formatTime(exportRangeEnd) }),
        detail: veText("issue.range_enabled.detail"),
      });
    }
    const emptyCaptionCount = captions.filter((caption) => caption.text.trim().length === 0).length;
    if (trackStates.captions.visible && emptyCaptionCount > 0) {
      issues.push({
        id: "empty-captions",
        tone: "warning",
        label: veText("issue.empty_captions.label", { count: emptyCaptionCount }),
        detail: veText("issue.empty_captions.detail"),
      });
    }
    const missingAudioCount = audioCues.filter((cue) => (
      !cue.muted &&
      !trackStates.audio.muted &&
      !cue.assetDocumentId &&
      cue.prompt.trim().length === 0
    )).length;
    if (missingAudioCount > 0) {
      issues.push({
        id: "audio-without-source",
        tone: "warning",
        label: veText("issue.audio_without_source.label", { count: missingAudioCount }),
        detail: veText("issue.audio_without_source.detail"),
      });
    }
    const missingReplacementCount = clips.filter((clip) => clip.assetDocumentId && !clip.assetName).length;
    if (missingReplacementCount > 0) {
      issues.push({
        id: "replacement-name-missing",
        tone: "warning",
        label: veText("issue.replacement_name_missing.label", { count: missingReplacementCount }),
        detail: veText("issue.replacement_name_missing.detail"),
      });
    }
    if (!trackStates.video.visible) {
      issues.push({
        id: "video-hidden",
        tone: "info",
        label: veText("issue.video_hidden.label"),
        detail: veText("issue.video_hidden.detail"),
      });
    }
    if (trackStates.captions.visible === false && captions.length > 0) {
      issues.push({
        id: "captions-hidden",
        tone: "info",
        label: veText("issue.captions_hidden.label"),
        detail: veText("issue.captions_hidden.detail"),
      });
    }
    if (trackStates.audio.muted && audioCues.length > 0) {
      issues.push({
        id: "audio-track-muted",
        tone: "info",
        label: veText("issue.audio_muted.label"),
        detail: veText("issue.audio_muted.detail"),
      });
    }
    return issues;
  }, [audioCues, captions, clips, exportRangeDuration, exportRangeEnd, exportRangeStart, normalizedWorkArea.enabled, timelineDuration, trackStates.audio.muted, trackStates.captions.visible, trackStates.video.visible]);
  const renderBlockers = renderIssues.filter((issue) => issue.tone === "blocker");
  const renderWarnings = renderIssues.filter((issue) => issue.tone === "warning");
  const nudgeStepOptions = [
    { value: "0.05", label: "0.05s" },
    { value: "0.1", label: "0.1s" },
    { value: "0.25", label: "0.25s" },
    { value: "0.5", label: "0.5s" },
    { value: "1", label: "1s" },
  ];
  const captionStyleOptions = Object.keys(CAPTION_STYLE_LABELS).map((value) => ({
    value,
    label: captionStyleDisplayLabel(value as CaptionCue["style"]),
  }));
  const captionAlignOptions = (["left", "center", "right"] as CanvasTextAlign[]).map((value) => ({
    value,
    label: veText(`align.${value}`),
  }));
  const audioTypeOptions = Object.keys(AUDIO_TYPE_LABELS).map((value) => ({
    value,
    label: audioTypeDisplayLabel(value as AudioCueType),
  }));
  const compactSelectButtonStyle: CSSProperties = {
    height: 30,
    minHeight: 30,
    borderRadius: 8,
    borderColor: "#e7e5e4",
    background: "#ffffff",
    boxShadow: "none",
    padding: "0 9px",
    fontSize: 12,
    fontWeight: 800,
  };

  const resetEditorHistory = useCallback(() => {
    undoStackRef.current = [];
    redoStackRef.current = [];
    lastHistoryStateRef.current = null;
    latestTrackStateRef.current = null;
    historyTransactionRef.current = null;
    restoringHistoryRef.current = true;
    setHistoryVersion((version) => version + 1);
  }, []);

  const updateTimelineTrackState = useCallback((track: TimelineTrackId, patch: Partial<TimelineTrackState>) => {
    setTrackStates((current) => ({
      ...current,
      [track]: {
        ...current[track],
        ...patch,
      },
    }));
  }, []);

  const commitHistoryChange = useCallback((before: EditorTrackState, after: EditorTrackState) => {
    if (serializeEditorTrackState(before) === serializeEditorTrackState(after)) {
      lastHistoryStateRef.current = cloneEditorTrackState(after);
      return;
    }
    undoStackRef.current = [...undoStackRef.current, cloneEditorTrackState(before)].slice(-80);
    redoStackRef.current = [];
    lastHistoryStateRef.current = cloneEditorTrackState(after);
    setHistoryVersion((version) => version + 1);
  }, []);

  const beginEditorTransaction = useCallback(() => {
    if (historyTransactionRef.current) return;
    const snapshot = cloneEditorTrackState(latestTrackStateRef.current ?? currentTrackState);
    historyTransactionRef.current = { before: snapshot, closing: false };
  }, [currentTrackState]);

  const finishEditorTransaction = useCallback(() => {
    const transaction = historyTransactionRef.current;
    if (!transaction || transaction.closing) return;
    transaction.closing = true;
    window.setTimeout(() => {
      const pending = historyTransactionRef.current;
      if (!pending) return;
      const after = cloneEditorTrackState(latestTrackStateRef.current ?? currentTrackState);
      historyTransactionRef.current = null;
      commitHistoryChange(pending.before, after);
    }, 0);
  }, [commitHistoryChange, currentTrackState]);

  useEffect(() => {
    const snapshot = cloneEditorTrackState(currentTrackState);
    const previous = lastHistoryStateRef.current;
    latestTrackStateRef.current = snapshot;

    if (historyTransactionRef.current) {
      lastHistoryStateRef.current = snapshot;
      return;
    }

    if (restoringHistoryRef.current) {
      lastHistoryStateRef.current = snapshot;
      restoringHistoryRef.current = false;
      setHistoryVersion((version) => version + 1);
      return;
    }

    if (!previous) {
      lastHistoryStateRef.current = snapshot;
      return;
    }

    if (serializeEditorTrackState(previous) !== serializedTrackState) {
      commitHistoryChange(previous, snapshot);
    }
  }, [commitHistoryChange, currentTrackState, serializedTrackState]);

  const stopPreviewAudio = useCallback(() => {
    previewAudioRef.current.forEach(({ audio }) => {
      audio.pause();
    });
  }, []);

  const getClipAssetUrl = useCallback(async (documentId: string) => {
    const existing = clipAssetUrlRef.current.get(documentId);
    if (existing) return existing;
    const url = await api.documents.download(documentId);
    clipAssetUrlRef.current.set(documentId, url);
    return url;
  }, []);

  const ensurePreviewVideoSource = useCallback(async (clip: ClipSegment) => {
    const video = videoRef.current;
    let nextUrl = downloadUrl;
    if (clip.assetDocumentId) {
      try {
        nextUrl = await getClipAssetUrl(clip.assetDocumentId);
      } catch (error) {
        console.warn("Timeline clip video could not be loaded; falling back to source media", clip.assetName ?? clip.assetDocumentId, error);
      }
    }
    if (!video || !nextUrl) return video;
    const currentUrl = video.currentSrc || video.src;
    if (currentUrl !== nextUrl) {
      if (!video.paused) {
        suppressPreviewPauseRef.current = true;
        video.pause();
        window.setTimeout(() => {
          suppressPreviewPauseRef.current = false;
        }, 250);
      }
      setPreviewSourceUrl(nextUrl);
      video.src = nextUrl;
      video.load();
      if (video.readyState < 1) await waitForVideoEvent(video, "loadedmetadata").catch(() => undefined);
    }
    return video;
  }, [downloadUrl, getClipAssetUrl]);

  const syncPreviewAudio = useCallback(async (timelineTime: number, playing: boolean) => {
    if (trackStates.audio.muted) {
      stopPreviewAudio();
      return;
    }
    const activeIds = new Set(audioCues.map((cue) => cue.id));
    previewAudioRef.current.forEach(({ audio, url, generated }, cueId) => {
      if (!activeIds.has(cueId)) {
        clearMediaElementSource(audio, url);
        if (!generated) revokeObjectUrlSoon(url);
        previewAudioRef.current.delete(cueId);
      }
    });

    for (const cue of audioCues) {
      const cueDuration = cue.end - cue.start;
      const cueOffset = timelineTime - cue.start;
      const active = playing && !cue.muted && cueOffset >= 0 && cueOffset <= cueDuration;
      let preview = previewAudioRef.current.get(cue.id);
      if (!active) {
        preview?.audio.pause();
        continue;
      }
      if (!preview) {
        try {
          const generated = !cue.assetDocumentId;
          const url = cue.assetDocumentId
            ? await api.documents.download(cue.assetDocumentId)
            : createGeneratedAudioPreviewUrl(cue.type);
          const audio = new Audio(url);
          audio.preload = "auto";
          audio.loop = cue.loop;
          preview = { audio, url, generated };
          previewAudioRef.current.set(cue.id, preview);
        } catch (error) {
          console.warn("Preview audio cue could not be loaded", cue.label, error);
          continue;
        }
      }
      const audio = preview.audio;
      audio.loop = cue.loop;
      const mediaDuration = Number.isFinite(audio.duration) ? audio.duration : 0;
      if (!cue.loop && mediaDuration > 0 && cueOffset > mediaDuration) {
        audio.pause();
        continue;
      }
      audio.volume = clamp(getAudioCueGain(cue, timelineTime, audioCues), 0, 1);
      const targetTime = getAudioCueSourceTime(cue, cueOffset, mediaDuration);
      if (Math.abs(audio.currentTime - targetTime) > 0.2) {
        audio.currentTime = targetTime;
      }
      if (audio.paused) {
        await audio.play().catch(() => undefined);
      }
    }
  }, [audioCues, stopPreviewAudio, trackStates.audio.muted]);

  useEffect(() => {
    const mapped = mapTimelineTime(playhead, clips);
    if (videoRef.current && mapped) {
      videoRef.current.muted = shouldMuteSourceVideoAudio(mapped.clip, playhead, audioCues, trackStates);
    }
    if (trackStates.audio.muted) {
      stopPreviewAudio();
    } else if (isPlaying) {
      void syncPreviewAudio(playhead, true);
    }
  }, [audioCues, clips, isPlaying, playhead, stopPreviewAudio, syncPreviewAudio, trackStates]);

  useEffect(() => {
    if (isPlaying || draggingClipId) return;
    const mapped = mapTimelineTime(playhead, clips);
    if (!mapped || !videoRef.current) return;
    let cancelled = false;
    void ensurePreviewVideoSource(mapped.clip).then((video) => {
      if (cancelled || !video) return;
      video.muted = shouldMuteSourceVideoAudio(mapped.clip, playhead, audioCues, trackStates);
      const targetSourceTime = previewSourceTime(mapped);
      if (Math.abs(video.currentTime - targetSourceTime) > 0.04) {
        video.currentTime = targetSourceTime;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [audioCues, clips, draggingClipId, ensurePreviewVideoSource, isPlaying, playhead, trackStates]);

  useEffect(() => {
    setWorkArea((current) => normalizeWorkArea(current, timelineDuration));
  }, [timelineDuration]);

  useEffect(() => () => {
    if (playbackClockRef.current?.rafId != null) {
      window.cancelAnimationFrame(playbackClockRef.current.rafId);
    }
    playbackClockRef.current = null;
    previewAudioRef.current.forEach(({ audio, url, generated }) => {
      clearMediaElementSource(audio, url);
      if (!generated) revokeObjectUrlSoon(url);
    });
    previewAudioRef.current.clear();
    const mediaPreview = mediaAssetPreviewRef.current;
    if (mediaPreview) {
      clearMediaElementSource(mediaPreview.audio, mediaPreview.url);
      revokeObjectUrlSoon(mediaPreview.url);
      mediaAssetPreviewRef.current = null;
    }
    mediaAssetPreviewRequestRef.current += 1;
    clipAssetUrlRef.current.forEach((url) => revokeObjectUrlSoon(url));
    clipAssetUrlRef.current.clear();
  }, []);

  useEffect(() => {
    setSourceDocOverride(null);
    setPendingRouteRecipe(null);
    setRecipeDoc(null);
    setTrackStates(createDefaultTimelineTrackStates());
    setWorkArea(createDefaultWorkArea());
    setMarkers([]);
    setScanningProjectRecipe(false);
    initializedDocRef.current = null;
    autoProjectTimelineRef.current = null;
  }, [docId]);

  useEffect(() => {
    let alive = true;
    if (!routeDoc || !routeIsRecipe) return undefined;
    setLoadingRecipe(true);
    api.documents.getContent(routeDoc.id)
      .then(async ({ content }) => {
        const recipe = JSON.parse(content) as VideoEditRecipe;
        const sourceId = recipe.source_document?.id;
        if (!sourceId) throw new Error("Recipe does not include a source video id");
        const sourceDoc = await api.documents.get(sourceId);
        if (!isVideoDocument(sourceDoc)) throw new Error("Recipe source is not a video document");
        if (!alive) return;
        setRecipeDoc(routeDoc);
        setSourceDocOverride(sourceDoc);
        setPendingRouteRecipe(recipe);
      })
      .catch((error) => {
        console.error(error);
        if (alive) toast.error(veText("toast.open_project_failed"), error instanceof Error ? error.message : undefined);
      })
      .finally(() => {
        if (alive) setLoadingRecipe(false);
      });
    return () => {
      alive = false;
    };
  }, [routeDoc, routeIsRecipe, toast]);

  useEffect(() => {
    let alive = true;
    if (
      !routeDoc
      || routeIsRecipe
      || !isVideoDocument(routeDoc)
      || sourceDocOverride
      || recipeDoc
      || recipeQuery.data
      || recipeQuery.isLoading
      || pendingRouteRecipe
      || projectRecipeCandidatesQuery.isLoading
      || projectRecipeDocs.length === 0
    ) {
      return undefined;
    }

    setScanningProjectRecipe(true);
    (async () => {
      const inspected: Array<{ candidate: Document; recipe: VideoEditRecipe; score: number }> = [];
      const useCandidate = async (candidate: Document, recipe: VideoEditRecipe) => {
        if (!alive) return;
        const sourceId = recipe.source_document?.id;
        if (sourceId && sourceId !== routeDoc.id) {
          try {
            const sourceDoc = await api.documents.get(sourceId);
            if (alive && isVideoDocument(sourceDoc)) {
              setSourceDuration(0);
              setSourceDocOverride(sourceDoc);
            }
          } catch (error) {
            console.warn("Linked video edit recipe source could not be loaded", error);
          }
        }
        if (!alive) return;
        setRecipeDoc(candidate);
        setPendingRouteRecipe(recipe);
      };

      try {
        for (const candidate of projectRecipeDocs) {
          try {
            const { content } = await api.documents.getContent(candidate.id);
            const recipe = JSON.parse(content) as VideoEditRecipe;
            inspected.push({ candidate, recipe, score: recipeFallbackScore(candidate, recipe) });
            if (recipeReferencesDocument(recipe, routeDoc)) {
              await useCandidate(candidate, recipe);
              return;
            }
          } catch (error) {
            console.warn("Video edit recipe candidate could not be inspected", candidate.name, error);
          }
        }

        const fallback = inspected
          .filter((item) => item.score > 0)
          .sort((a, b) => (
            b.score - a.score
            || (b.candidate.created_at || "").localeCompare(a.candidate.created_at || "")
          ))[0] ?? (inspected.length === 1 ? inspected[0] : null);

        if (fallback) {
          await useCandidate(fallback.candidate, fallback.recipe);
        }
      } finally {
        if (alive) setScanningProjectRecipe(false);
      }
    })();

    return () => {
      alive = false;
    };
  }, [
    pendingRouteRecipe,
    projectRecipeDocs,
    projectRecipeCandidatesQuery.isLoading,
    recipeDoc,
    recipeQuery.data,
    recipeQuery.isLoading,
    routeDoc,
    routeIsRecipe,
    sourceDocOverride,
  ]);

  useEffect(() => {
    let alive = true;
    let objectUrl = "";
    if (!sourceDocId) return undefined;
    setDownloadUrl("");
    autoLoadedRecipeRef.current = null;
    setSourceLoading(true);
    api.documents.download(sourceDocId)
      .then((url) => {
        if (!alive) {
          revokeObjectUrlSoon(url);
          return;
        }
        objectUrl = url;
        setDownloadUrl(url);
        setPreviewSourceUrl(url);
      })
      .catch((error) => {
        console.error(error);
        toast.error(veText("toast.video_download_failed"), error instanceof Error ? error.message : undefined);
      })
      .finally(() => {
        if (alive) setSourceLoading(false);
      });
    return () => {
      alive = false;
      if (objectUrl) {
        clearMediaElementSource(videoRef.current, objectUrl);
        revokeObjectUrlSoon(objectUrl);
      }
    };
  }, [sourceDocId, toast]);

  const seedTimeline = useCallback((duration: number) => {
    if (!sourceDocId || initializedDocRef.current === sourceDocId || duration <= 0) return;
    initializedDocRef.current = sourceDocId;
    resetEditorHistory();
    setTrackStates(createDefaultTimelineTrackStates());
    setWorkArea(createDefaultWorkArea(duration));
    setClips([
      {
        id: "clip-1",
        label: veText("default.source_clip"),
        sourceStart: 0,
        sourceEnd: duration,
        muted: false,
        color: CLIP_COLORS[0],
        assetDocumentId: null,
        assetName: null,
        assetMimeType: null,
        assetDuration: null,
        replacementPrompt: "",
        editNotes: "",
      },
    ]);
    setShotBeats([
      {
        id: "shot-1",
        title: veText("default.opening_beat"),
        scene: veText("default.scene", { index: 1 }),
        shot: veText("default.shot", { index: 1 }),
        start: 0,
        end: duration,
        location: "",
        camera: veText("default.medium_shot"),
        action: "",
        dialogue: "",
        notes: "",
      },
    ]);
    setCaptions([]);
    setAudioCues([]);
    setMarkers([]);
    setSelection({ type: "clip", id: "clip-1" });
    setPlayhead(0);
  }, [resetEditorHistory, sourceDocId]);

  const handleLoadedMetadata = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    const currentUrl = video.currentSrc || video.src;
    if (downloadUrl && currentUrl && currentUrl !== downloadUrl) return;
    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    const width = video.videoWidth || 1920;
    const height = video.videoHeight || 1080;
    setSourceDuration(duration);
    setMediaSize({ width, height });
    seedTimeline(duration);
  }, [downloadUrl, seedTimeline]);

  useEffect(() => {
    let alive = true;
    if (
      !routeDoc
      || routeIsRecipe
      || !isVideoDocument(routeDoc)
      || sourceDuration <= 0
      || sourceDocOverride
      || recipeDoc
      || recipeQuery.data
      || recipeQuery.isLoading
      || pendingRouteRecipe
      || projectRecipeCandidatesQuery.isLoading
      || scanningProjectRecipe
      || clips.length !== 1
    ) {
      return undefined;
    }

    const docsById = new Map<string, Document>();
    docsById.set(routeDoc.id, routeDoc);
    projectMediaAssets.forEach((asset) => {
      if (isVideoDocument(asset)) docsById.set(asset.id, asset);
    });
    const orderedProjectClips = sortProjectVideoDocuments(
      Array.from(docsById.values()).filter((asset) => projectClipOrder(asset) !== Number.MAX_SAFE_INTEGER),
    );
    if (orderedProjectClips.length < 2) return undefined;

    const signature = `${routeDoc.folder_id ?? "root"}:${orderedProjectClips.map((asset) => asset.id).join(",")}`;
    const loadingSignature = `loading:${signature}`;
    if (autoProjectTimelineRef.current === signature || autoProjectTimelineRef.current === loadingSignature) return undefined;
    autoProjectTimelineRef.current = loadingSignature;
    setLoadingRecipe(true);

    (async () => {
      let reconstructed = false;
      try {
        const nextClips: ClipSegment[] = [];
        for (const [index, asset] of orderedProjectClips.entries()) {
          let duration = 0;
          let durationReadFailed = false;
          try {
            const assetUrl = asset.id === routeDoc.id && downloadUrl ? downloadUrl : await getClipAssetUrl(asset.id);
            duration = await readVideoUrlDuration(assetUrl);
          } catch (error) {
            durationReadFailed = true;
            console.warn("Project clip duration could not be read; using fallback duration", asset.name, error);
          }
          const safeDuration = duration > 0 ? duration : asset.id === routeDoc.id ? sourceDuration : 5;
          nextClips.push({
            id: makeId("clip"),
            label: baseName(asset.name),
            sourceStart: 0,
            sourceEnd: safeDuration,
            muted: false,
            color: CLIP_COLORS[index % CLIP_COLORS.length],
            assetDocumentId: asset.id,
            assetName: asset.name,
            assetMimeType: asset.mime_type || asset.file_type || null,
            assetDuration: safeDuration,
            replacementPrompt: "",
            editNotes: durationReadFailed || duration <= 0
              ? veText("default.recovered_video_edit_note")
              : veText("default.imported_video_edit_note"),
          });
        }
        if (!alive || nextClips.length < 2) return;
        const nextDuration = nextClips.reduce((total, clip) => total + Math.max(0, clip.sourceEnd - clip.sourceStart), 0);
        let cursor = 0;
        const nextShots = nextClips.map((clip, index) => {
          const start = cursor;
          const clipDuration = Math.max(0.05, clip.sourceEnd - clip.sourceStart);
          cursor += clipDuration;
          return {
            id: makeId("shot"),
            title: clip.label,
            scene: veText("default.scene", { index: index + 1 }),
            shot: veText("default.shot", { index: index + 1 }),
            start,
            end: cursor,
            location: "",
            camera: veText("default.medium_shot"),
            action: "",
            dialogue: "",
            notes: "",
          } satisfies ShotBeat;
        });

        resetEditorHistory();
        setTrackStates(createDefaultTimelineTrackStates());
        setWorkArea(createDefaultWorkArea(nextDuration));
        setClips(nextClips);
        setShotBeats(nextShots);
        setCaptions([]);
        setAudioCues([]);
        setMarkers([]);
        setSelection({ type: "clip", id: nextClips[0].id });
        setPlayhead(0);
        autoProjectTimelineRef.current = signature;
        reconstructed = true;
      } catch (error) {
        console.warn("Project clip timeline could not be reconstructed", error);
      } finally {
        if (!reconstructed && autoProjectTimelineRef.current === loadingSignature) {
          autoProjectTimelineRef.current = null;
        }
        if (alive) setLoadingRecipe(false);
      }
    })();

    return () => {
      alive = false;
    };
  }, [
    clips.length,
    downloadUrl,
    getClipAssetUrl,
    pendingRouteRecipe,
    projectMediaAssets,
    projectRecipeCandidatesQuery.isLoading,
    recipeDoc,
    recipeQuery.data,
    recipeQuery.isLoading,
    resetEditorHistory,
    routeDoc,
    routeIsRecipe,
    scanningProjectRecipe,
    sourceDocOverride,
    sourceDuration,
  ]);

  const seekTimeline = useCallback((value: number) => {
    const next = snapTimelineTime(value);
    setPlayhead(next);
    const mapped = mapTimelineTime(next, clips);
    activePlaybackMapRef.current = mapped;
    if (mapped && videoRef.current) {
      void ensurePreviewVideoSource(mapped.clip).then((video) => {
        if (!video) return;
        video.currentTime = previewSourceTime(mapped);
        video.muted = shouldMuteSourceVideoAudio(mapped.clip, next, audioCues, trackStates);
      });
    }
    stopPreviewAudio();
  }, [audioCues, clips, ensurePreviewVideoSource, snapTimelineTime, stopPreviewAudio, trackStates]);

  const beginTimelineScrub = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || timelineDuration <= 0) return;
    const lane = event.currentTarget;
    const rect = lane.getBoundingClientRect();
    if (rect.width <= 0) return;
    event.preventDefault();
    try {
      lane.setPointerCapture(event.pointerId);
    } catch {
      // The window listeners below keep scrubbing reliable if capture is unavailable.
    }

    const updateFromClientX = (clientX: number) => {
      const time = timelineTimeFromLaneClientX(lane, clientX, timelineDuration);
      seekTimeline(time);
    };
    updateFromClientX(event.clientX);
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (lane.hasPointerCapture(event.pointerId)) lane.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale capture cleanup.
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [seekTimeline, timelineDuration]);

  const beginPlayheadDrag = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || timelineDuration <= 0) return;
    const playheadNode = event.currentTarget;
    const content = playheadNode.closest(".ve-timeline-content");
    if (!(content instanceof HTMLElement)) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      playheadNode.setPointerCapture(event.pointerId);
    } catch {
      // Window listeners below keep the drag reliable if capture is unavailable.
    }
    const contentRect = content.getBoundingClientRect();
    const laneLeft = contentRect.left + TIMELINE_LABEL_COLUMN_WIDTH;
    const laneWidth = Math.max(1, contentRect.width - TIMELINE_LABEL_COLUMN_WIDTH);
    const currentPlayheadClientX = laneLeft + (clamp(playheadRef.current, 0, timelineDuration) / timelineDuration) * laneWidth;
    const pointerOffset = event.clientX - currentPlayheadClientX;

    const updateFromClientX = (clientX: number) => {
      seekTimeline(timelineTimeFromContentClientX(content, clientX - pointerOffset, timelineDuration));
    };
    updateFromClientX(event.clientX);
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (playheadNode.hasPointerCapture(event.pointerId)) playheadNode.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale capture cleanup.
      }
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [seekTimeline, timelineDuration]);

  const pausePlayback = useCallback(() => {
    playbackAdvancePendingRef.current = false;
    if (playbackClockRef.current?.rafId != null) {
      window.cancelAnimationFrame(playbackClockRef.current.rafId);
    }
    playbackClockRef.current = null;
    activePlaybackMapRef.current = null;
    videoRef.current?.pause();
    stopPreviewAudio();
    setIsPlaying(false);
  }, [stopPreviewAudio]);

  const stopMediaAssetPreview = useCallback(() => {
    mediaAssetPreviewRequestRef.current += 1;
    const preview = mediaAssetPreviewRef.current;
    if (preview) {
      clearMediaElementSource(preview.audio, preview.url);
      revokeObjectUrlSoon(preview.url);
      mediaAssetPreviewRef.current = null;
    }
    setPreviewingMediaAssetId(null);
    setMediaAssetPreviewLoadingId(null);
  }, []);

  const toggleMediaAssetPreview = useCallback(async (asset: Document) => {
    if (mediaAssetPreviewRef.current?.assetId === asset.id) {
      stopMediaAssetPreview();
      return;
    }

    pausePlayback();
    stopMediaAssetPreview();
    const requestId = mediaAssetPreviewRequestRef.current + 1;
    mediaAssetPreviewRequestRef.current = requestId;
    setMediaAssetPreviewLoadingId(asset.id);

    let url: string | null = null;
    try {
      url = await api.documents.download(asset.id, { cache: true });
      if (mediaAssetPreviewRequestRef.current !== requestId) {
        revokeObjectUrlSoon(url);
        return;
      }

      const audio = new Audio(url);
      audio.preload = "auto";
      audio.volume = 1;
      audio.addEventListener("ended", () => {
        if (mediaAssetPreviewRef.current?.assetId === asset.id) {
          stopMediaAssetPreview();
        }
      }, { once: true });
      mediaAssetPreviewRef.current = { assetId: asset.id, audio, url };
      setPreviewingMediaAssetId(asset.id);

      audio.load();
      await waitForAudioReady(audio);
      if (mediaAssetPreviewRequestRef.current !== requestId) {
        clearMediaElementSource(audio, url);
        revokeObjectUrlSoon(url);
        if (mediaAssetPreviewRef.current?.assetId === asset.id) {
          mediaAssetPreviewRef.current = null;
        }
        return;
      }

      setMediaAssetPreviewLoadingId(null);
      await audio.play();
    } catch (error) {
      if (url && mediaAssetPreviewRef.current?.assetId !== asset.id) {
        revokeObjectUrlSoon(url);
      }
      if (mediaAssetPreviewRequestRef.current === requestId) {
        stopMediaAssetPreview();
        toast.error(veText("toast.audio_preview_failed"), error instanceof Error ? error.message : undefined);
      }
    }
  }, [pausePlayback, stopMediaAssetPreview, toast]);

  const previewVideoMediaAsset = useCallback(async (asset: Document) => {
    if (previewingMediaAssetId === asset.id && !mediaAssetPreviewRef.current) {
      videoRef.current?.pause();
      setPreviewingMediaAssetId(null);
      setMediaAssetPreviewLoadingId(null);
      return;
    }

    pausePlayback();
    stopMediaAssetPreview();
    const requestId = mediaAssetPreviewRequestRef.current + 1;
    mediaAssetPreviewRequestRef.current = requestId;
    setMediaAssetPreviewLoadingId(asset.id);

    try {
      const url = await getClipAssetUrl(asset.id);
      if (mediaAssetPreviewRequestRef.current !== requestId) return;
      const video = videoRef.current;
      if (!video) throw new Error("Preview player unavailable");

      setPreviewSourceUrl(url);
      if ((video.currentSrc || video.src) !== url) {
        video.src = url;
        video.load();
        if (video.readyState < 1) {
          await waitForVideoEvent(video, "loadedmetadata").catch(() => undefined);
        }
      }
      if (mediaAssetPreviewRequestRef.current !== requestId) return;

      video.muted = false;
      video.currentTime = 0;
      setPreviewingMediaAssetId(asset.id);
      setMediaAssetPreviewLoadingId(null);
      await video.play();
    } catch (error) {
      if (mediaAssetPreviewRequestRef.current === requestId) {
        videoRef.current?.pause();
        setPreviewingMediaAssetId(null);
        setMediaAssetPreviewLoadingId(null);
        toast.error(veText("toast.video_preview_failed"), error instanceof Error ? error.message : undefined);
      }
    }
  }, [getClipAssetUrl, pausePlayback, previewingMediaAssetId, stopMediaAssetPreview, toast]);

  const handlePreviewPause = useCallback(() => {
    if (suppressPreviewPauseRef.current || playbackAdvancePendingRef.current || playbackClockRef.current) {
      suppressPreviewPauseRef.current = false;
      return;
    }
    playbackAdvancePendingRef.current = false;
    stopPreviewAudio();
    setIsPlaying(false);
  }, [stopPreviewAudio]);

  const restoreTrackState = useCallback((state: EditorTrackState) => {
    const snapshot = cloneEditorTrackState(state);
    historyTransactionRef.current = null;
    restoringHistoryRef.current = true;
    if (playbackClockRef.current?.rafId != null) {
      window.cancelAnimationFrame(playbackClockRef.current.rafId);
    }
    playbackClockRef.current = null;
    activePlaybackMapRef.current = null;
    playbackAdvancePendingRef.current = false;
    videoRef.current?.pause();
    stopPreviewAudio();
    setIsPlaying(false);
    setClips(snapshot.clips);
    setShotBeats(snapshot.shotBeats);
    setCaptions(snapshot.captions);
    setAudioCues(snapshot.audioCues);
    setMarkers(snapshot.markers);
    setSelection(null);
    setPlayhead((current) => clamp(current, 0, editorTrackDuration(snapshot)));
  }, [stopPreviewAudio]);

  const undoHistory = useCallback(() => {
    const previous = undoStackRef.current.pop();
    if (!previous) return;
    redoStackRef.current = [...redoStackRef.current, cloneEditorTrackState(currentTrackState)].slice(-80);
    restoreTrackState(previous);
    setHistoryVersion((version) => version + 1);
  }, [currentTrackState, restoreTrackState]);

  const redoHistory = useCallback(() => {
    const next = redoStackRef.current.pop();
    if (!next) return;
    undoStackRef.current = [...undoStackRef.current, cloneEditorTrackState(currentTrackState)].slice(-80);
    restoreTrackState(next);
    setHistoryVersion((version) => version + 1);
  }, [currentTrackState, restoreTrackState]);

  const stopPlaybackClock = useCallback(() => {
    if (playbackClockRef.current?.rafId != null) {
      window.cancelAnimationFrame(playbackClockRef.current.rafId);
    }
    playbackClockRef.current = null;
  }, []);

  const startPlaybackClock = useCallback(() => {
    stopPlaybackClock();
    const clock = {
      activeClipId: activePlaybackMapRef.current?.clip.id ?? null,
      lastTickAt: performance.now(),
      rafId: null as number | null,
      switching: false,
    };
    playbackClockRef.current = clock;

    const finishPlayback = () => {
      playbackClockRef.current = null;
      activePlaybackMapRef.current = null;
      videoRef.current?.pause();
      stopPreviewAudio();
      setIsPlaying(false);
      setPlayhead(timelineDuration);
    };

    const requestTick = () => {
      clock.rafId = window.requestAnimationFrame(tick);
    };

    let tick: FrameRequestCallback = () => undefined;
    tick = () => {
      if (playbackClockRef.current !== clock) return;
      clock.lastTickAt = performance.now();
      const video = videoRef.current;
      const mapped = activePlaybackMapRef.current ?? mapTimelineTime(playheadRef.current, clips);
      if (!video || !mapped) {
        requestTick();
        return;
      }

      activePlaybackMapRef.current = mapped;
      if (clock.activeClipId !== mapped.clip.id) {
        if (clock.switching) return;
        clock.switching = true;
        void ensurePreviewVideoSource(mapped.clip).then((nextVideo) => {
          if (!nextVideo || playbackClockRef.current !== clock) return;
          const mediaDuration = Number.isFinite(nextVideo.duration) && nextVideo.duration > 0 ? nextVideo.duration : mapped.clip.sourceEnd;
          nextVideo.currentTime = clamp(mapped.sourceTime, 0, mediaDuration);
          nextVideo.muted = shouldMuteSourceVideoAudio(mapped.clip, mapped.timelineStart, audioCues, trackStates);
          clock.activeClipId = mapped.clip.id;
          clock.switching = false;
          if (nextVideo.paused) {
            void nextVideo.play().catch(() => undefined);
          }
          requestTick();
        }).catch((error) => {
          console.error(error);
          if (playbackClockRef.current !== clock) return;
          clock.switching = false;
          finishPlayback();
        });
        return;
      }

      const mediaDuration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : mapped.clip.sourceEnd;
      const sourceEnd = Math.min(mapped.clip.sourceEnd, mediaDuration);
      const sourceTime = clamp(video.currentTime, mapped.clip.sourceStart, sourceEnd);
      const timelineTime = clamp(
        mapped.timelineStart + Math.max(0, sourceTime - mapped.clip.sourceStart),
        0,
        timelineDuration,
      );
      video.muted = shouldMuteSourceVideoAudio(mapped.clip, timelineTime, audioCues, trackStates);

      setPlayhead(timelineTime);
      void syncPreviewAudio(timelineTime, true);

      if (video.ended || sourceTime >= sourceEnd - 0.04) {
        const nextTimelineTime = mapped.timelineEnd + PLAYBACK_BOUNDARY_EPSILON;
        if (nextTimelineTime >= timelineDuration - PLAYBACK_BOUNDARY_EPSILON) {
          finishPlayback();
          return;
        }
        const nextMapped = mapTimelineTime(nextTimelineTime, clips);
        if (!nextMapped) {
          finishPlayback();
          return;
        }
        activePlaybackMapRef.current = nextMapped;
        setPlayhead(clamp(nextTimelineTime, 0, timelineDuration));
        requestTick();
        return;
      }

      requestTick();
    };

    requestTick();
  }, [audioCues, clips, ensurePreviewVideoSource, stopPlaybackClock, stopPreviewAudio, syncPreviewAudio, timelineDuration, trackStates]);

  const isPlaybackClockFresh = useCallback(() => {
    const clock = playbackClockRef.current;
    return Boolean(clock && performance.now() - clock.lastTickAt < PLAYBACK_CLOCK_STALE_MS);
  }, []);

  const playTimelineFrom = useCallback(async (timelineTime: number) => {
    if (timelineDuration <= 0) return;
    stopMediaAssetPreview();
    const start = clamp(timelineTime, 0, timelineDuration);
    if (start >= timelineDuration - PLAYBACK_BOUNDARY_EPSILON) {
      stopPlaybackClock();
      activePlaybackMapRef.current = null;
      videoRef.current?.pause();
      stopPreviewAudio();
      setIsPlaying(false);
      setPlayhead(timelineDuration);
      return;
    }
    const mapped = mapTimelineTime(start, clips);
    if (!mapped) return;
    activePlaybackMapRef.current = mapped;
    setIsPlaying(true);
    setPlayhead(start);
    try {
      const activeVideo = await ensurePreviewVideoSource(mapped.clip);
      if (!activeVideo) return;
      activeVideo.currentTime = mapped.sourceTime;
      activeVideo.muted = shouldMuteSourceVideoAudio(mapped.clip, start, audioCues, trackStates);
      startPlaybackClock();
      void syncPreviewAudio(start, true);
      await activeVideo.play();
    } catch (error) {
      console.error(error);
      stopPlaybackClock();
      activePlaybackMapRef.current = null;
      playbackAdvancePendingRef.current = false;
      setIsPlaying(false);
      toast.warning(veText("toast.preview_blocked"), veText("toast.preview_blocked_detail"));
    }
  }, [audioCues, clips, ensurePreviewVideoSource, startPlaybackClock, stopMediaAssetPreview, stopPlaybackClock, stopPreviewAudio, syncPreviewAudio, timelineDuration, toast, trackStates]);

  const continuePlaybackFrom = useCallback((timelineTime: number) => {
    if (playbackAdvancePendingRef.current) return;
    playbackAdvancePendingRef.current = true;
    void playTimelineFrom(timelineTime).finally(() => {
      playbackAdvancePendingRef.current = false;
    });
  }, [playTimelineFrom]);

  const togglePlayback = useCallback(async () => {
    const video = videoRef.current;
    if (!video || timelineDuration <= 0) return;
    if (isPlaying) {
      pausePlayback();
      return;
    }
    playbackAdvancePendingRef.current = false;
    activePlaybackMapRef.current = null;
    const start = playhead >= timelineDuration - 0.03 ? 0 : playhead;
    await playTimelineFrom(start);
  }, [isPlaying, pausePlayback, playTimelineFrom, playhead, timelineDuration]);

  const handleTimeUpdate = useCallback(() => {
    if (!isPlaying) return;
    if (isPlaybackClockFresh()) return;
    stopPlaybackClock();
    const video = videoRef.current;
    if (!video) return;
    let mapped = activePlaybackMapRef.current;
    if (!mapped || !clips.some((clip) => clip.id === mapped?.clip.id)) {
      mapped = mapTimelineTime(playheadRef.current, clips);
      activePlaybackMapRef.current = mapped;
    }
    if (!mapped) return;
    const mediaDuration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : mapped.clip.sourceEnd;
    const sourceEnd = Math.min(mapped.clip.sourceEnd, mediaDuration);
    if (video.ended || video.currentTime >= sourceEnd - 0.04) {
      continuePlaybackFrom(mapped.timelineEnd + PLAYBACK_BOUNDARY_EPSILON);
      return;
    }
    const timelineTime = mapped.timelineStart + Math.max(0, video.currentTime - mapped.clip.sourceStart);
    setPlayhead(clamp(timelineTime, 0, timelineDuration));
    void syncPreviewAudio(timelineTime, true);
  }, [clips, continuePlaybackFrom, isPlaybackClockFresh, isPlaying, stopPlaybackClock, syncPreviewAudio, timelineDuration]);

  const handleVideoEnded = useCallback(() => {
    if (previewingMediaAssetId && !mediaAssetPreviewRef.current) {
      setPreviewingMediaAssetId(null);
      setMediaAssetPreviewLoadingId(null);
      return;
    }
    if (!isPlaying) return;
    if (isPlaybackClockFresh()) return;
    stopPlaybackClock();
    const mapped = activePlaybackMapRef.current ?? mapTimelineTime(playheadRef.current, clips);
    if (!mapped) {
      activePlaybackMapRef.current = null;
      stopPreviewAudio();
      setIsPlaying(false);
      return;
    }
    continuePlaybackFrom(mapped.timelineEnd + PLAYBACK_BOUNDARY_EPSILON);
  }, [clips, continuePlaybackFrom, isPlaybackClockFresh, isPlaying, previewingMediaAssetId, stopPlaybackClock, stopPreviewAudio]);

  const updateClip = useCallback((id: string, patch: Partial<ClipSegment>) => {
    setClips((current) => current.map((clip) => {
      if (clip.id !== id) return clip;
      const next = { ...clip, ...patch };
      const maxDuration = getClipMaxDuration(next, sourceDuration);
      next.sourceStart = clamp(next.sourceStart, 0, Math.max(0, maxDuration - 0.05));
      next.sourceEnd = clamp(next.sourceEnd, next.sourceStart + 0.05, maxDuration || next.sourceStart + 0.05);
      return next;
    }));
  }, [sourceDuration]);

  const applyClipReorderResult = useCallback((result: ClipReorderResult, selectedId: string) => {
    setClips(result.clips);
    setShotBeats(result.shotBeats);
    setCaptions(result.captions);
    setAudioCues(result.audioCues);
    setMarkers(result.markers);
    setSelection({ type: "clip", id: selectedId });
    const span = getClipTimelineSpans(result.clips).find((item) => item.clip.id === selectedId);
    if (span) setPlayhead(clamp(span.start, 0, editorTrackDuration(result)));
  }, []);

  const moveClip = useCallback((id: string, direction: -1 | 1) => {
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    const index = clips.findIndex((clip) => clip.id === id);
    const targetIndex = index + direction;
    if (index < 0 || targetIndex < 0 || targetIndex >= clips.length) return;

    const nextClips = moveArrayItem(clips, index, targetIndex).map((clip) => (
      clip.id === id
        ? { ...clip, editNotes: clip.editNotes || "Moved manually on the timeline." }
        : clip
    ));
    applyClipReorderResult(buildClipReorderResult(currentTrackState, nextClips), id);
  }, [applyClipReorderResult, clips, currentTrackState, toast, trackStates.video.locked]);

  const beginClipDrag = useCallback((event: ReactPointerEvent<HTMLButtonElement>, clipId: string) => {
    if (event.button !== 0 || timelineDuration <= 0 || trackStates.video.locked) return;
    const dragTarget = event.currentTarget;
    const lane = dragTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    if (rect.width <= 0) return;

    event.preventDefault();
    event.stopPropagation();
    try {
      dragTarget.setPointerCapture(event.pointerId);
    } catch {
      // Some browsers can refuse capture after the pointer leaves the element; document listeners still carry the drag.
    }
    setSelection({ type: "clip", id: clipId });

    const startX = event.clientX;
    const startY = event.clientY;
    const previousCursor = document.body.style.cursor;
    const previousUserSelect = document.body.style.userSelect;
    const baselineState = cloneEditorTrackState(currentTrackState);
    const clipById = new Map(baselineState.clips.map((clip) => [clip.id, clip]));
    let order = baselineState.clips.map((clip) => clip.id);
    let didDrag = false;

    const getOrderedClips = (ids = order) => ids
      .map((id) => clipById.get(id))
      .filter((clip): clip is ClipSegment => Boolean(clip));
    const getOrderedClipsWithoutDragged = () => getOrderedClips(order.filter((id) => id !== clipId));
    const buildOrderForTargetIndex = (targetIndex: number) => {
      const nextOrder = order.filter((id) => id !== clipId);
      nextOrder.splice(clamp(targetIndex, 0, nextOrder.length), 0, clipId);
      return nextOrder;
    };
    const applyDropPreview = (nextOrder: string[], targetIndex: number) => {
      setClipDropPreview({
        index: targetIndex,
        time: clipBoundaryTimeForIndex(getOrderedClips(nextOrder), targetIndex),
      });
    };
    const targetIndexFromClientX = (clientX: number) => {
      const pointerTime = timelineTimeFromLaneClientX(lane, clientX, timelineDuration);
      return clipInsertIndexAtTime(getOrderedClipsWithoutDragged(), pointerTime);
    };
    const applyOrder = (nextOrder: string[]) => {
      const nextClips = getOrderedClips(nextOrder).map((clip) => (
        clip.id === clipId
          ? { ...clip, editNotes: clip.editNotes || "Moved manually on the timeline." }
          : clip
      ));
      applyClipReorderResult(buildClipReorderResult(baselineState, nextClips), clipId);
    };
    const scrollTimelineNearEdge = (clientX: number) => {
      const scroller = timelineScrollRef.current;
      if (!scroller) return;
      const scrollerRect = scroller.getBoundingClientRect();
      const edge = 56;
      let delta = 0;
      if (clientX < scrollerRect.left + edge) {
        delta = -Math.ceil((edge - (clientX - scrollerRect.left)) / 3);
      } else if (clientX > scrollerRect.right - edge) {
        delta = Math.ceil((edge - (scrollerRect.right - clientX)) / 3);
      }
      if (delta !== 0) scroller.scrollLeft += delta;
    };

    const onMove = (moveEvent: PointerEvent) => {
      const distance = Math.hypot(moveEvent.clientX - startX, moveEvent.clientY - startY);
      if (!didDrag && distance < 6) return;
      if (!didDrag) {
        didDrag = true;
        beginEditorTransaction();
        setDraggingClipId(clipId);
        document.body.style.cursor = "grabbing";
        document.body.style.userSelect = "none";
      }

      moveEvent.preventDefault();
      scrollTimelineNearEdge(moveEvent.clientX);
      const targetIndex = targetIndexFromClientX(moveEvent.clientX);
      const nextOrder = buildOrderForTargetIndex(targetIndex);
      applyDropPreview(nextOrder, targetIndex);
      if (nextOrder.join("\u0000") === order.join("\u0000")) return;
      order = nextOrder;
      applyOrder(order);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (dragTarget.hasPointerCapture(event.pointerId)) dragTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale pointer capture cleanup.
      }
      document.body.style.cursor = previousCursor;
      document.body.style.userSelect = previousUserSelect;
      setDraggingClipId(null);
      setClipDropPreview(null);
      if (didDrag) finishEditorTransaction();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [applyClipReorderResult, beginEditorTransaction, currentTrackState, finishEditorTransaction, timelineDuration, trackStates.video.locked]);

  const duplicateClip = useCallback((clip: ClipSegment) => {
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    const spans = getClipTimelineSpans(clips);
    const span = spans.find((item) => item.clip.id === clip.id);
    if (!span || span.duration <= 0) return;
    const copy: ClipSegment = {
      ...clip,
      id: makeId("clip"),
      label: `${clip.label} Copy`,
      color: CLIP_COLORS[clips.length % CLIP_COLORS.length],
      editNotes: clip.editNotes || "Duplicated for manual story timing.",
    };
    const duplicateTimedItems = <T extends TimedTrackItem>(items: T[], clone: (item: T) => T) => sortTimedItems(items.flatMap((item) => {
      if (midpointInSpan(item, span)) return [item, clone(shiftTimedItem(item, span.duration))];
      if (item.start >= span.end - 0.001) return [shiftTimedItem(item, span.duration)];
      return [item];
    }));
    const duplicateMarkers = (items: TimelineMarker[]) => [...items].flatMap((marker) => {
      if (marker.time >= span.start - 0.001 && marker.time <= span.end + 0.001) {
        return [marker, { ...marker, id: makeId("marker"), label: `${marker.label} Copy`, time: marker.time + span.duration }];
      }
      if (marker.time >= span.end - 0.001) return [{ ...marker, time: marker.time + span.duration }];
      return [marker];
    }).sort((a, b) => a.time - b.time);

    setClips([
      ...clips.slice(0, span.index + 1),
      copy,
      ...clips.slice(span.index + 1),
    ]);
    setShotBeats((current) => duplicateTimedItems(current, (shot) => ({
      ...shot,
      id: makeId("shot"),
      title: `${shot.title} Copy`,
    })));
    setCaptions((current) => duplicateTimedItems(current, (caption) => ({
      ...caption,
      id: makeId("caption"),
    })));
    setAudioCues((current) => duplicateTimedItems(current, (cue) => ({
      ...cue,
      id: makeId("audio"),
      label: `${cue.label} Copy`,
    })));
    setMarkers((current) => duplicateMarkers(current));
    setSelection({ type: "clip", id: copy.id });
    setPlayhead(span.end);
  }, [clips, toast, trackStates.video.locked]);

  const beginClipTrim = useCallback((
    event: ReactPointerEvent<HTMLSpanElement>,
    clipId: string,
    edge: "start" | "end",
  ) => {
    if (event.button !== 0 || timelineDuration <= 0 || trackStates.video.locked) return;
    const resizeTarget = event.currentTarget;
    const lane = resizeTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    const clip = clips.find((item) => item.id === clipId);
    if (!clip || rect.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      resizeTarget.setPointerCapture(event.pointerId);
    } catch {
      // Window listeners below keep trimming reliable if capture is unavailable.
    }
    beginEditorTransaction();
    setSelection({ type: "clip", id: clipId });

    const startX = event.clientX;
    const startSource = clip.sourceStart;
    const endSource = clip.sourceEnd;
    let clipStartOnTimeline = 0;
    for (const item of clips) {
      if (item.id === clipId) break;
      clipStartOnTimeline += Math.max(0, item.sourceEnd - item.sourceStart);
    }
    const dragTimelineDuration = timelineDuration;
    const maxDuration = getClipMaxDuration(clip, sourceDuration);
    const updateFromClientX = (clientX: number) => {
      const startTime = timelineTimeFromLaneClientX(lane, startX, dragTimelineDuration);
      const nextTime = timelineTimeFromLaneClientX(lane, clientX, dragTimelineDuration);
      const delta = nextTime - startTime;
      if (edge === "start") {
        const sourceStart = clamp(startSource + delta, 0, endSource - 0.05);
        updateClip(clipId, { sourceStart, editNotes: clip.editNotes || "Trimmed manually on the timeline." });
        setPlayhead(clipStartOnTimeline);
      } else {
        const sourceEnd = clamp(endSource + delta, startSource + 0.05, maxDuration || endSource + Math.max(0, delta));
        updateClip(clipId, { sourceEnd, editNotes: clip.editNotes || "Trimmed manually on the timeline." });
        const nextDuration = Math.max(0.05, sourceEnd - startSource);
        setPlayhead(clipStartOnTimeline + nextDuration);
      }
    };
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (resizeTarget.hasPointerCapture(event.pointerId)) resizeTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale capture cleanup.
      }
      finishEditorTransaction();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [beginEditorTransaction, clips, finishEditorTransaction, sourceDuration, timelineDuration, trackStates.video.locked, updateClip]);

  const updateShot = useCallback((id: string, patch: Partial<ShotBeat>) => {
    setShotBeats((current) => current.map((shot) => {
      if (shot.id !== id) return shot;
      const next = { ...shot, ...patch };
      next.start = clamp(next.start, 0, timelineDuration);
      next.end = clamp(next.end, next.start + 0.05, Math.max(next.start + 0.05, timelineDuration));
      return next;
    }));
  }, [timelineDuration]);

  const updateCaption = useCallback((id: string, patch: Partial<CaptionCue>) => {
    setCaptions((current) => current.map((caption) => {
      if (caption.id !== id) return caption;
      const next = { ...caption, ...patch };
      next.start = clamp(next.start, 0, timelineDuration);
      next.end = clamp(next.end, next.start + 0.05, Math.max(next.start + 0.05, timelineDuration));
      next.x = clamp(next.x, 0, 100);
      next.y = clamp(next.y, 0, 100);
      next.size = clamp(next.size, 10, 96);
      next.backgroundOpacity = clamp(next.backgroundOpacity, 0, 1);
      return next;
    }));
  }, [timelineDuration]);

  const beginCaptionOverlayDrag = useCallback((event: ReactPointerEvent<HTMLDivElement>, caption: CaptionCue) => {
    if (event.button !== 0 || trackStates.captions.locked) return;
    const preview = event.currentTarget.closest(".ve-preview");
    if (!(preview instanceof HTMLElement)) return;
    const rect = preview.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    beginEditorTransaction();
    setSelection({ type: "caption", id: caption.id });

    const updateFromClient = (clientX: number, clientY: number) => {
      const x = clamp(((clientX - rect.left) / rect.width) * 100, 0, 100);
      const y = clamp(((clientY - rect.top) / rect.height) * 100, 0, 100);
      updateCaption(caption.id, { x, y });
    };
    updateFromClient(event.clientX, event.clientY);
    const onMove = (moveEvent: PointerEvent) => updateFromClient(moveEvent.clientX, moveEvent.clientY);
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      finishEditorTransaction();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [beginEditorTransaction, finishEditorTransaction, trackStates.captions.locked, updateCaption]);

  const updateAudioCue = useCallback((id: string, patch: Partial<AudioCue>) => {
    setAudioCues((current) => current.map((cue) => {
      if (cue.id !== id) return cue;
      const next = { ...cue, ...patch };
      next.start = clamp(next.start, 0, timelineDuration);
      next.end = clamp(next.end, next.start + 0.05, Math.max(next.start + 0.05, timelineDuration));
      next.volumeDb = clamp(next.volumeDb, -48, 6);
      const duration = Math.max(0.05, next.end - next.start);
      next.fadeIn = clamp(next.fadeIn, 0, Math.min(10, duration));
      next.fadeOut = clamp(next.fadeOut, 0, Math.min(10, duration));
      return next;
    }));
  }, [timelineDuration]);

  const updateMarker = useCallback((id: string, patch: Partial<TimelineMarker>) => {
    setMarkers((current) => current.map((marker) => {
      if (marker.id !== id) return marker;
      return {
        ...marker,
        ...patch,
        time: clamp(numberOr(patch.time, marker.time), 0, timelineDuration),
      };
    }).sort((a, b) => a.time - b.time));
  }, [timelineDuration]);

  const nudgeSelection = useCallback((direction: -1 | 1) => {
    if (selectedTrackLocked) {
      toast.warning(veText("toast.selected_track_locked"));
      return;
    }
    const delta = direction * nudgeStep;
    const moveRange = (start: number, end: number) => {
      const duration = Math.max(0.05, end - start);
      const nextStart = clamp(start + delta, 0, Math.max(0, timelineDuration - duration));
      return { start: nextStart, end: nextStart + duration };
    };

    if (selectedClip) {
      const duration = Math.max(0.05, selectedClip.sourceEnd - selectedClip.sourceStart);
      const maxDuration = getClipMaxDuration(selectedClip, sourceDuration);
      const sourceStart = clamp(selectedClip.sourceStart + delta, 0, Math.max(0, maxDuration - duration));
      updateClip(selectedClip.id, {
        sourceStart,
        sourceEnd: sourceStart + duration,
        editNotes: selectedClip.editNotes || "Slip-adjusted manually.",
      });
      const span = getClipTimelineSpans(clips).find((item) => item.clip.id === selectedClip.id);
      setPlayhead(span?.start ?? playhead);
      return;
    }

    if (selectedShot) {
      const next = moveRange(selectedShot.start, selectedShot.end);
      updateShot(selectedShot.id, next);
      setPlayhead(next.start);
      return;
    }

    if (selectedCaption) {
      const next = moveRange(selectedCaption.start, selectedCaption.end);
      updateCaption(selectedCaption.id, next);
      setPlayhead(next.start);
      return;
    }

    if (selectedAudio) {
      const next = moveRange(selectedAudio.start, selectedAudio.end);
      updateAudioCue(selectedAudio.id, next);
      setPlayhead(next.start);
      return;
    }

    if (selectedMarker) {
      const time = clamp(selectedMarker.time + delta, 0, timelineDuration);
      updateMarker(selectedMarker.id, { time });
      setPlayhead(time);
    }
  }, [
    clips,
    nudgeStep,
    playhead,
    selectedAudio,
    selectedCaption,
    selectedClip,
    selectedMarker,
    selectedShot,
    selectedTrackLocked,
    sourceDuration,
    timelineDuration,
    toast,
    updateAudioCue,
    updateCaption,
    updateClip,
    updateMarker,
    updateShot,
  ]);

  const fitTimelineZoom = useCallback(() => {
    const viewportWidth = timelineScrollRef.current?.clientWidth ?? 900;
    const laneViewportWidth = Math.max(320, viewportWidth - TIMELINE_LABEL_COLUMN_WIDTH);
    const nextPixelsPerSecond = timelineDuration > 0
      ? clamp(laneViewportWidth / timelineDuration, 16, 140)
      : 48;
    setTimelinePixelsPerSecond(nextPixelsPerSecond);
    if (timelineScrollRef.current) timelineScrollRef.current.scrollLeft = 0;
  }, [timelineDuration]);

  const setWorkAreaInPoint = useCallback(() => {
    setWorkArea((current) => {
      const normalized = normalizeWorkArea(current, timelineDuration);
      const start = clamp(playhead, 0, Math.max(0, timelineDuration - 0.05));
      const end = clamp(Math.max(normalized.end, start + 0.05), start + 0.05, Math.max(start + 0.05, timelineDuration));
      return { enabled: true, start, end };
    });
  }, [playhead, timelineDuration]);

  const setWorkAreaOutPoint = useCallback(() => {
    setWorkArea((current) => {
      const normalized = normalizeWorkArea(current, timelineDuration);
      const end = clamp(playhead, 0.05, timelineDuration);
      const start = clamp(Math.min(normalized.start, end - 0.05), 0, Math.max(0, end - 0.05));
      return { enabled: true, start, end };
    });
  }, [playhead, timelineDuration]);

  const resetWorkArea = useCallback(() => {
    setWorkArea(createDefaultWorkArea(timelineDuration));
  }, [timelineDuration]);

  const applyNormalizedRecipe = useCallback((
    normalized: NormalizedVideoEditRecipe,
    source: Document | null,
    showToast = true,
    focus?: AiEditFocus | null,
  ) => {
    const { state } = normalized;
    if (normalized.mediaSize) setMediaSize(normalized.mediaSize);
    resetEditorHistory();
    setTrackStates(normalized.trackStates);
    setWorkArea(normalized.workArea);
    setClips(state.clips);
    setShotBeats(state.shotBeats);
    setCaptions(state.captions);
    setAudioCues(state.audioCues);
    setMarkers(state.markers);
    setSelection(focus?.selection ?? { type: "clip", id: state.clips[0].id });
    setPlayhead(clamp(focus?.time ?? 0, 0, normalized.duration));
    if (showToast) toast.success(veText("toast.recipe_loaded"), source?.name);
  }, [resetEditorHistory, toast]);

  const applyRecipe = useCallback((recipe: VideoEditRecipe, source: Document | null, showToast = true, focus?: AiEditFocus | null) => {
    const normalized = normalizeVideoEditRecipe(recipe, sourceDuration);
    applyNormalizedRecipe(normalized, source, showToast, focus);
    return normalized;
  }, [applyNormalizedRecipe, sourceDuration]);

  const loadSavedRecipe = useCallback(async (showToast = true) => {
    const savedRecipeDoc = recipeDoc ?? recipeQuery.data;
    if (!savedRecipeDoc) return;
    setLoadingRecipe(true);
    try {
      const { content } = await api.documents.getContent(savedRecipeDoc.id);
      const recipe = JSON.parse(content) as VideoEditRecipe;
      if (recipe.source_document?.id && recipe.source_document.id !== sourceDocId) {
        toast.warning(veText("toast.recipe_source_differs"), veText("toast.recipe_source_differs_detail"));
        const sourceDoc = await api.documents.get(recipe.source_document.id);
        if (isVideoDocument(sourceDoc)) {
          setRecipeDoc(savedRecipeDoc);
          setSourceDuration(0);
          setSourceDocOverride(sourceDoc);
          setPendingRouteRecipe(recipe);
          return;
        }
      }
      applyRecipe(recipe, savedRecipeDoc, showToast);
      setRecipeDoc(savedRecipeDoc);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.load_recipe_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setLoadingRecipe(false);
    }
  }, [applyRecipe, recipeDoc, recipeQuery.data, sourceDocId, toast]);

  useEffect(() => {
    if (!pendingRouteRecipe || sourceDuration <= 0 || !recipeDoc) return;
    applyRecipe(pendingRouteRecipe, recipeDoc, false);
    setPendingRouteRecipe(null);
  }, [applyRecipe, pendingRouteRecipe, recipeDoc, sourceDuration]);

  useEffect(() => {
    if (routeIsRecipe) return;
    const savedRecipeDoc = recipeQuery.data;
    if (!savedRecipeDoc || sourceDuration <= 0 || autoLoadedRecipeRef.current === savedRecipeDoc.id) return;
    autoLoadedRecipeRef.current = savedRecipeDoc.id;
    void loadSavedRecipe(false);
  }, [loadSavedRecipe, recipeQuery.data, routeIsRecipe, sourceDuration]);

  const beginCueDrag = useCallback((
    event: ReactPointerEvent<HTMLButtonElement>,
    cueType: "shot" | "caption" | "audio",
    cueId: string,
  ) => {
    const trackId = cueType === "shot" ? "shots" : cueType === "caption" ? "captions" : "audio";
    if (event.button !== 0 || timelineDuration <= 0 || trackStates[trackId].locked) return;
    const dragTarget = event.currentTarget;
    const lane = dragTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    const cue = cueType === "caption"
      ? captions.find((item) => item.id === cueId)
      : cueType === "audio"
        ? audioCues.find((item) => item.id === cueId)
        : shotBeats.find((item) => item.id === cueId);
    if (!cue || rect.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      dragTarget.setPointerCapture(event.pointerId);
    } catch {
      // Window listeners below keep the drag alive if capture is unavailable.
    }
    beginEditorTransaction();
    setSelection({ type: cueType, id: cueId });

    const duration = Math.max(0.05, cue.end - cue.start);
    const pointerTime = timelineTimeFromLaneClientX(lane, event.clientX, timelineDuration);
    const grabOffset = clamp(pointerTime - cue.start, 0, duration);
    const updateFromClientX = (clientX: number) => {
      const rawTime = timelineTimeFromLaneClientX(lane, clientX, timelineDuration);
      const rawStart = clamp(rawTime - grabOffset, 0, Math.max(0, timelineDuration - duration));
      const start = clamp(snapTimelineTime(rawStart), 0, Math.max(0, timelineDuration - duration));
      const end = start + duration;
      if (cueType === "caption") updateCaption(cueId, { start, end });
      else if (cueType === "audio") updateAudioCue(cueId, { start, end });
      else updateShot(cueId, { start, end });
      setPlayhead(start);
    };
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (dragTarget.hasPointerCapture(event.pointerId)) dragTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale capture cleanup.
      }
      finishEditorTransaction();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [audioCues, beginEditorTransaction, captions, finishEditorTransaction, shotBeats, snapTimelineTime, timelineDuration, trackStates, updateAudioCue, updateCaption, updateShot]);

  const beginCueResize = useCallback((
    event: ReactPointerEvent<HTMLSpanElement>,
    cueType: "shot" | "caption" | "audio",
    cueId: string,
    edge: "start" | "end",
  ) => {
    const trackId = cueType === "shot" ? "shots" : cueType === "caption" ? "captions" : "audio";
    if (event.button !== 0 || timelineDuration <= 0 || trackStates[trackId].locked) return;
    const resizeTarget = event.currentTarget;
    const lane = event.currentTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    const cue = cueType === "caption"
      ? captions.find((item) => item.id === cueId)
      : cueType === "audio"
        ? audioCues.find((item) => item.id === cueId)
        : shotBeats.find((item) => item.id === cueId);
    if (!cue || rect.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      resizeTarget.setPointerCapture(event.pointerId);
    } catch {
      // Window listeners below keep resizing reliable if capture is unavailable.
    }
    beginEditorTransaction();
    setSelection({ type: cueType, id: cueId });
    const updateFromClientX = (clientX: number) => {
      const time = snapTimelineTime(timelineTimeFromLaneClientX(lane, clientX, timelineDuration));
      if (edge === "start") {
        const start = clamp(time, 0, cue.end - 0.05);
        if (cueType === "caption") updateCaption(cueId, { start });
        else if (cueType === "audio") updateAudioCue(cueId, { start });
        else updateShot(cueId, { start });
        setPlayhead(start);
      } else {
        const end = clamp(time, cue.start + 0.05, timelineDuration);
        if (cueType === "caption") updateCaption(cueId, { end });
        else if (cueType === "audio") updateAudioCue(cueId, { end });
        else updateShot(cueId, { end });
        setPlayhead(end);
      }
    };
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (resizeTarget.hasPointerCapture(event.pointerId)) resizeTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale capture cleanup.
      }
      finishEditorTransaction();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [audioCues, beginEditorTransaction, captions, finishEditorTransaction, shotBeats, snapTimelineTime, timelineDuration, trackStates, updateAudioCue, updateCaption, updateShot]);

  const beginMarkerDrag = useCallback((event: ReactPointerEvent<HTMLButtonElement>, markerId: string) => {
    if (event.button !== 0 || timelineDuration <= 0 || trackStates.markers.locked) return;
    const dragTarget = event.currentTarget;
    const lane = dragTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    if (rect.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    try {
      dragTarget.setPointerCapture(event.pointerId);
    } catch {
      // Window listeners below keep the marker drag alive if capture is unavailable.
    }
    beginEditorTransaction();
    setSelection({ type: "marker", id: markerId });

    const updateFromClientX = (clientX: number) => {
      const time = snapTimelineTime(timelineTimeFromLaneClientX(lane, clientX, timelineDuration));
      updateMarker(markerId, { time });
      setPlayhead(time);
    };
    updateFromClientX(event.clientX);
    const onMove = (moveEvent: PointerEvent) => {
      moveEvent.preventDefault();
      updateFromClientX(moveEvent.clientX);
    };
    const onEnd = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onEnd);
      window.removeEventListener("pointercancel", onEnd);
      try {
        if (dragTarget.hasPointerCapture(event.pointerId)) dragTarget.releasePointerCapture(event.pointerId);
      } catch {
        // Ignore stale capture cleanup.
      }
      finishEditorTransaction();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onEnd, { once: true });
    window.addEventListener("pointercancel", onEnd, { once: true });
  }, [beginEditorTransaction, finishEditorTransaction, snapTimelineTime, timelineDuration, trackStates.markers.locked, updateMarker]);

  const splitClipAtPlayhead = useCallback(() => {
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    const mapped = mapTimelineTime(playhead, clips);
    if (!mapped) return;
    const offset = playhead - mapped.timelineStart;
    const duration = mapped.clip.sourceEnd - mapped.clip.sourceStart;
    if (offset < 0.12 || duration - offset < 0.12) {
      toast.warning(veText("toast.playhead_outside_clip"), veText("toast.playhead_outside_clip_detail"));
      return;
    }
    const splitSource = mapped.clip.sourceStart + offset;
    const left: ClipSegment = {
      ...mapped.clip,
      id: makeId("clip"),
      label: `${mapped.clip.label} A`,
      sourceEnd: splitSource,
    };
    const right: ClipSegment = {
      ...mapped.clip,
      id: makeId("clip"),
      label: `${mapped.clip.label} B`,
      sourceStart: splitSource,
      color: CLIP_COLORS[(mapped.index + 1) % CLIP_COLORS.length],
    };
    setClips((current) => [
      ...current.slice(0, mapped.index),
      left,
      right,
      ...current.slice(mapped.index + 1),
    ]);
    setSelection({ type: "clip", id: right.id });
  }, [clips, playhead, toast, trackStates.video.locked]);

  const addCaption = useCallback(() => {
    if (trackStates.captions.locked) {
      toast.warning(veText("toast.captions_locked"));
      return;
    }
    const start = snapTimelineTime(playhead);
    const cue: CaptionCue = {
      id: makeId("caption"),
      speaker: "",
      emotion: "",
      style: "speechBubble",
      text: veText("default.caption_text"),
      start,
      end: clamp(start + 2.2, start + 0.05, Math.max(start + 0.05, timelineDuration)),
      x: 50,
      y: 84,
      size: 32,
      color: "#1c1917",
      background: "rgba(255,255,255,0.94)",
      backgroundColor: "#ffffff",
      backgroundOpacity: 0.94,
      align: "center",
    };
    setCaptions((current) => [...current, cue]);
    setSelection({ type: "caption", id: cue.id });
  }, [playhead, snapTimelineTime, timelineDuration, toast, trackStates.captions.locked]);

  const addShotBeat = useCallback(() => {
    if (trackStates.shots.locked) {
      toast.warning(veText("toast.shots_locked"));
      return;
    }
    const start = snapTimelineTime(playhead);
    const index = shotBeats.length + 1;
    const beat: ShotBeat = {
      id: makeId("shot"),
      title: veText("default.beat", { index }),
      scene: veText("default.scene", { index }),
      shot: veText("default.shot", { index }),
      start,
      end: clamp(start + 4, start + 0.05, Math.max(start + 0.05, timelineDuration)),
      location: "",
      camera: veText("default.medium_shot"),
      action: "",
      dialogue: "",
      notes: "",
    };
    setShotBeats((current) => [...current, beat]);
    setSelection({ type: "shot", id: beat.id });
  }, [playhead, shotBeats.length, snapTimelineTime, timelineDuration, toast, trackStates.shots.locked]);

  const addMarker = useCallback(() => {
    if (trackStates.markers.locked) {
      toast.warning(veText("toast.markers_locked"));
      return;
    }
    const marker: TimelineMarker = {
      id: makeId("marker"),
      time: snapTimelineTime(playhead),
      label: veText("default.marker", { index: markers.length + 1 }),
      color: MARKER_COLORS[markers.length % MARKER_COLORS.length],
      notes: "",
    };
    setMarkers((current) => [...current, marker].sort((a, b) => a.time - b.time));
    setSelection({ type: "marker", id: marker.id });
  }, [markers.length, playhead, snapTimelineTime, toast, trackStates.markers.locked]);

  const jumpToMarker = useCallback((direction: -1 | 1) => {
    if (markers.length === 0) return;
    const sortedMarkers = [...markers].sort((a, b) => a.time - b.time);
    const marker = direction < 0
      ? [...sortedMarkers].reverse().find((item) => item.time < playhead - 0.05) ?? sortedMarkers[sortedMarkers.length - 1]
      : sortedMarkers.find((item) => item.time > playhead + 0.05) ?? sortedMarkers[0];
    if (!marker) return;
    setSelection({ type: "marker", id: marker.id });
    seekTimeline(marker.time);
  }, [markers, playhead, seekTimeline]);

  const handleSubtitleFileSelected = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    if (trackStates.captions.locked) {
      toast.warning(veText("toast.captions_locked"));
      return;
    }
    try {
      const content = await file.text();
      const importedCaptions = parseSrtCaptions(content, timelineDuration);
      if (importedCaptions.length === 0) {
        toast.warning(veText("toast.no_subtitle_cues"), file.name);
        return;
      }
      setCaptions(importedCaptions);
      setSelection({ type: "caption", id: importedCaptions[0].id });
      toast.success(veText("toast.subtitles_imported"), veText("toast.cues_count", { count: importedCaptions.length }));
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.import_subtitles_failed"), error instanceof Error ? error.message : undefined);
    }
  }, [timelineDuration, toast, trackStates.captions.locked]);

  const exportSubtitles = useCallback(async () => {
    if (!doc || captions.length === 0) {
      toast.warning(veText("toast.no_subtitles_to_export"));
      return;
    }
    setSavingSubtitles(true);
    try {
      const file = new File(
        [captionsToSrt(captions)],
        `${baseName(doc.name)}.srt`,
        { type: "application/x-subrip" },
      );
      const uploaded = await api.documents.upload(file, doc.folder_id ?? undefined);
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      toast.success(veText("toast.subtitles_exported"), uploaded.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.export_subtitles_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setSavingSubtitles(false);
    }
  }, [captions, doc, queryClient, toast]);

  const addVideoClipFromDocument = useCallback(async (asset: Document, insertIndex?: number) => {
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    setUploadingVideo(true);
    try {
      const assetUrl = await getClipAssetUrl(asset.id);
      const duration = await readVideoUrlDuration(assetUrl);
      const safeDuration = duration > 0 ? duration : 5;
      const clipId = makeId("clip");
      const clip: ClipSegment = {
        id: clipId,
        label: baseName(asset.name),
        sourceStart: 0,
        sourceEnd: safeDuration,
        muted: false,
        color: CLIP_COLORS[clips.length % CLIP_COLORS.length],
        assetDocumentId: asset.id,
        assetName: asset.name,
        assetMimeType: asset.mime_type || asset.file_type || null,
        assetDuration: duration || null,
        replacementPrompt: "",
        editNotes: veText("default.imported_video_edit_note"),
      };
      if (typeof insertIndex === "number") {
        const nextState = insertClipIntoTrackState(currentTrackState, clip, insertIndex);
        setClips(nextState.clips);
        setShotBeats(nextState.shotBeats);
        setCaptions(nextState.captions);
        setAudioCues(nextState.audioCues);
        setMarkers(nextState.markers);
        const span = getClipTimelineSpans(nextState.clips).find((item) => item.clip.id === clipId);
        setPlayhead(span?.start ?? 0);
      } else {
        setClips((current) => [...current, clip]);
        setPlayhead(timelineDuration);
      }
      setSelection({ type: "clip", id: clipId });
      toast.success(veText("toast.video_clip_added"), asset.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.add_video_clip_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingVideo(false);
    }
  }, [clips.length, currentTrackState, getClipAssetUrl, timelineDuration, toast, trackStates.video.locked]);

  const addAudioCue = useCallback((type: AudioCueType = "ambience") => {
    if (trackStates.audio.locked) {
      toast.warning(veText("toast.audio_locked"));
      return;
    }
    const start = snapTimelineTime(playhead);
    const cue: AudioCue = {
      id: makeId("audio"),
      type,
      label: audioTypeDisplayLabel(type),
      start,
      end: clamp(start + (type === "sfx" ? 1.4 : 5), start + 0.05, Math.max(start + 0.05, timelineDuration)),
      volumeDb: defaultAudioVolumeDb(type),
      fadeIn: defaultAudioFade(type),
      fadeOut: defaultAudioFade(type),
      loop: defaultAudioLoop(type),
      duckUnderDialogue: defaultDuckUnderDialogue(type),
      muted: false,
      assetDocumentId: null,
      assetName: null,
      assetMimeType: null,
      sourcePlan: type === "dialogue"
        ? veText("default.dialogue_source_plan")
        : veText("default.audio_source_plan"),
      prompt: "",
    };
    setAudioCues((current) => [...current, cue]);
    setSelection({ type: "audio", id: cue.id });
  }, [playhead, snapTimelineTime, timelineDuration, toast, trackStates.audio.locked]);

  const addAudioCueFromDocument = useCallback(async (asset: Document, startOverride?: number) => {
    if (trackStates.audio.locked) {
      toast.warning(veText("toast.audio_locked"));
      return;
    }
    const type = inferAudioCueType(asset.name);
    const start = typeof startOverride === "number" ? snapTimelineTime(startOverride) : snapTimelineTime(playhead);
    let assetDuration = 0;
    let assetUrl = "";
    try {
      assetUrl = await api.documents.download(asset.id);
      assetDuration = await readAudioUrlDuration(assetUrl);
    } catch (error) {
      console.warn("Could not read audio asset duration", asset.name, error);
    } finally {
      if (assetUrl) revokeObjectUrlSoon(assetUrl);
    }
    const duration = assetDuration > 0 ? Math.min(assetDuration, Math.max(0.05, timelineDuration - start)) : 5;
    const cue: AudioCue = {
      id: makeId("audio"),
      type,
      label: asset.name,
      start,
      end: clamp(start + duration, start + 0.05, Math.max(start + 0.05, timelineDuration)),
      volumeDb: defaultAudioVolumeDb(type),
      fadeIn: defaultAudioFade(type),
      fadeOut: defaultAudioFade(type),
      loop: defaultAudioLoop(type),
      duckUnderDialogue: defaultDuckUnderDialogue(type),
      muted: false,
      assetDocumentId: asset.id,
      assetName: asset.name,
      assetMimeType: asset.mime_type || asset.file_type || null,
      sourcePlan: veText("default.asset_source_plan"),
      prompt: "",
    };
    setAudioCues((current) => [...current, cue]);
    setSelection({ type: "audio", id: cue.id });
    setPlayhead(start);
    toast.success(veText("toast.audio_asset_added"), asset.name);
  }, [playhead, snapTimelineTime, timelineDuration, toast, trackStates.audio.locked]);

  const attachVideoDocumentToSelectedClip = useCallback(async (asset: Document) => {
    if (!selectedClip) {
      toast.warning(veText("toast.select_clip_first"), veText("toast.select_clip_first_detail"));
      return;
    }
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    setUploadingVideo(true);
    try {
      const replacementUrl = await getClipAssetUrl(asset.id);
      const duration = await readVideoUrlDuration(replacementUrl);
      const nextEnd = duration > 0 ? duration : selectedClip.sourceEnd - selectedClip.sourceStart;
      updateClip(selectedClip.id, {
        assetDocumentId: asset.id,
        assetName: asset.name,
        assetMimeType: asset.mime_type || asset.file_type || null,
        assetDuration: duration || null,
        sourceStart: 0,
        sourceEnd: Math.max(0.05, nextEnd),
        muted: selectedClip.muted,
        replacementPrompt: selectedClip.replacementPrompt || veText("default.replacement_prompt", { name: asset.name }),
      });
      const activeAtPlayhead = mapTimelineTime(playhead, clips)?.clip.id === selectedClip.id;
      if (activeAtPlayhead && videoRef.current) {
        setPreviewSourceUrl(replacementUrl);
        videoRef.current.pause();
        videoRef.current.src = replacementUrl;
        videoRef.current.load();
      }
      toast.success(veText("toast.replacement_attached"), asset.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.attach_project_video_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingVideo(false);
    }
  }, [clips, getClipAssetUrl, playhead, selectedClip, toast, trackStates.video.locked, updateClip]);

  const handleAudioFileSelected = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || !doc) return;
    if (trackStates.audio.locked) {
      toast.warning(veText("toast.audio_locked"));
      return;
    }
    setUploadingAudio(true);
    try {
      const uploaded = await api.documents.upload(file, doc.folder_id ?? undefined);
      const start = snapTimelineTime(playhead);
      const type = inferAudioCueType(file.name);
      const cue: AudioCue = {
        id: makeId("audio"),
        type,
        label: uploaded.name || file.name,
        start,
        end: clamp(start + 5, start + 0.05, Math.max(start + 0.05, timelineDuration)),
        volumeDb: defaultAudioVolumeDb(type),
        fadeIn: defaultAudioFade(type),
        fadeOut: defaultAudioFade(type),
        loop: defaultAudioLoop(type),
        duckUnderDialogue: defaultDuckUnderDialogue(type),
        muted: false,
        assetDocumentId: uploaded.id,
        assetName: uploaded.name,
        assetMimeType: uploaded.mime_type || uploaded.file_type || file.type || null,
        sourcePlan: veText("default.uploaded_audio_source_plan"),
        prompt: "",
      };
      setAudioCues((current) => [...current, cue]);
      setSelection({ type: "audio", id: cue.id });
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      toast.success(veText("toast.audio_asset_added"), uploaded.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.add_audio_asset_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingAudio(false);
    }
  }, [doc, playhead, queryClient, snapTimelineTime, timelineDuration, toast, trackStates.audio.locked]);

  const importMediaFiles = useCallback(async (files: File[]) => {
    if (files.length === 0 || !doc) return;
    setImportingMedia(true);
    let importedCount = 0;
    try {
      for (const file of files) {
        const kind = fileMediaKind(file);
        if (!kind) {
          toast.warning(veText("toast.unsupported_media_file"), file.name);
          continue;
        }
        await api.documents.upload(file, doc.folder_id ?? undefined);
        importedCount += 1;
      }
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      await queryClient.invalidateQueries({ queryKey: ["video-editor-assets"] });
      setMediaSourceTab("project");
      if (importedCount > 0) {
        toast.success(veText("toast.media_imported"), veText("toast.files_count", { count: importedCount }));
      }
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.import_media_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setImportingMedia(false);
    }
  }, [doc, queryClient, toast]);

  const handleMediaFilesSelected = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    await importMediaFiles(files);
  }, [importMediaFiles]);

  const handleMediaDrop = useCallback(async (event: ReactDragEvent<HTMLDivElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setMediaDropActive(false);
    await importMediaFiles(Array.from(event.dataTransfer.files ?? []));
  }, [importMediaFiles]);

  const beginMediaAssetDrag = useCallback((event: ReactDragEvent<HTMLElement>, asset: Document) => {
    const kind = projectAssetKind(asset);
    if (!kind) return;
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData(MEDIA_DRAG_MIME, JSON.stringify({ id: asset.id, kind }));
    event.dataTransfer.setData("text/plain", asset.name);
  }, []);

  const draggedMediaAsset = useCallback((event: ReactDragEvent<HTMLElement>) => {
    const raw = event.dataTransfer.getData(MEDIA_DRAG_MIME);
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as { id?: unknown };
      return typeof parsed.id === "string" ? mediaAssetsById.get(parsed.id) ?? null : null;
    } catch {
      return null;
    }
  }, [mediaAssetsById]);

  const allowTimelineMediaDrop = useCallback((event: ReactDragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes(MEDIA_DRAG_MIME)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  const handleTimelineMediaDrop = useCallback(async (
    event: ReactDragEvent<HTMLDivElement>,
    targetTrack: "video" | "audio",
  ) => {
    event.preventDefault();
    const asset = draggedMediaAsset(event);
    const kind = asset ? projectAssetKind(asset) : null;
    if (!asset || !kind) return;
    const dropTime = timelineTimeFromLaneClientX(event.currentTarget, event.clientX, timelineDuration);
    if (targetTrack === "video") {
      if (kind !== "video") {
        toast.warning(veText("toast.drop_video_track_only"));
        return;
      }
      await addVideoClipFromDocument(asset, clipInsertIndexAtTime(clips, dropTime));
      return;
    }
    if (kind !== "audio") {
      toast.warning(veText("toast.drop_audio_track_only"));
      return;
    }
    await addAudioCueFromDocument(asset, dropTime);
  }, [addAudioCueFromDocument, addVideoClipFromDocument, clips, draggedMediaAsset, timelineDuration, toast]);

  const handleReplacementVideoSelected = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || !doc || !selectedClip) return;
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    setUploadingVideo(true);
    try {
      const duration = await readVideoFileDuration(file);
      const uploaded = await api.documents.upload(file, doc.folder_id ?? undefined);
      const replacementUrl = await getClipAssetUrl(uploaded.id);
      const nextEnd = duration > 0 ? duration : selectedClip.sourceEnd - selectedClip.sourceStart;
      updateClip(selectedClip.id, {
        assetDocumentId: uploaded.id,
        assetName: uploaded.name,
        assetMimeType: uploaded.mime_type || uploaded.file_type || file.type || null,
        assetDuration: duration || null,
        sourceStart: 0,
        sourceEnd: Math.max(0.05, nextEnd),
        muted: selectedClip.muted,
        replacementPrompt: selectedClip.replacementPrompt || veText("default.replacement_prompt", { name: uploaded.name }),
      });
      const activeAtPlayhead = mapTimelineTime(playhead, clips)?.clip.id === selectedClip.id;
      if (activeAtPlayhead && videoRef.current) {
        setPreviewSourceUrl(replacementUrl);
        videoRef.current.pause();
        videoRef.current.src = replacementUrl;
        videoRef.current.load();
      }
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      toast.success(veText("toast.replacement_attached"), uploaded.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.attach_replacement_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingVideo(false);
    }
  }, [clips, doc, getClipAssetUrl, playhead, queryClient, selectedClip, toast, trackStates.video.locked, updateClip]);

  const deleteSelection = useCallback(() => {
    if (!selection) return;
    if (selectedTrackLocked) {
      toast.warning(veText("toast.selected_track_locked"));
      return;
    }
    if (selection.type === "clip") {
      if (clips.length <= 1) {
        toast.warning(veText("toast.keep_one_clip"));
        return;
      }
      setClips((current) => current.filter((clip) => clip.id !== selection.id));
    } else if (selection.type === "shot") {
      setShotBeats((current) => current.filter((shot) => shot.id !== selection.id));
    } else if (selection.type === "caption") {
      setCaptions((current) => current.filter((caption) => caption.id !== selection.id));
    } else if (selection.type === "audio") {
      setAudioCues((current) => current.filter((cue) => cue.id !== selection.id));
    } else {
      setMarkers((current) => current.filter((marker) => marker.id !== selection.id));
    }
    setSelection(null);
  }, [clips.length, selectedTrackLocked, selection, toast]);

  const duplicateSelection = useCallback(() => {
    if (!selection) return;
    if (selectedTrackLocked) {
      toast.warning(veText("toast.selected_track_locked"));
      return;
    }

    if (selectedClip) {
      duplicateClip(selectedClip);
      return;
    }

    if (selectedShot) {
      const range = duplicateTimedRange(selectedShot, timelineDuration);
      const copy: ShotBeat = {
        ...selectedShot,
        ...range,
        id: makeId("shot"),
        title: veText("default.copy_label", { label: selectedShot.title }),
      };
      setShotBeats((current) => sortTimedItems([...current, copy]));
      setSelection({ type: "shot", id: copy.id });
      setPlayhead(copy.start);
      return;
    }

    if (selectedCaption) {
      const range = duplicateTimedRange(selectedCaption, timelineDuration);
      const copy: CaptionCue = {
        ...selectedCaption,
        ...range,
        id: makeId("caption"),
      };
      setCaptions((current) => sortTimedItems([...current, copy]));
      setSelection({ type: "caption", id: copy.id });
      setPlayhead(copy.start);
      return;
    }

    if (selectedAudio) {
      const range = duplicateTimedRange(selectedAudio, timelineDuration);
      const copy: AudioCue = {
        ...selectedAudio,
        ...range,
        id: makeId("audio"),
        label: veText("default.copy_label", { label: selectedAudio.label }),
      };
      setAudioCues((current) => sortTimedItems([...current, copy]));
      setSelection({ type: "audio", id: copy.id });
      setPlayhead(copy.start);
      return;
    }

    if (selectedMarker) {
      const copy: TimelineMarker = {
        ...selectedMarker,
        id: makeId("marker"),
        time: clamp(selectedMarker.time + 0.5, 0, timelineDuration),
        label: veText("default.copy_label", { label: selectedMarker.label }),
      };
      setMarkers((current) => [...current, copy].sort((a, b) => a.time - b.time));
      setSelection({ type: "marker", id: copy.id });
      setPlayhead(copy.time);
    }
  }, [
    duplicateClip,
    selectedAudio,
    selectedCaption,
    selectedClip,
    selectedMarker,
    selectedShot,
    selectedTrackLocked,
    selection,
    timelineDuration,
    toast,
  ]);

  const buildRecipe = useCallback((options: BuildRecipeOptions = {}) => {
    let cursor = 0;
    const manualEdits: NonNullable<VideoEditRecipe["manual_edits"]> = [];
    for (const clip of clips) {
      const duration = Math.max(0, clip.sourceEnd - clip.sourceStart);
      const hasManualEdit = clipHasManualEdit(clip, sourceDuration);
      if (hasManualEdit) {
        manualEdits.push({
          clip_id: clip.id,
          label: clip.label,
          timeline_start: cursor,
          timeline_end: cursor + duration,
          source_start: clip.sourceStart,
          source_end: clip.sourceEnd,
          replacement_document: clip.assetDocumentId
            ? {
                id: clip.assetDocumentId,
                name: clip.assetName ?? null,
                mime_type: clip.assetMimeType ?? null,
                duration: clip.assetDuration ?? null,
              }
            : null,
          replacement_prompt: clip.replacementPrompt ?? null,
          edit_notes: clip.editNotes ?? null,
        });
      }
      cursor += duration;
    }

    const finalDocument = options.finalDocument ?? null;
    const finalVideoPath = normalizeRecipePath(finalDocument?.fs_path);
    const sourceVideoPath = normalizeRecipePath(doc?.fs_path);

    return {
      version: 1,
      kind: "manor.video_edit_recipe",
      created_at: new Date().toISOString(),
      created_by: options.createdBy ?? "video_editor_manual_edit",
      source_document: {
        id: doc?.id,
        name: doc?.name,
        folder_id: doc?.folder_id ?? null,
        fs_path: doc?.fs_path ?? null,
        mime_type: doc?.mime_type ?? doc?.file_type ?? null,
      },
      canvas: {
        width: mediaSize.width,
        height: mediaSize.height,
      },
      timeline: {
        duration: timelineDuration,
        clips,
        shots: shotBeats,
        captions,
        audio_cues: audioCues,
        markers,
      },
      manual_edits: manualEdits,
      editor_settings: {
        track_states: trackStates,
        work_area: normalizedWorkArea,
      },
      comic_drama: {
        workflow: "manual_comic_drama_edit",
        tracks: ["markers", "shots", "replacement_clips", "captions", "audio_cues"],
        expected_use: "Scene/shot planning, review markers, replacement clip patching, speech bubble or subtitle timing, dialogue/SFX/BGM placement, and server-side final MP4 render.",
      },
      ai_composition: {
        final_video_document_id: finalDocument?.id ?? null,
        final_video_name: finalDocument?.name ?? null,
        final_video_path: finalVideoPath || null,
        source_video_document_id: doc?.id ?? null,
        source_video_path: sourceVideoPath || null,
        clip_count: clips.length,
        shot_count: shotBeats.length,
        caption_count: captions.length,
        audio_track_count: audioCues.length,
        editable_sources: ["clips", "shots", "captions", "audio_cues", "markers"],
      },
      render_contract: {
        video: "Apply clip order, source/replacement asset trims, video track visibility, mute flags, and burned captions when the captions track is visible.",
        audio: "Generate or attach cue stems, respect start/end timing, loop/fade/ducking controls, track mute state, and volumeDb, then mix under dialogue.",
        export: "Server render should honor editor work area when enabled, produce final mp4 plus editable recipe sidecar, and preserve manual_edits.",
      },
    };
  }, [audioCues, captions, clips, doc, markers, mediaSize.height, mediaSize.width, normalizedWorkArea, shotBeats, sourceDuration, timelineDuration, trackStates]);

  const uploadRecipeDocument = useCallback(async (
    recipe: Record<string, unknown>,
    fileName: string,
    existingRecipe?: Document | null,
  ): Promise<Document> => {
    if (!doc) throw new Error("No source document");
    const file = new File(
      [JSON.stringify(recipe, null, 2)],
      fileName,
      { type: "application/json" },
    );
    return existingRecipe
      ? api.documents.replaceFile(existingRecipe.id, file)
      : api.documents.upload(file, doc.folder_id ?? undefined);
  }, [doc]);

  const findRecipeDocumentByName = useCallback(async (fileName: string): Promise<Document | null> => {
    if (!doc?.folder_id) return null;
    const result = await api.documents.list({
      folder_id: doc.folder_id,
      search: fileName,
      include_generated_assets: true,
      limit: 25,
    });
    return result.items.find((item) => item.name === fileName && isVideoEditRecipeDocument(item)) ?? null;
  }, [doc?.folder_id]);

  const saveExportRecipeSidecar = useCallback(async (finalDocument: Document): Promise<Document> => {
    if (!doc) throw new Error("No source document");
    const fileName = `${baseName(finalDocument.name)}.video-edit.json`;
    const recipe = buildRecipe({
      finalDocument,
      createdBy: "video_editor_browser_export",
    });
    const existingRecipe = await findRecipeDocumentByName(fileName);
    const uploaded = await uploadRecipeDocument(recipe, fileName, existingRecipe);
    return uploaded;
  }, [buildRecipe, doc, findRecipeDocumentByName, uploadRecipeDocument]);

  const saveRecipe = useCallback(async (): Promise<Document | null> => {
    if (!doc) return null;
    setSaving(true);
    try {
      const recipe = buildRecipe();
      const fileName = `${baseName(doc.name)}.video-edit.json`;
      const existingRecipe = recipeDoc ?? recipeQuery.data;
      const uploaded = await uploadRecipeDocument(recipe, fileName, existingRecipe);
      setRecipeDoc(uploaded);
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      await queryClient.invalidateQueries({ queryKey: ["video-edit-recipe"] });
      toast.success(veText("toast.recipe_saved"), uploaded.name);
      return uploaded;
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.save_recipe_failed"), error instanceof Error ? error.message : undefined);
      return null;
    } finally {
      setSaving(false);
    }
  }, [buildRecipe, doc, queryClient, recipeDoc, recipeQuery.data, toast, uploadRecipeDocument]);

  const drawExportFrame = useCallback((
    ctx: CanvasRenderingContext2D,
    canvas: HTMLCanvasElement,
    sourceVideo: HTMLVideoElement,
    timelineTime: number,
  ) => {
    ctx.fillStyle = "#020617";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    const videoAspect = sourceVideo.videoWidth && sourceVideo.videoHeight
      ? sourceVideo.videoWidth / sourceVideo.videoHeight
      : canvas.width / canvas.height;
    const canvasAspect = canvas.width / canvas.height;
    let drawWidth = canvas.width;
    let drawHeight = canvas.height;
    let drawX = 0;
    let drawY = 0;
    if (videoAspect > canvasAspect) {
      drawHeight = canvas.width / videoAspect;
      drawY = (canvas.height - drawHeight) / 2;
    } else {
      drawWidth = canvas.height * videoAspect;
      drawX = (canvas.width - drawWidth) / 2;
    }
    if (trackStates.video.visible) {
      ctx.drawImage(sourceVideo, drawX, drawY, drawWidth, drawHeight);
    }

    const active = trackStates.captions.visible
      ? captions.filter((caption) => timelineTime >= caption.start && timelineTime <= caption.end)
      : [];
    for (const caption of active) {
      const fontSize = Math.round((caption.size / 1080) * canvas.height);
      ctx.font = `700 ${fontSize}px Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
      ctx.textAlign = caption.align;
      ctx.textBaseline = "middle";
      const display = captionDisplay(caption);
      const captionText = caption.speaker ? `${caption.speaker}: ${caption.text}` : caption.text;
      const lines = wrapCanvasText(ctx, captionText, canvas.width * 0.78);
      const lineHeight = fontSize * 1.22;
      const blockWidth = Math.min(
        canvas.width * 0.86,
        Math.max(...lines.map((line) => ctx.measureText(line).width), 0) + fontSize * 1.3,
      );
      const blockHeight = lines.length * lineHeight + fontSize * 0.75;
      const x = (caption.x / 100) * canvas.width;
      const y = (caption.y / 100) * canvas.height;
      const boxX = caption.align === "left"
        ? x - fontSize * 0.5
        : caption.align === "right"
          ? x - blockWidth + fontSize * 0.5
          : x - blockWidth / 2;
      const boxY = y - blockHeight / 2;
      ctx.fillStyle = display.background;
      drawRoundedRect(ctx, boxX, boxY, blockWidth, blockHeight, fontSize * 0.25);
      ctx.fill();
      if (caption.style === "speechBubble") {
        ctx.strokeStyle = "rgba(28,25,23,0.2)";
        ctx.lineWidth = Math.max(2, fontSize * 0.05);
        ctx.stroke();
      }
      ctx.fillStyle = display.color;
      lines.forEach((line, index) => {
        const lineY = y - ((lines.length - 1) * lineHeight) / 2 + index * lineHeight;
        ctx.fillText(line, x, lineY);
      });
    }
  }, [captions, trackStates.captions.visible, trackStates.video.visible]);

  const exportPreview = useCallback(async () => {
    if (!doc || !downloadUrl || timelineDuration <= 0 || exportRangeDuration <= 0) return;
    if (!("MediaRecorder" in window)) {
      toast.error(veText("toast.browser_export_unsupported"));
      return;
    }
    setExporting(true);
    setExportProgress(0);
    try {
      const sourceVideo = document.createElement("video");
      sourceVideo.src = downloadUrl;
      sourceVideo.crossOrigin = "anonymous";
      sourceVideo.playsInline = true;
      sourceVideo.preload = "auto";
      sourceVideo.muted = false;
      sourceVideo.volume = 1;
      sourceVideo.load();
      if (sourceVideo.readyState < 1) await waitForVideoEvent(sourceVideo, "loadedmetadata");

      const canvas = document.createElement("canvas");
      canvas.width = sourceVideo.videoWidth || mediaSize.width || 1920;
      canvas.height = sourceVideo.videoHeight || mediaSize.height || 1080;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("Canvas renderer unavailable");
      const canvasStream = canvas.captureStream(30);

      const AudioContextCtor = window.AudioContext ?? (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      const audioContext = AudioContextCtor ? new AudioContextCtor() : null;
      const audioDestination = audioContext?.createMediaStreamDestination() ?? null;
      const sourceAudioGain = audioContext?.createGain() ?? null;
      const assetUrls: string[] = [];
      let capturedSource: MediaStream | null = null;
      const assetAudioNodes: {
        cue: AudioCue;
        audio: HTMLAudioElement;
        gain: GainNode;
        wasActive: boolean;
      }[] = [];
      const clipVideoUrls: string[] = [];
      const replacementClipVideos = new Map<string, {
        video: HTMLVideoElement;
        gain: GainNode | null;
      }>();

      if (audioContext && audioDestination && sourceAudioGain) {
        try {
          const sourceAudioNode = audioContext.createMediaElementSource(sourceVideo);
          sourceAudioGain.gain.value = 1;
          sourceAudioNode.connect(sourceAudioGain).connect(audioDestination);
        } catch (error) {
          console.warn("Source audio could not be attached to preview mix", error);
        }

        const assetCues = trackStates.audio.muted ? [] : audioCues.filter((cue) => !cue.muted);
        for (const cue of assetCues) {
          try {
            const generated = !cue.assetDocumentId;
            const assetUrl = cue.assetDocumentId
              ? await api.documents.download(cue.assetDocumentId)
              : createGeneratedAudioPreviewUrl(cue.type);
            if (!generated) assetUrls.push(assetUrl);
            const audio = new Audio(assetUrl);
            audio.crossOrigin = "anonymous";
            audio.preload = "auto";
            audio.loop = cue.loop;
            audio.volume = 1;
            audio.load();
            await waitForAudioReady(audio);
            const source = audioContext.createMediaElementSource(audio);
            const gain = audioContext.createGain();
            gain.gain.value = dbToGain(cue.volumeDb);
            source.connect(gain).connect(audioDestination);
            assetAudioNodes.push({ cue, audio, gain, wasActive: false });
          } catch (error) {
            console.warn("Audio cue could not be attached to preview mix", cue.label, error);
          }
        }

        const replacementIds = [...new Set(clips.map((clip) => clip.assetDocumentId).filter((id): id is string => Boolean(id)))];
        for (const replacementId of replacementIds) {
          try {
            const clipUrl = await api.documents.download(replacementId);
            clipVideoUrls.push(clipUrl);
            const video = document.createElement("video");
            video.src = clipUrl;
            video.crossOrigin = "anonymous";
            video.playsInline = true;
            video.preload = "auto";
            video.muted = false;
            video.volume = 1;
            video.load();
            if (video.readyState < 1) await waitForVideoEvent(video, "loadedmetadata");
            const source = audioContext.createMediaElementSource(video);
            const gain = audioContext.createGain();
            gain.gain.value = 0;
            source.connect(gain).connect(audioDestination);
            replacementClipVideos.set(replacementId, { video, gain });
          } catch (error) {
            console.warn("Replacement clip could not be attached to preview export", replacementId, error);
          }
        }

        audioDestination.stream.getAudioTracks().forEach((track) => canvasStream.addTrack(track));
        await audioContext.resume().catch(() => undefined);
      } else {
        capturedSource = (sourceVideo as HTMLVideoElement & {
          captureStream?: () => MediaStream;
          mozCaptureStream?: () => MediaStream;
        }).captureStream?.() ?? (sourceVideo as HTMLVideoElement & { mozCaptureStream?: () => MediaStream }).mozCaptureStream?.() ?? null;
        capturedSource?.getAudioTracks().forEach((track) => canvasStream.addTrack(track));
      }

      if (replacementClipVideos.size === 0) {
        const replacementIds = [...new Set(clips.map((clip) => clip.assetDocumentId).filter((id): id is string => Boolean(id)))];
        for (const replacementId of replacementIds) {
          try {
            const clipUrl = await api.documents.download(replacementId);
            clipVideoUrls.push(clipUrl);
            const video = document.createElement("video");
            video.src = clipUrl;
            video.crossOrigin = "anonymous";
            video.playsInline = true;
            video.preload = "auto";
            video.muted = true;
            video.load();
            if (video.readyState < 1) await waitForVideoEvent(video, "loadedmetadata");
            replacementClipVideos.set(replacementId, { video, gain: null });
          } catch (error) {
            console.warn("Replacement clip could not be loaded for preview export", replacementId, error);
          }
        }
      }

      const syncAssetAudio = async (timelineTime: number, realtime: boolean) => {
        for (const node of assetAudioNodes) {
          const duration = node.cue.end - node.cue.start;
          const cueOffset = timelineTime - node.cue.start;
          const mediaDuration = Number.isFinite(node.audio.duration) ? node.audio.duration : 0;
          const sourceInRange = node.cue.loop || mediaDuration <= 0 || cueOffset <= mediaDuration;
          const active = realtime && cueOffset >= 0 && cueOffset <= duration && sourceInRange;
          node.gain.gain.value = getAudioCueGain(node.cue, timelineTime, audioCues);
          if (!active) {
            if (!node.audio.paused) node.audio.pause();
            node.wasActive = false;
            continue;
          }
          node.audio.loop = node.cue.loop;
          const targetTime = getAudioCueSourceTime(node.cue, cueOffset, mediaDuration);
          if (!node.wasActive || Math.abs(node.audio.currentTime - targetTime) > 0.18) {
            node.audio.currentTime = targetTime;
          }
          node.wasActive = true;
          if (node.audio.paused) {
            await node.audio.play().catch(() => undefined);
          }
        }
      };

      const getClipVideo = (clip: ClipSegment) => {
        if (!clip.assetDocumentId) return sourceVideo;
        return replacementClipVideos.get(clip.assetDocumentId)?.video ?? sourceVideo;
      };

      const syncClipAudioGain = (clip: ClipSegment, timelineTime: number) => {
        const muteSourceAudio = shouldMuteSourceVideoAudio(clip, timelineTime, audioCues, trackStates);
        if (sourceAudioGain) sourceAudioGain.gain.value = clip.assetDocumentId || muteSourceAudio ? 0 : 1;
        replacementClipVideos.forEach(({ gain }, id) => {
          if (gain) gain.gain.value = clip.assetDocumentId === id && !muteSourceAudio ? 1 : 0;
        });
      };

      const mimeType = [
        "video/webm;codecs=vp9,opus",
        "video/webm;codecs=vp8,opus",
        "video/webm",
      ].find((candidate) => MediaRecorder.isTypeSupported(candidate)) || "";
      const recorder = new MediaRecorder(canvasStream, mimeType ? { mimeType } : undefined);
      const chunks: Blob[] = [];
      const done = new Promise<void>((resolve) => {
        recorder.ondataavailable = (event) => {
          if (event.data.size > 0) chunks.push(event.data);
        };
        recorder.onstop = () => resolve();
      });
      recorder.start(500);

      const updateExportProgress = (timelineTime: number) => {
        const rangeOffset = clamp(timelineTime - exportRangeStart, 0, exportRangeDuration);
        setExportProgress(Math.round((rangeOffset / exportRangeDuration) * 100));
      };

      let cursor = 0;
      for (const clip of clips) {
        const duration = Math.max(0, clip.sourceEnd - clip.sourceStart);
        const clipStart = cursor;
        const clipEnd = cursor + duration;
        if (duration <= 0 || clipEnd <= exportRangeStart || clipStart >= exportRangeEnd) {
          cursor = clipEnd;
          continue;
        }
        const segmentStart = Math.max(clipStart, exportRangeStart);
        const segmentEnd = Math.min(clipEnd, exportRangeEnd);
        const sourceStart = clip.sourceStart + (segmentStart - clipStart);
        const sourceEnd = clip.sourceStart + (segmentEnd - clipStart);
        const activeVideo = getClipVideo(clip);
        activeVideo.muted = audioContext ? false : shouldMuteSourceVideoAudio(clip, segmentStart, audioCues, trackStates);
        syncClipAudioGain(clip, segmentStart);
        await seekVideo(activeVideo, sourceStart);
        const playing = await activeVideo.play().then(() => true).catch(() => false);
        if (playing) {
          let stalledFrames = 0;
          let lastTime = activeVideo.currentTime;
          while (activeVideo.currentTime < sourceEnd - 0.03 && stalledFrames < 45) {
            const timelineTime = clipStart + Math.max(0, activeVideo.currentTime - clip.sourceStart);
            activeVideo.muted = audioContext ? false : shouldMuteSourceVideoAudio(clip, timelineTime, audioCues, trackStates);
            syncClipAudioGain(clip, timelineTime);
            await syncAssetAudio(timelineTime, true);
            drawExportFrame(ctx, canvas, activeVideo, timelineTime);
            updateExportProgress(timelineTime);
            await nextFrame();
            stalledFrames = Math.abs(activeVideo.currentTime - lastTime) < 0.001 ? stalledFrames + 1 : 0;
            lastTime = activeVideo.currentTime;
          }
        } else {
          const frameStep = 1 / 30;
          for (let sourceTime = sourceStart; sourceTime < sourceEnd; sourceTime += frameStep) {
            await seekVideo(activeVideo, sourceTime);
            const timelineTime = clipStart + Math.max(0, sourceTime - clip.sourceStart);
            await syncAssetAudio(timelineTime, false);
            drawExportFrame(ctx, canvas, activeVideo, timelineTime);
            updateExportProgress(timelineTime);
            await nextFrame();
          }
        }
        activeVideo.pause();
        await syncAssetAudio(segmentEnd, false);
        drawExportFrame(ctx, canvas, activeVideo, segmentEnd);
        updateExportProgress(segmentEnd);
        cursor = clipEnd;
      }
      recorder.stop();
      await done;
      canvasStream.getTracks().forEach((track) => track.stop());
      audioDestination?.stream.getTracks().forEach((track) => track.stop());
      capturedSource?.getTracks().forEach((track) => track.stop());
      assetAudioNodes.forEach((node) => clearMediaElementSource(node.audio));
      replacementClipVideos.forEach(({ video }) => clearMediaElementSource(video));
      assetUrls.forEach((url) => revokeObjectUrlSoon(url));
      clipVideoUrls.forEach((url) => revokeObjectUrlSoon(url));
      await audioContext?.close().catch(() => undefined);
      setExportProgress(100);

      const blob = new Blob(chunks, { type: mimeType || "video/webm" });
      const fileName = normalizedWorkArea.enabled
        ? `${baseName(doc.name)}-range-edit.webm`
        : `${baseName(doc.name)}-edited.webm`;
      const file = new File([blob], fileName, { type: blob.type || "video/webm" });
      const uploaded = await api.documents.upload(file, doc.folder_id ?? undefined);
      const exportRecipe = await saveExportRecipeSidecar(uploaded);
      await queryClient.invalidateQueries({ queryKey: ["documents"] });
      await queryClient.invalidateQueries({ queryKey: ["video-edit-recipe"] });
      await queryClient.invalidateQueries({ queryKey: ["video-editor-recipe-candidates"] });
      setLastExportDoc(uploaded);
      toast.success(veText("toast.preview_exported"), `${uploaded.name} + ${exportRecipe.name}`);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.preview_export_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setExporting(false);
    }
  }, [audioCues, clips, doc, downloadUrl, drawExportFrame, exportRangeDuration, exportRangeEnd, exportRangeStart, mediaSize.height, mediaSize.width, normalizedWorkArea.enabled, queryClient, saveExportRecipeSidecar, timelineDuration, toast, trackStates.audio.muted, trackStates.video.muted]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isTextEditingTarget(event.target)) return;
      const key = event.key.toLowerCase();
      const usesModifier = event.metaKey || event.ctrlKey;
      const hasModifier = usesModifier || event.altKey;

      if (usesModifier && key === "z") {
        event.preventDefault();
        if (event.shiftKey) redoHistory();
        else undoHistory();
        return;
      }
      if (usesModifier && key === "y") {
        event.preventDefault();
        redoHistory();
        return;
      }
      if (usesModifier && key === "s") {
        event.preventDefault();
        if (event.shiftKey) {
          if (!saving && timelineDuration > 0) void saveRecipe();
        } else if (!exporting && exportRangeDuration > 0) {
          void exportPreview();
        }
        return;
      }
      if (usesModifier && key === "e") {
        event.preventDefault();
        if (!exporting && exportRangeDuration > 0) void exportPreview();
        return;
      }
      if (usesModifier && key === "d") {
        event.preventDefault();
        duplicateSelection();
        return;
      }
      if (hasModifier && key !== "arrowleft" && key !== "arrowright") return;

      if (event.code === "Space") {
        event.preventDefault();
        void togglePlayback();
      } else if (key === "escape") {
        event.preventDefault();
        setSelection(null);
      } else if (key === "home") {
        event.preventDefault();
        seekTimeline(0);
      } else if (key === "end") {
        event.preventDefault();
        seekTimeline(timelineDuration);
      } else if (key === "i") {
        event.preventDefault();
        setWorkAreaInPoint();
      } else if (key === "o") {
        event.preventDefault();
        setWorkAreaOutPoint();
      } else if (key === "j") {
        event.preventDefault();
        jumpToMarker(-1);
      } else if (key === "k") {
        event.preventDefault();
        jumpToMarker(1);
      } else if (key === "arrowleft" || key === "arrowright") {
        event.preventDefault();
        const direction = key === "arrowleft" ? -1 : 1;
        if (event.altKey && selectedClip) moveClip(selectedClip.id, direction);
        else if (selection && !event.shiftKey) nudgeSelection(direction);
        else seekTimeline(playhead + direction * nudgeStep);
      } else if (key === "s") {
        event.preventDefault();
        splitClipAtPlayhead();
      } else if (key === "delete" || key === "backspace") {
        if (!selection) return;
        event.preventDefault();
        deleteSelection();
      } else if (key === "m") {
        event.preventDefault();
        if (event.shiftKey || !selectedClip && !selectedAudio) {
          addMarker();
          return;
        }
        if (selectedTrackLocked) return;
        if (selectedClip) updateClip(selectedClip.id, { muted: !selectedClip.muted });
        else if (selectedAudio) updateAudioCue(selectedAudio.id, { muted: !selectedAudio.muted });
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [
    deleteSelection,
    duplicateSelection,
    exportPreview,
    exportRangeDuration,
    exporting,
    addMarker,
    jumpToMarker,
    moveClip,
    nudgeSelection,
    nudgeStep,
    playhead,
    redoHistory,
    saveRecipe,
    saving,
    seekTimeline,
    setWorkAreaInPoint,
    setWorkAreaOutPoint,
    selectedAudio,
    selectedClip,
    selectedTrackLocked,
    selection,
    splitClipAtPlayhead,
    timelineDuration,
    togglePlayback,
    undoHistory,
    updateAudioCue,
    updateClip,
  ]);

  const backTarget = getKnowledgeReturnTo(location.state) || (sourceDocId ? `/viewer/${sourceDocId}` : docId ? `/viewer/${docId}` : "/knowledge");

  const openMediaPicker = useCallback((source?: MediaSourceTab) => {
    const nextSource = source ?? mediaSourceTab;
    const nextCounts = nextSource === "project" ? projectMediaKindCounts : knowledgeMediaKindCounts;
    setMediaSourceTab(nextSource);
    if (mediaKindFilter === "all" || (nextSource === "knowledge" && mediaKindFilter === "project")) {
      setMediaKindFilter(nextCounts.all > 0 ? preferredMediaFilter(nextCounts, "video") : "video");
    }
    setMediaPickerOpen(true);
  }, [knowledgeMediaKindCounts, mediaKindFilter, mediaSourceTab, projectMediaKindCounts]);

  const closeMediaPicker = useCallback(() => {
    setMediaPickerOpen(false);
    setMediaPickerSelectedIds([]);
  }, []);

  const toggleMediaPickerSelection = useCallback((assetId: string) => {
    setMediaPickerSelectedIds((ids) => (
      ids.includes(assetId)
        ? ids.filter((id) => id !== assetId)
        : [...ids, assetId]
    ));
  }, []);

  const selectMediaKindFilter = useCallback((filter: MediaKindFilter) => {
    if (filter !== "all" && activeMediaKindCounts[filter] === 0) return;
    setMediaKindFilter(filter);
    setMediaPickerSelectedIds([]);
  }, [activeMediaKindCounts]);

  const switchMediaPickerSource = useCallback((source: MediaSourceTab) => {
    const nextCounts = source === "project" ? projectMediaKindCounts : knowledgeMediaKindCounts;
    setMediaSourceTab(source);
    setMediaPickerSelectedIds([]);
    if (mediaKindFilter === "all" || (source === "knowledge" && mediaKindFilter === "project")) {
      setMediaKindFilter(nextCounts.all > 0 ? preferredMediaFilter(nextCounts, "video") : "video");
    }
  }, [knowledgeMediaKindCounts, mediaKindFilter, projectMediaKindCounts]);

  const selectVisibleMediaAssets = useCallback(() => {
    setMediaPickerSelectedIds((ids) => {
      const next = new Set(ids);
      activeMediaAssets.forEach((asset) => next.add(asset.id));
      return Array.from(next);
    });
  }, [activeMediaAssets]);

  const addSelectedMediaAssets = useCallback(async () => {
    if (mediaPickerSelectedAssets.length === 0) return;
    for (const asset of mediaPickerSelectedAssets) {
      const kind = projectAssetKind(asset);
      if (kind === "video") {
        await addVideoClipFromDocument(asset);
      } else if (kind === "audio") {
        await addAudioCueFromDocument(asset);
      } else if (kind === "project") {
        navigate(`/video-editor/${asset.id}`, { state: location.state });
        closeMediaPicker();
        return;
      }
    }
    closeMediaPicker();
  }, [addAudioCueFromDocument, addVideoClipFromDocument, closeMediaPicker, location.state, mediaPickerSelectedAssets, navigate]);

  const renderTrackLabel = (
    track: TimelineTrackId,
    label: string,
    options: { visibility?: boolean; mute?: boolean } = {},
  ) => {
    const state = trackStates[track];
    const help = TRACK_HELP_KEYS[track];
    return (
      <div className={`ve-track-label ${state.locked ? "is-locked" : ""}`}>
        <span className="ve-track-label-name">
          <span>{label}</span>
          <VideoEditorHelp
            titleKey={help.titleKey}
            bodyKey={help.bodyKey}
            itemKeys={help.itemKeys}
            align="left"
          />
        </span>
        <div className="ve-track-controls">
          {options.visibility !== false && (
            <button
              className={`ve-track-toggle ${state.visible ? "is-active" : ""}`}
              type="button"
              title={state.visible ? veText("hide_track") : veText("show_track")}
              onClick={() => updateTimelineTrackState(track, { visible: !state.visible })}
            >
              {state.visible ? <IconEye size={13} /> : <IconEyeOff size={13} />}
            </button>
          )}
          {options.mute && (
            <button
              className={`ve-track-toggle ${!state.muted ? "is-active" : ""}`}
              type="button"
              title={state.muted ? veText("unmute_track") : veText("mute_track")}
              onClick={() => updateTimelineTrackState(track, { muted: !state.muted })}
            >
              M
            </button>
          )}
          <button
            className={`ve-track-toggle ${!state.locked ? "is-active" : "is-locked"}`}
            type="button"
            title={state.locked ? veText("unlock_track") : veText("lock_track")}
            onClick={() => updateTimelineTrackState(track, { locked: !state.locked })}
          >
            <IconLock size={13} />
          </button>
        </div>
      </div>
    );
  };

  const renderMediaAssetCard = (
    asset: Document,
    sourceLabel: string,
    options: { selectable?: boolean; selected?: boolean; onToggle?: (asset: Document) => void } = {},
  ) => {
    const kind = projectAssetKind(asset);
    if (!kind) return null;
    const selectable = Boolean(options.selectable);
    const selected = Boolean(options.selected);
    const meta = [
      sourceLabel,
      asset.file_size != null ? formatFileSize(asset.file_size) : null,
    ].filter(Boolean).join(" · ");
    const runDefaultAction = () => {
      if (kind === "video") void addVideoClipFromDocument(asset);
      else if (kind === "audio") void addAudioCueFromDocument(asset);
      else navigate(`/video-editor/${asset.id}`, { state: location.state });
    };
    const mediaPreviewing = kind !== "project" && previewingMediaAssetId === asset.id;
    const mediaPreviewLoading = kind !== "project" && mediaAssetPreviewLoadingId === asset.id;
    const mediaPreviewTitle = kind === "video" ? veText("preview_video") : veText("preview_audio");
    const mediaPreviewLabel = kind === "video" ? veText("preview_video_short") : veText("preview_audio_short");
    const stopPreviewTitle = kind === "video" ? veText("stop_video_preview") : veText("stop_audio_preview");
    const previewButtonTitle = mediaPreviewLoading ? veText("loading_media_preview") : mediaPreviewing ? stopPreviewTitle : mediaPreviewTitle;
    const handleMediaPreview = () => {
      if (kind === "video") void previewVideoMediaAsset(asset);
      else if (kind === "audio") void toggleMediaAssetPreview(asset);
    };
    return (
      <div
        key={asset.id}
        className={`ve-media-card is-${kind} ${selected ? "is-selected" : ""}`}
        draggable={kind !== "project"}
        onDragStart={(event) => beginMediaAssetDrag(event, asset)}
        onDoubleClick={runDefaultAction}
      >
        {selectable && (
          <button
            className={`ve-media-select ${selected ? "is-selected" : ""}`}
            type="button"
            aria-pressed={selected}
            title={selected ? veText("media_picker_deselect") : veText("media_picker_select")}
            onClick={(event) => {
              event.stopPropagation();
              options.onToggle?.(asset);
            }}
          >
            {selected && <IconCheck size={13} />}
          </button>
        )}
        <MediaAssetThumbnail asset={asset} kind={kind} label={veText(`media_kind.${kind}`)} />
        <div className="ve-media-card-body">
          <strong title={asset.name}>{asset.name}</strong>
          <span>{meta || veText(`media_kind.${kind}`)}</span>
          {kind !== "project" && (
            <div className="ve-media-card-quick">
              <button
                className={`ve-media-preview-pill ${mediaPreviewing ? "is-active" : ""}`}
                type="button"
                title={previewButtonTitle}
                aria-label={previewButtonTitle}
                disabled={mediaPreviewLoading}
                onClick={(event) => {
                  event.stopPropagation();
                  handleMediaPreview();
                }}
              >
                {mediaPreviewLoading ? <span className="ve-audio-preview-spinner" aria-hidden="true" /> : mediaPreviewing ? <IconStop size={12} /> : <IconPlay size={12} />}
                <span>{mediaPreviewLoading ? veText("loading_media_preview_short") : mediaPreviewing ? veText("stop_preview_short") : mediaPreviewLabel}</span>
              </button>
              <small>{veText("drag_to_timeline")}</small>
            </div>
          )}
        </div>
        <div className="ve-media-card-actions">
          {kind === "video" && (
            <>
              <button
                className="ve-media-action"
                type="button"
                title={veText("add_to_timeline")}
                disabled={trackStates.video.locked || uploadingVideo}
                onClick={() => { void addVideoClipFromDocument(asset); }}
              >
                <IconPlus size={14} />
              </button>
              <button
                className="ve-media-action"
                type="button"
                title={veText("replace_selected")}
                disabled={!selectedClip || trackStates.video.locked || uploadingVideo}
                onClick={() => { void attachVideoDocumentToSelectedClip(asset); }}
              >
                <IconRefresh size={14} />
              </button>
            </>
          )}
          {kind === "audio" && (
            <button
              className="ve-media-action"
              type="button"
              title={veText("add_to_timeline")}
              disabled={trackStates.audio.locked}
              onClick={() => { void addAudioCueFromDocument(asset); }}
            >
              <IconPlus size={14} />
            </button>
          )}
          {kind === "project" && (
            <Link className="ve-media-link" to={`/video-editor/${asset.id}`} state={location.state}>
              {veText("open")}
            </Link>
          )}
        </div>
      </div>
    );
  };

  const selectedInspectorLabel = selection
    ? veText(`selection.${selection.type}`)
    : veText("project_overview");

  if (docQuery.isLoading || sourceLoading || (routeIsRecipe && loadingRecipe)) {
    return (
      <div className="ve-loading">
        <LoadingSpinner size={28} />
      </div>
    );
  }

  if (docQuery.error || !doc) {
    return (
      <EmptyState
        icon={<IconDocument size={32} />}
        title={veText("empty.not_found_title")}
        description={veText("empty.not_found_description")}
      />
    );
  }

  if (!isVideoDocument(doc)) {
    return (
      <EmptyState
        icon={<IconDocument size={32} />}
        title={veText("empty.not_video_title")}
        description={veText("empty.not_video_description")}
        action={<Link className="ve-link-button" to={backTarget}>{veText("back_to_file")}</Link>}
      />
    );
  }

  const sourceCardTitle = clips.length > 1 ? veText("timeline") : doc.name;
  const sourceCardDetail = clips.length > 1
    ? `${veText("video_clips_count", { count: clips.length })} · ${formatTime(timelineDuration)}`
    : `${mediaSize.width} x ${mediaSize.height}`;

  return (
    <div className="ve-shell">
      <style>{VIDEO_EDITOR_STYLES}</style>
      <header className="ve-topbar">
        <div className="ve-topbar-left">
          <button className="ve-icon-button" type="button" title={veText("back")} onClick={() => navigate(backTarget)}>
            <IconArrowLeft size={18} />
          </button>
          <div className="ve-title-block">
            <span className="ve-kicker">{veText("title")}</span>
            <div className="ve-title-row">
              <h1>{doc.name}</h1>
              <VideoEditorHelp
                titleKey="help.editor.title"
                bodyKey="help.editor.body"
                itemKeys={["help.editor.item1", "help.editor.item2", "help.editor.item3", "help.editor.item4"]}
              />
            </div>
          </div>
        </div>
        <div className="ve-topbar-actions">
          <AiEditButton
            onClick={() =>
              openEditorLiveChat({
                documentId: doc.id,
                documentName: doc.name,
                fileType: doc.file_type || "video",
                mimeType: doc.mime_type,
                editorType: "Video",
                getContent: () => JSON.stringify(buildRecipe(), null, 2),
                applyContent: (next) => {
                  try {
                    const recipe = JSON.parse(next) as VideoEditRecipe;
                    const before = cloneEditorTrackState(currentTrackState);
                    const normalized = normalizeVideoEditRecipe(recipe, sourceDuration);
                    const notice = buildAiEditNotice(before, normalized.state);
                    applyNormalizedRecipe(normalized, recipeDoc ?? recipeQuery.data ?? doc, false, notice.focus);
                    setAiEditNotice(notice);
                  } catch (error) {
                    toast.error(veText("toast.load_recipe_failed"), error instanceof Error ? error.message : undefined);
                  }
                },
              })
            }
          />
          {lastExportDoc && (
            <Link className="ve-link-button" to={`/viewer/${lastExportDoc.id}`} state={location.state}>
              <IconDocument size={15} />
              {veText("open_export")}
            </Link>
          )}
          <button className="ve-icon-button" type="button" title={veText("shortcut.undo")} onClick={undoHistory} disabled={!canUndo}>
            <IconUndo size={15} />
          </button>
          <button className="ve-icon-button" type="button" title={veText("shortcut.redo")} onClick={redoHistory} disabled={!canRedo}>
            <IconRedo size={15} />
          </button>
          {(recipeDoc || recipeQuery.data) && (
            <button className="ve-icon-button" type="button" title={veText("reload_recipe")} onClick={() => loadSavedRecipe(true)} disabled={loadingRecipe}>
              <IconDocument size={15} />
            </button>
          )}
          <button className="ve-button" type="button" title={veText("shortcut.save_recipe")} onClick={saveRecipe} disabled={saving || timelineDuration <= 0}>
            <IconEdit size={15} />
            {saving ? veText("saving") : veText("save_recipe")}
          </button>
          <button className="ve-button ve-button-primary" type="button" title={veText("shortcut.export_preview")} onClick={exportPreview} disabled={exporting || exportRangeDuration <= 0}>
            <IconDownload size={15} />
            {exporting ? veText("exporting", { progress: exportProgress }) : veText("export_preview")}
          </button>
        </div>
      </header>

      <div className="ve-workspace">
        <aside className="ve-panel ve-media-panel">
          <div className="ve-panel-header">
            <div className="ve-section-title">
              <h2>{veText("media")}</h2>
              <VideoEditorHelp
                titleKey="help.media.title"
                bodyKey="help.media.body"
                itemKeys={["help.media.item1", "help.media.item2", "help.media.item3"]}
              />
            </div>
            <span>{formatTime(sourceDuration)}</span>
          </div>
          <div className="ve-source-card">
            <div className="ve-source-thumb">
              <MediaAssetThumbnail asset={doc} kind="video" label={veText("media_kind.video")} />
            </div>
            <div className="ve-source-meta">
              <strong title={doc.name}>{sourceCardTitle}</strong>
              <span>{sourceCardDetail}</span>
            </div>
          </div>
          <div className="ve-recipe-strip">
            <strong>{recipeQuery.isLoading || scanningProjectRecipe ? veText("checking_recipe") : recipeDoc || recipeQuery.data ? veText("editable_project_linked") : veText("new_editable_project")}</strong>
            <span>{recipeDoc?.name || recipeQuery.data?.name || recipeFileName}</span>
          </div>
          <div className="ve-panel-header ve-panel-header-spaced">
            <div className="ve-section-title">
              <h2>{veText("quick_actions")}</h2>
              <VideoEditorHelp
                titleKey="help.quick_actions.title"
                bodyKey="help.quick_actions.body"
                itemKeys={["help.quick_actions.item1", "help.quick_actions.item2", "help.quick_actions.item3"]}
              />
            </div>
          </div>
          <input
            ref={subtitleFileInputRef}
            type="file"
            accept=".srt,.vtt,text/vtt,text/plain"
            hidden
            onChange={handleSubtitleFileSelected}
          />
          <div className="ve-quick-actions-grid">
            <button className="ve-track-action" type="button" title={veText("shortcut.split")} disabled={trackStates.video.locked} onClick={splitClipAtPlayhead}>
              <IconClock size={15} />
              {veText("split_at_playhead")}
            </button>
            <button className="ve-track-action" type="button" title={veText("shortcut.add_marker")} disabled={trackStates.markers.locked} onClick={addMarker}>
              <IconClock size={15} />
              {veText("add_marker")}
            </button>
            <button className="ve-track-action" type="button" disabled={trackStates.shots.locked} onClick={addShotBeat}>
              <IconPlus size={15} />
              {veText("add_story_beat")}
            </button>
            <button className="ve-track-action" type="button" disabled={trackStates.captions.locked} onClick={addCaption}>
              <IconText size={15} />
              {veText("add_dialogue_bubble")}
            </button>
            <button className="ve-track-action" type="button" disabled={trackStates.captions.locked} onClick={() => subtitleFileInputRef.current?.click()}>
              <IconUpload size={15} />
              {veText("import_subtitles")}
            </button>
            <button className="ve-track-action" type="button" disabled={savingSubtitles || captions.length === 0} onClick={exportSubtitles}>
              <IconDownload size={15} />
              {savingSubtitles ? veText("saving_subtitles") : veText("export_subtitles")}
            </button>
          </div>
          <details className="ve-more-actions">
            <summary>
              <IconPlus size={15} />
              {veText("more_actions")}
            </summary>
            <div className="ve-more-actions-grid">
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={() => addAudioCue("dialogue")}>
                <IconPlus size={15} />
                {veText("add_dialogue_cue")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={() => addAudioCue("ambience")}>
                <IconPlus size={15} />
                {veText("add_ambience_cue")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={() => addAudioCue("music")}>
                <IconPlus size={15} />
                {veText("add_music_cue")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={() => addAudioCue("sfx")}>
                <IconPlus size={15} />
                {veText("add_sfx_cue")}
              </button>
              <input
                ref={audioFileInputRef}
                type="file"
                accept="audio/*"
                hidden
                onChange={handleAudioFileSelected}
              />
              <button
                className="ve-track-action is-wide"
                type="button"
                disabled={uploadingAudio || trackStates.audio.locked}
                onClick={() => audioFileInputRef.current?.click()}
              >
                <IconPlus size={15} />
                {uploadingAudio ? veText("uploading_audio") : veText("upload_audio_asset")}
              </button>
            </div>
          </details>
          <div className="ve-panel-header ve-panel-header-spaced">
            <div className="ve-section-title">
              <h2>{veText("import_media")}</h2>
              <VideoEditorHelp
                titleKey="help.import_media.title"
                bodyKey="help.import_media.body"
                itemKeys={["help.import_media.item1", "help.import_media.item2", "help.import_media.item3"]}
              />
            </div>
            <span>{veText("capcut_style")}</span>
          </div>
          <div className="ve-media-intake">
            <input
              ref={mediaFileInputRef}
              type="file"
              accept="video/*,audio/*"
              multiple
              hidden
              onChange={handleMediaFilesSelected}
            />
            <div
              className={`ve-import-dropzone ${mediaDropActive ? "is-active" : ""}`}
              onClick={() => mediaFileInputRef.current?.click()}
              onDragOver={(event) => {
                event.preventDefault();
                setMediaDropActive(true);
              }}
              onDragLeave={() => setMediaDropActive(false)}
              onDrop={handleMediaDrop}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  mediaFileInputRef.current?.click();
                }
              }}
              role="button"
              tabIndex={0}
            >
              <IconUpload size={18} />
              <div>
                <strong>{importingMedia ? veText("importing_media") : veText("import_local_media")}</strong>
                <span>{mediaDropActive ? veText("drop_import_active") : veText("drop_import_detail")}</span>
              </div>
            </div>
            <button
              className="ve-open-media-picker"
              type="button"
              onClick={() => openMediaPicker(mediaSourceTab)}
            >
              <IconFolder size={16} />
              <span>
                <strong>{veText("browse_media_library")}</strong>
                <small>{veText("browse_media_library_detail")}</small>
              </span>
              <IconChevronRight size={15} />
            </button>
            <div className="ve-media-tabs">
              <button
                type="button"
                className={mediaSourceTab === "project" ? "is-active" : ""}
                onClick={() => openMediaPicker("project")}
              >
                {veText("project_media")}
                <span>{projectMediaAssets.length}</span>
              </button>
              <button
                type="button"
                className={mediaSourceTab === "knowledge" ? "is-active" : ""}
                onClick={() => openMediaPicker("knowledge")}
              >
                {veText("knowledge_media")}
                <span>{trimmedMediaSearch.length >= 2 ? knowledgeMediaAssets.length : "..."}</span>
              </button>
            </div>
            <div className="ve-media-search">
              <IconSearch size={15} />
              <input
                className="ve-input"
                value={mediaSearch}
                onChange={(event) => setMediaSearch(event.currentTarget.value)}
                placeholder={veText("search_media")}
              />
            </div>
            <div className="ve-media-filter-row">
              {(["all", "video", "audio", "project"] as MediaKindFilter[]).map((filter) => (
                <button
                  key={filter}
                  type="button"
                  className={mediaKindFilter === filter ? "is-active" : ""}
                  disabled={filter !== "all" && activeMediaKindCounts[filter] === 0}
                  onClick={() => selectMediaKindFilter(filter)}
                >
                  {veText(`media_filter.${filter}`)}
                  <span>{activeMediaKindCounts[filter]}</span>
                </button>
              ))}
            </div>
            <div className="ve-media-grid">
              {mediaSourceTab === "knowledge" && trimmedMediaSearch.length < 2 && (
                <div className="ve-bin-empty">{veText("search_knowledge_hint")}</div>
              )}
              {mediaSourceTab === "knowledge" && trimmedMediaSearch.length >= 2 && knowledgeMediaQuery.isLoading && (
                <div className="ve-bin-empty">{veText("loading")}</div>
              )}
              {mediaSourceTab === "knowledge" && trimmedMediaSearch.length >= 2 && !knowledgeMediaQuery.isLoading && activeMediaAssets.length === 0 && (
                <div className="ve-bin-empty">{mediaPickerEmptyMessage}</div>
              )}
              {mediaSourceTab === "project" && projectAssetsQuery.isLoading && (
                <div className="ve-bin-empty">{veText("loading")}</div>
              )}
              {mediaSourceTab === "project" && !projectAssetsQuery.isLoading && activeMediaAssets.length === 0 && (
                <div className="ve-bin-empty">{mediaPickerEmptyMessage}</div>
              )}
              {activeMediaAssets.slice(0, 24).map((asset) => renderMediaAssetCard(
                asset,
                mediaSourceTab === "project" ? veText("media_source.project") : veText("media_source.knowledge"),
              ))}
            </div>
          </div>
          <details
            className={`ve-render-status ${renderBlockers.length > 0 ? "has-blockers" : renderWarnings.length > 0 ? "has-warnings" : "is-ready"}`}
            open={renderBlockers.length > 0}
          >
            <summary>
              <strong>{renderBlockers.length > 0 ? veText("render_blocked") : renderWarnings.length > 0 ? veText("render_needs_review") : veText("ready_to_render")}</strong>
              <span>{renderIssues.length > 0 ? veText("render_issues_count", { count: renderIssues.length }) : veText("no_render_issues")}</span>
              <VideoEditorHelp
                titleKey="help.render_status.title"
                bodyKey="help.render_status.body"
                itemKeys={["help.render_status.item1", "help.render_status.item2", "help.render_status.item3"]}
              />
            </summary>
            <div className="ve-render-issue-list">
              {renderIssues.slice(0, 4).map((issue) => (
                <div key={issue.id} className={`ve-render-issue is-${issue.tone}`}>
                  <b>{issue.label}</b>
                  <small>{issue.detail}</small>
                </div>
              ))}
            </div>
          </details>
        </aside>

        <main className="ve-center">
          <section className="ve-preview">
            {aiEditNotice && (
              <div className="ve-ai-edit-notice" role="status">
                <IconSparkles size={15} />
                <div>
                  <strong>{aiEditNotice.title}</strong>
                  <span>{aiEditNotice.detail}</span>
                </div>
              </div>
            )}
            <div className="ve-preview-help">
              <VideoEditorHelp
                titleKey="help.preview.title"
                bodyKey="help.preview.body"
                itemKeys={["help.preview.item1", "help.preview.item2", "help.preview.item3"]}
                align="left"
              />
            </div>
            {downloadUrl ? (
              <>
                <video
                  ref={videoRef}
                  className="ve-preview-video"
                  src={previewSourceUrl || downloadUrl}
                  style={{ opacity: trackStates.video.visible ? 1 : 0 }}
                  preload="metadata"
                  playsInline
                  onLoadedMetadata={handleLoadedMetadata}
                  onTimeUpdate={handleTimeUpdate}
                  onPause={handlePreviewPause}
                  onEnded={handleVideoEnded}
                />
                {activeCaption && (
                  <div
                    className={`ve-caption-overlay ve-caption-${activeCaption.style} ${selection?.type === "caption" && selection.id === activeCaption.id ? "is-selected" : ""} ${hasAiHighlight("caption", activeCaption.id) ? "is-ai-highlighted" : ""}`}
                    style={{
                      left: `${activeCaption.x}%`,
                      top: `${activeCaption.y}%`,
                      color: captionDisplay(activeCaption).color,
                      background: captionDisplay(activeCaption).background,
                      fontSize: `${Math.max(14, activeCaption.size * 0.48)}px`,
                      textAlign: activeCaption.align,
                      transform: "translate(-50%, -50%)",
                    }}
                    onPointerDown={(event) => beginCaptionOverlayDrag(event, activeCaption)}
                  >
                    {activeCaption.speaker ? <strong>{activeCaption.speaker}</strong> : null}
                    <span>{activeCaption.text}</span>
                  </div>
                )}
                {activeAudioCue && (
                  <div className={`ve-preview-audio-chip ${hasAiHighlight("audio", activeAudioCue.id) ? "is-ai-highlighted" : ""}`}>
                    <span className="ve-preview-audio-meter" aria-hidden="true" />
                    <span>{activeAudioCue.label}</span>
                    {!activeAudioCue.assetDocumentId && <small>preview tone</small>}
                  </div>
                )}
                {activeShot && (
                  <div className="ve-shot-overlay">
                    <strong>{activeShot.scene} · {activeShot.shot}</strong>
                    <span>{activeShot.title}</span>
                  </div>
                )}
              </>
            ) : (
              <LoadingSpinner />
            )}
          </section>
          <div className="ve-transport">
            <button className="ve-round-button" type="button" title={isPlaying ? veText("shortcut.pause") : veText("shortcut.play")} onClick={togglePlayback}>
              {isPlaying ? <IconPause size={18} /> : <IconPlay size={18} />}
            </button>
            <button className="ve-round-button" type="button" title={veText("stop")} onClick={() => { pausePlayback(); seekTimeline(0); }}>
              <IconStop size={18} />
            </button>
            <div className="ve-timecode">{formatTime(playhead)} / {formatTime(timelineDuration)}</div>
            <div className="ve-workarea-controls">
              <label className="ve-check ve-compact-check" title={veText("work_area_title")}>
                <input
                  type="checkbox"
                  checked={normalizedWorkArea.enabled}
                  disabled={timelineDuration <= 0}
                  onChange={(event) => {
                    const enabled = event.currentTarget.checked;
                    setWorkArea((current) => ({ ...normalizeWorkArea(current, timelineDuration), enabled }));
                  }}
                />
                {veText("range")}
              </label>
              <button className="ve-mini-text-button" type="button" title={veText("shortcut.range_in")} disabled={timelineDuration <= 0} onClick={setWorkAreaInPoint}>{veText("in")}</button>
              <button className="ve-mini-text-button" type="button" title={veText("shortcut.range_out")} disabled={timelineDuration <= 0} onClick={setWorkAreaOutPoint}>{veText("out")}</button>
              <button className="ve-mini-text-button" type="button" disabled={timelineDuration <= 0 || !normalizedWorkArea.enabled} onClick={resetWorkArea}>{veText("full")}</button>
              <span>{formatTime(exportRangeStart)}-{formatTime(exportRangeEnd)}</span>
            </div>
            <input
              className="ve-scrubber"
              type="range"
              min={0}
              max={Math.max(0.05, timelineDuration)}
              step={0.05}
              value={clamp(playhead, 0, Math.max(0.05, timelineDuration))}
              onChange={(event) => seekTimeline(Number(event.currentTarget.value))}
            />
          </div>
        </main>

        <aside className="ve-panel ve-inspector">
          <div className="ve-panel-header">
            <div>
              <div className="ve-section-title">
                <h2>{veText("inspector")}</h2>
                <VideoEditorHelp
                  titleKey="help.inspector.title"
                  bodyKey="help.inspector.body"
                  itemKeys={["help.inspector.item1", "help.inspector.item2", "help.inspector.item3", "help.inspector.item4"]}
                  align="left"
                />
              </div>
              <span className="ve-panel-subtitle">{selectedInspectorLabel}</span>
            </div>
            {selection && (
              <div className="ve-inspector-actions">
                <button className="ve-icon-button" type="button" title={veText("shortcut.duplicate_selection")} disabled={selectedTrackLocked} onClick={duplicateSelection}>
                  <IconCopy size={16} />
                </button>
                <button className="ve-icon-button ve-danger" type="button" title={veText("shortcut.delete_selection")} disabled={selectedTrackLocked} onClick={deleteSelection}>
                  <IconTrash size={16} />
                </button>
              </div>
            )}
          </div>

          {!selection && (
            <div className="ve-project-overview">
              <div className={`ve-overview-status ${renderBlockers.length > 0 ? "has-blockers" : renderWarnings.length > 0 ? "has-warnings" : "is-ready"}`}>
                <strong>{renderBlockers.length > 0 ? veText("render_blocked") : renderWarnings.length > 0 ? veText("render_needs_review") : veText("ready_to_render")}</strong>
                <span>{timelineDuration > 0 ? veText("timeline_duration", { duration: formatTime(timelineDuration) }) : veText("no_timeline_duration")}</span>
              </div>
              <div className="ve-overview-grid">
                <span><b>{clips.length}</b>{veText("overview.clips")}</span>
                <span><b>{shotBeats.length}</b>{veText("overview.beats")}</span>
                <span><b>{captions.length}</b>{veText("overview.captions")}</span>
                <span><b>{audioCues.length}</b>{veText("overview.audio")}</span>
                <span><b>{markers.length}</b>{veText("overview.markers")}</span>
                <span><b>{renderIssues.length}</b>{veText("overview.issues")}</span>
              </div>
            </div>
          )}

          {selectedMarker && (
            <div className="ve-inspector-stack">
              <div className="ve-field">
                <FieldLabel>{veText("field.marker_label")}</FieldLabel>
                <input className="ve-input" value={selectedMarker.label} onChange={(event) => updateMarker(selectedMarker.id, { label: event.currentTarget.value })} />
              </div>
              <div className="ve-two-col">
                <NumberField label={veText("field.time")} value={selectedMarker.time} min={0} max={timelineDuration} onChange={(value) => updateMarker(selectedMarker.id, { time: value })} />
                <div className="ve-field">
                  <FieldLabel>{veText("field.color")}</FieldLabel>
                  <input className="ve-color" type="color" value={selectedMarker.color} onChange={(event) => updateMarker(selectedMarker.id, { color: event.currentTarget.value })} />
                </div>
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.review_notes")}</FieldLabel>
                <textarea
                  className="ve-textarea"
                  rows={5}
                  value={selectedMarker.notes}
                  onChange={(event) => updateMarker(selectedMarker.id, { notes: event.currentTarget.value })}
                  placeholder={veText("placeholder.review_notes")}
                />
              </div>
              <button className="ve-track-action" type="button" onClick={() => seekTimeline(selectedMarker.time)}>
                <IconClock size={15} />
                {veText("jump_to_marker")}
              </button>
            </div>
          )}

          {selectedClip && (
            <div className="ve-inspector-stack">
              <div className="ve-field">
                <FieldLabel>{veText("field.clip_label")}</FieldLabel>
                <input className="ve-input" value={selectedClip.label} onChange={(event) => updateClip(selectedClip.id, { label: event.currentTarget.value })} />
              </div>
              {selectedClip.assetDocumentId && (
                <div className="ve-asset-pill">
                  <strong>{veText("replacement_clip")}</strong>
                  <span>{selectedClip.assetName || selectedClip.assetDocumentId}</span>
                </div>
              )}
              <NumberField label={veText("field.source_start")} value={selectedClip.sourceStart} min={0} max={selectedClip.assetDuration || sourceDuration} onChange={(value) => updateClip(selectedClip.id, { sourceStart: value })} />
              <NumberField label={veText("field.source_end")} value={selectedClip.sourceEnd} min={0} max={selectedClip.assetDuration || sourceDuration} onChange={(value) => updateClip(selectedClip.id, { sourceEnd: value })} />
              <NumberField
                label={veText("field.duration")}
                value={Math.max(0.05, selectedClip.sourceEnd - selectedClip.sourceStart)}
                min={0.05}
                max={Math.max(0.05, getClipMaxDuration(selectedClip, sourceDuration) - selectedClip.sourceStart)}
                onChange={(value) => updateClip(selectedClip.id, { sourceEnd: selectedClip.sourceStart + Math.max(0.05, value) })}
              />
              <div className="ve-clip-actions">
                <button
                  className="ve-track-action"
                  type="button"
                  disabled={selectedTrackLocked || selectedClipIndex <= 0}
                  onClick={() => moveClip(selectedClip.id, -1)}
                >
                  <IconChevronLeft size={15} />
                  {veText("move_left")}
                </button>
                <button
                  className="ve-track-action"
                  type="button"
                  disabled={selectedTrackLocked || selectedClipIndex < 0 || selectedClipIndex >= clips.length - 1}
                  onClick={() => moveClip(selectedClip.id, 1)}
                >
                  <IconChevronRight size={15} />
                  {veText("move_right")}
                </button>
                <button
                  className="ve-track-action"
                  type="button"
                  disabled={selectedTrackLocked}
                  onClick={() => duplicateClip(selectedClip)}
                >
                  <IconCopy size={15} />
                  {veText("duplicate")}
                </button>
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.replacement_brief")}</FieldLabel>
                <textarea
                  className="ve-textarea"
                  rows={3}
                  value={selectedClip.replacementPrompt || ""}
                  onChange={(event) => updateClip(selectedClip.id, { replacementPrompt: event.currentTarget.value })}
                  placeholder={veText("placeholder.replacement_brief")}
                />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.manual_edit_notes")}</FieldLabel>
                <textarea
                  className="ve-textarea"
                  rows={3}
                  value={selectedClip.editNotes || ""}
                  onChange={(event) => updateClip(selectedClip.id, { editNotes: event.currentTarget.value })}
                  placeholder={veText("placeholder.manual_edit_notes")}
                />
              </div>
              <input
                ref={replacementVideoInputRef}
                type="file"
                accept="video/*"
                hidden
                onChange={handleReplacementVideoSelected}
              />
              <button
                className="ve-track-action"
                type="button"
                disabled={uploadingVideo || selectedTrackLocked}
                onClick={() => replacementVideoInputRef.current?.click()}
              >
                <IconPlus size={15} />
                {uploadingVideo ? veText("uploading_replacement") : veText("attach_replacement_video")}
              </button>
              {selectedClip.assetDocumentId && (
                <button
                  className="ve-track-action"
                  type="button"
                  disabled={selectedTrackLocked}
                  onClick={() => {
                    updateClip(selectedClip.id, { assetDocumentId: null, assetName: null, assetMimeType: null, assetDuration: null });
                    const activeAtPlayhead = mapTimelineTime(playhead, clips)?.clip.id === selectedClip.id;
                    if (activeAtPlayhead && videoRef.current && downloadUrl) {
                      setPreviewSourceUrl(downloadUrl);
                      videoRef.current.pause();
                      videoRef.current.src = downloadUrl;
                      videoRef.current.load();
                    }
                  }}
                >
                  <IconTrash size={15} />
                  {veText("remove_replacement")}
                </button>
              )}
              <label className="ve-check">
                <input type="checkbox" checked={selectedClip.muted} onChange={(event) => updateClip(selectedClip.id, { muted: event.currentTarget.checked })} />
                {veText("mute_source_audio")}
              </label>
            </div>
          )}

          {selectedShot && (
            <div className="ve-inspector-stack">
              <div className="ve-field">
                <FieldLabel>{veText("field.beat_title")}</FieldLabel>
                <input className="ve-input" value={selectedShot.title} onChange={(event) => updateShot(selectedShot.id, { title: event.currentTarget.value })} />
              </div>
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>{veText("field.scene")}</FieldLabel>
                  <input className="ve-input" value={selectedShot.scene} onChange={(event) => updateShot(selectedShot.id, { scene: event.currentTarget.value })} />
                </div>
                <div className="ve-field">
                  <FieldLabel>{veText("field.shot")}</FieldLabel>
                  <input className="ve-input" value={selectedShot.shot} onChange={(event) => updateShot(selectedShot.id, { shot: event.currentTarget.value })} />
                </div>
              </div>
              <div className="ve-two-col">
                <NumberField label={veText("field.start")} value={selectedShot.start} min={0} max={timelineDuration} onChange={(value) => updateShot(selectedShot.id, { start: value })} />
                <NumberField label={veText("field.end")} value={selectedShot.end} min={0} max={timelineDuration} onChange={(value) => updateShot(selectedShot.id, { end: value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.location")}</FieldLabel>
                <input className="ve-input" value={selectedShot.location} onChange={(event) => updateShot(selectedShot.id, { location: event.currentTarget.value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.camera")}</FieldLabel>
                <input className="ve-input" value={selectedShot.camera} onChange={(event) => updateShot(selectedShot.id, { camera: event.currentTarget.value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.action")}</FieldLabel>
                <textarea className="ve-textarea" rows={3} value={selectedShot.action} onChange={(event) => updateShot(selectedShot.id, { action: event.currentTarget.value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.dialogue_intent")}</FieldLabel>
                <textarea className="ve-textarea" rows={3} value={selectedShot.dialogue} onChange={(event) => updateShot(selectedShot.id, { dialogue: event.currentTarget.value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.notes")}</FieldLabel>
                <textarea className="ve-textarea" rows={3} value={selectedShot.notes} onChange={(event) => updateShot(selectedShot.id, { notes: event.currentTarget.value })} />
              </div>
            </div>
          )}

          {selectedCaption && (
            <div className="ve-inspector-stack">
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>{veText("field.speaker")}</FieldLabel>
                  <input className="ve-input" value={selectedCaption.speaker || ""} onChange={(event) => updateCaption(selectedCaption.id, { speaker: event.currentTarget.value })} />
                </div>
                <div className="ve-field">
                  <FieldLabel>{veText("field.emotion")}</FieldLabel>
                  <input className="ve-input" value={selectedCaption.emotion || ""} onChange={(event) => updateCaption(selectedCaption.id, { emotion: event.currentTarget.value })} />
                </div>
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.caption_style")}</FieldLabel>
                <Select
                  value={selectedCaption.style}
                  onChange={(value) => updateCaption(selectedCaption.id, { style: value as CaptionCue["style"] })}
                  options={captionStyleOptions}
                  buttonStyle={{ boxShadow: "none" }}
                />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.caption_text")}</FieldLabel>
                <textarea className="ve-textarea" value={selectedCaption.text} rows={4} onChange={(event) => updateCaption(selectedCaption.id, { text: event.currentTarget.value })} />
              </div>
              <div className="ve-two-col">
                <NumberField label={veText("field.start")} value={selectedCaption.start} min={0} max={timelineDuration} onChange={(value) => updateCaption(selectedCaption.id, { start: value })} />
                <NumberField label={veText("field.end")} value={selectedCaption.end} min={0} max={timelineDuration} onChange={(value) => updateCaption(selectedCaption.id, { end: value })} />
              </div>
              <div className="ve-two-col">
                <NumberField label="X %" value={selectedCaption.x} min={0} max={100} step={1} onChange={(value) => updateCaption(selectedCaption.id, { x: value })} />
                <NumberField label="Y %" value={selectedCaption.y} min={0} max={100} step={1} onChange={(value) => updateCaption(selectedCaption.id, { y: value })} />
              </div>
              <NumberField label={veText("field.size")} value={selectedCaption.size} min={10} max={96} step={1} onChange={(value) => updateCaption(selectedCaption.id, { size: value })} />
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>{veText("field.text_color")}</FieldLabel>
                  <input className="ve-color" type="color" value={selectedCaption.color} onChange={(event) => updateCaption(selectedCaption.id, { color: event.currentTarget.value })} />
                </div>
                <div className="ve-field">
                  <FieldLabel>{veText("field.bubble_color")}</FieldLabel>
                  <input className="ve-color" type="color" value={selectedCaption.backgroundColor} onChange={(event) => updateCaption(selectedCaption.id, { backgroundColor: event.currentTarget.value })} />
                </div>
              </div>
              <div className="ve-two-col">
                <NumberField label={veText("field.bubble_opacity")} value={selectedCaption.backgroundOpacity} min={0} max={1} step={0.05} onChange={(value) => updateCaption(selectedCaption.id, { backgroundOpacity: value })} />
                <div className="ve-field">
                  <FieldLabel>{veText("field.align")}</FieldLabel>
                  <Select
                    value={selectedCaption.align}
                    onChange={(value) => updateCaption(selectedCaption.id, { align: value as CanvasTextAlign })}
                    options={captionAlignOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
              </div>
            </div>
          )}

          {selectedAudio && (
            <div className="ve-inspector-stack">
              <div className="ve-field">
                <FieldLabel>{veText("field.audio_type")}</FieldLabel>
                <Select
                  value={selectedAudio.type}
                  onChange={(value) => {
                    const nextType = value as AudioCueType;
                    updateAudioCue(selectedAudio.id, {
                      type: nextType,
                      label: AUDIO_TYPE_LABELS[nextType],
                      fadeIn: defaultAudioFade(nextType),
                      fadeOut: defaultAudioFade(nextType),
                      loop: defaultAudioLoop(nextType),
                      duckUnderDialogue: defaultDuckUnderDialogue(nextType),
                    });
                  }}
                  options={audioTypeOptions}
                  buttonStyle={{ boxShadow: "none" }}
                />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.label")}</FieldLabel>
                <input className="ve-input" value={selectedAudio.label} onChange={(event) => updateAudioCue(selectedAudio.id, { label: event.currentTarget.value })} />
              </div>
              {selectedAudio.assetDocumentId && (
                <div className="ve-asset-pill">
                  <strong>{veText("audio_asset")}</strong>
                  <span>{selectedAudio.assetName || selectedAudio.assetDocumentId}</span>
                </div>
              )}
              <div className="ve-two-col">
                <NumberField label={veText("field.start")} value={selectedAudio.start} min={0} max={timelineDuration} onChange={(value) => updateAudioCue(selectedAudio.id, { start: value })} />
                <NumberField label={veText("field.end")} value={selectedAudio.end} min={0} max={timelineDuration} onChange={(value) => updateAudioCue(selectedAudio.id, { end: value })} />
              </div>
              <NumberField label={veText("field.volume_db")} value={selectedAudio.volumeDb} min={-48} max={6} step={1} onChange={(value) => updateAudioCue(selectedAudio.id, { volumeDb: value })} />
              <div className="ve-two-col">
                <NumberField label={veText("field.fade_in")} value={selectedAudio.fadeIn} min={0} max={10} step={0.1} onChange={(value) => updateAudioCue(selectedAudio.id, { fadeIn: value })} />
                <NumberField label={veText("field.fade_out")} value={selectedAudio.fadeOut} min={0} max={10} step={0.1} onChange={(value) => updateAudioCue(selectedAudio.id, { fadeOut: value })} />
              </div>
              <div className="ve-two-col">
                <label className="ve-check">
                  <input type="checkbox" checked={selectedAudio.loop} onChange={(event) => updateAudioCue(selectedAudio.id, { loop: event.currentTarget.checked })} />
                  {veText("loop_asset")}
                </label>
                <label className="ve-check">
                  <input type="checkbox" checked={selectedAudio.duckUnderDialogue} onChange={(event) => updateAudioCue(selectedAudio.id, { duckUnderDialogue: event.currentTarget.checked })} />
                  {veText("duck_under_dialogue")}
                </label>
              </div>
              <label className="ve-check">
                <input type="checkbox" checked={selectedAudio.muted} onChange={(event) => updateAudioCue(selectedAudio.id, { muted: event.currentTarget.checked })} />
                {veText("mute_this_cue")}
              </label>
              <div className="ve-field">
                <FieldLabel>{veText("field.source_plan")}</FieldLabel>
                <textarea className="ve-textarea" rows={3} value={selectedAudio.sourcePlan} onChange={(event) => updateAudioCue(selectedAudio.id, { sourcePlan: event.currentTarget.value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.generation_prompt")}</FieldLabel>
                <textarea className="ve-textarea" rows={4} value={selectedAudio.prompt} onChange={(event) => updateAudioCue(selectedAudio.id, { prompt: event.currentTarget.value })} />
              </div>
            </div>
          )}
        </aside>
      </div>

      <section className="ve-timeline">
        <div className="ve-timeline-header">
          <div>
            <strong>{veText("timeline")}</strong>
            <VideoEditorHelp
              titleKey="help.timeline.title"
              bodyKey="help.timeline.body"
              itemKeys={["help.timeline.item1", "help.timeline.item2", "help.timeline.item3", "help.timeline.item4"]}
              align="left"
            />
            <span>{formatTime(timelineDuration)}</span>
          </div>
          <div className="ve-timeline-summary">
            <span>{veText("video_clips_count", { count: clips.length })}</span>
            <span>{veText("captions_count", { count: captions.length })}</span>
            <span>{veText("audio_cues_count", { count: audioCues.length })}</span>
          </div>
          <div className="ve-timeline-tools">
            <label className="ve-check ve-compact-check">
              <input type="checkbox" checked={snapEnabled} onChange={(event) => setSnapEnabled(event.currentTarget.checked)} />
              {veText("snap")}
            </label>
            <Select
              value={String(nudgeStep)}
              onChange={(value) => setNudgeStep(Number(value))}
              options={nudgeStepOptions}
              style={{ width: 86 }}
              buttonStyle={compactSelectButtonStyle}
            />
            <button className="ve-mini-button" type="button" title={veText("shortcut.nudge_left")} disabled={!selection || selectedTrackLocked} onClick={() => nudgeSelection(-1)}>
              <IconChevronLeft size={14} />
            </button>
            <button className="ve-mini-button" type="button" title={veText("shortcut.nudge_right")} disabled={!selection || selectedTrackLocked} onClick={() => nudgeSelection(1)}>
              <IconChevronRight size={14} />
            </button>
            <button className="ve-mini-button" type="button" title={veText("shortcut.previous_marker")} disabled={markers.length === 0} onClick={() => jumpToMarker(-1)}>
              <IconChevronLeft size={14} />
            </button>
            <button className="ve-mini-button" type="button" title={veText("shortcut.next_marker")} disabled={markers.length === 0} onClick={() => jumpToMarker(1)}>
              <IconChevronRight size={14} />
            </button>
            <button className="ve-mini-text-button" type="button" title={veText("shortcut.add_marker")} disabled={trackStates.markers.locked} onClick={addMarker}>{veText("marker")}</button>
            <div className="ve-zoom-control">
              <span>{veText("zoom")}</span>
              <input
                className="ve-zoom-slider"
                type="range"
                min={16}
                max={140}
                step={1}
                value={timelinePixelsPerSecond}
                onChange={(event) => setTimelinePixelsPerSecond(Number(event.currentTarget.value))}
              />
              <button className="ve-mini-text-button" type="button" onClick={fitTimelineZoom}>{veText("fit")}</button>
            </div>
          </div>
        </div>
        <div className="ve-timeline-scroll" ref={timelineScrollRef}>
          <div className="ve-timeline-content" style={timelineTrackStyle}>
            {normalizedWorkArea.enabled && timelineDuration > 0 && (
              <div className="ve-workarea-overlay" style={{ left: `${workAreaLeft}px`, width: `${Math.max(2, workAreaWidth)}px` }} />
            )}
            <div className="ve-time-ruler">
              <div className="ve-time-ruler-label">{veText("time")}</div>
              <div
                className="ve-time-ruler-lane"
                role="slider"
                aria-label={veText("timeline_position")}
                aria-valuemin={0}
                aria-valuemax={Math.max(0, timelineDuration)}
                aria-valuenow={clamp(playhead, 0, timelineDuration)}
                onPointerDown={beginTimelineScrub}
              >
                {timelineRulerTicks.map((tick) => {
                  const left = timelineDuration ? (tick.time / timelineDuration) * 100 : 0;
                  return (
                    <span key={`${tick.time}-${tick.major ? "major" : "minor"}`} className={`ve-ruler-tick ${tick.major ? "is-major" : ""}`} style={{ left: `${left}%` }}>
                      {tick.major && <b>{formatTime(tick.time)}</b>}
                    </span>
                  );
                })}
              </div>
            </div>
            <div className="ve-track ve-marker-track">
              {renderTrackLabel("markers", veText("markers"), { visibility: false })}
              <div className={`ve-track-lane ve-marker-lane ${trackStates.markers.locked ? "is-locked-track" : ""}`}>
                {markers.length === 0 && (
                  <span className="ve-marker-empty">{veText("marker_empty")}</span>
                )}
                {markers.map((marker) => {
                  const left = timelineDuration ? (marker.time / timelineDuration) * 100 : 0;
                  return (
                    <button
                      key={marker.id}
                      type="button"
                      disabled={trackStates.markers.locked}
                      className={`ve-marker-pin ${selection?.type === "marker" && selection.id === marker.id ? "is-selected" : ""} ${hasAiHighlight("marker", marker.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, color: marker.color }}
                      title={`${marker.label} · ${formatTime(marker.time)}`}
                      onPointerDown={(event) => beginMarkerDrag(event, marker.id)}
                      onClick={() => {
                        setSelection({ type: "marker", id: marker.id });
                        seekTimeline(marker.time);
                      }}
                    >
                      <span className="ve-marker-diamond" />
                      <span>{marker.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="ve-track">
              {renderTrackLabel("shots", veText("shots"))}
              <div className={`ve-track-lane ${trackStates.shots.visible ? "" : "is-hidden-track"} ${trackStates.shots.locked ? "is-locked-track" : ""}`}>
                {shotBeats.map((shot) => {
                  const left = timelineDuration ? (shot.start / timelineDuration) * 100 : 0;
                  const width = timelineDuration ? ((shot.end - shot.start) / timelineDuration) * 100 : 0;
                  return (
                    <button
                      key={shot.id}
                      type="button"
                      disabled={trackStates.shots.locked}
                      className={`ve-shot-block ${trackStates.shots.visible ? "" : "is-hidden-track"} ${trackStates.shots.locked ? "is-locked-track" : ""} ${selection?.type === "shot" && selection.id === shot.id ? "is-selected" : ""} ${hasAiHighlight("shot", shot.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${Math.max(1.5, width)}%` }}
                      onPointerDown={(event) => beginCueDrag(event, "shot", shot.id)}
                      onClick={() => setSelection({ type: "shot", id: shot.id })}
                    >
                      <span className="ve-resize-handle ve-resize-start" onPointerDown={(event) => beginCueResize(event, "shot", shot.id, "start")} />
                      <span className="ve-block-label">{shot.scene} · {shot.title}</span>
                      <span className="ve-resize-handle ve-resize-end" onPointerDown={(event) => beginCueResize(event, "shot", shot.id, "end")} />
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="ve-track">
              {renderTrackLabel("video", veText("video"), { mute: true })}
              <div
                className={`ve-track-lane ve-video-lane ${trackStates.video.visible ? "" : "is-hidden-track"} ${trackStates.video.locked ? "is-locked-track" : ""} ${trackStates.video.muted ? "is-muted-track" : ""}`}
                onDragOver={allowTimelineMediaDrop}
                onDrop={(event) => { void handleTimelineMediaDrop(event, "video"); }}
                onPointerDown={(event) => {
                  if (event.target === event.currentTarget) beginTimelineScrub(event);
                }}
              >
                {videoClipSpans.length === 0 && (
                  <span className="ve-video-lane-empty">{veText("video_lane_empty")}</span>
                )}
                {videoClipSpans.map((span) => {
                  const clip = span.clip;
                  const duration = Math.max(0.05, span.duration);
                  const left = timelineDuration ? (span.start / timelineDuration) * 100 : 0;
                  const width = timelineDuration ? (duration / timelineDuration) * 100 : 0;
                  const label = clip.assetDocumentId ? `${clip.label} · ${veText("replacement_clip")}` : clip.label;
                  return (
                    <button
                      key={clip.id}
                      type="button"
                      disabled={trackStates.video.locked}
                      className={`ve-clip-block ${trackStates.video.visible ? "" : "is-hidden-track"} ${trackStates.video.locked ? "is-locked-track" : ""} ${trackStates.video.muted ? "is-muted-track" : ""} ${clipHasManualEdit(clip, sourceDuration) ? "is-manual" : ""} ${selection?.type === "clip" && selection.id === clip.id ? "is-selected" : ""} ${draggingClipId === clip.id ? "is-dragging" : ""} ${hasAiHighlight("clip", clip.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${width}%`, background: clip.color }}
                      title={veText("clip_drag_hint")}
                      aria-label={veText("clip_aria_label", { label, duration: formatTime(duration) })}
                      aria-grabbed={draggingClipId === clip.id}
                      draggable={false}
                      onPointerDown={(event) => beginClipDrag(event, clip.id)}
                      onDragStart={(event) => event.preventDefault()}
                      onClick={() => setSelection({ type: "clip", id: clip.id })}
                    >
                      <span className="ve-resize-handle ve-resize-start" title={veText("trim_start")} onPointerDown={(event) => beginClipTrim(event, clip.id, "start")} />
                      <span className="ve-clip-grip" aria-hidden="true"><IconDragHandle size={13} /></span>
                      <span className="ve-clip-title">{label}</span>
                      <small>{formatTime(duration)}</small>
                      <span className="ve-resize-handle ve-resize-end" title={veText("trim_end")} onPointerDown={(event) => beginClipTrim(event, clip.id, "end")} />
                    </button>
                  );
                })}
                {clipDropPreview && timelineDuration > 0 && (
                  <div
                    className="ve-clip-drop-indicator"
                    style={{ left: `${(clipDropPreview.time / timelineDuration) * 100}%` }}
                    aria-hidden="true"
                  >
                    <span>{veText("drop_here")}</span>
                  </div>
                )}
              </div>
            </div>
            <div className="ve-track">
              {renderTrackLabel("captions", veText("captions"))}
              <div className={`ve-track-lane ${trackStates.captions.visible ? "" : "is-hidden-track"} ${trackStates.captions.locked ? "is-locked-track" : ""}`}>
                {captions.map((caption) => {
                  const left = timelineDuration ? (caption.start / timelineDuration) * 100 : 0;
                  const width = timelineDuration ? ((caption.end - caption.start) / timelineDuration) * 100 : 0;
                  return (
                    <button
                      key={caption.id}
                      type="button"
                      disabled={trackStates.captions.locked}
                      className={`ve-caption-block ${trackStates.captions.visible ? "" : "is-hidden-track"} ${trackStates.captions.locked ? "is-locked-track" : ""} ${selection?.type === "caption" && selection.id === caption.id ? "is-selected" : ""} ${hasAiHighlight("caption", caption.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${Math.max(1.5, width)}%` }}
                      onPointerDown={(event) => beginCueDrag(event, "caption", caption.id)}
                      onClick={() => setSelection({ type: "caption", id: caption.id })}
                    >
                      <span className="ve-resize-handle ve-resize-start" onPointerDown={(event) => beginCueResize(event, "caption", caption.id, "start")} />
                      <span className="ve-block-label">{caption.speaker ? `${caption.speaker}: ${caption.text}` : caption.text}</span>
                      <span className="ve-resize-handle ve-resize-end" onPointerDown={(event) => beginCueResize(event, "caption", caption.id, "end")} />
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="ve-track">
              {renderTrackLabel("audio", veText("audio"), { mute: true, visibility: false })}
              <div
                className={`ve-track-lane ${trackStates.audio.locked ? "is-locked-track" : ""} ${trackStates.audio.muted ? "is-muted-track" : ""}`}
                onDragOver={allowTimelineMediaDrop}
                onDrop={(event) => { void handleTimelineMediaDrop(event, "audio"); }}
                onPointerDown={(event) => {
                  if (event.target === event.currentTarget) beginTimelineScrub(event);
                }}
              >
                {audioCues.map((cue) => {
                  const left = timelineDuration ? (cue.start / timelineDuration) * 100 : 0;
                  const width = timelineDuration ? ((cue.end - cue.start) / timelineDuration) * 100 : 0;
                  return (
                    <button
                      key={cue.id}
                      type="button"
                      disabled={trackStates.audio.locked}
                      className={`ve-audio-block ${trackStates.audio.locked ? "is-locked-track" : ""} ${trackStates.audio.muted ? "is-muted-track" : ""} ${selection?.type === "audio" && selection.id === cue.id ? "is-selected" : ""} ${hasAiHighlight("audio", cue.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${Math.max(1.5, width)}%`, background: AUDIO_TYPE_COLORS[cue.type] }}
                      onPointerDown={(event) => beginCueDrag(event, "audio", cue.id)}
                      onClick={() => setSelection({ type: "audio", id: cue.id })}
                    >
                      <span className="ve-resize-handle ve-resize-start" onPointerDown={(event) => beginCueResize(event, "audio", cue.id, "start")} />
                      <span className="ve-block-label">{cue.label}</span>
                      <span className="ve-resize-handle ve-resize-end" onPointerDown={(event) => beginCueResize(event, "audio", cue.id, "end")} />
                    </button>
                  );
                })}
              </div>
            </div>
            <div
              className="ve-playhead"
              style={timelinePlayheadStyle}
              role="slider"
              tabIndex={0}
              aria-label={veText("timeline_position")}
              aria-valuemin={0}
              aria-valuemax={Math.max(0, timelineDuration)}
              aria-valuenow={clamp(playhead, 0, timelineDuration)}
              title={veText("timeline_position")}
              onPointerDown={beginPlayheadDrag}
            />
          </div>
        </div>
      </section>

      <Modal
        open={mediaPickerOpen}
        onClose={closeMediaPicker}
        title={veText("media_picker_title")}
        className="ve-media-picker-dialog"
        bodyClassName="ve-media-picker-dialog-body"
        width="min(1120px, calc(100vw - 48px))"
        height="min(780px, calc(100dvh - 48px))"
        maxWidth="1120px"
        footer={(
          <div className="ve-picker-footer">
            <span>{veText("media_picker_selected_count", { count: mediaPickerSelectedAssets.length })}</span>
            <button className="ve-button" type="button" onClick={closeMediaPicker}>{t("action.cancel")}</button>
            <button
              className="ve-button"
              type="button"
              disabled={activeMediaAssets.length === 0}
              onClick={selectVisibleMediaAssets}
            >
              {veText("media_picker_select_visible")}
            </button>
            <button
              className="ve-button ve-button-primary"
              type="button"
              disabled={mediaPickerSelectedAssets.length === 0 || uploadingVideo || uploadingAudio}
              onClick={() => { void addSelectedMediaAssets(); }}
            >
              {veText("media_picker_add_selected", { count: mediaPickerSelectedAssets.length })}
            </button>
          </div>
        )}
      >
        <div className="ve-media-picker">
          <div className="ve-picker-toolbar">
            <div className="ve-media-tabs ve-picker-source-tabs">
              <button
                type="button"
                className={mediaSourceTab === "project" ? "is-active" : ""}
                onClick={() => switchMediaPickerSource("project")}
              >
                {veText("project_media")}
                <span>{projectMediaAssets.length}</span>
              </button>
              <button
                type="button"
                className={mediaSourceTab === "knowledge" ? "is-active" : ""}
                onClick={() => switchMediaPickerSource("knowledge")}
              >
                {veText("knowledge_media")}
                <span>{knowledgeMediaAssets.length}</span>
              </button>
            </div>
            <div className="ve-media-search ve-picker-search">
              <IconSearch size={15} />
              <input
                className="ve-input"
                value={mediaSearch}
                onChange={(event) => setMediaSearch(event.currentTarget.value)}
                placeholder={mediaSourceTab === "knowledge" ? veText("search_knowledge_media") : veText("search_media")}
                autoFocus
              />
            </div>
            <div className="ve-media-filter-row ve-picker-filters">
              {(["all", "video", "audio", "project"] as MediaKindFilter[]).map((filter) => (
                <button
                  key={filter}
                  type="button"
                  className={mediaKindFilter === filter ? "is-active" : ""}
                  disabled={filter !== "all" && activeMediaKindCounts[filter] === 0}
                  onClick={() => selectMediaKindFilter(filter)}
                >
                  {veText(`media_filter.${filter}`)}
                  <span>{activeMediaKindCounts[filter]}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="ve-picker-hint">
            <span>{mediaSourceTab === "knowledge" ? veText("media_picker_knowledge_hint") : veText("media_picker_project_hint")}</span>
            {mediaPickerSelectedIds.length > 0 && (
              <button type="button" onClick={() => setMediaPickerSelectedIds([])}>
                {veText("media_picker_clear_selection")}
              </button>
            )}
          </div>
          <div className="ve-picker-grid">
            {mediaSourceTab === "knowledge" && knowledgeMediaQuery.isLoading && (
              <div className="ve-bin-empty">{veText("loading")}</div>
            )}
            {mediaSourceTab === "knowledge" && !knowledgeMediaQuery.isLoading && activeMediaAssets.length === 0 && (
              <div className="ve-bin-empty">{mediaPickerEmptyMessage}</div>
            )}
            {mediaSourceTab === "project" && projectAssetsQuery.isLoading && (
              <div className="ve-bin-empty">{veText("loading")}</div>
            )}
            {mediaSourceTab === "project" && !projectAssetsQuery.isLoading && activeMediaAssets.length === 0 && (
              <div className="ve-bin-empty">{mediaPickerEmptyMessage}</div>
            )}
            {activeMediaAssets.map((asset) => renderMediaAssetCard(
              asset,
              mediaSourceTab === "project" ? veText("media_source.project") : veText("media_source.knowledge"),
              {
                selectable: true,
                selected: mediaPickerSelectedIds.includes(asset.id),
                onToggle: (item) => toggleMediaPickerSelection(item.id),
              },
            ))}
          </div>
        </div>
      </Modal>
    </div>
  );
}

const VIDEO_EDITOR_STYLES = `
@media (min-width: 761px) {
  body.video-editor-page-active {
    overflow: hidden;
  }
}
.ve-shell {
  --ve-border: rgba(214, 211, 209, 0.72);
  --ve-border-soft: rgba(231, 229, 228, 0.78);
  --ve-surface: rgba(255, 255, 255, 0.86);
  --ve-surface-solid: #ffffff;
  --ve-muted-surface: rgba(250, 250, 249, 0.82);
  --ve-teal: #436b65;
  box-sizing: border-box;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: hidden;
  padding: 12px 14px 14px;
  background: #fafaf9;
  color: #1c1917;
}
.ve-loading {
  min-height: 320px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.ve-topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 10px 12px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 14px;
  background: var(--ve-surface);
  box-shadow: 0 12px 28px rgba(28,25,23,.04);
}
.ve-topbar-left, .ve-topbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ve-topbar-actions {
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}
.ve-title-block {
  min-width: 0;
}
.ve-title-row, .ve-section-title, .ve-track-label-name {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ve-title-row h1, .ve-section-title h2, .ve-track-label-name > span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ve-help-anchor {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  line-height: 1;
}
.ve-help-popover {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.ve-help-popover strong {
  color: #1c1917;
  font-size: 12px;
}
.ve-help-popover p {
  margin: 0;
  color: #57534e;
}
.ve-help-popover ul {
  margin: 0;
  padding-left: 17px;
  color: #44403c;
}
.ve-help-popover li + li {
  margin-top: 4px;
}
.ve-kicker {
  display: block;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0;
  color: #78716c;
  font-weight: 800;
}
.ve-title-block h1 {
  margin: 1px 0 0;
  font-size: 16px;
  line-height: 1.2;
  white-space: nowrap;
  max-width: 44vw;
}
.ve-workspace {
  display: grid;
  grid-template-columns: minmax(250px, 280px) minmax(420px, 1fr) minmax(284px, 330px);
  gap: 12px;
  flex: 1 1 auto;
  height: auto;
  min-height: 0;
  align-items: stretch;
}
.ve-panel, .ve-preview, .ve-timeline {
  border: 1px solid var(--ve-border-soft);
  border-radius: 14px;
  background: var(--ve-surface-solid);
}
.ve-panel {
  padding: 12px;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  scrollbar-gutter: stable;
}
.ve-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 9px;
}
.ve-panel-header-spaced {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--ve-border-soft);
}
.ve-panel-header h2 {
  margin: 0;
  font-size: 13px;
  font-weight: 800;
  color: #292524;
}
.ve-panel-subtitle {
  display: block;
  margin-top: 2px;
  color: #a8a29e;
  font-size: 11px;
  font-weight: 800;
}
.ve-panel-header span {
  color: #78716c;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.ve-source-card {
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 10px;
  align-items: center;
  padding: 8px;
  border-radius: 12px;
  background: var(--ve-muted-surface);
}
.ve-source-thumb {
  height: 52px;
  overflow: hidden;
  border-radius: 8px;
  background: #e7e5e4;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #78716c;
}
.ve-source-thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.ve-source-thumb .ve-media-thumb {
  width: 100%;
  height: 100%;
  border-radius: inherit;
}
.ve-source-meta {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.ve-source-meta strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
}
.ve-source-meta span {
  font-size: 12px;
  color: #78716c;
}
.ve-recipe-strip {
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 10px;
  background: #fafaf9;
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.ve-recipe-strip strong {
  font-size: 12px;
  color: #436b65;
}
.ve-recipe-strip span {
  font-size: 12px;
  color: #78716c;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-media-intake {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ve-import-dropzone {
  min-height: 58px;
  border: 1px dashed #99cfc7;
  border-radius: 12px;
  background: #f8fffd;
  color: #436b65;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  cursor: pointer;
  transition: border-color .12s ease, background .12s ease, box-shadow .12s ease;
}
.ve-import-dropzone:hover, .ve-import-dropzone.is-active {
  border-color: #436b65;
  background: #eefdfa;
  box-shadow: inset 0 0 0 1px rgba(67,107,101,.12);
}
.ve-import-dropzone > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-import-dropzone strong {
  font-size: 12px;
  color: #1c1917;
}
.ve-import-dropzone span {
  font-size: 11px;
  color: #78716c;
}
.ve-open-media-picker {
  min-height: 44px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 12px;
  background: #ffffff;
  color: #436b65;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  padding: 8px 10px;
  text-align: left;
  cursor: pointer;
  transition: border-color .12s ease, box-shadow .12s ease, transform .12s ease;
}
.ve-open-media-picker:hover {
  border-color: #436b65;
  box-shadow: 0 8px 18px rgba(28,25,23,.06);
  transform: translateY(-1px);
}
.ve-open-media-picker span {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.ve-open-media-picker strong {
  color: #1c1917;
  font-size: 12px;
}
.ve-open-media-picker small {
  color: #78716c;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-media-tabs {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.ve-media-tabs button, .ve-media-filter-row button {
  min-width: 0;
  border: 1px solid var(--ve-border-soft);
  background: #ffffff;
  color: #57534e;
  border-radius: 8px;
  min-height: 30px;
  font-size: 11px;
  font-weight: 850;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-media-tabs button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 0 9px;
}
.ve-media-tabs button span {
  color: #a8a29e;
}
.ve-media-tabs button.is-active, .ve-media-filter-row button.is-active {
  border-color: #436b65;
  background: #ecfdf8;
  color: #436b65;
}
.ve-media-search {
  position: relative;
  display: flex;
  align-items: center;
}
.ve-media-search svg {
  position: absolute;
  left: 10px;
  color: #a8a29e;
  pointer-events: none;
}
.ve-media-search .ve-input {
  padding-left: 31px;
}
.ve-media-filter-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 5px;
}
.ve-media-filter-row button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  padding: 0 8px;
}
.ve-media-filter-row button span {
  color: #a8a29e;
  font-size: 10px;
  font-weight: 900;
}
.ve-media-filter-row button.is-active span {
  color: inherit;
}
.ve-media-filter-row button:disabled {
  background: #fafaf9;
  color: #a8a29e;
  cursor: not-allowed;
  opacity: 0.72;
}
.ve-media-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 7px;
  max-height: 245px;
  overflow: auto;
  padding: 1px 2px 2px 1px;
}
.ve-media-grid .ve-bin-empty {
  grid-column: 1 / -1;
}
.ve-media-card {
  position: relative;
  min-width: 0;
  height: 74px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 12px;
  background: #ffffff;
  overflow: hidden;
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr) 38px;
  grid-template-rows: 1fr;
  cursor: grab;
  transition: border-color .12s ease, box-shadow .12s ease, transform .12s ease;
}
.ve-media-card:hover {
  border-color: #a7c9c2;
  box-shadow: 0 9px 18px rgba(28,25,23,.06);
  transform: translateY(-1px);
}
.ve-media-card.is-project {
  cursor: default;
}
.ve-media-select {
  position: absolute;
  top: 6px;
  right: 6px;
  z-index: 3;
  width: 22px;
  height: 22px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,.86);
  background: rgba(28,25,23,.62);
  color: #ffffff;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  box-shadow: 0 4px 10px rgba(28,25,23,.18);
}
.ve-media-select.is-selected {
  border-color: #436b65;
  background: #436b65;
}
.ve-media-thumb {
  position: relative;
  height: 100%;
  min-height: 0;
  background: #eef4f3;
  color: #436b65;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.ve-media-thumb.has-thumbnail {
  background: #020617;
}
.ve-media-thumb img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}
.ve-media-thumb video {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  background: #020617;
}
.ve-media-thumb.is-loading::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(110deg, rgba(255,255,255,0) 18%, rgba(255,255,255,.38) 42%, rgba(255,255,255,0) 66%);
  transform: translateX(-100%);
  animation: ve-thumb-shimmer 1.1s ease-in-out infinite;
  pointer-events: none;
}
.ve-media-card.is-audio .ve-media-thumb {
  background: #eef6ff;
  color: #4869ac;
}
.ve-media-card.is-project .ve-media-thumb {
  background: #f9f4ec;
  color: #b66a3c;
}
.ve-media-type-badge {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 1;
  max-width: calc(100% - 12px);
  padding: 3px 6px;
  border-radius: 6px;
  background: rgba(28,25,23,.74);
  color: #ffffff;
  font-size: 9px;
  font-weight: 900;
  text-transform: uppercase;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-video-glyph {
  display: grid;
  grid-template-columns: repeat(3, 18px);
  gap: 5px;
}
@keyframes ve-thumb-shimmer {
  to {
    transform: translateX(100%);
  }
}
.ve-video-glyph span {
  height: 24px;
  border-radius: 4px;
  background: rgba(67,107,101,.22);
  box-shadow: inset 0 0 0 1px rgba(67,107,101,.22);
}
.ve-audio-wave {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 34px;
}
.ve-audio-wave i {
  display: block;
  width: 5px;
  border-radius: 999px;
  background: currentColor;
  opacity: .72;
}
.ve-audio-wave i:nth-child(1), .ve-audio-wave i:nth-child(5) { height: 13px; }
.ve-audio-wave i:nth-child(2), .ve-audio-wave i:nth-child(4) { height: 25px; }
.ve-audio-wave i:nth-child(3) { height: 34px; }
.ve-media-card-body {
  min-width: 0;
  min-height: 0;
  padding: 8px 7px;
  display: flex;
  flex-direction: column;
  gap: 3px;
  justify-content: center;
}
.ve-media-card-body strong {
  min-width: 0;
  color: #1c1917;
  font-size: 12px;
  line-height: 1.22;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-media-card-body span, .ve-media-card-body small {
  color: #78716c;
  font-size: 10px;
  font-weight: 750;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-media-card-body small {
  color: #436b65;
}
.ve-media-card-quick {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}
.ve-media-card-quick small {
  min-width: 0;
  flex: 1 1 auto;
}
.ve-media-preview-pill {
  flex: 0 0 auto;
  height: 22px;
  padding: 0 7px;
  border: 1px solid rgba(67, 107, 101, .26);
  border-radius: 999px;
  background: #f1f6f3;
  color: #436b65;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  font-size: 10px;
  font-weight: 900;
  line-height: 1;
  cursor: pointer;
}
.ve-media-preview-pill:hover:not(:disabled), .ve-media-preview-pill.is-active {
  border-color: #436b65;
  background: #436b65;
  color: #ffffff;
}
.ve-media-preview-pill:disabled {
  border-color: #dbe7e4;
  background: #fafaf9;
  color: #a8a29e;
  cursor: wait;
}
.ve-media-preview-pill .ve-audio-preview-spinner {
  width: 11px;
  height: 11px;
  border-width: 2px;
}
.ve-media-card-actions {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 5px;
  padding: 6px 6px 6px 0;
  min-height: 0;
}
.ve-media-action, .ve-media-link {
  min-width: 28px;
  height: 26px;
  border-radius: 7px;
  border: 1px solid var(--ve-border-soft);
  background: #ffffff;
  color: #436b65;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
  font-size: 11px;
  font-weight: 850;
  cursor: pointer;
}
.ve-media-action:hover:not(:disabled), .ve-media-action.is-active {
  border-color: #436b65;
  background: #436b65;
  color: #ffffff;
}
.ve-media-action:disabled {
  color: #a8a29e;
  cursor: not-allowed;
  background: #fafaf9;
}
.ve-audio-preview-spinner {
  width: 13px;
  height: 13px;
  border: 2px solid rgba(168, 162, 158, .35);
  border-top-color: currentColor;
  border-radius: 999px;
  animation: ve-audio-preview-spin .75s linear infinite;
}
@keyframes ve-audio-preview-spin {
  to {
    transform: rotate(360deg);
  }
}
.ve-media-link {
  padding: 0 9px;
}
.ve-media-picker {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
}
.ve-media-picker-dialog .manor-dialog-header {
  flex: 0 0 auto;
}
.ve-media-picker-dialog-body {
  display: flex;
  min-height: 0;
  overflow: hidden;
}
.ve-media-picker-dialog .manor-dialog-footer {
  flex: 0 0 auto;
}
.ve-picker-toolbar {
  display: grid;
  grid-template-columns: 210px minmax(220px, 1fr) 280px;
  gap: 10px;
  align-items: center;
}
.ve-picker-source-tabs {
  min-width: 0;
}
.ve-picker-search {
  min-width: 0;
}
.ve-picker-filters {
  min-width: 0;
}
.ve-picker-hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 10px;
  border: 1px solid #dbe7e4;
  border-radius: 10px;
  background: #fafaf9;
  color: #78716c;
  font-size: 12px;
}
.ve-picker-hint button {
  border: 0;
  background: transparent;
  color: #436b65;
  font-size: 12px;
  font-weight: 850;
  cursor: pointer;
  white-space: nowrap;
}
.ve-picker-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  flex: 1 1 auto;
  min-height: 0;
  max-height: none;
  overflow: auto;
  padding: 2px 2px 4px;
}
.ve-picker-grid .ve-bin-empty {
  grid-column: 1 / -1;
}
.ve-picker-grid .ve-media-card {
  height: 190px;
  grid-template-columns: 1fr;
  grid-template-rows: 82px minmax(65px, 1fr) 35px;
}
.ve-picker-grid .ve-media-thumb {
  height: 82px;
}
.ve-picker-grid .ve-media-card-actions {
  flex-direction: row;
  justify-content: flex-start;
  padding: 0 7px 7px;
}
.ve-picker-footer {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}
.ve-picker-footer span {
  margin-right: auto;
  color: #78716c;
  font-size: 12px;
  font-weight: 800;
}
.ve-bin-empty {
  padding: 9px 10px;
  border: 1px dashed #d6d3d1;
  border-radius: 8px;
  background: #fafaf9;
  color: #78716c;
  font-size: 12px;
  line-height: 1.35;
}
.ve-render-status {
  margin-top: 10px;
  padding: 0;
  border: 1px solid var(--ve-border-soft);
  border-radius: 12px;
  background: #ffffff;
  overflow: hidden;
}
.ve-render-status summary {
  min-height: 38px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  cursor: pointer;
  list-style: none;
}
.ve-render-status summary::-webkit-details-marker {
  display: none;
}
.ve-render-status summary::after {
  content: "";
  width: 7px;
  height: 7px;
  border-right: 2px solid #a8a29e;
  border-bottom: 2px solid #a8a29e;
  transform: rotate(45deg);
  margin-left: auto;
  transition: transform .12s ease;
}
.ve-render-status[open] summary::after {
  transform: rotate(225deg);
}
.ve-render-status strong {
  font-size: 12px;
  color: #436b65;
}
.ve-render-status.has-warnings strong {
  color: #936027;
}
.ve-render-status.has-blockers strong {
  color: #a23e38;
}
.ve-render-status summary span {
  font-size: 12px;
  color: #78716c;
  white-space: nowrap;
}
.ve-render-status .ve-help-anchor {
  margin-left: 2px;
}
.ve-render-issue-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 0 8px 8px;
}
.ve-render-issue {
  padding: 7px 8px;
  border-radius: 7px;
  background: #fafaf9;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-render-issue b {
  font-size: 11px;
  color: #44403c;
}
.ve-render-issue small {
  color: #78716c;
  font-size: 11px;
  line-height: 1.25;
}
.ve-render-issue.is-warning {
  background: #faf7ef;
}
.ve-render-issue.is-blocker {
  background: #fff1f2;
}
.ve-render-issue.is-info {
  background: #f3f6fa;
}
.ve-track-action, .ve-button, .ve-link-button {
  min-height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: #44403c;
  font-size: 12px;
  font-weight: 800;
  cursor: pointer;
  text-decoration: none;
  transition: background .12s ease, border-color .12s ease, color .12s ease, box-shadow .12s ease;
}
.ve-track-action {
  width: 100%;
  justify-content: flex-start;
  padding: 6px 9px;
  background: #fafaf9;
  border-color: var(--ve-border-soft);
}
.ve-track-action:hover:not(:disabled), .ve-button:hover:not(:disabled), .ve-link-button:hover {
  background: #fafaf9;
  border-color: #d6d3d1;
}
.ve-track-action:disabled {
  opacity: .55;
  cursor: not-allowed;
}
.ve-button, .ve-link-button {
  padding: 6px 11px;
  border-color: var(--ve-border-soft);
  background: #ffffff;
}
.ve-action-group {
  margin-top: 8px;
  padding: 8px;
  border: 1px solid #dbe7e4;
  border-radius: 10px;
  background: #fafaf9;
}
.ve-action-group-title {
  margin-bottom: 7px;
  color: #78716c;
  font-size: 10px;
  font-weight: 900;
  letter-spacing: 0;
  text-transform: uppercase;
}
.ve-action-row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.ve-action-row .ve-track-action {
  min-height: 32px;
  justify-content: center;
  padding: 6px 8px;
  font-size: 12px;
}
.ve-action-row .ve-track-action.is-wide {
  grid-column: 1 / -1;
}
.ve-quick-actions-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.ve-quick-actions-grid .ve-track-action {
  min-height: 32px;
  justify-content: center;
  padding: 6px 8px;
  font-size: 12px;
}
.ve-quick-actions-grid .ve-track-action.is-wide {
  grid-column: 1 / -1;
}
.ve-more-actions {
  margin-top: 6px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 12px;
  background: #ffffff;
  overflow: hidden;
}
.ve-more-actions summary {
  min-height: 32px;
  padding: 0 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  color: #44403c;
  font-size: 12px;
  font-weight: 850;
  cursor: pointer;
  list-style: none;
}
.ve-more-actions summary::-webkit-details-marker {
  display: none;
}
.ve-more-actions summary::after {
  content: "";
  width: 7px;
  height: 7px;
  border-right: 2px solid #a8a29e;
  border-bottom: 2px solid #a8a29e;
  transform: rotate(45deg);
  margin-left: auto;
  transition: transform .12s ease;
}
.ve-more-actions[open] summary {
  border-bottom: 1px solid var(--ve-border-soft);
}
.ve-more-actions[open] summary::after {
  transform: rotate(225deg);
}
.ve-more-actions-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
  padding: 8px;
  background: #fafaf9;
}
.ve-more-actions-grid .ve-track-action {
  min-height: 31px;
  justify-content: center;
  padding: 6px 8px;
  font-size: 12px;
}
.ve-more-actions-grid .ve-track-action.is-wide {
  grid-column: 1 / -1;
}
.ve-button:disabled {
  opacity: .55;
  cursor: not-allowed;
}
.ve-button-primary {
  border-color: #436b65;
  background: #436b65;
  color: #ffffff;
}
.ve-icon-button, .ve-round-button {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: #44403c;
  cursor: pointer;
  transition: background .12s ease, border-color .12s ease, color .12s ease;
}
.ve-icon-button:hover:not(:disabled), .ve-round-button:hover:not(:disabled) {
  border-color: var(--ve-border-soft);
  background: #ffffff;
}
.ve-icon-button:disabled, .ve-round-button:disabled {
  opacity: .45;
  cursor: not-allowed;
}
.ve-round-button {
  border-radius: 999px;
}
.ve-danger {
  color: #a23e38;
}
	.ve-center {
	  display: flex;
	  flex-direction: column;
	  gap: 10px;
	  min-width: 0;
	  min-height: 0;
	  overflow: hidden;
	}
.ve-preview {
  flex: 1;
  min-height: 0;
  background: #020617;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border-color: #1c1917;
}
.ve-preview-help {
  position: absolute;
  top: 12px;
  right: 12px;
  z-index: 5;
  border-radius: 999px;
  background: rgba(28,25,23,.72);
  box-shadow: 0 8px 20px rgba(2,6,23,.22);
}
.ve-preview-help .ve-help-anchor button {
  color: #e7e5e4;
}
.ve-ai-edit-notice {
  position: absolute;
  left: 14px;
  top: 14px;
  z-index: 7;
  display: inline-flex;
  align-items: center;
  gap: 9px;
  max-width: min(440px, calc(100% - 88px));
  padding: 8px 10px;
  border: 1px solid rgba(125,211,252,.38);
  border-radius: 10px;
  background: rgba(8,13,29,.78);
  color: #fafaf9;
  box-shadow: 0 12px 28px rgba(2,6,23,.24), 0 0 0 1px rgba(255,255,255,.06) inset;
  backdrop-filter: blur(14px);
  pointer-events: none;
}
.ve-ai-edit-notice svg {
  flex: 0 0 auto;
  color: #67e8f9;
  filter: drop-shadow(0 0 8px rgba(103,232,249,.48));
}
.ve-ai-edit-notice div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-ai-edit-notice strong {
  font-size: 12px;
  line-height: 1.1;
}
.ve-ai-edit-notice span {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #bae6fd;
  font-size: 11px;
  font-weight: 750;
}
.ve-preview-video {
  width: 100%;
  height: 100%;
  max-height: 62vh;
  object-fit: contain;
  display: block;
}
.ve-caption-overlay {
  position: absolute;
  max-width: 78%;
  padding: 8px 14px;
  border-radius: 8px;
  font-weight: 800;
  line-height: 1.18;
  white-space: pre-wrap;
  pointer-events: auto;
  display: flex;
  flex-direction: column;
  gap: 3px;
  cursor: grab;
  touch-action: none;
  user-select: none;
}
.ve-caption-overlay.is-selected {
  outline: 2px solid rgba(95,146,138,.78);
  outline-offset: 3px;
}
.ve-caption-overlay.is-ai-highlighted {
  box-shadow: 0 0 0 2px rgba(125,211,252,.72), 0 0 24px rgba(56,189,248,.42);
}
.ve-caption-overlay strong {
  font-size: .72em;
  color: inherit;
  opacity: .72;
}
.ve-caption-speechBubble {
  border: 1px solid rgba(28,25,23,.16);
  box-shadow: 0 10px 24px rgba(28,25,23,.18);
}
.ve-caption-narrationBox {
  border: 1px solid rgba(250,250,249,.22);
  box-shadow: 0 10px 24px rgba(28,25,23,.24);
}
.ve-shot-overlay {
  position: absolute;
  left: 14px;
  top: 62px;
  max-width: 42%;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(28,25,23,.78);
  color: #fafaf9;
  pointer-events: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
  box-shadow: 0 10px 24px rgba(28,25,23,.24);
}
.ve-shot-overlay strong {
  font-size: 11px;
}
.ve-shot-overlay span {
  font-size: 12px;
  color: #d6d3d1;
}
.ve-preview-audio-chip {
  position: absolute;
  left: 14px;
  bottom: 14px;
  z-index: 6;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: min(420px, calc(100% - 28px));
  padding: 7px 10px;
  border: 1px solid rgba(130,173,164,.34);
  border-radius: 999px;
  background: rgba(8,13,29,.76);
  color: #dffcf8;
  box-shadow: 0 10px 22px rgba(2,6,23,.22);
  backdrop-filter: blur(12px);
  pointer-events: none;
}
.ve-preview-audio-chip span:not(.ve-preview-audio-meter) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 850;
}
.ve-preview-audio-chip small {
  flex: 0 0 auto;
  color: #ccded9;
  font-size: 10px;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 0;
}
.ve-preview-audio-chip.is-ai-highlighted {
  border-color: rgba(125,211,252,.7);
  box-shadow: 0 0 0 2px rgba(56,189,248,.2), 0 0 28px rgba(130,173,164,.28);
}
.ve-preview-audio-meter {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 999px;
  background: #82ada4;
  box-shadow: 0 0 0 5px rgba(130,173,164,.14), 0 0 16px rgba(130,173,164,.6);
  animation: veAudioMeterPulse 1.1s ease-in-out infinite;
}
	.ve-transport {
	  min-height: 46px;
	  display: flex;
	  align-items: center;
	  gap: 8px;
	  flex-wrap: wrap;
	  padding: 7px 9px;
	  border: 1px solid var(--ve-border-soft);
	  border-radius: 12px;
	  background: #ffffff;
	  overflow: hidden;
	}
.ve-timecode {
  color: #44403c;
  font-size: 12px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
	.ve-workarea-controls {
	  display: flex;
	  align-items: center;
	  gap: 6px;
	  flex: 0 1 300px;
	  min-width: 0;
	  flex-wrap: wrap;
	}
.ve-workarea-controls > span {
  color: #78716c;
  font-size: 11px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
	.ve-scrubber {
	  flex: 1 1 220px;
	  min-width: 160px;
	  accent-color: #436b65;
	}
.ve-project-overview {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.ve-overview-status {
  padding: 10px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 10px;
  background: #fafaf9;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.ve-overview-status strong {
  font-size: 13px;
  color: #436b65;
}
.ve-overview-status.has-warnings strong {
  color: #936027;
}
.ve-overview-status.has-blockers strong {
  color: #a23e38;
}
.ve-overview-status span {
  color: #78716c;
  font-size: 12px;
}
.ve-overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.ve-overview-grid span {
  min-height: 58px;
  padding: 9px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 10px;
  background: #ffffff;
  color: #78716c;
  font-size: 11px;
  font-weight: 850;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
}
.ve-overview-grid b {
  color: #1c1917;
  font-size: 20px;
  line-height: 1;
}
.ve-asset-pill {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-radius: 8px;
  background: #f1f6f3;
  border: 1px solid #bbf7d0;
  min-width: 0;
}
.ve-asset-pill strong {
  font-size: 11px;
  color: #3f7361;
}
.ve-asset-pill span {
  font-size: 12px;
  color: #1c1917;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-inspector-stack {
  display: flex;
  flex-direction: column;
  gap: 11px;
}
.ve-inspector-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ve-clip-actions {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
  gap: 8px;
}
.ve-clip-actions .ve-track-action {
  min-height: 34px;
  justify-content: center;
}
.ve-two-col {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 10px;
}
.ve-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}
.ve-field-label {
  font-size: 11px;
  font-weight: 800;
  color: #78716c;
}
.ve-input, .ve-textarea, .ve-color {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--ve-border-soft);
  border-radius: 8px;
  background: #ffffff;
  color: #1c1917;
  font-size: 13px;
  transition: border-color .12s ease, box-shadow .12s ease;
}
.ve-input {
  height: 34px;
  padding: 0 9px;
}
.ve-input:focus, .ve-textarea:focus {
  outline: none;
  border-color: #436b65;
  box-shadow: 0 0 0 3px rgba(67,107,101,.08);
}
.ve-textarea {
  padding: 9px;
  resize: vertical;
}
.ve-color {
  height: 34px;
  padding: 3px;
}
.ve-check {
  display: flex;
  gap: 8px;
  align-items: center;
  color: #44403c;
  font-size: 13px;
}
.ve-timeline {
  flex: 0 0 clamp(290px, 38vh, 330px);
  min-height: 0;
  position: relative;
  padding: 10px 12px 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.ve-timeline-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.ve-timeline-header div:first-child {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ve-timeline-header strong {
  font-size: 13px;
}
.ve-timeline-header span {
  font-size: 12px;
  color: #78716c;
  font-variant-numeric: tabular-nums;
}
.ve-timeline-summary {
  flex: 1 1 280px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.ve-timeline-summary span {
  min-height: 26px;
  padding: 5px 8px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 999px;
  background: #fafaf9;
  color: #78716c;
  font-size: 11px;
  font-weight: 850;
}
.ve-timeline-tools {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex: 1 1 420px;
  flex-wrap: wrap;
}
.ve-zoom-control {
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 8px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 8px;
  background: #ffffff;
}
.ve-zoom-control span {
  font-size: 12px;
  color: #78716c;
  font-weight: 800;
}
.ve-zoom-slider {
  width: 112px;
  accent-color: #436b65;
}
.ve-compact-check {
  min-height: 30px;
  padding: 0 8px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 8px;
  background: #ffffff;
}
.ve-small-select {
  width: 86px;
  height: 30px;
  padding: 0 7px;
}
.ve-mini-button {
  width: 30px;
  height: 30px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: #1c1917;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.ve-mini-button:hover:not(:disabled) {
  border-color: var(--ve-border-soft);
  background: #ffffff;
}
.ve-mini-button:disabled {
  opacity: .45;
  cursor: not-allowed;
}
.ve-mini-text-button {
  height: 22px;
  border: 0;
  border-radius: 6px;
  background: #f1f6f3;
  color: #436b65;
  font-size: 11px;
  font-weight: 850;
  cursor: pointer;
  padding: 0 8px;
}
.ve-mini-text-button:disabled {
  opacity: .45;
  cursor: not-allowed;
}
.ve-mini-link-button {
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  background: #f3f6fa;
  color: #4869ac;
  font-size: 11px;
  font-weight: 850;
  text-decoration: none;
  padding: 0 8px;
}
.ve-timeline-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  padding-bottom: 6px;
  scrollbar-gutter: stable;
}
.ve-timeline-content {
  position: relative;
  min-width: 100%;
  isolation: isolate;
}
.ve-time-ruler {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: 132px var(--ve-lane-width, 1fr);
  gap: 12px;
  align-items: stretch;
  min-height: 32px;
}
.ve-time-ruler-label {
  display: flex;
  align-items: center;
  padding-left: 6px;
  color: #78716c;
  font-size: 11px;
  font-weight: 850;
}
.ve-time-ruler-lane {
  position: relative;
  min-height: 32px;
  border-bottom: 1px solid #d6d3d1;
  background-image: linear-gradient(90deg, rgba(168,162,158,0.26) 1px, transparent 1px);
  background-size: var(--ve-second-width, 48px) 100%;
  cursor: crosshair;
  touch-action: none;
  user-select: none;
}
.ve-ruler-tick {
  position: absolute;
  top: 13px;
  bottom: 0;
  width: 1px;
  background: #d6d3d1;
  pointer-events: none;
}
.ve-ruler-tick.is-major {
  top: 7px;
  background: #a8a29e;
}
.ve-ruler-tick b {
  position: absolute;
  top: -7px;
  left: 5px;
  color: #78716c;
  font-size: 10px;
  font-weight: 850;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ve-workarea-overlay {
  position: absolute;
  top: 0;
  bottom: 6px;
  border-left: 2px solid rgba(95,146,138,.72);
  border-right: 2px solid rgba(95,146,138,.72);
  background: rgba(95,146,138,.08);
  pointer-events: none;
  z-index: 0;
}
.ve-track {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 132px var(--ve-lane-width, 1fr);
  gap: 12px;
  align-items: stretch;
  min-height: 48px;
  margin-top: 8px;
}
.ve-track-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding-left: 6px;
  color: #57534e;
  font-size: 12px;
  font-weight: 850;
  min-width: 0;
}
.ve-track-label-name {
  min-width: 0;
  flex: 1 1 auto;
}
.ve-track-label-name > span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-track-controls {
  display: flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}
.ve-track-toggle {
  width: 22px;
  height: 22px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #d6d3d1;
  border-radius: 6px;
  background: #ffffff;
  color: #a8a29e;
  cursor: pointer;
  font-size: 10px;
  font-weight: 900;
  padding: 0;
}
.ve-track-toggle.is-active {
  color: #436b65;
  border-color: #ccded9;
  background: #f2f6f5;
}
.ve-track-toggle.is-locked {
  color: #a23e38;
  border-color: #ecc8c5;
  background: #fff1f2;
}
.ve-track-lane {
  position: relative;
  min-height: 48px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 8px;
  background-color: #fbfdff;
  background-image: linear-gradient(90deg, rgba(168,162,158,0.22) 1px, transparent 1px);
  background-size: var(--ve-second-width, 48px) 100%;
  overflow: hidden;
}
.ve-marker-track {
  min-height: 38px;
}
.ve-marker-lane {
  min-height: 38px;
}
.ve-marker-empty {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: #a8a29e;
  font-size: 12px;
  font-weight: 700;
  pointer-events: none;
}
.ve-video-lane-empty {
  position: absolute;
  left: 12px;
  top: 50%;
  transform: translateY(-50%);
  color: #a8a29e;
  font-size: 12px;
  font-weight: 800;
  pointer-events: none;
}
.ve-marker-pin {
  position: absolute;
  top: 5px;
  max-width: 180px;
  height: 28px;
  transform: translateX(-9px);
  border: 1px solid currentColor;
  border-radius: 8px;
  background: rgba(255,255,255,.94);
  color: #cf9b44;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 8px 0 7px;
  font-size: 12px;
  font-weight: 850;
  cursor: grab;
  touch-action: none;
  overflow: hidden;
  box-shadow: 0 6px 14px rgba(28,25,23,.08);
}
.ve-marker-pin span:last-child {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-marker-pin:disabled {
  cursor: not-allowed;
  opacity: .58;
}
.ve-marker-diamond {
  width: 9px;
  height: 9px;
  flex: 0 0 auto;
  transform: rotate(45deg);
  border-radius: 2px;
  background: currentColor;
}
.ve-track-lane.is-locked-track {
  background-color: rgba(245,245,244,.7);
}
.is-hidden-track {
  opacity: .38;
}
.is-muted-track {
  filter: saturate(.72);
}
.ve-clip-block, .ve-shot-block, .ve-caption-block, .ve-audio-block {
  border: 0;
  color: #ffffff;
  font-weight: 800;
  cursor: pointer;
  min-width: 0;
  overflow: hidden;
}
.ve-clip-block:disabled, .ve-shot-block:disabled, .ve-caption-block:disabled, .ve-audio-block:disabled {
  cursor: not-allowed;
}
.ve-clip-block {
  position: absolute;
  top: 6px;
  height: 34px;
  border-radius: 7px;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: flex-start;
  padding: 4px 14px 4px 31px;
  text-align: left;
  cursor: grab;
  touch-action: none;
  user-select: none;
  transition: transform .12s ease, box-shadow .12s ease, filter .12s ease;
}
.ve-clip-block:active {
  cursor: grabbing;
}
.ve-clip-block.is-dragging {
  z-index: 5;
  transform: translateY(-2px);
  filter: saturate(1.08);
  box-shadow: 0 11px 22px rgba(28,25,23,.22), inset 0 0 0 2px rgba(255,255,255,.78);
}
.ve-clip-block.is-selected {
  box-shadow: 0 0 0 2px rgba(95,146,138,.4), inset 0 0 0 2px rgba(255,255,255,.6);
}
.ve-clip-block > span:not(.ve-resize-handle), .ve-clip-block small {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-clip-block small {
  opacity: .82;
  font-size: 11px;
}
.ve-clip-block.is-manual {
  box-shadow: inset 0 0 0 2px rgba(255,255,255,.72), inset 0 -4px 0 rgba(28,25,23,.24);
}
.ve-clip-block.is-manual.is-selected {
  box-shadow: 0 0 0 2px rgba(95,146,138,.42), inset 0 0 0 2px rgba(255,255,255,.72), inset 0 -4px 0 rgba(28,25,23,.24);
}
.ve-clip-block.is-dragging,
.ve-clip-block.is-manual.is-dragging {
  box-shadow: 0 11px 22px rgba(28,25,23,.22), inset 0 0 0 2px rgba(255,255,255,.78);
}
.ve-clip-grip {
  position: absolute;
  left: 15px;
  top: 50%;
  transform: translateY(-50%);
  width: 13px;
  height: 18px;
  color: rgba(255,255,255,.84);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}
.ve-clip-drop-indicator {
  position: absolute;
  top: 2px;
  bottom: 2px;
  z-index: 7;
  width: 0;
  pointer-events: none;
}
.ve-clip-drop-indicator::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: -1px;
  width: 2px;
  border-radius: 999px;
  background: #5f928a;
  box-shadow: 0 0 0 2px rgba(255,255,255,.86), 0 0 0 5px rgba(95,146,138,.18);
}
.ve-clip-drop-indicator span {
  position: absolute;
  left: 8px;
  top: 3px;
  padding: 2px 6px;
  border-radius: 6px;
  background: #436b65;
  color: #ffffff;
  font-size: 10px;
  font-weight: 900;
  white-space: nowrap;
  box-shadow: 0 6px 14px rgba(28,25,23,.18);
}
.ve-clip-block .ve-resize-handle {
  opacity: .82;
}
.ve-shot-block, .ve-caption-block, .ve-audio-block {
  position: absolute;
  top: 6px;
  height: 34px;
  border-radius: 7px;
  padding: 0 12px;
  text-align: left;
  white-space: nowrap;
  text-overflow: ellipsis;
  cursor: grab;
  touch-action: none;
  display: flex;
  align-items: center;
}
.ve-block-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-resize-handle {
  position: absolute;
  top: 3px;
  bottom: 3px;
  width: 11px;
  border-radius: 5px;
  background: rgba(255,255,255,.34);
  cursor: ew-resize;
  z-index: 3;
  touch-action: none;
}
.ve-resize-handle::after {
  content: "";
  position: absolute;
  top: 7px;
  bottom: 7px;
  left: 50%;
  width: 2px;
  transform: translateX(-50%);
  border-radius: 999px;
  background: rgba(255,255,255,.82);
}
.ve-resize-handle:hover {
  background: rgba(255,255,255,.52);
}
.ve-resize-start {
  left: 2px;
}
.ve-resize-end {
  right: 2px;
}
.ve-caption-block {
  background: #1c1917;
}
.ve-shot-block {
  background: #57534e;
}
.ve-audio-block {
  background: #4f7e87;
}
.ve-clip-block.is-ai-highlighted,
.ve-shot-block.is-ai-highlighted,
.ve-caption-block.is-ai-highlighted,
.ve-audio-block.is-ai-highlighted,
.ve-marker-pin.is-ai-highlighted {
  box-shadow: 0 0 0 2px rgba(125,211,252,.62), 0 0 24px rgba(56,189,248,.38), inset 0 0 0 1px rgba(255,255,255,.58);
  animation: veAiTimelineGlow 1.8s ease-in-out infinite;
}
.ve-marker-pin.is-ai-highlighted {
  border-color: #38bdf8;
}
.is-selected {
  outline: 3px solid rgba(95,146,138,.34);
  outline-offset: -3px;
}
.ve-playhead {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 28px;
  background: transparent;
  z-index: 1000;
  pointer-events: auto;
  cursor: ew-resize;
  touch-action: none;
  user-select: none;
}
.ve-playhead::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: var(--ve-playhead-line-offset, 14px);
  width: 2px;
  transform: translateX(-1px);
  border-radius: 999px;
  background: #d65f59;
  box-shadow: 0 0 0 1px rgba(255,255,255,.9), 0 0 0 4px rgba(214,95,89,.1);
  pointer-events: none;
}
.ve-playhead::after {
  content: "";
  position: absolute;
  top: 0;
  left: var(--ve-playhead-line-offset, 14px);
  width: 10px;
  height: 10px;
  transform: translate(-50%, -35%);
  border-radius: 999px;
  background: #d65f59;
  box-shadow: 0 0 0 2px rgba(255,255,255,.92), 0 6px 12px rgba(28,25,23,.18);
  pointer-events: none;
}
.ve-playhead:hover::before,
.ve-playhead:focus-visible::before {
  width: 3px;
  box-shadow: 0 0 0 1px rgba(255,255,255,.95), 0 0 0 5px rgba(214,95,89,.14);
}
.ve-playhead:focus-visible {
  outline: none;
}
@keyframes veAiTimelineGlow {
  0%, 100% {
    filter: saturate(1);
  }
  50% {
    filter: saturate(1.18) brightness(1.05);
  }
}
@keyframes veAudioMeterPulse {
  0%, 100% {
    transform: scale(.78);
    opacity: .72;
  }
  50% {
    transform: scale(1.12);
    opacity: 1;
  }
}
@media (max-width: 1180px) {
  .ve-workspace {
    grid-template-columns: 210px minmax(320px, 1fr);
  }
  .ve-inspector {
    grid-column: 1 / -1;
  }
}
@media (max-width: 760px) {
  .ve-shell {
    padding: 12px;
    overflow: auto;
  }
  .ve-topbar {
    align-items: flex-start;
    flex-direction: column;
  }
  .ve-title-block h1 {
    max-width: 82vw;
  }
  .ve-workspace {
    grid-template-columns: 1fr;
    flex: 0 0 auto;
    min-height: auto;
  }
  .ve-timeline {
    flex-basis: 320px;
  }
  .ve-transport {
    align-items: flex-start;
  }
  .ve-timecode {
    flex: 1 1 120px;
  }
  .ve-workarea-controls {
    flex-basis: 100%;
  }
  .ve-scrubber {
    flex-basis: 100%;
  }
  .ve-track {
    grid-template-columns: 132px var(--ve-lane-width, 1fr);
  }
  .ve-track-toggle {
    width: 20px;
    height: 20px;
  }
  .ve-timeline-summary {
    flex-basis: 100%;
  }
  .ve-picker-toolbar {
    grid-template-columns: 1fr;
  }
  .ve-picker-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    max-height: 55vh;
  }
  .ve-picker-hint,
  .ve-picker-footer {
    align-items: stretch;
    flex-direction: column;
  }
  .ve-picker-footer span {
    margin-right: 0;
  }
}
`;
