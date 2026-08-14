import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  CSSProperties,
  ChangeEvent,
  DragEvent as ReactDragEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import "@fontsource-variable/manrope";
import "@fontsource-variable/space-grotesk";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { FreeMusicTrack } from "../lib/api";
import { invalidateKnowledgeQueries } from "../lib/knowledgeInvalidation";
import { t } from "../lib/i18n";
import { formatFileSize } from "../lib/format";
import type { Document, DocumentFolderInfo } from "../lib/types";
import MediaInsertDialog from "../components/MediaInsertDialog";
import type { InsertableMediaAsset } from "../lib/mediaInsertion";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import InfoPopover from "../components/ui/InfoPopover";
import Modal from "../components/ui/Modal";
import Select from "../components/ui/Select";
import AiEditButton from "../components/ui/AiEditButton";
import { PageHeaderSubtitle, PageHeaderTitle } from "../components/ui/PageHeader";
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
  IconExternalLink,
  IconEye,
  IconEyeOff,
  IconFolder,
  IconGrid4,
  IconHelp,
  IconKey,
  IconLock,
  IconMusicNote,
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
import { getPlayableMediaDuration, requestMediaDurationProbe } from "../lib/mediaDuration";
import { captionAnchorTransform, containedMediaSize, fittedMediaSize } from "../lib/videoEditorGeometry";
import {
  MAX_VIDEO_CLIP_FADE_SECONDS,
  MAX_VIDEO_CLIP_SPEED,
  MIN_VIDEO_CLIP_SPEED,
  normalizeVideoClipFade,
  normalizeVideoClipSpeed,
  videoClipEdgeFadeOpacity,
  videoClipSourceTimeAtOffset,
  videoClipTimelineDuration,
  videoClipTimelineOffsetAtSourceTime,
  videoOverlaySourceTimeAtTimelineTime,
  videoOverlaySourceWindow,
} from "../lib/videoEditorTimeline";
import {
  MOTION_EASINGS,
  VIDEO_EDITOR_FPS,
  createMotionPresetKeyframes,
  motionEasingProgress,
  motionFrameNumberAtTime,
  motionPathSamples,
  motionPoseAtTime,
  normalizeMotionBezier,
  normalizeMotionKeyframes,
  retimeMotionKeyframe,
  snapMotionTimeToFrame,
  upsertMotionKeyframe,
} from "../lib/videoEditorMotion";
import type {
  MotionEasing,
  MotionKeyframe,
  MotionPathMode,
  MotionPose,
  MotionPreset,
} from "../lib/videoEditorMotion";
import {
  MOTION_DESIGN_PRESETS,
  createMotionDesignComposition,
} from "../lib/videoEditorMotionDesign";
import type { MotionDesignRequest } from "../lib/videoEditorMotionDesign";
import {
  captionFontStack,
  graphicCssClipPath,
  graphicCssFilter,
  graphicCssFill,
  graphicCssShadow,
  graphicEffectOverlayBackground,
  layerZIndex,
  normalizeCaptionVisualStyle,
  normalizeGraphicVisualStyle,
  revealedCaptionText,
  transformCaptionText,
} from "../lib/videoEditorVisualStyle";
import type { CaptionVisualStyle, GraphicVisualStyle } from "../lib/videoEditorVisualStyle";
import {
  PARTICLE_MOTIONS,
  PARTICLE_SHAPES,
  drawParticleLayer,
  normalizeParticleLayer,
} from "../lib/videoEditorParticles";
import type { ParticleMotion, ParticleShape } from "../lib/videoEditorParticles";
import {
  DEFAULT_VIDEO_EDITOR_SHADER_STYLE,
  VIDEO_EDITOR_SHADER_PRESETS,
  createVideoEditorShaderSurface,
  disposeVideoEditorShaderSurface,
  normalizeVideoEditorShaderStyle,
  renderVideoEditorShaderFrame,
  resizeVideoEditorShaderSurface,
} from "../lib/videoEditorShader";
import type { VideoEditorShaderStyle, VideoEditorShaderSurface } from "../lib/videoEditorShader";

type ClipSegment = {
  id: string;
  label: string;
  sourceStart: number;
  sourceEnd: number;
  speed: number;
  fadeIn: number;
  fadeOut: number;
  muted: boolean;
  color: string;
  fit: "contain" | "cover";
  x: number;
  y: number;
  scale: number;
  scaleX?: number;
  scaleY?: number;
  rotationX?: number;
  rotationY?: number;
  perspective?: number;
  blur?: number;
  rotation: number;
  opacity: number;
  keyframes: MotionKeyframe[];
  assetDocumentId?: string | null;
  assetName?: string | null;
  assetMimeType?: string | null;
  assetDuration?: number | null;
  replacementPrompt?: string | null;
  editNotes?: string | null;
};

type CaptionCue = Partial<CaptionVisualStyle> & {
  id: string;
  speaker?: string | null;
  emotion?: string | null;
  style: "subtitle" | "speechBubble" | "narrationBox" | "titleCard" | "lowerThird";
  text: string;
  start: number;
  end: number;
  x: number;
  y: number;
  scale: number;
  scaleX?: number;
  scaleY?: number;
  rotationX?: number;
  rotationY?: number;
  perspective?: number;
  blur?: number;
  rotation: number;
  opacity: number;
  keyframes: MotionKeyframe[];
  size: number;
  color: string;
  background: string;
  backgroundColor: string;
  backgroundOpacity: number;
  align: CanvasTextAlign;
  parentId?: string | null;
};

type GraphicLayerKind = "group" | "rectangle" | "ellipse" | "line" | "path" | "particle" | "shader" | "image" | "video";

type GraphicLayer = Partial<GraphicVisualStyle> & Partial<VideoEditorShaderStyle> & {
  id: string;
  kind: GraphicLayerKind;
  label: string;
  start: number;
  end: number;
  x: number;
  y: number;
  scale: number;
  scaleX?: number;
  scaleY?: number;
  rotationX?: number;
  rotationY?: number;
  perspective?: number;
  /** Path-only base reveal amount. Motion keyframes may override it seek-safely. */
  pathProgress?: number;
  rotation: number;
  opacity: number;
  keyframes: MotionKeyframe[];
  width: number;
  height: number;
  fill: string;
  stroke: string;
  strokeWidth: number;
  cornerRadius: number;
  assetDocumentId?: string | null;
  assetName?: string | null;
  assetMimeType?: string | null;
  assetDuration?: number | null;
  sourceStart?: number;
  sourceEnd?: number | null;
  speed?: number;
  loop?: boolean;
  particleCount?: number;
  particleShape?: ParticleShape;
  particleMotion?: ParticleMotion;
  particleSeed?: number;
  particleSize?: number;
  particleSpeed?: number;
  particleGravity?: number;
  particleSpread?: number;
  particleLoop?: boolean;
  /** Parent group. Child coordinates and dimensions are percentages of the group bounds. */
  parentId?: string | null;
  /** Group-only: crop descendants to this group's animated bounds. */
  clipChildren?: boolean;
  /** Group-only: keep this group off-canvas and expose it as a reusable subscene. */
  isTemplate?: boolean;
  /** Group-only: render the descendants of another group with this group's timing and transform. */
  instanceOf?: string | null;
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
  x: number;
  y: number;
  scale: number;
  scaleX?: number;
  scaleY?: number;
  rotation: number;
  rotationX?: number;
  rotationY?: number;
  perspective?: number;
  blur?: number;
  opacity: number;
  keyframes: MotionKeyframe[];
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
  sourceStart?: number;
  sourceEnd?: number | null;
  assetDuration?: number | null;
  assetDocumentId?: string | null;
  assetName?: string | null;
  assetMimeType?: string | null;
  catalogTrackId?: string | null;
  musicCreator?: string | null;
  musicLicense?: string | null;
  musicLicenseName?: string | null;
  musicLicenseUrl?: string | null;
  musicSourceUrl?: string | null;
  musicAttribution?: string | null;
  sourcePlan: string;
  prompt: string;
};

type FreeMusicPreviewProgress = {
  trackId: string;
  currentTime: number;
  duration: number;
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
  | { type: "graphic"; id: string }
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
  graphicLayers: GraphicLayer[];
  audioCues: AudioCue[];
  markers: TimelineMarker[];
};

type EditorHistoryTransaction = {
  before: EditorTrackState;
  closing: boolean;
};

type TimelineTrackId = "markers" | "shots" | "video" | "graphics" | "captions" | "audio";

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
  graphicLayers: GraphicLayer[];
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
    graphics?: Partial<GraphicLayer>[];
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
    playback_rate: number;
    visual?: {
      fit: ClipSegment["fit"];
      x: number;
      y: number;
      scale: number;
      rotation: number;
      opacity: number;
      fade_in: number;
      fade_out: number;
      keyframes: MotionKeyframe[];
    };
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
  motion_design?: MotionDesignRequest;
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

type TimelineTool = "select" | "razor";

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

const FREE_MUSIC_SEARCH_PRESETS = [
  { key: "cinematic", query: "cinematic" },
  { key: "technology", query: "technology" },
  { key: "ambient", query: "ambient atmospheric" },
  { key: "upbeat", query: "upbeat energetic" },
  { key: "emotional", query: "emotional piano" },
] as const;
const FREE_MUSIC_PREVIEW_RING_CIRCUMFERENCE = 2 * Math.PI * 17;
const DEFAULT_TIMELINE_TRACK_STATES: Record<TimelineTrackId, TimelineTrackState> = {
  markers: { locked: false, muted: false, visible: true },
  shots: { locked: false, muted: false, visible: true },
  video: { locked: false, muted: false, visible: true },
  graphics: { locked: false, muted: false, visible: true },
  captions: { locked: false, muted: false, visible: true },
  audio: { locked: false, muted: false, visible: true },
};
const CAPTION_STYLE_LABELS: Record<CaptionCue["style"], string> = {
  subtitle: "Subtitle",
  speechBubble: "Speech bubble",
  narrationBox: "Narration box",
  titleCard: "Title card",
  lowerThird: "Lower third",
};
const GRAPHIC_KIND_LABELS: Record<GraphicLayerKind, string> = {
  group: "Group",
  rectangle: "Rectangle",
  ellipse: "Ellipse",
  line: "Line",
  path: "Vector path",
  particle: "Particles",
  shader: "WebGL shader",
  image: "Image",
  video: "Video overlay",
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
const timelineFilmstripCache = new Map<string, string[]>();
const timelineFilmstripFailureCache = new Set<string>();
const timelineFilmstripInflight = new Map<string, Promise<string[]>>();
const timelineAudioWaveformCache = new Map<string, { duration: number; peaks: number[] }>();
const graphicShaderRenderCache = new Map<string, VideoEditorShaderSurface>();
const timelineAudioWaveformFailureCache = new Set<string>();
const timelineAudioWaveformInflight = new Map<string, Promise<{ duration: number; peaks: number[] }>>();
const graphicImageUrlCache = new Map<string, string>();
const graphicImageElementCache = new Map<string, HTMLImageElement>();
const graphicImageFailureCache = new Set<string>();
const graphicImageInflight = new Map<string, Promise<HTMLImageElement>>();
const graphicVideoElementCache = new Map<string, HTMLVideoElement>();
const graphicVideoFailureCache = new Set<string>();
const graphicVideoInflight = new Map<string, Promise<HTMLVideoElement>>();
const graphicVideoRenderElementCache = new Map<string, { assetId: string; video: HTMLVideoElement }>();
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
  graphics: {
    titleKey: "help.track.graphics.title",
    bodyKey: "help.track.graphics.body",
    itemKeys: ["help.track.graphics.item1", "help.track.graphics.item2"],
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
    graphics: { ...DEFAULT_TIMELINE_TRACK_STATES.graphics },
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
  if (type === "graphic") return "graphics";
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

function getClipTimelineDuration(clip: ClipSegment): number {
  return videoClipTimelineDuration(clip.sourceStart, clip.sourceEnd, clip.speed);
}

function getClipSourceTime(clip: ClipSegment, timelineOffset: number): number {
  return videoClipSourceTimeAtOffset(
    clip.sourceStart,
    clip.sourceEnd,
    clip.speed,
    timelineOffset,
  );
}

function getClipTimelineOffset(clip: ClipSegment, sourceTime: number): number {
  return videoClipTimelineOffsetAtSourceTime(
    clip.sourceStart,
    clip.sourceEnd,
    clip.speed,
    sourceTime,
  );
}

function clipPreviewPlaybackRate(clip: ClipSegment, timelinePlaybackRate: number): number {
  return normalizeVideoClipSpeed(clip.speed) * timelinePlaybackRate;
}

function clipEdgeFadeOpacityAtLocalTime(clip: ClipSegment, localTime: number): number {
  return videoClipEdgeFadeOpacity(
    localTime,
    getClipTimelineDuration(clip),
    clip.fadeIn,
    clip.fadeOut,
  );
}

function clipHasManualEdit(clip: ClipSegment, sourceDuration: number): boolean {
  const maxDuration = getClipMaxDuration(clip, sourceDuration);
  const hasTrim = maxDuration > 0
    ? clip.sourceStart > 0.001 || Math.abs(clip.sourceEnd - maxDuration) > 0.001
    : clip.sourceStart > 0.001;
  return Boolean(
    hasTrim ||
    Math.abs(normalizeVideoClipSpeed(clip.speed) - 1) > 0.001 ||
    clip.fadeIn > 0.001 ||
    clip.fadeOut > 0.001 ||
    clip.muted ||
    clip.fit !== "contain" ||
    Math.abs(clip.x - 50) > 0.001 ||
    Math.abs(clip.y - 50) > 0.001 ||
    Math.abs(clip.scale - 1) > 0.001 ||
    Math.abs(clip.rotation) > 0.001 ||
    Math.abs(clip.opacity - 1) > 0.001 ||
    clip.keyframes.length > 0 ||
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
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
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
    .filter((caption) => caption.style !== "titleCard" && caption.style !== "lowerThird")
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
  if (caption.style === "titleCard") {
    return {
      color: caption.color,
      background: hexToRgba(caption.backgroundColor || "#0f172a", opacity ?? 0.56),
    };
  }
  if (caption.style === "lowerThird") {
    return {
      color: caption.color,
      background: hexToRgba(caption.backgroundColor || "#0f172a", opacity ?? 0.9),
    };
  }
  return {
    color: caption.color,
    background: caption.backgroundColor ? hexToRgba(caption.backgroundColor, opacity ?? 0.72) : caption.background,
  };
}

function captionBaseMotionPose(caption: CaptionCue): MotionPose {
  return clampCaptionMotionPose({
    x: numberOr(caption.x, 50),
    y: numberOr(caption.y, 84),
    scale: numberOr(caption.scale, 1),
    ...(typeof caption.scaleX === "number" ? { scaleX: caption.scaleX } : {}),
    ...(typeof caption.scaleY === "number" ? { scaleY: caption.scaleY } : {}),
    ...(typeof caption.rotationX === "number" ? { rotationX: caption.rotationX } : {}),
    ...(typeof caption.rotationY === "number" ? { rotationY: caption.rotationY } : {}),
    ...(typeof caption.perspective === "number" ? { perspective: caption.perspective } : {}),
    ...(typeof caption.blur === "number" ? { blur: caption.blur } : {}),
    rotation: numberOr(caption.rotation, 0),
    opacity: numberOr(caption.opacity, 1),
  });
}

function clampCaptionMotionPose(pose: MotionPose): MotionPose {
  const normalized: MotionPose = {
    x: clamp(pose.x, 0, 100),
    y: clamp(pose.y, 0, 100),
    scale: clamp(pose.scale, 0.1, 4),
    rotation: clamp(pose.rotation, -360, 360),
    opacity: clamp(pose.opacity, 0, 1),
  };
  if (pose.scaleX !== undefined) normalized.scaleX = clamp(pose.scaleX, 0.01, 8);
  if (pose.scaleY !== undefined) normalized.scaleY = clamp(pose.scaleY, 0.01, 8);
  if (pose.rotationX !== undefined) normalized.rotationX = clamp(pose.rotationX, -180, 180);
  if (pose.rotationY !== undefined) normalized.rotationY = clamp(pose.rotationY, -180, 180);
  if (pose.perspective !== undefined) normalized.perspective = clamp(pose.perspective, 200, 4000);
  if (pose.blur !== undefined) normalized.blur = clamp(pose.blur, 0, 80);
  if (pose.effectStrength !== undefined) normalized.effectStrength = clamp(pose.effectStrength, 0, 1);
  if (pose.pathProgress !== undefined) normalized.pathProgress = clamp(pose.pathProgress, 0, 1);
  return normalized;
}

function shotBaseMotionPose(shot: ShotBeat): MotionPose {
  return clampCaptionMotionPose({
    x: numberOr(shot.x, 50),
    y: numberOr(shot.y, 50),
    scale: numberOr(shot.scale, 1),
    ...(typeof shot.scaleX === "number" ? { scaleX: shot.scaleX } : {}),
    ...(typeof shot.scaleY === "number" ? { scaleY: shot.scaleY } : {}),
    ...(typeof shot.rotationX === "number" ? { rotationX: shot.rotationX } : {}),
    ...(typeof shot.rotationY === "number" ? { rotationY: shot.rotationY } : {}),
    ...(typeof shot.perspective === "number" ? { perspective: shot.perspective } : {}),
    ...(typeof shot.blur === "number" ? { blur: shot.blur } : {}),
    rotation: numberOr(shot.rotation, 0),
    opacity: numberOr(shot.opacity, 1),
  });
}

function shotMotionPoseAtTimelineTime(shot: ShotBeat, timelineTime: number): MotionPose {
  return motionPoseAtTime(
    shot.keyframes ?? [],
    clamp(timelineTime - shot.start, 0, Math.max(0, shot.end - shot.start)),
    shotBaseMotionPose(shot),
  );
}

function composeMotionPoses(child: MotionPose, parent: MotionPose | null): MotionPose {
  if (!parent) return child;
  const angle = (parent.rotation * Math.PI) / 180;
  const parentScaleX = parent.scale * (parent.scaleX ?? 1);
  const parentScaleY = parent.scale * (parent.scaleY ?? 1);
  const dx = (child.x - 50) * parentScaleX;
  const dy = (child.y - 50) * parentScaleY;
  return clampCaptionMotionPose({
    x: parent.x + dx * Math.cos(angle) - dy * Math.sin(angle),
    y: parent.y + dx * Math.sin(angle) + dy * Math.cos(angle),
    scale: child.scale * parent.scale,
    scaleX: (child.scaleX ?? 1) * (parent.scaleX ?? 1),
    scaleY: (child.scaleY ?? 1) * (parent.scaleY ?? 1),
    rotation: child.rotation + parent.rotation,
    rotationX: (child.rotationX ?? 0) + (parent.rotationX ?? 0),
    rotationY: (child.rotationY ?? 0) + (parent.rotationY ?? 0),
    perspective: child.perspective ?? parent.perspective ?? 1200,
    blur: (child.blur ?? 0) + (parent.blur ?? 0),
    ...(child.effectStrength !== undefined ? { effectStrength: child.effectStrength } : {}),
    ...(child.pathProgress !== undefined ? { pathProgress: child.pathProgress } : {}),
    opacity: child.opacity * parent.opacity,
  });
}

function motionDepthProjection(pose: MotionPose): { a: number; b: number; c: number; d: number } {
  const rotationX = ((pose.rotationX ?? 0) * Math.PI) / 180;
  const rotationY = ((pose.rotationY ?? 0) * Math.PI) / 180;
  const perspectiveFactor = clamp(1200 / (pose.perspective ?? 1200), 0.3, 2.5);
  return {
    a: Math.cos(rotationY),
    b: -Math.sin(rotationX) * 0.18 * perspectiveFactor,
    c: Math.sin(rotationY) * 0.18 * perspectiveFactor,
    d: Math.cos(rotationX),
  };
}

function motionCssTransform(anchor: string, pose: MotionPose): string {
  const projection = motionDepthProjection(pose);
  const scaleX = pose.scale * (pose.scaleX ?? 1);
  const scaleY = pose.scale * (pose.scaleY ?? 1);
  return `${anchor} rotate(${pose.rotation}deg) matrix(${projection.a}, ${projection.b}, ${projection.c}, ${projection.d}, 0, 0) scale(${scaleX}, ${scaleY})`;
}

function applyCanvasMotionTransform(ctx: CanvasRenderingContext2D, pose: MotionPose) {
  const projection = motionDepthProjection(pose);
  ctx.rotate((pose.rotation * Math.PI) / 180);
  ctx.transform(projection.a, projection.b, projection.c, projection.d, 0, 0);
  ctx.scale(
    pose.scale * (pose.scaleX ?? 1),
    pose.scale * (pose.scaleY ?? 1),
  );
}

function captionMotionPoseAtTimelineTime(caption: CaptionCue, timelineTime: number): MotionPose {
  return motionPoseAtTime(
    caption.keyframes ?? [],
    clamp(timelineTime - caption.start, 0, Math.max(0, caption.end - caption.start)),
    captionBaseMotionPose(caption),
  );
}

function clipBaseMotionPose(clip: ClipSegment): MotionPose {
  return clampCaptionMotionPose({
    x: numberOr(clip.x, 50),
    y: numberOr(clip.y, 50),
    scale: numberOr(clip.scale, 1),
    ...(typeof clip.scaleX === "number" ? { scaleX: clip.scaleX } : {}),
    ...(typeof clip.scaleY === "number" ? { scaleY: clip.scaleY } : {}),
    ...(typeof clip.rotationX === "number" ? { rotationX: clip.rotationX } : {}),
    ...(typeof clip.rotationY === "number" ? { rotationY: clip.rotationY } : {}),
    ...(typeof clip.perspective === "number" ? { perspective: clip.perspective } : {}),
    ...(typeof clip.blur === "number" ? { blur: clip.blur } : {}),
    rotation: numberOr(clip.rotation, 0),
    opacity: numberOr(clip.opacity, 1),
  });
}

function clipMotionPoseAtLocalTime(clip: ClipSegment, localTime: number): MotionPose {
  return motionPoseAtTime(
    clip.keyframes ?? [],
    clamp(localTime, 0, getClipTimelineDuration(clip)),
    clipBaseMotionPose(clip),
  );
}

function graphicBaseMotionPose(graphic: GraphicLayer): MotionPose {
  return clampCaptionMotionPose({
    x: numberOr(graphic.x, 50),
    y: numberOr(graphic.y, 50),
    scale: numberOr(graphic.scale, 1),
    ...(typeof graphic.scaleX === "number" ? { scaleX: graphic.scaleX } : {}),
    ...(typeof graphic.scaleY === "number" ? { scaleY: graphic.scaleY } : {}),
    ...(typeof graphic.rotationX === "number" ? { rotationX: graphic.rotationX } : {}),
    ...(typeof graphic.rotationY === "number" ? { rotationY: graphic.rotationY } : {}),
    ...(typeof graphic.perspective === "number" ? { perspective: graphic.perspective } : {}),
    ...(typeof graphic.blur === "number" ? { blur: graphic.blur } : {}),
    ...(typeof graphic.effectStrength === "number" ? { effectStrength: graphic.effectStrength } : {}),
    ...(typeof graphic.pathProgress === "number" ? { pathProgress: graphic.pathProgress } : {}),
    rotation: numberOr(graphic.rotation, 0),
    opacity: numberOr(graphic.opacity, 1),
  });
}

function graphicMotionPoseAtTimelineTime(graphic: GraphicLayer, timelineTime: number): MotionPose {
  return motionPoseAtTime(
    graphic.keyframes ?? [],
    clamp(timelineTime - graphic.start, 0, Math.max(0, graphic.end - graphic.start)),
    graphicBaseMotionPose(graphic),
  );
}

function graphicKindDisplayLabel(kind: GraphicLayerKind): string {
  return veText(`graphic_kind.${kind}`) || GRAPHIC_KIND_LABELS[kind];
}

function loadGraphicImageAsset(assetId: string): Promise<HTMLImageElement> {
  const cachedImage = graphicImageElementCache.get(assetId);
  if (cachedImage) return Promise.resolve(cachedImage);
  if (graphicImageFailureCache.has(assetId)) return Promise.reject(new Error("Image overlay unavailable"));
  const inflight = graphicImageInflight.get(assetId);
  if (inflight) return inflight;

  const request = api.documents.download(assetId, { cache: true })
    .then((url) => new Promise<HTMLImageElement>((resolve, reject) => {
      const image = new Image();
      image.decoding = "async";
      image.onload = () => {
        graphicImageUrlCache.set(assetId, url);
        graphicImageElementCache.set(assetId, image);
        resolve(image);
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("Image overlay failed to decode"));
      };
      image.src = url;
    }))
    .catch((error) => {
      graphicImageFailureCache.add(assetId);
      throw error;
    })
    .finally(() => {
      graphicImageInflight.delete(assetId);
    });
  graphicImageInflight.set(assetId, request);
  return request;
}

function loadGraphicVideoAsset(assetId: string): Promise<HTMLVideoElement> {
  const cachedVideo = graphicVideoElementCache.get(assetId);
  if (cachedVideo) return Promise.resolve(cachedVideo);
  if (graphicVideoFailureCache.has(assetId)) return Promise.reject(new Error("Video overlay unavailable"));
  const inflight = graphicVideoInflight.get(assetId);
  if (inflight) return inflight;

  const request = loadMediaVideoPreviewUrl(assetId)
    .then((url) => new Promise<HTMLVideoElement>((resolve, reject) => {
      const video = document.createElement("video");
      video.crossOrigin = "anonymous";
      video.muted = true;
      video.playsInline = true;
      video.preload = "auto";
      video.onloadedmetadata = () => {
        graphicVideoElementCache.set(assetId, video);
        resolve(video);
      };
      video.onerror = () => reject(new Error("Video overlay failed to decode"));
      video.src = url;
      video.load();
    }))
    .catch((error) => {
      graphicVideoFailureCache.add(assetId);
      throw error;
    })
    .finally(() => {
      graphicVideoInflight.delete(assetId);
    });
  graphicVideoInflight.set(assetId, request);
  return request;
}

function getGraphicVideoSourceWindow(graphic: GraphicLayer, mediaDuration = 0) {
  return videoOverlaySourceWindow(
    mediaDuration || graphic.assetDuration,
    graphic.sourceStart,
    graphic.sourceEnd,
    graphic.end - graphic.start,
  );
}

function getGraphicVideoSourceTime(graphic: GraphicLayer, timelineTime: number, mediaDuration = 0) {
  return videoOverlaySourceTimeAtTimelineTime({
    timelineStart: graphic.start,
    timelineTime,
    speed: graphic.speed,
    loop: graphic.loop,
    assetDuration: mediaDuration || graphic.assetDuration,
    sourceStart: graphic.sourceStart,
    sourceEnd: graphic.sourceEnd,
    fallbackDuration: graphic.end - graphic.start,
  });
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
    const beat = time * 2;
    const chordIndex = Math.floor(time) % 4;
    const roots = [55, 65.406, 48.999, 73.416];
    const root = roots[chordIndex];
    const chordPhase = time % 1;
    const chordEnvelope = Math.min(1, chordPhase / 0.045, (1 - chordPhase) / 0.08);
    const pad = (
      sine(root * 2, 0.16)
      + sine(root * 2.9966, 0.11)
      + sine(root * 4.4898, 0.075)
    ) * Math.max(0, chordEnvelope) * (0.82 + sine(0.25, 0.12));
    const kickPhase = time % 0.5;
    const kickFrequency = 48 + 88 * Math.exp(-kickPhase * 28);
    const kick = Math.sin(2 * Math.PI * kickFrequency * kickPhase) * Math.exp(-kickPhase * 18) * 0.62;
    const bassGate = Math.min(1, (time % 0.5) / 0.015) * Math.exp(-(time % 0.5) * 3.6);
    const bass = sine(root, 0.34) * bassGate;
    const arpIntervals = [12, 19, 24, 31];
    const arpStep = Math.floor(beat * 2) % arpIntervals.length;
    const arpFrequency = root * (2 ** (arpIntervals[arpStep] / 12));
    const arpPhase = time % 0.25;
    const arp = sine(arpFrequency, 0.13) * Math.exp(-arpPhase * 8.5);
    const hatPhase = time % 0.25;
    const hat = noiseSample(index * 17 + 23) * Math.exp(-hatPhase * 42) * (Math.floor(beat * 2) % 2 ? 0.075 : 0.045);
    const sidechain = 0.72 + 0.28 * Math.min(1, kickPhase * 9);
    return (pad + bass + arp) * sidechain + kick + hat;
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

async function waitForDecodedVideoFrame(video: HTMLVideoElement, timeoutMs = 180): Promise<void> {
  const frameVideo = video as HTMLVideoElement & {
    requestVideoFrameCallback?: (callback: (now: number, metadata: unknown) => void) => number;
    cancelVideoFrameCallback?: (handle: number) => void;
  };
  await new Promise<void>((resolve) => {
    let finished = false;
    let frameHandle: number | null = null;
    let animationHandle: number | null = null;
    let secondAnimationHandle: number | null = null;
    const timer = window.setTimeout(finish, timeoutMs);
    function finish() {
      if (finished) return;
      finished = true;
      window.clearTimeout(timer);
      if (frameHandle !== null) frameVideo.cancelVideoFrameCallback?.(frameHandle);
      if (animationHandle !== null) window.cancelAnimationFrame(animationHandle);
      if (secondAnimationHandle !== null) window.cancelAnimationFrame(secondAnimationHandle);
      resolve();
    }

    if (typeof frameVideo.requestVideoFrameCallback === "function") {
      frameHandle = frameVideo.requestVideoFrameCallback(() => finish());
      return;
    }
    animationHandle = window.requestAnimationFrame(() => {
      secondAnimationHandle = window.requestAnimationFrame(() => finish());
    });
  });
}

function timelineFilmstripSampleTimes(start: number, end: number, frameCount: number): number[] {
  const count = Math.max(1, Math.floor(frameCount));
  return Array.from({ length: count }, (_, index) => {
    const progress = (index + 0.5) / count;
    return clamp(start + (end - start) * progress, start, end);
  });
}

async function captureTimelineFilmstrip(
  videoUrl: string,
  sourceStart: number,
  sourceEnd: number,
  frameCount: number,
): Promise<string[]> {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.src = videoUrl;
  video.load();
  if (video.readyState < HTMLMediaElement.HAVE_METADATA) {
    await waitForMediaElement(video, ["loadedmetadata", "loadeddata"]);
  }

  const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : sourceEnd;
  const start = clamp(sourceStart, 0, Math.max(0, duration - 0.01));
  const end = clamp(sourceEnd, start + 0.01, Math.max(start + 0.01, duration));
  const width = 144;
  const height = 81;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas is not available");
  const frames: string[] = [];

  const sampleTimes = timelineFilmstripSampleTimes(start, end, frameCount);
  for (const targetTime of sampleTimes) {
    if (Math.abs(video.currentTime - targetTime) > 0.025) {
      await new Promise<void>((resolve) => {
        let timer: number | null = null;
        const finish = () => {
          if (timer !== null) window.clearTimeout(timer);
          video.removeEventListener("seeked", finish);
          video.removeEventListener("error", finish);
          resolve();
        };
        video.addEventListener("seeked", finish, { once: true });
        video.addEventListener("error", finish, { once: true });
        timer = window.setTimeout(finish, 2200);
        try {
          video.currentTime = targetTime;
        } catch {
          finish();
        }
      });
    }
    if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
      await waitForMediaElement(video, ["loadeddata", "canplay"], 3000).catch(() => undefined);
    }
    // `seeked` can fire before the decoder has presented the requested frame.
    // Waiting for a video-frame callback prevents the previous thumbnail from
    // being copied repeatedly across the filmstrip on keyframe-heavy codecs.
    await waitForDecodedVideoFrame(video);
    context.fillStyle = "#1c1917";
    context.fillRect(0, 0, width, height);
    if (video.videoWidth && video.videoHeight) {
      const sourceAspect = video.videoWidth / video.videoHeight;
      const targetAspect = width / height;
      let drawWidth = width;
      let drawHeight = height;
      let drawX = 0;
      let drawY = 0;
      if (sourceAspect > targetAspect) {
        drawWidth = height * sourceAspect;
        drawX = (width - drawWidth) / 2;
      } else {
        drawHeight = width / sourceAspect;
        drawY = (height - drawHeight) / 2;
      }
      context.drawImage(video, drawX, drawY, drawWidth, drawHeight);
    }
    frames.push(canvas.toDataURL("image/jpeg", 0.72));
  }

  video.removeAttribute("src");
  video.load();
  return frames;
}

function loadTimelineFilmstrip(
  assetId: string,
  sourceStart: number,
  sourceEnd: number,
  frameCount: number,
): Promise<string[]> {
  const cacheKey = `${assetId}:${sourceStart.toFixed(2)}:${sourceEnd.toFixed(2)}:${frameCount}`;
  const cached = timelineFilmstripCache.get(cacheKey);
  if (cached) return Promise.resolve(cached);
  if (timelineFilmstripFailureCache.has(cacheKey)) return Promise.reject(new Error("Filmstrip unavailable"));
  const inflight = timelineFilmstripInflight.get(cacheKey);
  if (inflight) return inflight;

  const request = loadMediaVideoPreviewUrl(assetId)
    .then((url) => captureTimelineFilmstrip(url, sourceStart, sourceEnd, frameCount))
    .then((frames) => {
      if (timelineFilmstripCache.size >= 80) {
        const oldestKey = timelineFilmstripCache.keys().next().value;
        if (oldestKey) timelineFilmstripCache.delete(oldestKey);
      }
      timelineFilmstripCache.set(cacheKey, frames);
      return frames;
    })
    .catch((error) => {
      timelineFilmstripFailureCache.add(cacheKey);
      throw error;
    })
    .finally(() => {
      timelineFilmstripInflight.delete(cacheKey);
    });
  timelineFilmstripInflight.set(cacheKey, request);
  return request;
}

function TimelineClipFilmstrip({
  assetId,
  sourceStart,
  sourceEnd,
  frameCount,
}: {
  assetId: string;
  sourceStart: number;
  sourceEnd: number;
  frameCount: number;
}) {
  const [frames, setFrames] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFrames([]);
    setFailed(false);
    const timer = window.setTimeout(() => {
      loadTimelineFilmstrip(assetId, sourceStart, sourceEnd, frameCount)
        .then((nextFrames) => {
          if (!cancelled) setFrames(nextFrames);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [assetId, frameCount, sourceEnd, sourceStart]);

  return (
    <span className={`ve-clip-filmstrip ${frames.length === 0 && !failed ? "is-loading" : ""}`} aria-hidden="true">
      {frames.map((frame, index) => <img key={`${frame.slice(-18)}-${index}`} src={frame} alt="" draggable={false} />)}
    </span>
  );
}

function ParticleGraphicCanvas({
  graphic,
  timelineTime,
}: {
  graphic: GraphicLayer;
  timelineTime: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.translate(canvas.width / 2, canvas.height / 2);
    drawParticleLayer(
      ctx,
      graphic,
      timelineTime - graphic.start,
      Math.max(0.05, graphic.end - graphic.start),
      canvas.width,
      canvas.height,
    );
    ctx.restore();
  }, [graphic, timelineTime]);

  return <canvas ref={canvasRef} className="ve-particle-canvas" width={480} height={480} aria-hidden="true" />;
}

function ShaderGraphicCanvas({
  graphic,
  timelineTime,
}: {
  graphic: GraphicLayer;
  timelineTime: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const surfaceRef = useRef<VideoEditorShaderSurface | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const surface = createVideoEditorShaderSurface(960, 540, canvas);
    surfaceRef.current = surface;
    return () => {
      disposeVideoEditorShaderSurface(surface);
      surfaceRef.current = null;
    };
  }, []);

  useEffect(() => {
    const surface = surfaceRef.current;
    if (!surface) return;
    renderVideoEditorShaderFrame(surface, {
      time: timelineTime - graphic.start,
      duration: Math.max(0.05, graphic.end - graphic.start),
      colorA: graphic.fill,
      colorB: graphic.fillSecondary ?? "#7047eb",
      colorC: graphic.stroke,
      style: graphic,
    });
  }, [graphic, timelineTime]);

  return <canvas ref={canvasRef} className="ve-particle-canvas ve-shader-canvas" width={960} height={540} aria-hidden="true" />;
}

async function decodeTimelineAudioWaveform(assetId: string): Promise<{ duration: number; peaks: number[] }> {
  const url = await api.documents.download(assetId, { cache: true });
  try {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`Audio waveform request failed: ${response.status}`);
    const bytes = await response.arrayBuffer();
    const AudioContextCtor = window.AudioContext ?? (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!AudioContextCtor) throw new Error("Web Audio is unavailable");
    const context = new AudioContextCtor();
    try {
      const buffer = await context.decodeAudioData(bytes.slice(0));
      const peakCount = 256;
      const bucketSize = Math.max(1, Math.floor(buffer.length / peakCount));
      const sampleStride = Math.max(1, Math.floor(bucketSize / 96));
      const peaks = Array.from({ length: peakCount }, (_, bucketIndex) => {
        const bucketStart = bucketIndex * bucketSize;
        const bucketEnd = Math.min(buffer.length, bucketStart + bucketSize);
        let peak = 0;
        for (let channelIndex = 0; channelIndex < buffer.numberOfChannels; channelIndex += 1) {
          const channel = buffer.getChannelData(channelIndex);
          for (let sampleIndex = bucketStart; sampleIndex < bucketEnd; sampleIndex += sampleStride) {
            peak = Math.max(peak, Math.abs(channel[sampleIndex] ?? 0));
          }
        }
        return clamp(peak, 0.025, 1);
      });
      return { duration: buffer.duration, peaks };
    } finally {
      await context.close().catch(() => undefined);
    }
  } finally {
    revokeObjectUrlSoon(url);
  }
}

function loadTimelineAudioWaveform(assetId: string): Promise<{ duration: number; peaks: number[] }> {
  const cached = timelineAudioWaveformCache.get(assetId);
  if (cached) return Promise.resolve(cached);
  if (timelineAudioWaveformFailureCache.has(assetId)) return Promise.reject(new Error("Audio waveform unavailable"));
  const inflight = timelineAudioWaveformInflight.get(assetId);
  if (inflight) return inflight;
  const request = decodeTimelineAudioWaveform(assetId)
    .then((waveform) => {
      if (timelineAudioWaveformCache.size >= 80) {
        const oldestKey = timelineAudioWaveformCache.keys().next().value;
        if (oldestKey) timelineAudioWaveformCache.delete(oldestKey);
      }
      timelineAudioWaveformCache.set(assetId, waveform);
      return waveform;
    })
    .catch((error) => {
      timelineAudioWaveformFailureCache.add(assetId);
      throw error;
    })
    .finally(() => timelineAudioWaveformInflight.delete(assetId));
  timelineAudioWaveformInflight.set(assetId, request);
  return request;
}

function TimelineAudioWaveform({ cue, barCount }: { cue: AudioCue; barCount: number }) {
  const [waveform, setWaveform] = useState<{ duration: number; peaks: number[] } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setWaveform(null);
    setFailed(false);
    if (!cue.assetDocumentId) return undefined;
    const timer = window.setTimeout(() => {
      loadTimelineAudioWaveform(cue.assetDocumentId as string)
        .then((value) => {
          if (!cancelled) setWaveform(value);
        })
        .catch(() => {
          if (!cancelled) setFailed(true);
        });
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [cue.assetDocumentId]);

  const count = clamp(Math.round(barCount), 8, 120);
  const peaks = waveform?.peaks ?? Array.from({ length: count }, (_, index) => 0.18 + ((index * 17) % 11) / 24);
  const mediaDuration = waveform?.duration ?? cue.assetDuration ?? cue.sourceEnd ?? Math.max(0.05, cue.end - cue.start);
  const source = getAudioCueSourceWindow(cue, mediaDuration);
  const startProgress = source.start / Math.max(0.01, mediaDuration);
  const endProgress = source.end / Math.max(0.01, mediaDuration);
  const visiblePeaks = Array.from({ length: count }, (_, index) => {
    const progress = count <= 1 ? 0 : index / (count - 1);
    const sourceProgress = startProgress + (endProgress - startProgress) * progress;
    return peaks[clamp(Math.round(sourceProgress * (peaks.length - 1)), 0, peaks.length - 1)] ?? 0.08;
  });

  return (
    <span className={`ve-audio-waveform ${!waveform && !failed ? "is-loading" : ""}`} aria-hidden="true">
      {visiblePeaks.map((peak, index) => (
        <i key={index} style={{ height: `${Math.max(10, peak * 92)}%` }} />
      ))}
    </span>
  );
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
    const duration = getClipTimelineDuration(clip);
    const timelineEnd = cursor + duration;
    if (time < timelineEnd || index === clips.length - 1) {
      const offset = clamp(time - cursor, 0, duration);
      return {
        clip,
        index,
        timelineStart: cursor,
        timelineEnd,
        sourceTime: getClipSourceTime(clip, offset),
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
    const duration = getClipTimelineDuration(clip);
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
  const duration = Math.max(0.05, getClipTimelineDuration(clip));
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
    graphicLayers: sortTimedItems(baseState.graphicLayers.map(shiftRangeAfterInsert)),
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
    graphicLayers: remapTimedItemsForClipOrder(baseState.graphicLayers, originalSpans, nextSpansByClipId, nextDuration),
    audioCues: remapTimedItemsForClipOrder(baseState.audioCues, originalSpans, nextSpansByClipId, nextDuration),
    markers: remapMarkersForClipOrder(baseState.markers, originalSpans, nextSpansByClipId, nextDuration),
  };
}

function cloneEditorTrackState(state: EditorTrackState): EditorTrackState {
  return {
    clips: state.clips.map((clip) => ({
      ...clip,
      keyframes: (clip.keyframes ?? []).map((keyframe) => ({ ...keyframe })),
    })),
    shotBeats: state.shotBeats.map((shot) => ({
      ...shot,
      keyframes: (shot.keyframes ?? []).map((keyframe) => ({ ...keyframe })),
    })),
    captions: state.captions.map((caption) => ({
      ...caption,
      keyframes: (caption.keyframes ?? []).map((keyframe) => ({ ...keyframe })),
    })),
    graphicLayers: state.graphicLayers.map((graphic) => ({
      ...graphic,
      keyframes: (graphic.keyframes ?? []).map((keyframe) => ({ ...keyframe })),
    })),
    audioCues: state.audioCues.map((cue) => ({ ...cue })),
    markers: state.markers.map((marker) => ({ ...marker })),
  };
}

function serializeEditorTrackState(state: EditorTrackState): string {
  return JSON.stringify(state);
}

function editorTrackDuration(state: EditorTrackState): number {
  return state.clips.reduce((total, clip) => total + getClipTimelineDuration(clip), 0);
}

function normalizeCompositionLinks(
  graphics: GraphicLayer[],
  captions: CaptionCue[],
): { graphics: GraphicLayer[]; captions: CaptionCue[] } {
  const graphicsById = new Map(graphics.map((graphic) => [graphic.id, graphic]));
  const validParentId = (ownerId: string, candidate: unknown): string | null => {
    if (typeof candidate !== "string" || !candidate || candidate === ownerId) return null;
    const parent = graphicsById.get(candidate);
    if (!parent || parent.kind !== "group") return null;
    const visited = new Set([ownerId]);
    let cursor: GraphicLayer | undefined = parent;
    while (cursor) {
      if (visited.has(cursor.id)) return null;
      visited.add(cursor.id);
      cursor = typeof cursor.parentId === "string" ? graphicsById.get(cursor.parentId) : undefined;
    }
    return parent.id;
  };

  const normalizedGraphics = graphics.map((graphic) => {
    const parentId = validParentId(graphic.id, graphic.parentId);
    const isTemplate = graphic.kind === "group" ? Boolean(graphic.isTemplate) : false;
    const instanceTarget = graphic.kind === "group" && typeof graphic.instanceOf === "string"
      ? graphicsById.get(graphic.instanceOf)
      : null;
    return {
      ...graphic,
      parentId,
      clipChildren: graphic.kind === "group" ? Boolean(graphic.clipChildren) : false,
      isTemplate,
      instanceOf: graphic.kind === "group"
        && !isTemplate
        && instanceTarget?.kind === "group"
        && instanceTarget.isTemplate
        && instanceTarget.id !== graphic.id
        ? instanceTarget.id
        : null,
    };
  });
  const normalizedGroupIds = new Set(normalizedGraphics.filter((graphic) => graphic.kind === "group").map((graphic) => graphic.id));
  return {
    graphics: normalizedGraphics,
    captions: captions.map((caption) => ({
      ...caption,
      parentId: typeof caption.parentId === "string" && normalizedGroupIds.has(caption.parentId)
        ? caption.parentId
        : null,
    })),
  };
}

function normalizeVideoEditRecipe(recipe: VideoEditRecipe, sourceDuration: number): NormalizedVideoEditRecipe {
  const rawClips = Array.isArray(recipe.timeline?.clips) ? recipe.timeline?.clips ?? [] : [];
  if (rawClips.length === 0) throw new Error("Recipe has no video clips");
  const maxSourceDuration = sourceDuration || Math.max(...rawClips.map((clip) => numberOr(clip.sourceEnd, 0)), 1);
  const nextClips = rawClips.map((clip, index) => {
    const clipMaxDuration = typeof clip.assetDuration === "number" && clip.assetDuration > 0 ? clip.assetDuration : maxSourceDuration;
    const start = clamp(numberOr(clip.sourceStart, 0), 0, Math.max(0, clipMaxDuration - 0.05));
    const end = clamp(numberOr(clip.sourceEnd, clipMaxDuration), start + 0.05, clipMaxDuration);
    const speed = normalizeVideoClipSpeed(clip.speed);
    const timelineDuration = videoClipTimelineDuration(start, end, speed);
    const basePose = clampCaptionMotionPose({
      x: numberOr(clip.x, 50),
      y: numberOr(clip.y, 50),
      scale: numberOr(clip.scale, 1),
      ...(typeof clip.scaleX === "number" ? { scaleX: clip.scaleX } : {}),
      ...(typeof clip.scaleY === "number" ? { scaleY: clip.scaleY } : {}),
      ...(typeof clip.rotationX === "number" ? { rotationX: clip.rotationX } : {}),
      ...(typeof clip.rotationY === "number" ? { rotationY: clip.rotationY } : {}),
      ...(typeof clip.perspective === "number" ? { perspective: clip.perspective } : {}),
      ...(typeof clip.blur === "number" ? { blur: clip.blur } : {}),
      rotation: numberOr(clip.rotation, 0),
      opacity: numberOr(clip.opacity, 1),
    });
    return {
      id: typeof clip.id === "string" ? clip.id : makeId("clip"),
      label: typeof clip.label === "string" ? clip.label : `Clip ${index + 1}`,
      sourceStart: start,
      sourceEnd: end,
      speed,
      fadeIn: normalizeVideoClipFade(clip.fadeIn, timelineDuration),
      fadeOut: normalizeVideoClipFade(clip.fadeOut, timelineDuration),
      muted: Boolean(clip.muted),
      color: typeof clip.color === "string" ? clip.color : CLIP_COLORS[index % CLIP_COLORS.length],
      fit: clip.fit === "cover" ? "cover" : "contain",
      ...basePose,
      keyframes: normalizeMotionKeyframes(
        clip.keyframes,
        basePose,
        timelineDuration,
      ),
      assetDocumentId: typeof clip.assetDocumentId === "string" ? clip.assetDocumentId : null,
      assetName: typeof clip.assetName === "string" ? clip.assetName : null,
      assetMimeType: typeof clip.assetMimeType === "string" ? clip.assetMimeType : null,
      assetDuration: typeof clip.assetDuration === "number" ? clip.assetDuration : null,
      replacementPrompt: typeof clip.replacementPrompt === "string" ? clip.replacementPrompt : "",
      editNotes: typeof clip.editNotes === "string" ? clip.editNotes : "",
    } satisfies ClipSegment;
  });
  const recipeDuration = nextClips.reduce((total, clip) => total + getClipTimelineDuration(clip), 0);
  const generatedMotionDesign = createMotionDesignComposition(recipe.motion_design, recipeDuration);
  const appendGeneratedMotionDesign = generatedMotionDesign && recipe.motion_design?.mode === "append";
  const recipeShots = Array.isArray(recipe.timeline?.shots) ? recipe.timeline?.shots ?? [] : [];
  const rawShots = generatedMotionDesign
    ? appendGeneratedMotionDesign ? [...recipeShots, ...generatedMotionDesign.shots] : generatedMotionDesign.shots
    : recipeShots;
  const nextShotBeats = rawShots.map((shot, index) => {
    const start = clamp(numberOr(shot.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(shot.end, start + 3), start + 0.05, Math.max(start + 0.05, recipeDuration));
    const basePose = clampCaptionMotionPose({
      x: numberOr(shot.x, 50),
      y: numberOr(shot.y, 50),
      scale: numberOr(shot.scale, 1),
      ...(typeof shot.scaleX === "number" ? { scaleX: shot.scaleX } : {}),
      ...(typeof shot.scaleY === "number" ? { scaleY: shot.scaleY } : {}),
      ...(typeof shot.rotationX === "number" ? { rotationX: shot.rotationX } : {}),
      ...(typeof shot.rotationY === "number" ? { rotationY: shot.rotationY } : {}),
      ...(typeof shot.perspective === "number" ? { perspective: shot.perspective } : {}),
      ...(typeof shot.blur === "number" ? { blur: shot.blur } : {}),
      rotation: numberOr(shot.rotation, 0),
      opacity: numberOr(shot.opacity, 1),
    });
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
      ...basePose,
      keyframes: normalizeMotionKeyframes(shot.keyframes, basePose, Math.max(0, end - start)),
    } satisfies ShotBeat;
  });
  const recipeCaptions = Array.isArray(recipe.timeline?.captions) ? recipe.timeline?.captions ?? [] : [];
  const rawCaptions = generatedMotionDesign
    ? appendGeneratedMotionDesign ? [...recipeCaptions, ...generatedMotionDesign.captions] : generatedMotionDesign.captions
    : recipeCaptions;
  const nextCaptions = rawCaptions.map((caption) => {
    const start = clamp(numberOr(caption.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(caption.end, start + 2), start + 0.05, Math.max(start + 0.05, recipeDuration));
    const style = caption.style === "speechBubble"
      || caption.style === "narrationBox"
      || caption.style === "subtitle"
      || caption.style === "titleCard"
      || caption.style === "lowerThird"
      ? caption.style
      : "subtitle";
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
      scale: clamp(numberOr(caption.scale, 1), 0.1, 4),
      ...(typeof caption.scaleX === "number" ? { scaleX: clamp(caption.scaleX, 0.01, 8) } : {}),
      ...(typeof caption.scaleY === "number" ? { scaleY: clamp(caption.scaleY, 0.01, 8) } : {}),
      ...(typeof caption.rotationX === "number" ? { rotationX: clamp(caption.rotationX, -180, 180) } : {}),
      ...(typeof caption.rotationY === "number" ? { rotationY: clamp(caption.rotationY, -180, 180) } : {}),
      ...(typeof caption.perspective === "number" ? { perspective: clamp(caption.perspective, 200, 4000) } : {}),
      ...(typeof caption.blur === "number" ? { blur: clamp(caption.blur, 0, 80) } : {}),
      rotation: clamp(numberOr(caption.rotation, 0), -360, 360),
      opacity: clamp(numberOr(caption.opacity, 1), 0, 1),
      keyframes: normalizeMotionKeyframes(caption.keyframes, {
        x: clamp(numberOr(caption.x, 50), 0, 100),
        y: clamp(numberOr(caption.y, 84), 0, 100),
        scale: clamp(numberOr(caption.scale, 1), 0.1, 4),
        ...(typeof caption.scaleX === "number" ? { scaleX: clamp(caption.scaleX, 0.01, 8) } : {}),
        ...(typeof caption.scaleY === "number" ? { scaleY: clamp(caption.scaleY, 0.01, 8) } : {}),
        ...(typeof caption.rotationX === "number" ? { rotationX: clamp(caption.rotationX, -180, 180) } : {}),
        ...(typeof caption.rotationY === "number" ? { rotationY: clamp(caption.rotationY, -180, 180) } : {}),
        ...(typeof caption.perspective === "number" ? { perspective: clamp(caption.perspective, 200, 4000) } : {}),
        ...(typeof caption.blur === "number" ? { blur: clamp(caption.blur, 0, 80) } : {}),
        rotation: clamp(numberOr(caption.rotation, 0), -360, 360),
        opacity: clamp(numberOr(caption.opacity, 1), 0, 1),
      }, Math.max(0, end - start)),
      size: clamp(numberOr(caption.size, 32), 10, 480),
      color: typeof caption.color === "string" ? caption.color : "#ffffff",
      background: typeof caption.background === "string" ? caption.background : "rgba(28,25,23,0.72)",
      backgroundColor: typeof caption.backgroundColor === "string"
        ? caption.backgroundColor
        : style === "speechBubble"
          ? "#ffffff"
          : style === "titleCard" || style === "lowerThird"
            ? "#0f172a"
            : "#1c1917",
      backgroundOpacity: clamp(numberOr(
        caption.backgroundOpacity,
        style === "speechBubble" ? 0.94 : style === "narrationBox" ? 0.86 : style === "titleCard" ? 0.56 : style === "lowerThird" ? 0.9 : 0.72,
      ), 0, 1),
      align: caption.align === "left" || caption.align === "right" || caption.align === "center" ? caption.align : "center",
      parentId: typeof caption.parentId === "string" ? caption.parentId : null,
      ...normalizeCaptionVisualStyle(caption),
    } satisfies CaptionCue;
  });
  const recipeGraphics = Array.isArray(recipe.timeline?.graphics) ? recipe.timeline?.graphics ?? [] : [];
  const rawGraphics = generatedMotionDesign
    ? appendGeneratedMotionDesign ? [...recipeGraphics, ...generatedMotionDesign.graphics] : generatedMotionDesign.graphics
    : recipeGraphics;
  const nextGraphicLayers = rawGraphics.map((graphic, index) => {
    const start = clamp(numberOr(graphic.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(graphic.end, start + 3), start + 0.05, Math.max(start + 0.05, recipeDuration));
    const kind: GraphicLayerKind = graphic.kind === "group"
      || graphic.kind === "ellipse"
      || graphic.kind === "line"
      || graphic.kind === "path"
      || graphic.kind === "particle"
      || graphic.kind === "shader"
      || graphic.kind === "image"
      || graphic.kind === "video"
      ? graphic.kind
      : "rectangle";
    const assetDuration = typeof graphic.assetDuration === "number" && graphic.assetDuration > 0
      ? graphic.assetDuration
      : null;
    const sourceLimit = assetDuration ?? Math.max(0.05, numberOr(graphic.sourceEnd, end - start));
    const sourceStart = clamp(numberOr(graphic.sourceStart, 0), 0, Math.max(0, sourceLimit - 0.01));
    const sourceEnd = clamp(numberOr(graphic.sourceEnd, sourceLimit), sourceStart + 0.01, sourceLimit);
    const basePose = {
      x: clamp(numberOr(graphic.x, 50), 0, 100),
      y: clamp(numberOr(graphic.y, 50), 0, 100),
      scale: clamp(numberOr(graphic.scale, 1), 0.1, 4),
      ...(typeof graphic.scaleX === "number" ? { scaleX: clamp(graphic.scaleX, 0.01, 8) } : {}),
      ...(typeof graphic.scaleY === "number" ? { scaleY: clamp(graphic.scaleY, 0.01, 8) } : {}),
      ...(typeof graphic.rotationX === "number" ? { rotationX: clamp(graphic.rotationX, -180, 180) } : {}),
      ...(typeof graphic.rotationY === "number" ? { rotationY: clamp(graphic.rotationY, -180, 180) } : {}),
      ...(typeof graphic.perspective === "number" ? { perspective: clamp(graphic.perspective, 200, 4000) } : {}),
      ...(typeof graphic.blur === "number" ? { blur: clamp(graphic.blur, 0, 80) } : {}),
      ...(typeof graphic.effectStrength === "number" ? { effectStrength: clamp(graphic.effectStrength, 0, 1) } : {}),
      ...(typeof graphic.pathProgress === "number" ? { pathProgress: clamp(graphic.pathProgress, 0, 1) } : {}),
      rotation: clamp(numberOr(graphic.rotation, 0), -360, 360),
      opacity: clamp(numberOr(graphic.opacity, 1), 0, 1),
    };
    return {
      id: typeof graphic.id === "string" ? graphic.id : makeId("graphic"),
      kind,
      label: typeof graphic.label === "string" ? graphic.label : `${GRAPHIC_KIND_LABELS[kind]} ${index + 1}`,
      start,
      end,
      ...basePose,
      keyframes: normalizeMotionKeyframes(graphic.keyframes, basePose, Math.max(0, end - start)),
      width: clamp(numberOr(graphic.width, kind === "group" || kind === "shader" ? 100 : kind === "image" || kind === "video" ? 32 : 24), 0.1, 100),
      height: clamp(numberOr(graphic.height, kind === "group" || kind === "shader" ? 100 : kind === "image" || kind === "video" ? 32 : 18), 0.1, 100),
      fill: typeof graphic.fill === "string" ? graphic.fill : "#5f928a",
      stroke: typeof graphic.stroke === "string" ? graphic.stroke : "#ffffff",
      strokeWidth: clamp(numberOr(graphic.strokeWidth, 0), 0, 20),
      cornerRadius: clamp(numberOr(graphic.cornerRadius, kind === "rectangle" ? 12 : 0), 0, 100),
      assetDocumentId: typeof graphic.assetDocumentId === "string" ? graphic.assetDocumentId : null,
      assetName: typeof graphic.assetName === "string" ? graphic.assetName : null,
      assetMimeType: typeof graphic.assetMimeType === "string" ? graphic.assetMimeType : null,
      parentId: typeof graphic.parentId === "string" ? graphic.parentId : null,
      clipChildren: kind === "group" ? Boolean(graphic.clipChildren) : false,
      isTemplate: kind === "group" ? Boolean(graphic.isTemplate) : false,
      instanceOf: kind === "group" && typeof graphic.instanceOf === "string" ? graphic.instanceOf : null,
      ...(kind === "video" ? {
        assetDuration,
        sourceStart,
        sourceEnd,
        speed: normalizeVideoClipSpeed(graphic.speed),
        loop: Boolean(graphic.loop),
      } : {}),
      ...normalizeGraphicVisualStyle(graphic),
      ...(kind === "particle" ? normalizeParticleLayer(graphic) : {}),
      ...(kind === "shader" ? normalizeVideoEditorShaderStyle(graphic) : {}),
    } satisfies GraphicLayer;
  });
  const recipeAudioCues = Array.isArray(recipe.timeline?.audio_cues) ? recipe.timeline?.audio_cues ?? [] : [];
  const rawAudioCues: Partial<AudioCue>[] = generatedMotionDesign
    ? appendGeneratedMotionDesign ? [...recipeAudioCues, ...generatedMotionDesign.audioCues] : generatedMotionDesign.audioCues
    : recipeAudioCues;
  const nextAudioCues = rawAudioCues.map((cue) => {
    const start = clamp(numberOr(cue.start, 0), 0, recipeDuration);
    const end = clamp(numberOr(cue.end, start + 2), start + 0.05, Math.max(start + 0.05, recipeDuration));
    const type = isAudioCueType(cue.type) ? cue.type : "ambience";
    const duration = Math.max(0.05, end - start);
    const assetDuration = typeof cue.assetDuration === "number" && cue.assetDuration > 0
      ? cue.assetDuration
      : null;
    const sourceLimit = assetDuration ?? Math.max(duration, numberOr(cue.sourceEnd, duration));
    const sourceStart = clamp(numberOr(cue.sourceStart, 0), 0, Math.max(0, sourceLimit - 0.01));
    const sourceEnd = clamp(numberOr(cue.sourceEnd, sourceLimit), sourceStart + 0.01, sourceLimit);
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
      sourceStart,
      sourceEnd,
      assetDuration,
      assetDocumentId: typeof cue.assetDocumentId === "string" ? cue.assetDocumentId : null,
      assetName: typeof cue.assetName === "string" ? cue.assetName : null,
      assetMimeType: typeof cue.assetMimeType === "string" ? cue.assetMimeType : null,
      catalogTrackId: typeof cue.catalogTrackId === "string" ? cue.catalogTrackId : null,
      musicCreator: typeof cue.musicCreator === "string" ? cue.musicCreator : null,
      musicLicense: typeof cue.musicLicense === "string" ? cue.musicLicense : null,
      musicLicenseName: typeof cue.musicLicenseName === "string" ? cue.musicLicenseName : null,
      musicLicenseUrl: typeof cue.musicLicenseUrl === "string" ? cue.musicLicenseUrl : null,
      musicSourceUrl: typeof cue.musicSourceUrl === "string" ? cue.musicSourceUrl : null,
      musicAttribution: typeof cue.musicAttribution === "string" ? cue.musicAttribution : null,
      sourcePlan: typeof cue.sourcePlan === "string" ? cue.sourcePlan : "",
      prompt: typeof cue.prompt === "string" ? cue.prompt : "",
    } satisfies AudioCue;
  });
  const recipeMarkers = Array.isArray(recipe.timeline?.markers) ? recipe.timeline?.markers ?? [] : [];
  const rawMarkers = generatedMotionDesign
    ? appendGeneratedMotionDesign ? [...recipeMarkers, ...generatedMotionDesign.markers] : generatedMotionDesign.markers
    : recipeMarkers;
  const nextMarkers = rawMarkers.map((marker, index) => ({
    id: typeof marker.id === "string" ? marker.id : makeId("marker"),
    time: clamp(numberOr(marker.time, 0), 0, recipeDuration),
    label: typeof marker.label === "string" ? marker.label : `Marker ${index + 1}`,
    color: typeof marker.color === "string" ? marker.color : MARKER_COLORS[index % MARKER_COLORS.length],
    notes: typeof marker.notes === "string" ? marker.notes : "",
  } satisfies TimelineMarker));
  const linkedComposition = normalizeCompositionLinks(nextGraphicLayers, nextCaptions);

  return {
    mediaSize: recipe.canvas?.width && recipe.canvas?.height
      ? { width: Number(recipe.canvas.width), height: Number(recipe.canvas.height) }
      : null,
    trackStates: normalizeTimelineTrackStates(recipe.editor_settings?.track_states),
    workArea: normalizeWorkArea(recipe.editor_settings?.work_area, recipeDuration),
    state: {
      clips: nextClips,
      shotBeats: nextShotBeats,
      captions: linkedComposition.captions,
      graphicLayers: linkedComposition.graphics,
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
  if (selection.type === "graphic") return state.graphicLayers.find((graphic) => graphic.id === selection.id)?.start ?? 0;
  if (selection.type === "audio") return state.audioCues.find((cue) => cue.id === selection.id)?.start ?? 0;
  return state.markers.find((marker) => marker.id === selection.id)?.time ?? 0;
}

function buildAiEditNotice(before: EditorTrackState, after: EditorTrackState): AiEditNotice {
  const addedClips = addedItems(before.clips, after.clips);
  const addedShots = addedItems(before.shotBeats, after.shotBeats);
  const addedCaptions = addedItems(before.captions, after.captions);
  const addedGraphics = addedItems(before.graphicLayers, after.graphicLayers);
  const addedAudio = addedItems(before.audioCues, after.audioCues);
  const addedMarkers = addedItems(before.markers, after.markers);
  const changedClips = modifiedItems(before.clips, after.clips);
  const changedShots = modifiedItems(before.shotBeats, after.shotBeats);
  const changedCaptions = modifiedItems(before.captions, after.captions);
  const changedGraphics = modifiedItems(before.graphicLayers, after.graphicLayers);
  const changedAudio = modifiedItems(before.audioCues, after.audioCues);
  const changedMarkers = modifiedItems(before.markers, after.markers);
  const details = [
    addedClips.length ? formatAiEditCount(addedClips.length, "clip") : "",
    addedShots.length ? formatAiEditCount(addedShots.length, "shot") : "",
    addedCaptions.length ? formatAiEditCount(addedCaptions.length, "caption") : "",
    addedGraphics.length ? formatAiEditCount(addedGraphics.length, "graphic") : "",
    addedAudio.length ? formatAiEditCount(addedAudio.length, "audio cue") : "",
    addedMarkers.length ? formatAiEditCount(addedMarkers.length, "marker") : "",
  ].filter(Boolean);
  const changedCount = changedClips.length + changedShots.length + changedCaptions.length + changedGraphics.length + changedAudio.length + changedMarkers.length;
  if (changedCount > 0) details.push(`${changedCount} updated`);

  const highlights: NonNullable<Selection>[] = [
    ...addedAudio.map((cue) => ({ type: "audio" as const, id: cue.id })),
    ...addedCaptions.map((caption) => ({ type: "caption" as const, id: caption.id })),
    ...addedGraphics.map((graphic) => ({ type: "graphic" as const, id: graphic.id })),
    ...addedClips.map((clip) => ({ type: "clip" as const, id: clip.id })),
    ...addedShots.map((shot) => ({ type: "shot" as const, id: shot.id })),
    ...addedMarkers.map((marker) => ({ type: "marker" as const, id: marker.id })),
  ];
  if (highlights.length === 0) {
    highlights.push(
      ...changedAudio.slice(0, 2).map((cue) => ({ type: "audio" as const, id: cue.id })),
      ...changedCaptions.slice(0, 2).map((caption) => ({ type: "caption" as const, id: caption.id })),
      ...changedGraphics.slice(0, 2).map((graphic) => ({ type: "graphic" as const, id: graphic.id })),
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
  graphicLayers: GraphicLayer[],
  audioCues: AudioCue[],
  markers: TimelineMarker[],
  duration: number,
): number[] {
  const points = new Set<number>([0, duration]);
  getClipTimelineSpans(clips).forEach((span) => {
    points.add(span.start);
    points.add(span.end);
    (span.clip.keyframes ?? []).forEach((keyframe) => points.add(span.start + keyframe.time));
  });
  shots.forEach((shot) => {
    points.add(shot.start);
    points.add(shot.end);
  });
  captions.forEach((caption) => {
    points.add(caption.start);
    points.add(caption.end);
    (caption.keyframes ?? []).forEach((keyframe) => points.add(caption.start + keyframe.time));
  });
  graphicLayers.forEach((graphic) => {
    points.add(graphic.start);
    points.add(graphic.end);
    (graphic.keyframes ?? []).forEach((keyframe) => points.add(graphic.start + keyframe.time));
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

function getAudioCueSourceWindow(cue: AudioCue, mediaDuration: number): { start: number; end: number; duration: number } {
  const safeMediaDuration = Number.isFinite(mediaDuration) && mediaDuration > 0
    ? mediaDuration
    : Math.max(0.05, cue.assetDuration ?? cue.sourceEnd ?? cue.end - cue.start);
  const start = clamp(cue.sourceStart ?? 0, 0, Math.max(0, safeMediaDuration - 0.01));
  const end = clamp(cue.sourceEnd ?? safeMediaDuration, start + 0.01, safeMediaDuration);
  return { start, end, duration: Math.max(0.01, end - start) };
}

function getAudioCueSourceTime(cue: AudioCue, cueOffset: number, mediaDuration: number): number {
  const source = getAudioCueSourceWindow(cue, mediaDuration);
  if (cue.loop) return source.start + (Math.max(0, cueOffset) % source.duration);
  return clamp(source.start + Math.max(0, cueOffset), source.start, source.end);
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

async function loadGraphicVideoRenderer(graphic: GraphicLayer): Promise<HTMLVideoElement | null> {
  if (graphic.kind !== "video" || !graphic.assetDocumentId) return null;
  const cached = graphicVideoRenderElementCache.get(graphic.id);
  if (cached?.assetId === graphic.assetDocumentId) return cached.video;
  if (cached) {
    cached.video.pause();
    cached.video.removeAttribute("src");
    cached.video.load();
    graphicVideoRenderElementCache.delete(graphic.id);
  }
  const url = await loadMediaVideoPreviewUrl(graphic.assetDocumentId);
  const video = document.createElement("video");
  video.src = url;
  video.crossOrigin = "anonymous";
  video.muted = true;
  video.playsInline = true;
  video.preload = "auto";
  video.load();
  if (video.readyState < HTMLMediaElement.HAVE_METADATA) {
    await waitForVideoEvent(video, "loadedmetadata");
  }
  graphicVideoRenderElementCache.set(graphic.id, { assetId: graphic.assetDocumentId, video });
  return video;
}

async function syncGraphicVideoRenderers(
  graphics: GraphicLayer[],
  timelineTime: number,
  realtime: boolean,
  playbackRate = 1,
) {
  const activeIds = new Set<string>();
  for (const graphic of graphics) {
    if (graphic.kind !== "video" || !graphic.assetDocumentId) continue;
    const video = await loadGraphicVideoRenderer(graphic);
    if (!video) continue;
    const active = timelineTime >= graphic.start && timelineTime <= graphic.end;
    if (!active) {
      video.pause();
      continue;
    }
    activeIds.add(graphic.id);
    const mediaDuration = Number.isFinite(video.duration) && video.duration > 0
      ? video.duration
      : graphic.assetDuration ?? 0;
    const sourceWindow = getGraphicVideoSourceWindow(graphic, mediaDuration);
    const targetTime = getGraphicVideoSourceTime(graphic, timelineTime, mediaDuration);
    const sourceElapsed = Math.max(0, timelineTime - graphic.start) * normalizeVideoClipSpeed(graphic.speed);
    const shouldHoldLastFrame = !graphic.loop && sourceElapsed >= sourceWindow.duration - 0.001;
    video.muted = true;
    video.playbackRate = normalizeVideoClipSpeed(graphic.speed) * playbackRate;
    if (!realtime || shouldHoldLastFrame) video.pause();
    const driftTolerance = realtime ? 0.12 : 0.004;
    if (Math.abs(video.currentTime - targetTime) > driftTolerance) {
      await seekVideo(video, targetTime);
    }
    if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
      await waitForVideoEvent(video, "loadeddata").catch(() => undefined);
    }
    if (realtime && !shouldHoldLastFrame) {
      if (video.paused) await video.play().catch(() => undefined);
    } else {
      await waitForDecodedVideoFrame(video);
    }
  }
  graphicVideoRenderElementCache.forEach(({ video }, graphicId) => {
    if (!activeIds.has(graphicId)) video.pause();
  });
}

function nextFrame(minimumDelayMs = 0) {
  return new Promise<void>((resolve) => {
    if (minimumDelayMs > 0) {
      window.setTimeout(resolve, minimumDelayMs);
      return;
    }
    window.requestAnimationFrame(() => resolve());
  });
}

function setMediaRecorderPaused(recorder: MediaRecorder, paused: boolean) {
  const targetState: "paused" | "recording" = paused ? "paused" : "recording";
  if (recorder.state === targetState || recorder.state === "inactive") return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const eventName = paused ? "pause" : "resume";
    const cleanup = () => {
      recorder.removeEventListener(eventName, finish);
      window.clearTimeout(timer);
    };
    const finish = () => {
      cleanup();
      resolve();
    };
    const timer = window.setTimeout(finish, 800);
    recorder.addEventListener(eventName, finish, { once: true });
    try {
      if (paused) recorder.pause();
      else recorder.resume();
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

function wrapCanvasText(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const lines: string[] = [];
  for (const paragraph of text.replace(/\r\n|\r/g, "\n").split("\n")) {
    if (!paragraph) {
      lines.push("");
      continue;
    }
    const tokens = /\s/.test(paragraph) ? paragraph.split(/(\s+)/).filter(Boolean) : Array.from(paragraph);
    let line = "";
    for (const token of tokens) {
      const candidate = `${line}${token}`;
      if (ctx.measureText(candidate).width <= maxWidth || !line) {
        line = candidate;
      } else {
        lines.push(line.trimEnd());
        line = token.trimStart();
      }
    }
    lines.push(line.trimEnd());
  }
  return lines.length > 0 ? lines : [""];
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

const svgPathSampleCache = new Map<string, Array<{ x: number; y: number }>>();

function sampledSvgPath(pathData: string): Array<{ x: number; y: number }> {
  const cached = svgPathSampleCache.get(pathData);
  if (cached) return cached;
  if (typeof document === "undefined") return [];
  try {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    const length = path.getTotalLength();
    const sampleCount = Math.round(clamp(length * 1.5, 24, 600));
    const points = Array.from({ length: sampleCount + 1 }, (_, index) => {
      const point = path.getPointAtLength((length * index) / sampleCount);
      return { x: point.x, y: point.y };
    });
    svgPathSampleCache.set(pathData, points);
    return points;
  } catch {
    return [];
  }
}

function drawCanvasPartialSvgPath(
  ctx: CanvasRenderingContext2D,
  pathData: string,
  progress: number,
) {
  const points = sampledSvgPath(pathData);
  if (points.length < 2 || progress <= 0) return false;
  const scaledIndex = clamp(progress, 0, 1) * (points.length - 1);
  const fullIndex = Math.floor(scaledIndex);
  ctx.beginPath();
  ctx.moveTo(points[0].x, points[0].y);
  for (let index = 1; index <= fullIndex; index += 1) ctx.lineTo(points[index].x, points[index].y);
  if (fullIndex < points.length - 1) {
    const fraction = scaledIndex - fullIndex;
    const left = points[fullIndex];
    const right = points[fullIndex + 1];
    ctx.lineTo(left.x + (right.x - left.x) * fraction, left.y + (right.y - left.y) * fraction);
  }
  return true;
}

function canvasGraphicFill(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  fill: string,
  visualStyle: GraphicVisualStyle,
): string | CanvasGradient {
  if (visualStyle.fillType === "radial") {
    const radius = Math.max(width, height) * 0.72;
    const gradient = ctx.createRadialGradient(0, -height * 0.08, 0, 0, 0, radius);
    gradient.addColorStop(0, fill);
    gradient.addColorStop(1, visualStyle.fillSecondary);
    return gradient;
  }
  if (visualStyle.fillType === "linear") {
    const radians = (visualStyle.gradientAngle * Math.PI) / 180;
    const dx = Math.cos(radians) * width * 0.5;
    const dy = Math.sin(radians) * height * 0.5;
    const gradient = ctx.createLinearGradient(-dx, -dy, dx, dy);
    gradient.addColorStop(0, fill);
    gradient.addColorStop(1, visualStyle.fillSecondary);
    return gradient;
  }
  return fill;
}

function drawCanvasImageFitted(
  ctx: CanvasRenderingContext2D,
  media: HTMLImageElement | HTMLVideoElement,
  width: number,
  height: number,
  fit: "contain" | "cover",
) {
  const source = media instanceof HTMLVideoElement
    ? { width: media.videoWidth, height: media.videoHeight }
    : { width: media.naturalWidth, height: media.naturalHeight };
  const fitted = fittedMediaSize({ width, height }, source, fit);
  ctx.drawImage(media, -fitted.width / 2, -fitted.height / 2, fitted.width, fitted.height);
}

function canvasBlendMode(value: GraphicVisualStyle["blendMode"]): GlobalCompositeOperation {
  if (value === "soft-light") return "soft-light";
  if (value === "multiply" || value === "screen" || value === "overlay") return value;
  return "source-over";
}

function drawCanvasMaskPath(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  shape: GraphicVisualStyle["maskShape"],
  fallbackRadius = 0,
) {
  ctx.beginPath();
  if (shape === "circle") {
    ctx.ellipse(0, 0, width / 2, height / 2, 0, 0, Math.PI * 2);
    return;
  }
  if (shape === "diamond") {
    ctx.moveTo(0, -height / 2);
    ctx.lineTo(width / 2, 0);
    ctx.lineTo(0, height / 2);
    ctx.lineTo(-width / 2, 0);
    ctx.closePath();
    return;
  }
  if (shape === "hexagon") {
    ctx.moveTo(-width * 0.25, -height / 2);
    ctx.lineTo(width * 0.25, -height / 2);
    ctx.lineTo(width / 2, 0);
    ctx.lineTo(width * 0.25, height / 2);
    ctx.lineTo(-width * 0.25, height / 2);
    ctx.lineTo(-width / 2, 0);
    ctx.closePath();
    return;
  }
  drawRoundedRect(ctx, -width / 2, -height / 2, width, height, fallbackRadius);
}

function drawCanvasGraphicEffect(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  style: GraphicVisualStyle,
  pixelScale: number,
) {
  if (style.effect === "none") return;
  const strength = style.effectStrength;
  ctx.save();
  ctx.shadowColor = "transparent";
  ctx.shadowBlur = 0;
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = 0;
  if (style.effect === "glass") {
    const sheen = ctx.createLinearGradient(-width / 2, -height / 2, width / 2, height / 2);
    sheen.addColorStop(0, `rgba(255,255,255,${0.12 + strength * 0.3})`);
    sheen.addColorStop(0.44, "rgba(255,255,255,0.015)");
    sheen.addColorStop(1, `rgba(255,255,255,${0.03 + strength * 0.08})`);
    ctx.fillStyle = sheen;
    ctx.fillRect(-width / 2, -height / 2, width, height);
    ctx.strokeStyle = `rgba(255,255,255,${0.16 + strength * 0.28})`;
    ctx.lineWidth = Math.max(1, 1.4 * pixelScale);
    ctx.strokeRect(-width / 2 + ctx.lineWidth, -height / 2 + ctx.lineWidth, width - ctx.lineWidth * 2, height - ctx.lineWidth * 2);
  } else if (style.effect === "grain") {
    ctx.globalCompositeOperation = "soft-light";
    for (let index = 0; index < 360; index += 1) {
      const x = ((((index * 47) % 101) / 100) - 0.5) * width;
      const y = ((((index * 73 + 19) % 103) / 102) - 0.5) * height;
      const size = (0.45 + ((index * 29) % 7) / 7) * Math.max(0.8, pixelScale);
      ctx.fillStyle = index % 3 === 0
        ? `rgba(255,255,255,${0.08 + strength * 0.18})`
        : `rgba(0,0,0,${0.04 + strength * 0.13})`;
      ctx.fillRect(x, y, size, size);
    }
  } else if (style.effect === "scanlines") {
    const gap = Math.max(3, Math.round((4.5 - strength * 2) * pixelScale));
    ctx.fillStyle = `rgba(0,0,0,${0.06 + strength * 0.18})`;
    for (let y = -height / 2; y < height / 2; y += gap) {
      ctx.fillRect(-width / 2, y, width, Math.max(1, pixelScale));
    }
  } else if (style.effect === "vignette") {
    const vignette = ctx.createRadialGradient(0, 0, Math.min(width, height) * 0.12, 0, 0, Math.max(width, height) * 0.7);
    vignette.addColorStop(0, "rgba(3,7,18,0)");
    vignette.addColorStop(0.58, `rgba(3,7,18,${0.04 + strength * 0.12})`);
    vignette.addColorStop(1, `rgba(3,7,18,${0.28 + strength * 0.58})`);
    ctx.fillStyle = vignette;
    ctx.fillRect(-width / 2, -height / 2, width, height);
  } else if (style.effect === "glow") {
    const glow = ctx.createRadialGradient(0, -height * 0.06, 0, 0, 0, Math.max(width, height) * 0.66);
    glow.addColorStop(0, `rgba(255,255,255,${0.08 + strength * 0.2})`);
    glow.addColorStop(1, "rgba(255,255,255,0)");
    ctx.globalCompositeOperation = "screen";
    ctx.fillStyle = glow;
    ctx.fillRect(-width / 2, -height / 2, width, height);
  } else if (style.effect === "chromatic") {
    const band = Math.max(width * 0.06, 3 * pixelScale);
    const left = ctx.createLinearGradient(-width / 2, 0, -width / 2 + band, 0);
    left.addColorStop(0, `rgba(255,35,92,${0.08 + strength * 0.24})`);
    left.addColorStop(1, "rgba(255,35,92,0)");
    ctx.fillStyle = left;
    ctx.fillRect(-width / 2, -height / 2, band, height);
    const right = ctx.createLinearGradient(width / 2 - band, 0, width / 2, 0);
    right.addColorStop(0, "rgba(0,220,255,0)");
    right.addColorStop(1, `rgba(0,220,255,${0.08 + strength * 0.24})`);
    ctx.fillStyle = right;
    ctx.fillRect(width / 2 - band, -height / 2, band, height);
  } else if (style.effect === "lightLeak") {
    ctx.globalCompositeOperation = "screen";
    const leak = ctx.createRadialGradient(-width * 0.42, height * 0.02, 0, -width * 0.42, height * 0.02, width * 0.82);
    leak.addColorStop(0, `rgba(255,244,207,${0.34 + strength * 0.5})`);
    leak.addColorStop(0.24, `rgba(255,151,76,${0.2 + strength * 0.38})`);
    leak.addColorStop(0.62, `rgba(255,73,30,${0.04 + strength * 0.12})`);
    leak.addColorStop(1, "rgba(255,73,30,0)");
    ctx.fillStyle = leak;
    ctx.fillRect(-width / 2, -height / 2, width, height);
    const crown = ctx.createRadialGradient(-width * 0.1, -height * 0.46, 0, -width * 0.1, -height * 0.46, width * 0.5);
    crown.addColorStop(0, `rgba(255,205,126,${0.12 + strength * 0.26})`);
    crown.addColorStop(1, "rgba(255,112,54,0)");
    ctx.fillStyle = crown;
    ctx.fillRect(-width / 2, -height / 2, width, height);
  } else if (style.effect === "filmBurn") {
    ctx.globalCompositeOperation = "screen";
    const burn = ctx.createRadialGradient(-width * 0.58, height * 0.04, 0, -width * 0.58, height * 0.04, width * 1.02);
    burn.addColorStop(0, `rgba(255,251,220,${0.54 + strength * 0.4})`);
    burn.addColorStop(0.2, `rgba(255,159,63,${0.34 + strength * 0.44})`);
    burn.addColorStop(0.46, `rgba(201,47,18,${0.12 + strength * 0.3})`);
    burn.addColorStop(0.76, `rgba(65,8,4,${0.02 + strength * 0.06})`);
    burn.addColorStop(1, "rgba(65,8,4,0)");
    ctx.fillStyle = burn;
    ctx.fillRect(-width / 2, -height / 2, width, height);
  } else if (style.effect === "halation") {
    ctx.globalCompositeOperation = "screen";
    const halo = ctx.createRadialGradient(0, 0, Math.min(width, height) * 0.04, 0, 0, Math.max(width, height) * 0.64);
    halo.addColorStop(0, `rgba(255,248,223,${0.1 + strength * 0.22})`);
    halo.addColorStop(0.32, `rgba(255,116,58,${0.08 + strength * 0.22})`);
    halo.addColorStop(1, "rgba(255,58,29,0)");
    ctx.fillStyle = halo;
    ctx.fillRect(-width / 2, -height / 2, width, height);
  } else if (style.effect === "anamorphic") {
    ctx.globalCompositeOperation = "screen";
    const flare = ctx.createRadialGradient(width * 0.16, 0, 0, width * 0.16, 0, width * 0.34);
    flare.addColorStop(0, `rgba(255,248,218,${0.4 + strength * 0.46})`);
    flare.addColorStop(0.14, `rgba(255,171,89,${0.14 + strength * 0.28})`);
    flare.addColorStop(1, "rgba(255,91,38,0)");
    ctx.fillStyle = flare;
    ctx.fillRect(-width / 2, -height / 2, width, height);
    const bandHeight = Math.max(1, (1.5 + strength * 2.5) * pixelScale);
    const band = ctx.createLinearGradient(-width / 2, 0, width / 2, 0);
    band.addColorStop(0, "rgba(255,113,51,0)");
    band.addColorStop(0.42, `rgba(255,181,96,${0.08 + strength * 0.18})`);
    band.addColorStop(0.66, `rgba(255,247,220,${0.24 + strength * 0.42})`);
    band.addColorStop(1, "rgba(255,113,51,0)");
    ctx.fillStyle = band;
    ctx.fillRect(-width / 2, -bandHeight / 2, width, bandHeight);
  }
  ctx.restore();
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
  disabled = false,
  onChange,
}: {
  label: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
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
        disabled={disabled}
        onChange={(event) => onChange(Number(event.currentTarget.value))}
      />
    </div>
  );
}

function MotionKeyframeControls({
  keyframe,
  maxTime,
  disabled = false,
  onTimeChange,
  onKeyframePatch,
}: {
  keyframe: MotionKeyframe;
  maxTime: number;
  disabled?: boolean;
  onTimeChange: (time: number) => void;
  onKeyframePatch: (patch: Partial<Pick<MotionKeyframe, "easing" | "bezier" | "spatial">>) => void;
}) {
  const [draggingBezierHandle, setDraggingBezierHandle] = useState<"first" | "second" | null>(null);
  const bezier = normalizeMotionBezier(keyframe.bezier);
  const curvePoints = Array.from({ length: 41 }, (_, index) => {
    const progress = index / 40;
    const x = 8 + progress * 104;
    const y = 68 - motionEasingProgress(keyframe.easing, progress, bezier) * 40;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  const frameNumber = motionFrameNumberAtTime(keyframe.time);
  const easingOptions = MOTION_EASINGS.map((value) => ({
    value,
    label: veText(`motion.easing.${value}`),
  }));
  const spatialOptions = (["linear", "smooth"] as MotionPathMode[]).map((value) => ({
    value,
    label: veText(`motion.spatial.${value}`),
  }));
  const updateBezierHandle = (event: ReactPointerEvent<SVGCircleElement>, handle: "first" | "second") => {
    const svg = event.currentTarget.ownerSVGElement;
    if (!svg || disabled) return;
    const bounds = svg.getBoundingClientRect();
    if (bounds.width <= 0 || bounds.height <= 0) return;
    const viewBoxX = ((event.clientX - bounds.left) / bounds.width) * 120;
    const viewBoxY = -12 + ((event.clientY - bounds.top) / bounds.height) * 120;
    const x = Math.min(1, Math.max(0, (viewBoxX - 8) / 104));
    const y = Math.min(2, Math.max(-1, (68 - viewBoxY) / 40));
    onKeyframePatch({
      easing: "custom",
      bezier: normalizeMotionBezier(handle === "first"
        ? { ...bezier, x1: x, y1: y }
        : { ...bezier, x2: x, y2: y }),
    });
  };

  return (
    <div className="ve-motion-keyframe-controls">
      <div className="ve-two-col">
        <NumberField
          label={veText("motion.keyframe_time")}
          value={Number(keyframe.time.toFixed(3))}
          min={0}
          max={maxTime}
          step={1 / VIDEO_EDITOR_FPS}
          disabled={disabled}
          onChange={onTimeChange}
        />
        <div className="ve-field">
          <FieldLabel>{veText("motion.easing")}</FieldLabel>
          <Select
            value={keyframe.easing}
            onChange={(value) => onKeyframePatch({ easing: value as MotionEasing })}
            options={easingOptions}
            disabled={disabled}
            ariaLabel={veText("motion.easing")}
            buttonStyle={{ boxShadow: "none" }}
          />
        </div>
      </div>
      <div
        className="ve-motion-curve"
        role="img"
        aria-label={veText("motion.curve_preview_aria", {
          easing: veText(`motion.easing.${keyframe.easing}`),
        })}
      >
        <svg viewBox="0 -12 120 120" preserveAspectRatio="none" aria-hidden="true">
          <path className="ve-motion-curve-grid" d="M8 28H112M8 48H112M8 68H112M8 28V68M60 28V68M112 28V68" />
          {keyframe.easing === "custom" && (
            <path
              className="ve-motion-bezier-guides"
              d={`M8 68L${8 + bezier.x1 * 104} ${68 - bezier.y1 * 40}M112 28L${8 + bezier.x2 * 104} ${68 - bezier.y2 * 40}`}
            />
          )}
          <polyline points={curvePoints} />
          <circle cx="8" cy="68" r="2.5" />
          <circle cx="112" cy="28" r="2.5" />
          {keyframe.easing === "custom" && (
            <>
              <circle
                className="ve-motion-bezier-handle"
                cx={8 + bezier.x1 * 104}
                cy={68 - bezier.y1 * 40}
                r="4"
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  setDraggingBezierHandle("first");
                  updateBezierHandle(event, "first");
                }}
                onPointerMove={(event) => {
                  if (draggingBezierHandle === "first") updateBezierHandle(event, "first");
                }}
                onPointerUp={() => setDraggingBezierHandle(null)}
                onPointerCancel={() => setDraggingBezierHandle(null)}
              />
              <circle
                className="ve-motion-bezier-handle"
                cx={8 + bezier.x2 * 104}
                cy={68 - bezier.y2 * 40}
                r="4"
                onPointerDown={(event) => {
                  event.preventDefault();
                  event.currentTarget.setPointerCapture(event.pointerId);
                  setDraggingBezierHandle("second");
                  updateBezierHandle(event, "second");
                }}
                onPointerMove={(event) => {
                  if (draggingBezierHandle === "second") updateBezierHandle(event, "second");
                }}
                onPointerUp={() => setDraggingBezierHandle(null)}
                onPointerCancel={() => setDraggingBezierHandle(null)}
              />
            </>
          )}
        </svg>
        <div>
          <strong>{veText(`motion.easing.${keyframe.easing}`)}</strong>
          <span>{veText(`motion.easing_hint.${keyframe.easing}`)}</span>
          <small>{veText("motion.frame_number", { frame: frameNumber, fps: VIDEO_EDITOR_FPS })}</small>
        </div>
      </div>
      {keyframe.easing === "custom" && (
        <div className="ve-motion-bezier-fields">
          {(["x1", "y1", "x2", "y2"] as const).map((field) => (
            <NumberField
              key={field}
              label={field.toUpperCase()}
              value={Number(bezier[field].toFixed(3))}
              min={field.startsWith("x") ? 0 : -1}
              max={field.startsWith("x") ? 1 : 2}
              step={0.05}
              disabled={disabled}
              onChange={(value) => onKeyframePatch({
                easing: "custom",
                bezier: normalizeMotionBezier({ ...bezier, [field]: value }),
              })}
            />
          ))}
        </div>
      )}
      <div className="ve-field">
        <FieldLabel>{veText("motion.spatial")}</FieldLabel>
        <Select
          value={keyframe.spatial ?? "linear"}
          onChange={(value) => onKeyframePatch({ spatial: value as MotionPathMode })}
          options={spatialOptions}
          disabled={disabled}
          ariaLabel={veText("motion.spatial")}
          buttonStyle={{ boxShadow: "none" }}
        />
        <span className="ve-field-note">{veText(`motion.spatial_hint.${keyframe.spatial ?? "linear"}`)}</span>
      </div>
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
  const previewRef = useRef<HTMLElement | null>(null);
  const timelineScrollRef = useRef<HTMLDivElement | null>(null);
  const mediaFileInputRef = useRef<HTMLInputElement | null>(null);
  const audioFileInputRef = useRef<HTMLInputElement | null>(null);
  const replacementVideoInputRef = useRef<HTMLInputElement | null>(null);
  const subtitleFileInputRef = useRef<HTMLInputElement | null>(null);
  const graphicFileInputRef = useRef<HTMLInputElement | null>(null);
  const graphicVideoFileInputRef = useRef<HTMLInputElement | null>(null);
  const graphicVideoRefs = useRef<Map<string, HTMLVideoElement>>(new Map());
  const previewAudioRef = useRef<Map<string, PreviewAudioEntry>>(new Map());
  const mediaAssetPreviewRef = useRef<{ assetId: string; audio: HTMLAudioElement; url: string } | null>(null);
  const mediaAssetPreviewRequestRef = useRef(0);
  const freeMusicPreviewRef = useRef<{ trackId: string; audio: HTMLAudioElement } | null>(null);
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
  const previewLoopRef = useRef(false);
  const loopRestartRef = useRef<(() => void) | null>(null);
  const initializedDocRef = useRef<string | null>(null);
  const durationProbeUrlRef = useRef<string | null>(null);
  const autoProjectTimelineRef = useRef<string | null>(null);
  const undoStackRef = useRef<EditorTrackState[]>([]);
  const redoStackRef = useRef<EditorTrackState[]>([]);
  const lastHistoryStateRef = useRef<EditorTrackState | null>(null);
  const latestTrackStateRef = useRef<EditorTrackState | null>(null);
  const editorLiveContentRef = useRef("");
  const historyTransactionRef = useRef<EditorHistoryTransaction | null>(null);
  const restoringHistoryRef = useRef(false);

  const [downloadUrl, setDownloadUrl] = useState("");
  const [previewSourceUrl, setPreviewSourceUrl] = useState("");
  const [sourceLoading, setSourceLoading] = useState(false);
  const [sourceDuration, setSourceDuration] = useState(0);
  const [mediaSize, setMediaSize] = useState({ width: 1920, height: 1080 });
  const [previewViewportSize, setPreviewViewportSize] = useState({ width: 0, height: 0 });
  const [clips, setClips] = useState<ClipSegment[]>([]);
  const [shotBeats, setShotBeats] = useState<ShotBeat[]>([]);
  const [captions, setCaptions] = useState<CaptionCue[]>([]);
  const [graphicLayers, setGraphicLayers] = useState<GraphicLayer[]>([]);
  const [audioCues, setAudioCues] = useState<AudioCue[]>([]);
  const [markers, setMarkers] = useState<TimelineMarker[]>([]);
  const [selection, setSelection] = useState<Selection>(null);
  const [draggingClipId, setDraggingClipId] = useState<string | null>(null);
  const [previewingMediaAssetId, setPreviewingMediaAssetId] = useState<string | null>(null);
  const [mediaAssetPreviewLoadingId, setMediaAssetPreviewLoadingId] = useState<string | null>(null);
  const [playhead, setPlayhead] = useState(0);
  const [timelineTool, setTimelineTool] = useState<TimelineTool>("select");
  const [activeMotionKeyframeId, setActiveMotionKeyframeId] = useState<string | null>(null);
  const [autoRecordMotion, setAutoRecordMotion] = useState(false);
  const [razorPreview, setRazorPreview] = useState<{ clipId: string; percent: number } | null>(null);
  const [snapEnabled, setSnapEnabled] = useState(true);
  const [nudgeStep, setNudgeStep] = useState(0.1);
  const [timelinePixelsPerSecond, setTimelinePixelsPerSecond] = useState(48);
  const [timelineViewportWidth, setTimelineViewportWidth] = useState(0);
  const [trackStates, setTrackStates] = useState<Record<TimelineTrackId, TimelineTrackState>>(() => createDefaultTimelineTrackStates());
  const [workArea, setWorkArea] = useState<WorkAreaState>(() => createDefaultWorkArea());
  const [isPlaying, setIsPlaying] = useState(false);
  const [previewMuted, setPreviewMuted] = useState(false);
  const [previewLoop, setPreviewLoop] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [previewFullscreen, setPreviewFullscreen] = useState(false);
  const [mediaPanelOpen, setMediaPanelOpen] = useState(true);
  const [inspectorPanelOpen, setInspectorPanelOpen] = useState(true);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [historyVersion, setHistoryVersion] = useState(0);
  const [mediaSearch, setMediaSearch] = useState("");
  const [mediaSourceTab, setMediaSourceTab] = useState<MediaSourceTab>("project");
  const [mediaKindFilter, setMediaKindFilter] = useState<MediaKindFilter>("all");
  const [mediaPickerOpen, setMediaPickerOpen] = useState(false);
  const [unifiedMediaInsertOpen, setUnifiedMediaInsertOpen] = useState(false);
  const [mediaPickerSelectedIds, setMediaPickerSelectedIds] = useState<string[]>([]);
  const [freeMusicOpen, setFreeMusicOpen] = useState(false);
  const [freeMusicSearch, setFreeMusicSearch] = useState("cinematic");
  const [submittedFreeMusicSearch, setSubmittedFreeMusicSearch] = useState("cinematic");
  const [previewingFreeMusicId, setPreviewingFreeMusicId] = useState<string | null>(null);
  const [freeMusicPreviewProgress, setFreeMusicPreviewProgress] = useState<FreeMusicPreviewProgress | null>(null);
  const [importingFreeMusicId, setImportingFreeMusicId] = useState<string | null>(null);
  const addedFreeMusicIds = useMemo(
    () => new Set(audioCues.flatMap((cue) => cue.catalogTrackId ? [cue.catalogTrackId] : [])),
    [audioCues],
  );
  const [mediaDropActive, setMediaDropActive] = useState(false);
  const [clipDropPreview, setClipDropPreview] = useState<ClipDropPreview | null>(null);
  const [importingMedia, setImportingMedia] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadingRecipe, setLoadingRecipe] = useState(false);
  const [uploadingAudio, setUploadingAudio] = useState(false);
  const [uploadingVideo, setUploadingVideo] = useState(false);
  const [uploadingGraphic, setUploadingGraphic] = useState(false);
  const [uploadingGraphicVideo, setUploadingGraphicVideo] = useState(false);
  const [graphicAssetRevision, setGraphicAssetRevision] = useState(0);
  const [savingSubtitles, setSavingSubtitles] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState(0);
  const [lastExportDoc, setLastExportDoc] = useState<Document | null>(null);
  const [recipeDoc, setRecipeDoc] = useState<Document | null>(null);
  const [sourceDocOverride, setSourceDocOverride] = useState<Document | null>(null);
  const [pendingRouteRecipe, setPendingRouteRecipe] = useState<VideoEditRecipe | null>(null);
  const [aiEditNotice, setAiEditNotice] = useState<AiEditNotice | null>(null);
  const [scanningProjectRecipe, setScanningProjectRecipe] = useState(false);

  useEffect(() => {
    setAiEditNotice(null);
    setActiveMotionKeyframeId(null);
    setAutoRecordMotion(false);
  }, [docId]);
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
      for (const surface of graphicShaderRenderCache.values()) disposeVideoEditorShaderSurface(surface);
      graphicShaderRenderCache.clear();
    };
  }, []);

  useEffect(() => {
    const element = previewRef.current;
    if (!element) return undefined;
    const update = () => {
      const next = { width: element.clientWidth, height: element.clientHeight };
      setPreviewViewportSize((current) => current.width === next.width && current.height === next.height ? current : next);
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [downloadUrl]);

  useEffect(() => {
    playheadRef.current = playhead;
  }, [playhead]);

  useEffect(() => {
    previewLoopRef.current = previewLoop;
  }, [previewLoop]);

  useEffect(() => {
    const video = videoRef.current;
    const mapped = activePlaybackMapRef.current ?? mapTimelineTime(playheadRef.current, clips);
    if (video) {
      video.playbackRate = previewingMediaAssetId || !mapped
        ? playbackRate
        : clipPreviewPlaybackRate(mapped.clip, playbackRate);
    }
    previewAudioRef.current.forEach(({ audio }) => {
      audio.playbackRate = playbackRate;
    });
    if (mediaAssetPreviewRef.current) {
      mediaAssetPreviewRef.current.audio.playbackRate = playbackRate;
      mediaAssetPreviewRef.current.audio.muted = previewMuted;
    }
  }, [clips, playbackRate, previewMuted, previewingMediaAssetId]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      setPreviewFullscreen(document.fullscreenElement === previewRef.current);
    };
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

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
  const freeMusicQuery = useQuery({
    queryKey: ["video-editor-free-music", submittedFreeMusicSearch],
    queryFn: async () => {
      const result = await api.videoEditor.searchFreeMusic(submittedFreeMusicSearch, 1, 20);
      if (result.items.length > 0) return result;
      const fallbackQuery = submittedFreeMusicSearch.split(/\s+/, 1)[0] || submittedFreeMusicSearch;
      if (fallbackQuery === submittedFreeMusicSearch) return result;
      return api.videoEditor.searchFreeMusic(fallbackQuery, 1, 20);
    },
    enabled: freeMusicOpen && submittedFreeMusicSearch.trim().length >= 2,
    staleTime: 5 * 60_000,
    retry: 1,
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
    () => clips.reduce((total, clip) => total + getClipTimelineDuration(clip), 0),
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
  const previewMediaSize = useMemo(
    () => containedMediaSize(previewViewportSize, mediaSize),
    [mediaSize, previewViewportSize],
  );
  const exportRangeStart = normalizedWorkArea.enabled ? normalizedWorkArea.start : 0;
  const exportRangeEnd = normalizedWorkArea.enabled ? normalizedWorkArea.end : timelineDuration;
  const exportRangeDuration = Math.max(0, exportRangeEnd - exportRangeStart);
  const workAreaLeft = timelineDuration ? TIMELINE_LABEL_COLUMN_WIDTH + (normalizedWorkArea.start / timelineDuration) * timelineLaneWidth : TIMELINE_LABEL_COLUMN_WIDTH;
  const workAreaWidth = timelineDuration ? ((normalizedWorkArea.end - normalizedWorkArea.start) / timelineDuration) * timelineLaneWidth : 0;
  const currentTrackState = useMemo<EditorTrackState>(() => ({
    clips,
    shotBeats,
    captions,
    graphicLayers,
    audioCues,
    markers,
  }), [audioCues, captions, clips, graphicLayers, markers, shotBeats]);
  const serializedTrackState = useMemo(() => serializeEditorTrackState(currentTrackState), [currentTrackState]);
  const canUndo = useMemo(() => undoStackRef.current.length > 0, [historyVersion]);
  const canRedo = useMemo(() => redoStackRef.current.length > 0, [historyVersion]);
  const selectedClip = selection?.type === "clip" ? clips.find((clip) => clip.id === selection.id) ?? null : null;
  const selectedClipIndex = selectedClip ? clips.findIndex((clip) => clip.id === selectedClip.id) : -1;
  const selectedClipSpan = selectedClip ? videoClipSpans.find((span) => span.clip.id === selectedClip.id) ?? null : null;
  const selectedShot = selection?.type === "shot" ? shotBeats.find((shot) => shot.id === selection.id) ?? null : null;
  const selectedCaption = selection?.type === "caption" ? captions.find((caption) => caption.id === selection.id) ?? null : null;
  const selectedGraphic = selection?.type === "graphic" ? graphicLayers.find((graphic) => graphic.id === selection.id) ?? null : null;
  const selectedTimedMotionLayer = selectedShot ?? selectedCaption ?? selectedGraphic;
  const selectedMotionLayer = selectedClip ?? selectedTimedMotionLayer;
  const selectedMotionKeyframes = useMemo(() => selectedMotionLayer?.keyframes ?? [], [selectedMotionLayer]);
  const activeMotionKeyframe = selectedMotionKeyframes.find((keyframe) => keyframe.id === activeMotionKeyframeId) ?? null;
  const selectedClipContainsPlayhead = Boolean(
    selectedClipSpan && playhead >= selectedClipSpan.start && playhead <= selectedClipSpan.end,
  );
  const selectedTimedMotionLayerContainsPlayhead = Boolean(
    selectedTimedMotionLayer && playhead >= selectedTimedMotionLayer.start && playhead <= selectedTimedMotionLayer.end,
  );
  const selectedMotionLayerContainsPlayhead = Boolean(
    selectedClip ? selectedClipContainsPlayhead : selectedTimedMotionLayerContainsPlayhead,
  );
  const selectedCaptionContainsPlayhead = Boolean(
    selectedCaption && playhead >= selectedCaption.start && playhead <= selectedCaption.end,
  );
  const selectedMotionLayerStart = selectedClipSpan?.start ?? selectedTimedMotionLayer?.start ?? 0;
  const motionKeyframeAtPlayhead = selectedMotionLayer && selectedMotionLayerContainsPlayhead
    ? selectedMotionKeyframes.find((keyframe) => Math.abs((selectedMotionLayerStart + keyframe.time) - playhead) <= 0.005) ?? null
    : null;
  const selectedClipMotionPose = selectedClip
    ? activeMotionKeyframe ?? (autoRecordMotion && selectedClipContainsPlayhead && selectedClipSpan
      ? clipMotionPoseAtLocalTime(selectedClip, playhead - selectedClipSpan.start)
      : clipBaseMotionPose(selectedClip))
    : null;
  const selectedShotMotionPose = selectedShot
    ? activeMotionKeyframe ?? (autoRecordMotion && selectedTimedMotionLayerContainsPlayhead
      ? shotMotionPoseAtTimelineTime(selectedShot, playhead)
      : shotBaseMotionPose(selectedShot))
    : null;
  const selectedCaptionMotionPose = selectedCaption
    ? activeMotionKeyframe ?? (autoRecordMotion && selectedCaptionContainsPlayhead
      ? captionMotionPoseAtTimelineTime(selectedCaption, playhead)
      : captionBaseMotionPose(selectedCaption))
    : null;
  const selectedGraphicMotionPose = selectedGraphic
    ? activeMotionKeyframe ?? (autoRecordMotion && selectedTimedMotionLayerContainsPlayhead
      ? graphicMotionPoseAtTimelineTime(selectedGraphic, playhead)
      : graphicBaseMotionPose(selectedGraphic))
    : null;
  const selectedMotionPath = useMemo(() => {
    if (selectedClip && selectedClip.keyframes.length > 1) {
      return motionPathSamples(selectedClip.keyframes, clipBaseMotionPose(selectedClip), 18);
    }
    if (selectedShot && selectedShot.keyframes.length > 1) {
      return motionPathSamples(selectedShot.keyframes, shotBaseMotionPose(selectedShot), 18);
    }
    if (selectedGraphic && selectedGraphic.keyframes.length > 1) {
      return motionPathSamples(selectedGraphic.keyframes, graphicBaseMotionPose(selectedGraphic), 18);
    }
    if (selectedCaption && selectedCaption.keyframes.length > 1) {
      return motionPathSamples(selectedCaption.keyframes, captionBaseMotionPose(selectedCaption), 18);
    }
    return [];
  }, [selectedCaption, selectedClip, selectedGraphic, selectedShot]);
  const selectedMotionPathPose = selectedClip && selectedClipSpan && selectedClipContainsPlayhead
    ? clipMotionPoseAtLocalTime(selectedClip, playhead - selectedClipSpan.start)
    : selectedGraphic && selectedTimedMotionLayerContainsPlayhead
      ? graphicMotionPoseAtTimelineTime(selectedGraphic, playhead)
      : selectedCaption && selectedCaptionContainsPlayhead
        ? captionMotionPoseAtTimelineTime(selectedCaption, playhead)
        : selectedShot && selectedTimedMotionLayerContainsPlayhead
          ? shotMotionPoseAtTimelineTime(selectedShot, playhead)
          : null;
  const selectedAudio = selection?.type === "audio" ? audioCues.find((cue) => cue.id === selection.id) ?? null : null;
  const selectedMarker = selection?.type === "marker" ? markers.find((marker) => marker.id === selection.id) ?? null : null;
  const selectedTrackId = selection ? trackForSelectionType(selection.type) : null;
  const selectedTrackLocked = selectedTrackId ? trackStates[selectedTrackId].locked : false;
  const timelineSnapPoints = useMemo(
    () => getTimelineSnapPoints(clips, shotBeats, captions, graphicLayers, audioCues, markers, timelineDuration),
    [audioCues, captions, clips, graphicLayers, markers, shotBeats, timelineDuration],
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
  const activeSceneGroup = useMemo(
    () => shotBeats.find((shot) => playhead >= shot.start && playhead <= shot.end) ?? null,
    [playhead, shotBeats],
  );
  const activeSceneGroupPose = activeSceneGroup
    ? shotMotionPoseAtTimelineTime(activeSceneGroup, playhead)
    : null;
  const graphicGroupIds = useMemo(
    () => new Set(graphicLayers.filter((graphic) => graphic.kind === "group").map((graphic) => graphic.id)),
    [graphicLayers],
  );
  const activeCaptions = useMemo(
    () => trackStates.captions.visible
      ? captions.filter((caption) => playhead >= caption.start
        && playhead <= caption.end
        && (!caption.parentId || !graphicGroupIds.has(caption.parentId)))
      : [],
    [captions, graphicGroupIds, playhead, trackStates.captions.visible],
  );
  const activeGraphicLayers = useMemo(
    () => trackStates.graphics.visible
      ? graphicLayers
        .filter((graphic) => playhead >= graphic.start
          && playhead <= graphic.end
          && !graphic.isTemplate
          && (!graphic.parentId || !graphicGroupIds.has(graphic.parentId)))
        .sort((left, right) => layerZIndex(left) - layerZIndex(right))
      : [],
    [graphicGroupIds, graphicLayers, playhead, trackStates.graphics.visible],
  );
  const activeVideoMap = useMemo(
    () => previewingMediaAssetId ? null : mapTimelineTime(playhead, clips),
    [clips, playhead, previewingMediaAssetId],
  );
  const activeVideoMotionPose = activeVideoMap
    ? composeMotionPoses(
      clipMotionPoseAtLocalTime(activeVideoMap.clip, playhead - activeVideoMap.timelineStart),
      activeSceneGroupPose,
    )
    : { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 };
  const activeVideoEdgeFadeOpacity = activeVideoMap
    ? clipEdgeFadeOpacityAtLocalTime(activeVideoMap.clip, playhead - activeVideoMap.timelineStart)
    : 1;
  const graphicImageAssetIds = useMemo(
    () => graphicLayers
      .filter((graphic) => graphic.kind === "image" && graphic.assetDocumentId)
      .map((graphic) => graphic.assetDocumentId as string)
      .filter((id, index, ids) => ids.indexOf(id) === index),
    [graphicLayers],
  );
  const graphicImageAssetKey = graphicImageAssetIds.join("|");
  const graphicImageUrls = useMemo(() => {
    void graphicAssetRevision;
    return new Map(graphicImageAssetIds.map((id) => [id, graphicImageUrlCache.get(id) ?? ""]));
  }, [graphicAssetRevision, graphicImageAssetKey]);
  useEffect(() => {
    let cancelled = false;
    if (graphicImageAssetIds.length === 0) return undefined;
    void Promise.allSettled(graphicImageAssetIds.map((id) => loadGraphicImageAsset(id)))
      .then(() => {
        if (!cancelled) setGraphicAssetRevision((version) => version + 1);
      });
    return () => {
      cancelled = true;
    };
  }, [graphicImageAssetKey]);
  const graphicVideoAssetIds = useMemo(
    () => graphicLayers
      .filter((graphic) => graphic.kind === "video" && graphic.assetDocumentId)
      .map((graphic) => graphic.assetDocumentId as string)
      .filter((id, index, ids) => ids.indexOf(id) === index),
    [graphicLayers],
  );
  const graphicVideoAssetKey = graphicVideoAssetIds.join("|");
  const graphicVideoUrls = useMemo(() => {
    void graphicAssetRevision;
    return new Map(graphicVideoAssetIds.map((id) => [id, mediaVideoPreviewUrlCache.get(id) ?? ""]));
  }, [graphicAssetRevision, graphicVideoAssetKey]);
  useEffect(() => {
    let cancelled = false;
    if (graphicVideoAssetIds.length === 0) return undefined;
    void Promise.allSettled(graphicVideoAssetIds.map((id) => loadGraphicVideoAsset(id)))
      .then(() => {
        if (!cancelled) setGraphicAssetRevision((version) => version + 1);
      });
    return () => {
      cancelled = true;
    };
  }, [graphicVideoAssetKey]);
  useEffect(() => {
    const liveGraphicIds = new Set(graphicLayers.filter((graphic) => graphic.kind === "video").map((graphic) => graphic.id));
    graphicVideoRenderElementCache.forEach(({ video }, graphicId) => {
      if (liveGraphicIds.has(graphicId)) return;
      video.pause();
      video.removeAttribute("src");
      video.load();
      graphicVideoRenderElementCache.delete(graphicId);
    });
  }, [graphicLayers]);
  useEffect(() => {
    graphicVideoRefs.current.forEach((video, graphicId) => {
      const graphic = graphicLayers.find((item) => item.id === graphicId && item.kind === "video");
      const active = Boolean(graphic && playhead >= graphic.start && playhead <= graphic.end);
      if (!graphic || !active) {
        video.pause();
        return;
      }
      const mediaDuration = Number.isFinite(video.duration) && video.duration > 0
        ? video.duration
        : graphic.assetDuration ?? 0;
      const sourceWindow = getGraphicVideoSourceWindow(graphic, mediaDuration);
      const targetTime = getGraphicVideoSourceTime(graphic, playhead, mediaDuration);
      const sourceElapsed = Math.max(0, playhead - graphic.start) * normalizeVideoClipSpeed(graphic.speed);
      const shouldHoldLastFrame = !graphic.loop && sourceElapsed >= sourceWindow.duration - 0.001;
      video.muted = true;
      video.playbackRate = normalizeVideoClipSpeed(graphic.speed) * playbackRate;
      if (Math.abs(video.currentTime - targetTime) > (isPlaying ? 0.14 : 0.012)) {
        try {
          video.currentTime = targetTime;
        } catch {
          // Metadata may still be loading; onLoadedMetadata will resync.
        }
      }
      if (isPlaying && !shouldHoldLastFrame) {
        if (video.paused) void video.play().catch(() => undefined);
      } else {
        video.pause();
      }
    });
  }, [graphicLayers, isPlaying, playbackRate, playhead]);
  useEffect(() => {
    if (activeMotionKeyframeId && !selectedMotionKeyframes.some((keyframe) => keyframe.id === activeMotionKeyframeId)) {
      setActiveMotionKeyframeId(null);
    }
  }, [activeMotionKeyframeId, selectedMotionKeyframes]);
  useEffect(() => {
    if (!activeMotionKeyframe) return;
    const keyframeTimelineTime = selectedMotionLayerStart + activeMotionKeyframe.time;
    if (Math.abs(keyframeTimelineTime - playhead) > 0.005) setActiveMotionKeyframeId(null);
  }, [activeMotionKeyframe, playhead, selectedMotionLayerStart]);
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
    const missingGraphicAssetCount = graphicLayers.filter((graphic) => (
      (graphic.kind === "image" || graphic.kind === "video") && !graphic.assetDocumentId
    )).length;
    if (missingGraphicAssetCount > 0) {
      issues.push({
        id: "media-overlay-without-asset",
        tone: "warning",
        label: veText("issue.media_overlay_without_asset.label", { count: missingGraphicAssetCount }),
        detail: veText("issue.media_overlay_without_asset.detail"),
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
    if (trackStates.graphics.visible === false && graphicLayers.length > 0) {
      issues.push({
        id: "graphics-track-hidden",
        tone: "info",
        label: veText("issue.graphics_hidden.label"),
        detail: veText("issue.graphics_hidden.detail"),
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
  }, [audioCues, captions, clips, exportRangeDuration, exportRangeEnd, exportRangeStart, graphicLayers, normalizedWorkArea.enabled, timelineDuration, trackStates.audio.muted, trackStates.captions.visible, trackStates.graphics.visible, trackStates.video.visible]);
  const renderBlockers = renderIssues.filter((issue) => issue.tone === "blocker");
  const renderWarnings = renderIssues.filter((issue) => issue.tone === "warning");
  const nudgeStepOptions = [
    { value: String(1 / VIDEO_EDITOR_FPS), label: "1f" },
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
  const captionFontOptions = [
    { value: "sans", label: "Sans" },
    { value: "display", label: "Display" },
    { value: "mono", label: "Mono" },
  ];
  const captionRevealOptions = [
    { value: "none", label: "None" },
    { value: "words", label: "Words" },
    { value: "characters", label: "Characters" },
    { value: "wipe", label: "Wipe" },
  ];
  const captionTransformOptions = [
    { value: "none", label: "As typed" },
    { value: "uppercase", label: "UPPERCASE" },
    { value: "lowercase", label: "lowercase" },
  ];
  const graphicKindOptions = (["group", "rectangle", "ellipse", "line", "path", "particle", "shader"] as GraphicLayerKind[]).map((value) => ({
    value,
    label: graphicKindDisplayLabel(value),
  }));
  const selectedGraphicDescendantIds = new Set<string>();
  if (selectedGraphic?.kind === "group") {
    const queue = [selectedGraphic.id];
    while (queue.length > 0) {
      const parentId = queue.shift();
      graphicLayers.forEach((graphic) => {
        if (graphic.parentId !== parentId || selectedGraphicDescendantIds.has(graphic.id)) return;
        selectedGraphicDescendantIds.add(graphic.id);
        queue.push(graphic.id);
      });
    }
  }
  const graphicGroupOptions = [
    { value: "", label: veText("group.canvas") },
    ...graphicLayers
      .filter((graphic) => graphic.kind === "group"
        && graphic.id !== selectedGraphic?.id
        && !selectedGraphicDescendantIds.has(graphic.id))
      .map((graphic) => ({ value: graphic.id, label: graphic.label })),
  ];
  const reusableGroupOptions = [
    { value: "", label: veText("group.own_children") },
    ...graphicLayers
      .filter((graphic) => graphic.kind === "group" && graphic.isTemplate && graphic.id !== selectedGraphic?.id)
      .map((graphic) => ({ value: graphic.id, label: graphic.label })),
  ];
  const shaderPresetOptions = VIDEO_EDITOR_SHADER_PRESETS.map((value) => ({
    value,
    label: ({
      domainWarp: "Domain warp",
      liquidMetal: "Liquid metal",
      volumetricFog: "Volumetric fog",
      chromaticTunnel: "Chromatic tunnel",
      inkBloom: "Ink bloom",
      filmBurn: "Film burn",
      prismaticBurst: "Prismatic burst",
      parallaxGrid: "Parallax grid",
    } satisfies Record<VideoEditorShaderStyle["shaderPreset"], string>)[value],
  }));
  const particleShapeOptions = PARTICLE_SHAPES.map((value) => ({
    value,
    label: veText(`particle_shape.${value}`),
  }));
  const particleMotionOptions = PARTICLE_MOTIONS.map((value) => ({
    value,
    label: veText(`particle_motion.${value}`),
  }));
  const graphicFillOptions = [
    { value: "solid", label: "Solid" },
    { value: "linear", label: "Linear gradient" },
    { value: "radial", label: "Radial gradient" },
  ];
  const graphicBlendOptions = [
    { value: "normal", label: "Normal" },
    { value: "multiply", label: "Multiply" },
    { value: "screen", label: "Screen" },
    { value: "overlay", label: "Overlay" },
    { value: "soft-light", label: "Soft light" },
  ];
  const graphicEffectOptions = [
    { value: "none", label: "None" },
    { value: "glass", label: "Glass" },
    { value: "glow", label: "Glow bloom" },
    { value: "grain", label: "Film grain" },
    { value: "scanlines", label: "Scanlines" },
    { value: "chromatic", label: "Chromatic split" },
    { value: "vignette", label: "Vignette" },
    { value: "lightLeak", label: "Light leak" },
    { value: "filmBurn", label: "Film burn" },
    { value: "halation", label: "Film halation" },
    { value: "anamorphic", label: "Anamorphic flare" },
  ];
  const graphicMaskOptions = [
    { value: "none", label: "None" },
    { value: "circle", label: "Circle" },
    { value: "diamond", label: "Diamond" },
    { value: "hexagon", label: "Hexagon" },
  ];
  const videoFitOptions = (["contain", "cover"] as ClipSegment["fit"][]).map((value) => ({
    value,
    label: veText(`fit.${value}`),
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
    if (previewMuted || trackStates.audio.muted) {
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
          // Trimmed loops are driven by the seek-safe cue window rather than the media element's full-file loop.
          audio.loop = false;
          preview = { audio, url, generated };
          previewAudioRef.current.set(cue.id, preview);
        } catch (error) {
          console.warn("Preview audio cue could not be loaded", cue.label, error);
          continue;
        }
      }
      const audio = preview.audio;
      audio.loop = false;
      audio.playbackRate = playbackRate;
      const mediaDuration = Number.isFinite(audio.duration) ? audio.duration : 0;
      const sourceWindow = getAudioCueSourceWindow(cue, mediaDuration);
      if (!cue.loop && cueOffset > sourceWindow.duration) {
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
  }, [audioCues, playbackRate, previewMuted, stopPreviewAudio, trackStates.audio.muted]);

  useEffect(() => {
    const mapped = mapTimelineTime(playhead, clips);
    if (videoRef.current && mapped) {
      videoRef.current.muted = previewMuted || shouldMuteSourceVideoAudio(mapped.clip, playhead, audioCues, trackStates);
    }
    if (previewMuted || trackStates.audio.muted) {
      stopPreviewAudio();
    } else if (isPlaying) {
      void syncPreviewAudio(playhead, true);
    }
  }, [audioCues, clips, isPlaying, playhead, previewMuted, stopPreviewAudio, syncPreviewAudio, trackStates]);

  useEffect(() => {
    if (isPlaying || draggingClipId) return;
    const mapped = mapTimelineTime(playhead, clips);
    if (!mapped || !videoRef.current) return;
    let cancelled = false;
    void ensurePreviewVideoSource(mapped.clip).then((video) => {
      if (cancelled || !video) return;
      video.muted = previewMuted || shouldMuteSourceVideoAudio(mapped.clip, playhead, audioCues, trackStates);
      video.playbackRate = clipPreviewPlaybackRate(mapped.clip, playbackRate);
      const targetSourceTime = previewSourceTime(mapped);
      if (Math.abs(video.currentTime - targetSourceTime) > 0.04) {
        video.currentTime = targetSourceTime;
      }
    });
    return () => {
      cancelled = true;
    };
  }, [audioCues, clips, draggingClipId, ensurePreviewVideoSource, isPlaying, playbackRate, playhead, previewMuted, trackStates]);

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
    durationProbeUrlRef.current = null;
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
    durationProbeUrlRef.current = null;
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
        speed: 1,
        fadeIn: 0,
        fadeOut: 0,
        muted: false,
        color: CLIP_COLORS[0],
        fit: "contain",
        x: 50,
        y: 50,
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
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
        x: 50,
        y: 50,
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
      },
    ]);
    setCaptions([]);
    setGraphicLayers([]);
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
    const duration = getPlayableMediaDuration(video);
    const width = video.videoWidth || 1920;
    const height = video.videoHeight || 1080;
    setMediaSize({ width, height });
    if (duration <= 0) {
      const probeUrl = currentUrl || downloadUrl;
      if (probeUrl && durationProbeUrlRef.current !== probeUrl) {
        durationProbeUrlRef.current = probeUrl;
        if (!requestMediaDurationProbe(video)) durationProbeUrlRef.current = null;
      }
      return;
    }
    if (durationProbeUrlRef.current === (currentUrl || downloadUrl)) {
      durationProbeUrlRef.current = null;
      video.currentTime = 0;
    }
    setSourceDuration(duration);
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
            speed: 1,
            fadeIn: 0,
            fadeOut: 0,
            muted: false,
            color: CLIP_COLORS[index % CLIP_COLORS.length],
            fit: "contain",
            x: 50,
            y: 50,
            scale: 1,
            rotation: 0,
            opacity: 1,
            keyframes: [],
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
        const nextDuration = nextClips.reduce((total, clip) => total + getClipTimelineDuration(clip), 0);
        let cursor = 0;
        const nextShots = nextClips.map((clip, index) => {
          const start = cursor;
          const clipDuration = Math.max(0.05, getClipTimelineDuration(clip));
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
            x: 50,
            y: 50,
            scale: 1,
            rotation: 0,
            opacity: 1,
            keyframes: [],
          } satisfies ShotBeat;
        });

        resetEditorHistory();
        setTrackStates(createDefaultTimelineTrackStates());
        setWorkArea(createDefaultWorkArea(nextDuration));
        setClips(nextClips);
        setShotBeats(nextShots);
        setCaptions([]);
        setGraphicLayers([]);
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
        video.muted = previewMuted || shouldMuteSourceVideoAudio(mapped.clip, next, audioCues, trackStates);
        video.playbackRate = clipPreviewPlaybackRate(mapped.clip, playbackRate);
      });
    }
    stopPreviewAudio();
  }, [audioCues, clips, ensurePreviewVideoSource, playbackRate, previewMuted, snapTimelineTime, stopPreviewAudio, trackStates]);

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
      audio.muted = previewMuted;
      audio.playbackRate = playbackRate;
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
  }, [pausePlayback, playbackRate, previewMuted, stopMediaAssetPreview, toast]);

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

      video.muted = previewMuted;
      video.playbackRate = playbackRate;
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
  }, [getClipAssetUrl, pausePlayback, playbackRate, previewMuted, previewingMediaAssetId, stopMediaAssetPreview, toast]);

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
    setGraphicLayers(snapshot.graphicLayers);
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
      if (previewLoopRef.current && timelineDuration > PLAYBACK_BOUNDARY_EPSILON) {
        setPlayhead(0);
        window.queueMicrotask(() => loopRestartRef.current?.());
      } else {
        setPlayhead(timelineDuration);
      }
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
          nextVideo.muted = previewMuted || shouldMuteSourceVideoAudio(mapped.clip, mapped.timelineStart, audioCues, trackStates);
          nextVideo.playbackRate = clipPreviewPlaybackRate(mapped.clip, playbackRate);
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
        mapped.timelineStart + getClipTimelineOffset(mapped.clip, sourceTime),
        0,
        timelineDuration,
      );
      video.muted = previewMuted || shouldMuteSourceVideoAudio(mapped.clip, timelineTime, audioCues, trackStates);
      video.playbackRate = clipPreviewPlaybackRate(mapped.clip, playbackRate);

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
  }, [audioCues, clips, ensurePreviewVideoSource, playbackRate, previewMuted, stopPlaybackClock, stopPreviewAudio, syncPreviewAudio, timelineDuration, trackStates]);

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
      activeVideo.muted = previewMuted || shouldMuteSourceVideoAudio(mapped.clip, start, audioCues, trackStates);
      activeVideo.playbackRate = clipPreviewPlaybackRate(mapped.clip, playbackRate);
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
  }, [audioCues, clips, ensurePreviewVideoSource, playbackRate, previewMuted, startPlaybackClock, stopMediaAssetPreview, stopPlaybackClock, stopPreviewAudio, syncPreviewAudio, timelineDuration, toast, trackStates]);

  useEffect(() => {
    loopRestartRef.current = () => {
      void playTimelineFrom(0);
    };
    return () => {
      loopRestartRef.current = null;
    };
  }, [playTimelineFrom]);

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

  const togglePreviewFullscreen = useCallback(async () => {
    try {
      if (previewFullscreen || document.fullscreenElement) {
        if (document.exitFullscreen) await document.exitFullscreen();
        setPreviewFullscreen(false);
      } else if (previewRef.current?.requestFullscreen) {
        await previewRef.current.requestFullscreen();
      }
    } catch (error) {
      console.warn("Preview fullscreen could not be changed", error);
    }
  }, [previewFullscreen]);

  const handleTimeUpdate = useCallback(() => {
    if (durationProbeUrlRef.current) {
      handleLoadedMetadata();
      if (durationProbeUrlRef.current) return;
    }
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
    const timelineTime = mapped.timelineStart + getClipTimelineOffset(mapped.clip, video.currentTime);
    setPlayhead(clamp(timelineTime, 0, timelineDuration));
    void syncPreviewAudio(timelineTime, true);
  }, [clips, continuePlaybackFrom, handleLoadedMetadata, isPlaybackClockFresh, isPlaying, stopPlaybackClock, syncPreviewAudio, timelineDuration]);

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
      const previousTimelineDuration = getClipTimelineDuration(clip);
      const next = { ...clip, ...patch };
      const maxDuration = getClipMaxDuration(next, sourceDuration);
      next.sourceStart = clamp(next.sourceStart, 0, Math.max(0, maxDuration - 0.05));
      next.sourceEnd = clamp(next.sourceEnd, next.sourceStart + 0.05, maxDuration || next.sourceStart + 0.05);
      next.speed = normalizeVideoClipSpeed(next.speed);
      const nextTimelineDuration = getClipTimelineDuration(next);
      next.fadeIn = normalizeVideoClipFade(next.fadeIn, nextTimelineDuration);
      next.fadeOut = normalizeVideoClipFade(next.fadeOut, nextTimelineDuration);
      next.fit = next.fit === "cover" ? "cover" : "contain";
      Object.assign(next, clipBaseMotionPose(next));
      const keyframes = patch.speed !== undefined
        && patch.keyframes === undefined
        && previousTimelineDuration > 0
        ? next.keyframes.map((keyframe) => ({
            ...keyframe,
            time: keyframe.time * (nextTimelineDuration / previousTimelineDuration),
          }))
        : next.keyframes;
      next.keyframes = normalizeMotionKeyframes(
        keyframes,
        clipBaseMotionPose(next),
        nextTimelineDuration,
      );
      return next;
    }));
  }, [sourceDuration]);

  const updateClipMotion = useCallback((id: string, patch: Partial<MotionPose>) => {
    const nextKeyframeId = makeId("motion");
    setClips((current) => {
      const span = getClipTimelineSpans(current).find((item) => item.clip.id === id);
      return current.map((clip) => {
        if (clip.id !== id) return clip;
        const activeKeyframe = activeMotionKeyframeId
          ? clip.keyframes.find((keyframe) => keyframe.id === activeMotionKeyframeId) ?? null
          : null;
        const shouldRecordAtPlayhead = Boolean(
          autoRecordMotion && span && playhead >= span.start && playhead <= span.end,
        );
        const localTime = span
          ? clamp(playhead - span.start, 0, Math.max(0, span.duration))
          : 0;
        const referencePose = activeKeyframe
          ?? (shouldRecordAtPlayhead ? clipMotionPoseAtLocalTime(clip, localTime) : clipBaseMotionPose(clip));
        const nextPose = clampCaptionMotionPose({ ...referencePose, ...patch });
        if (activeKeyframe) {
          return {
            ...clip,
            keyframes: clip.keyframes.map((keyframe) => keyframe.id === activeKeyframe.id ? { ...keyframe, ...nextPose } : keyframe),
          };
        }
        if (shouldRecordAtPlayhead) {
          const existing = clip.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
          return {
            ...clip,
            keyframes: upsertMotionKeyframe(clip.keyframes, {
              ...nextPose,
              id: existing?.id ?? nextKeyframeId,
              time: localTime,
              easing: existing?.easing ?? "easeInOut",
            }),
          };
        }
        return { ...clip, ...nextPose };
      });
    });
  }, [activeMotionKeyframeId, autoRecordMotion, playhead]);

  const addClipKeyframe = useCallback(() => {
    if (!selectedClip || !selectedClipSpan || trackStates.video.locked) return;
    if (!selectedClipContainsPlayhead) {
      toast.warning(veText("toast.keyframe_outside_clip"));
      return;
    }
    const localTime = clamp(playhead - selectedClipSpan.start, 0, selectedClipSpan.duration);
    const existing = selectedClip.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
    const id = existing?.id ?? makeId("motion");
    const pose = clipMotionPoseAtLocalTime(selectedClip, localTime);
    setClips((current) => current.map((clip) => clip.id === selectedClip.id
      ? {
        ...clip,
        keyframes: upsertMotionKeyframe(clip.keyframes, {
          ...pose,
          id,
          time: localTime,
          easing: existing?.easing ?? "easeInOut",
        }),
      }
      : clip));
    setActiveMotionKeyframeId(id);
    toast.success(existing ? veText("toast.keyframe_updated") : veText("toast.keyframe_added"), formatTime(playhead));
  }, [playhead, selectedClip, selectedClipContainsPlayhead, selectedClipSpan, toast, trackStates.video.locked]);

  const deleteClipKeyframe = useCallback(() => {
    if (!selectedClip || !activeMotionKeyframeId || trackStates.video.locked) return;
    setClips((current) => current.map((clip) => clip.id === selectedClip.id
      ? { ...clip, keyframes: clip.keyframes.filter((keyframe) => keyframe.id !== activeMotionKeyframeId) }
      : clip));
    setActiveMotionKeyframeId(null);
  }, [activeMotionKeyframeId, selectedClip, trackStates.video.locked]);

  const updateClipKeyframeOptions = useCallback((patch: Partial<Pick<MotionKeyframe, "easing" | "bezier" | "spatial">>) => {
    if (!selectedClip || !activeMotionKeyframeId) return;
    setClips((current) => current.map((clip) => clip.id === selectedClip.id
      ? {
        ...clip,
        keyframes: clip.keyframes.map((keyframe) => keyframe.id === activeMotionKeyframeId ? { ...keyframe, ...patch } : keyframe),
      }
      : clip));
  }, [activeMotionKeyframeId, selectedClip]);

  const updateClipKeyframeTime = useCallback((time: number) => {
    if (!selectedClip || !selectedClipSpan || !activeMotionKeyframeId || trackStates.video.locked) return;
    const snappedTime = snapMotionTimeToFrame(time, selectedClipSpan.duration);
    pausePlayback();
    setClips((current) => current.map((clip) => clip.id === selectedClip.id
      ? {
        ...clip,
        keyframes: retimeMotionKeyframe(
          clip.keyframes,
          activeMotionKeyframeId,
          snappedTime,
          selectedClipSpan.duration,
        ),
      }
      : clip));
    seekTimeline(selectedClipSpan.start + snappedTime);
  }, [activeMotionKeyframeId, pausePlayback, seekTimeline, selectedClip, selectedClipSpan, trackStates.video.locked]);

  const applyClipMotionPreset = useCallback((preset: MotionPreset | null) => {
    if (!selectedClip || !selectedClipSpan || trackStates.video.locked) return;
    const prefix = makeId("motion-preset");
    setClips((current) => current.map((clip) => clip.id === selectedClip.id
      ? {
        ...clip,
        keyframes: preset
          ? createMotionPresetKeyframes(
            preset,
            Math.max(0.05, getClipTimelineDuration(clip)),
            clipBaseMotionPose(clip),
            prefix,
          )
          : [],
      }
      : clip));
    setActiveMotionKeyframeId(null);
    setAutoRecordMotion(false);
    seekTimeline(selectedClipSpan.start);
  }, [seekTimeline, selectedClip, selectedClipSpan, trackStates.video.locked]);

  const beginClipOverlayDrag = useCallback((event: ReactPointerEvent<HTMLVideoElement>, clip: ClipSegment) => {
    if (event.button !== 0 || trackStates.video.locked || previewingMediaAssetId) return;
    const preview = event.currentTarget.closest(".ve-preview-media-stage");
    if (!(preview instanceof HTMLElement)) return;
    const rect = preview.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    beginEditorTransaction();
    setSelection({ type: "clip", id: clip.id });
    const updateFromClient = (clientX: number, clientY: number) => updateClipMotion(clip.id, {
      x: clamp(((clientX - rect.left) / rect.width) * 100, 0, 100),
      y: clamp(((clientY - rect.top) / rect.height) * 100, 0, 100),
    });
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
  }, [beginEditorTransaction, finishEditorTransaction, previewingMediaAssetId, trackStates.video.locked, updateClipMotion]);

  const applyClipReorderResult = useCallback((result: ClipReorderResult, selectedId: string) => {
    setClips(result.clips);
    setShotBeats(result.shotBeats);
    setCaptions(result.captions);
    setGraphicLayers(result.graphicLayers);
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

  const beginClipDrag = useCallback((event: ReactPointerEvent<HTMLElement>, clipId: string) => {
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
      keyframes: clip.keyframes.map((keyframe) => ({ ...keyframe, id: makeId("motion") })),
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
    setGraphicLayers((current) => duplicateTimedItems(current, (graphic) => ({
      ...graphic,
      id: makeId("graphic"),
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
      clipStartOnTimeline += getClipTimelineDuration(item);
    }
    const dragTimelineDuration = timelineDuration;
    const maxDuration = getClipMaxDuration(clip, sourceDuration);
    const updateFromClientX = (clientX: number) => {
      const startTime = timelineTimeFromLaneClientX(lane, startX, dragTimelineDuration);
      const nextTime = timelineTimeFromLaneClientX(lane, clientX, dragTimelineDuration);
      const delta = (nextTime - startTime) * normalizeVideoClipSpeed(clip.speed);
      if (edge === "start") {
        const sourceStart = clamp(startSource + delta, 0, endSource - 0.05);
        updateClip(clipId, { sourceStart, editNotes: clip.editNotes || "Trimmed manually on the timeline." });
        setPlayhead(clipStartOnTimeline);
      } else {
        const sourceEnd = clamp(endSource + delta, startSource + 0.05, maxDuration || endSource + Math.max(0, delta));
        updateClip(clipId, { sourceEnd, editNotes: clip.editNotes || "Trimmed manually on the timeline." });
        const nextDuration = Math.max(0.05, videoClipTimelineDuration(startSource, sourceEnd, clip.speed));
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
      const previousDuration = Math.max(0.05, shot.end - shot.start);
      const next = { ...shot, ...patch };
      next.start = clamp(next.start, 0, timelineDuration);
      next.end = clamp(next.end, next.start + 0.05, Math.max(next.start + 0.05, timelineDuration));
      const nextPose = shotBaseMotionPose(next);
      Object.assign(next, nextPose);
      const nextDuration = Math.max(0.05, next.end - next.start);
      const keyframes = nextDuration !== previousDuration
        ? next.keyframes.map((keyframe) => ({
          ...keyframe,
          time: keyframe.time * (nextDuration / previousDuration),
        }))
        : next.keyframes;
      next.keyframes = normalizeMotionKeyframes(keyframes, nextPose, nextDuration);
      return next;
    }));
  }, [timelineDuration]);

  const updateShotMotion = useCallback((id: string, patch: Partial<MotionPose>) => {
    const nextKeyframeId = makeId("motion");
    setShotBeats((current) => current.map((shot) => {
      if (shot.id !== id) return shot;
      const activeKeyframe = activeMotionKeyframeId
        ? shot.keyframes.find((keyframe) => keyframe.id === activeMotionKeyframeId) ?? null
        : null;
      const shouldRecordAtPlayhead = autoRecordMotion && playhead >= shot.start && playhead <= shot.end;
      const referencePose = activeKeyframe
        ?? (shouldRecordAtPlayhead ? shotMotionPoseAtTimelineTime(shot, playhead) : shotBaseMotionPose(shot));
      const nextPose = clampCaptionMotionPose({ ...referencePose, ...patch });
      if (activeKeyframe) {
        return {
          ...shot,
          keyframes: shot.keyframes.map((keyframe) => keyframe.id === activeKeyframe.id ? { ...keyframe, ...nextPose } : keyframe),
        };
      }
      if (shouldRecordAtPlayhead) {
        const localTime = clamp(playhead - shot.start, 0, Math.max(0, shot.end - shot.start));
        const existing = shot.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
        return {
          ...shot,
          keyframes: upsertMotionKeyframe(shot.keyframes, {
            ...nextPose,
            id: existing?.id ?? nextKeyframeId,
            time: localTime,
            easing: existing?.easing ?? "easeInOut",
          }),
        };
      }
      return { ...shot, ...nextPose };
    }));
  }, [activeMotionKeyframeId, autoRecordMotion, playhead]);

  const addShotKeyframe = useCallback(() => {
    if (!selectedShot || trackStates.shots.locked) return;
    if (playhead < selectedShot.start || playhead > selectedShot.end) {
      toast.warning(veText("toast.keyframe_outside_clip"));
      return;
    }
    const localTime = clamp(playhead - selectedShot.start, 0, Math.max(0, selectedShot.end - selectedShot.start));
    const existing = selectedShot.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
    const id = existing?.id ?? makeId("motion");
    const pose = shotMotionPoseAtTimelineTime(selectedShot, playhead);
    setShotBeats((current) => current.map((shot) => shot.id === selectedShot.id
      ? {
        ...shot,
        keyframes: upsertMotionKeyframe(shot.keyframes, {
          ...pose,
          id,
          time: localTime,
          easing: existing?.easing ?? "easeInOut",
        }),
      }
      : shot));
    setActiveMotionKeyframeId(id);
    toast.success(existing ? veText("toast.keyframe_updated") : veText("toast.keyframe_added"), formatTime(playhead));
  }, [playhead, selectedShot, toast, trackStates.shots.locked]);

  const deleteShotKeyframe = useCallback(() => {
    if (!selectedShot || !activeMotionKeyframeId || trackStates.shots.locked) return;
    setShotBeats((current) => current.map((shot) => shot.id === selectedShot.id
      ? { ...shot, keyframes: shot.keyframes.filter((keyframe) => keyframe.id !== activeMotionKeyframeId) }
      : shot));
    setActiveMotionKeyframeId(null);
  }, [activeMotionKeyframeId, selectedShot, trackStates.shots.locked]);

  const applyShotMotionPreset = useCallback((preset: MotionPreset | null) => {
    if (!selectedShot || trackStates.shots.locked) return;
    const prefix = makeId("scene-motion");
    setShotBeats((current) => current.map((shot) => shot.id === selectedShot.id
      ? {
        ...shot,
        keyframes: preset
          ? createMotionPresetKeyframes(
            preset,
            Math.max(0.05, shot.end - shot.start),
            shotBaseMotionPose(shot),
            prefix,
          )
          : [],
      }
      : shot));
    setActiveMotionKeyframeId(null);
    setAutoRecordMotion(false);
    seekTimeline(selectedShot.start);
  }, [seekTimeline, selectedShot, trackStates.shots.locked]);

  const updateCaption = useCallback((id: string, patch: Partial<CaptionCue>) => {
    setCaptions((current) => current.map((caption) => {
      if (caption.id !== id) return caption;
      const next = { ...caption, ...patch };
      next.start = clamp(next.start, 0, timelineDuration);
      next.end = clamp(next.end, next.start + 0.05, Math.max(next.start + 0.05, timelineDuration));
      next.x = clamp(next.x, 0, 100);
      next.y = clamp(next.y, 0, 100);
      next.scale = clamp(next.scale, 0.1, 4);
      next.rotation = clamp(next.rotation, -360, 360);
      next.opacity = clamp(next.opacity, 0, 1);
      next.keyframes = normalizeMotionKeyframes(
        next.keyframes,
        captionBaseMotionPose(next),
        Math.max(0, next.end - next.start),
      );
      next.size = clamp(next.size, 10, 480);
      next.backgroundOpacity = clamp(next.backgroundOpacity, 0, 1);
      return next;
    }));
  }, [timelineDuration]);

  const updateCaptionMotion = useCallback((id: string, patch: Partial<MotionPose>) => {
    const nextKeyframeId = makeId("motion");
    setCaptions((current) => current.map((caption) => {
      if (caption.id !== id) return caption;
      const activeKeyframe = activeMotionKeyframeId
        ? caption.keyframes.find((keyframe) => keyframe.id === activeMotionKeyframeId) ?? null
        : null;
      const shouldRecordAtPlayhead = autoRecordMotion && playhead >= caption.start && playhead <= caption.end;
      const referencePose = activeKeyframe
        ?? (shouldRecordAtPlayhead ? captionMotionPoseAtTimelineTime(caption, playhead) : captionBaseMotionPose(caption));
      const nextPose = clampCaptionMotionPose({ ...referencePose, ...patch });

      if (activeKeyframe) {
        return {
          ...caption,
          keyframes: caption.keyframes.map((keyframe) => (
            keyframe.id === activeKeyframe.id ? { ...keyframe, ...nextPose } : keyframe
          )),
        };
      }
      if (shouldRecordAtPlayhead) {
        const localTime = clamp(playhead - caption.start, 0, Math.max(0, caption.end - caption.start));
        const existing = caption.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
        return {
          ...caption,
          keyframes: upsertMotionKeyframe(caption.keyframes, {
            ...nextPose,
            id: nextKeyframeId,
            time: localTime,
            easing: existing?.easing ?? "easeInOut",
          }),
        };
      }
      return { ...caption, ...nextPose };
    }));
  }, [activeMotionKeyframeId, autoRecordMotion, playhead]);

  const addCaptionKeyframe = useCallback(() => {
    if (!selectedCaption || trackStates.captions.locked) return;
    if (playhead < selectedCaption.start || playhead > selectedCaption.end) {
      toast.warning(veText("toast.keyframe_outside_caption"));
      return;
    }
    const localTime = clamp(playhead - selectedCaption.start, 0, Math.max(0, selectedCaption.end - selectedCaption.start));
    const existing = selectedCaption.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
    const id = existing?.id ?? makeId("motion");
    const pose = captionMotionPoseAtTimelineTime(selectedCaption, playhead);
    setCaptions((current) => current.map((caption) => (
      caption.id === selectedCaption.id
        ? {
          ...caption,
          keyframes: upsertMotionKeyframe(caption.keyframes, {
            ...pose,
            id,
            time: localTime,
            easing: existing?.easing ?? "easeInOut",
          }),
        }
        : caption
    )));
    setActiveMotionKeyframeId(id);
    toast.success(existing ? veText("toast.keyframe_updated") : veText("toast.keyframe_added"), formatTime(playhead));
  }, [playhead, selectedCaption, toast, trackStates.captions.locked]);

  const deleteCaptionKeyframe = useCallback(() => {
    if (!selectedCaption || !activeMotionKeyframeId || trackStates.captions.locked) return;
    setCaptions((current) => current.map((caption) => (
      caption.id === selectedCaption.id
        ? { ...caption, keyframes: caption.keyframes.filter((keyframe) => keyframe.id !== activeMotionKeyframeId) }
        : caption
    )));
    setActiveMotionKeyframeId(null);
  }, [activeMotionKeyframeId, selectedCaption, trackStates.captions.locked]);

  const updateCaptionKeyframeOptions = useCallback((patch: Partial<Pick<MotionKeyframe, "easing" | "bezier" | "spatial">>) => {
    if (!selectedCaption || !activeMotionKeyframeId) return;
    setCaptions((current) => current.map((caption) => (
      caption.id === selectedCaption.id
        ? {
          ...caption,
          keyframes: caption.keyframes.map((keyframe) => (
            keyframe.id === activeMotionKeyframeId ? { ...keyframe, ...patch } : keyframe
          )),
        }
        : caption
    )));
  }, [activeMotionKeyframeId, selectedCaption]);

  const updateCaptionKeyframeTime = useCallback((time: number) => {
    if (!selectedCaption || !activeMotionKeyframeId || trackStates.captions.locked) return;
    const duration = Math.max(0, selectedCaption.end - selectedCaption.start);
    const snappedTime = snapMotionTimeToFrame(time, duration);
    pausePlayback();
    setCaptions((current) => current.map((caption) => caption.id === selectedCaption.id
      ? {
        ...caption,
        keyframes: retimeMotionKeyframe(
          caption.keyframes,
          activeMotionKeyframeId,
          snappedTime,
          duration,
        ),
      }
      : caption));
    seekTimeline(selectedCaption.start + snappedTime);
  }, [activeMotionKeyframeId, pausePlayback, seekTimeline, selectedCaption, trackStates.captions.locked]);

  const applyCaptionMotionPreset = useCallback((preset: MotionPreset | null) => {
    if (!selectedCaption || trackStates.captions.locked) return;
    const prefix = makeId("motion-preset");
    setCaptions((current) => current.map((caption) => (
      caption.id === selectedCaption.id
        ? {
          ...caption,
          keyframes: preset
            ? createMotionPresetKeyframes(
              preset,
              Math.max(0.05, caption.end - caption.start),
              captionBaseMotionPose(caption),
              prefix,
            )
            : [],
        }
        : caption
    )));
    setActiveMotionKeyframeId(null);
    setAutoRecordMotion(false);
    seekTimeline(selectedCaption.start);
  }, [selectedCaption, seekTimeline, trackStates.captions.locked]);

  const beginCaptionOverlayDrag = useCallback((event: ReactPointerEvent<HTMLDivElement>, caption: CaptionCue) => {
    if (event.button !== 0 || trackStates.captions.locked) return;
    const preview = event.currentTarget.closest(".ve-preview-media-stage");
    if (!(preview instanceof HTMLElement)) return;
    const parentGroup = event.currentTarget.parentElement?.classList.contains("ve-graphic-group")
      ? event.currentTarget.parentElement
      : null;
    const rect = (parentGroup ?? preview).getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    beginEditorTransaction();
    setSelection({ type: "caption", id: caption.id });

    const updateFromClient = (clientX: number, clientY: number) => {
      const x = clamp(((clientX - rect.left) / rect.width) * 100, 0, 100);
      const y = clamp(((clientY - rect.top) / rect.height) * 100, 0, 100);
      updateCaptionMotion(caption.id, { x, y });
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
  }, [beginEditorTransaction, finishEditorTransaction, trackStates.captions.locked, updateCaptionMotion]);

  const updateGraphic = useCallback((id: string, patch: Partial<GraphicLayer>) => {
    setGraphicLayers((current) => current.map((graphic) => {
      if (graphic.id !== id) return graphic;
      const next = { ...graphic, ...patch };
      next.start = clamp(next.start, 0, timelineDuration);
      next.end = clamp(next.end, next.start + 0.05, Math.max(next.start + 0.05, timelineDuration));
      next.x = clamp(next.x, 0, 100);
      next.y = clamp(next.y, 0, 100);
      next.scale = clamp(next.scale, 0.1, 4);
      next.rotation = clamp(next.rotation, -360, 360);
      next.opacity = clamp(next.opacity, 0, 1);
      next.width = clamp(next.width, 0.1, 100);
      next.height = clamp(next.height, 0.1, 100);
      next.strokeWidth = clamp(next.strokeWidth, 0, 20);
      next.cornerRadius = clamp(next.cornerRadius, 0, 100);
      if (next.kind === "video") {
        const sourceLimit = Math.max(0.01, next.assetDuration ?? next.sourceEnd ?? next.end - next.start);
        next.sourceStart = clamp(next.sourceStart ?? 0, 0, Math.max(0, sourceLimit - 0.01));
        next.sourceEnd = clamp(next.sourceEnd ?? sourceLimit, next.sourceStart + 0.01, sourceLimit);
        next.speed = normalizeVideoClipSpeed(next.speed);
        next.loop = Boolean(next.loop);
      }
      next.keyframes = normalizeMotionKeyframes(next.keyframes, graphicBaseMotionPose(next), Math.max(0, next.end - next.start));
      return next;
    }));
  }, [timelineDuration]);

  const updateGraphicMotion = useCallback((id: string, patch: Partial<MotionPose>) => {
    const nextKeyframeId = makeId("motion");
    setGraphicLayers((current) => current.map((graphic) => {
      if (graphic.id !== id) return graphic;
      const activeKeyframe = activeMotionKeyframeId
        ? graphic.keyframes.find((keyframe) => keyframe.id === activeMotionKeyframeId) ?? null
        : null;
      const shouldRecordAtPlayhead = autoRecordMotion && playhead >= graphic.start && playhead <= graphic.end;
      const referencePose = activeKeyframe
        ?? (shouldRecordAtPlayhead ? graphicMotionPoseAtTimelineTime(graphic, playhead) : graphicBaseMotionPose(graphic));
      const nextPose = clampCaptionMotionPose({ ...referencePose, ...patch });
      if (activeKeyframe) {
        return {
          ...graphic,
          keyframes: graphic.keyframes.map((keyframe) => keyframe.id === activeKeyframe.id ? { ...keyframe, ...nextPose } : keyframe),
        };
      }
      if (shouldRecordAtPlayhead) {
        const localTime = clamp(playhead - graphic.start, 0, Math.max(0, graphic.end - graphic.start));
        const existing = graphic.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
        return {
          ...graphic,
          keyframes: upsertMotionKeyframe(graphic.keyframes, {
            ...nextPose,
            id: nextKeyframeId,
            time: localTime,
            easing: existing?.easing ?? "easeInOut",
          }),
        };
      }
      return { ...graphic, ...nextPose };
    }));
  }, [activeMotionKeyframeId, autoRecordMotion, playhead]);

  const addGraphicKeyframe = useCallback(() => {
    if (!selectedGraphic || trackStates.graphics.locked) return;
    if (!selectedMotionLayerContainsPlayhead) {
      toast.warning(veText("toast.keyframe_outside_layer"));
      return;
    }
    const localTime = clamp(playhead - selectedGraphic.start, 0, Math.max(0, selectedGraphic.end - selectedGraphic.start));
    const existing = selectedGraphic.keyframes.find((keyframe) => Math.abs(keyframe.time - localTime) <= 0.005);
    const id = existing?.id ?? makeId("motion");
    const pose = graphicMotionPoseAtTimelineTime(selectedGraphic, playhead);
    setGraphicLayers((current) => current.map((graphic) => graphic.id === selectedGraphic.id
      ? {
        ...graphic,
        keyframes: upsertMotionKeyframe(graphic.keyframes, {
          ...pose,
          id,
          time: localTime,
          easing: existing?.easing ?? "easeInOut",
        }),
      }
      : graphic));
    setActiveMotionKeyframeId(id);
    toast.success(existing ? veText("toast.keyframe_updated") : veText("toast.keyframe_added"), formatTime(playhead));
  }, [playhead, selectedGraphic, selectedMotionLayerContainsPlayhead, toast, trackStates.graphics.locked]);

  const deleteGraphicKeyframe = useCallback(() => {
    if (!selectedGraphic || !activeMotionKeyframeId || trackStates.graphics.locked) return;
    setGraphicLayers((current) => current.map((graphic) => graphic.id === selectedGraphic.id
      ? { ...graphic, keyframes: graphic.keyframes.filter((keyframe) => keyframe.id !== activeMotionKeyframeId) }
      : graphic));
    setActiveMotionKeyframeId(null);
  }, [activeMotionKeyframeId, selectedGraphic, trackStates.graphics.locked]);

  const updateGraphicKeyframeOptions = useCallback((patch: Partial<Pick<MotionKeyframe, "easing" | "bezier" | "spatial">>) => {
    if (!selectedGraphic || !activeMotionKeyframeId) return;
    setGraphicLayers((current) => current.map((graphic) => graphic.id === selectedGraphic.id
      ? {
        ...graphic,
        keyframes: graphic.keyframes.map((keyframe) => keyframe.id === activeMotionKeyframeId ? { ...keyframe, ...patch } : keyframe),
      }
      : graphic));
  }, [activeMotionKeyframeId, selectedGraphic]);

  const updateGraphicKeyframeTime = useCallback((time: number) => {
    if (!selectedGraphic || !activeMotionKeyframeId || trackStates.graphics.locked) return;
    const duration = Math.max(0, selectedGraphic.end - selectedGraphic.start);
    const snappedTime = snapMotionTimeToFrame(time, duration);
    pausePlayback();
    setGraphicLayers((current) => current.map((graphic) => graphic.id === selectedGraphic.id
      ? {
        ...graphic,
        keyframes: retimeMotionKeyframe(
          graphic.keyframes,
          activeMotionKeyframeId,
          snappedTime,
          duration,
        ),
      }
      : graphic));
    seekTimeline(selectedGraphic.start + snappedTime);
  }, [activeMotionKeyframeId, pausePlayback, seekTimeline, selectedGraphic, trackStates.graphics.locked]);

  const applyGraphicMotionPreset = useCallback((preset: MotionPreset | null) => {
    if (!selectedGraphic || trackStates.graphics.locked) return;
    const prefix = makeId("motion-preset");
    setGraphicLayers((current) => current.map((graphic) => graphic.id === selectedGraphic.id
      ? {
        ...graphic,
        keyframes: preset
          ? createMotionPresetKeyframes(preset, Math.max(0.05, graphic.end - graphic.start), graphicBaseMotionPose(graphic), prefix)
          : [],
      }
      : graphic));
    setActiveMotionKeyframeId(null);
    setAutoRecordMotion(false);
    seekTimeline(selectedGraphic.start);
  }, [selectedGraphic, seekTimeline, trackStates.graphics.locked]);

  const beginGraphicOverlayDrag = useCallback((event: ReactPointerEvent<HTMLDivElement>, graphic: GraphicLayer) => {
    if (event.button !== 0 || trackStates.graphics.locked) return;
    const preview = event.currentTarget.closest(".ve-preview-media-stage");
    if (!(preview instanceof HTMLElement)) return;
    const parentGroup = event.currentTarget.parentElement?.classList.contains("ve-graphic-group")
      ? event.currentTarget.parentElement
      : null;
    const rect = (parentGroup ?? preview).getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    beginEditorTransaction();
    setSelection({ type: "graphic", id: graphic.id });
    const updateFromClient = (clientX: number, clientY: number) => updateGraphicMotion(graphic.id, {
      x: clamp(((clientX - rect.left) / rect.width) * 100, 0, 100),
      y: clamp(((clientY - rect.top) / rect.height) * 100, 0, 100),
    });
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
  }, [beginEditorTransaction, finishEditorTransaction, trackStates.graphics.locked, updateGraphicMotion]);

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
      const sourceLimit = Math.max(0.01, next.assetDuration ?? next.sourceEnd ?? duration);
      next.sourceStart = clamp(next.sourceStart ?? 0, 0, Math.max(0, sourceLimit - 0.01));
      next.sourceEnd = clamp(next.sourceEnd ?? sourceLimit, next.sourceStart + 0.01, sourceLimit);
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

    if (selectedGraphic) {
      const next = moveRange(selectedGraphic.start, selectedGraphic.end);
      updateGraphic(selectedGraphic.id, next);
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
    selectedGraphic,
    selectedMarker,
    selectedShot,
    selectedTrackLocked,
    sourceDuration,
    timelineDuration,
    toast,
    updateAudioCue,
    updateCaption,
    updateClip,
    updateGraphic,
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
    setGraphicLayers(state.graphicLayers);
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
    // A specifically linked/opened recipe owns the editor state. A same-name
    // folder fallback must never replace it after save/query invalidation.
    if (routeIsRecipe || recipeDoc) return;
    const savedRecipeDoc = recipeQuery.data;
    if (!savedRecipeDoc || sourceDuration <= 0 || autoLoadedRecipeRef.current === savedRecipeDoc.id) return;
    autoLoadedRecipeRef.current = savedRecipeDoc.id;
    void loadSavedRecipe(false);
  }, [loadSavedRecipe, recipeDoc, recipeQuery.data, routeIsRecipe, sourceDuration]);

  const beginCueDrag = useCallback((
    event: ReactPointerEvent<HTMLElement>,
    cueType: "shot" | "caption" | "graphic" | "audio",
    cueId: string,
  ) => {
    const trackId = cueType === "shot" ? "shots" : cueType === "caption" ? "captions" : cueType === "graphic" ? "graphics" : "audio";
    if (event.button !== 0 || timelineDuration <= 0 || trackStates[trackId].locked) return;
    const dragTarget = event.currentTarget;
    const lane = dragTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    const cue = cueType === "caption"
      ? captions.find((item) => item.id === cueId)
      : cueType === "graphic"
        ? graphicLayers.find((item) => item.id === cueId)
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
      else if (cueType === "graphic") updateGraphic(cueId, { start, end });
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
  }, [audioCues, beginEditorTransaction, captions, finishEditorTransaction, graphicLayers, shotBeats, snapTimelineTime, timelineDuration, trackStates, updateAudioCue, updateCaption, updateGraphic, updateShot]);

  const beginCueResize = useCallback((
    event: ReactPointerEvent<HTMLSpanElement>,
    cueType: "shot" | "caption" | "graphic" | "audio",
    cueId: string,
    edge: "start" | "end",
  ) => {
    const trackId = cueType === "shot" ? "shots" : cueType === "caption" ? "captions" : cueType === "graphic" ? "graphics" : "audio";
    if (event.button !== 0 || timelineDuration <= 0 || trackStates[trackId].locked) return;
    const resizeTarget = event.currentTarget;
    const lane = event.currentTarget.closest(".ve-track-lane");
    if (!(lane instanceof HTMLElement)) return;
    const rect = lane.getBoundingClientRect();
    const cue = cueType === "caption"
      ? captions.find((item) => item.id === cueId)
      : cueType === "graphic"
        ? graphicLayers.find((item) => item.id === cueId)
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
        else if (cueType === "graphic") updateGraphic(cueId, { start });
        else if (cueType === "audio") updateAudioCue(cueId, { start });
        else updateShot(cueId, { start });
        setPlayhead(start);
      } else {
        const end = clamp(time, cue.start + 0.05, timelineDuration);
        if (cueType === "caption") updateCaption(cueId, { end });
        else if (cueType === "graphic") updateGraphic(cueId, { end });
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
  }, [audioCues, beginEditorTransaction, captions, finishEditorTransaction, graphicLayers, shotBeats, snapTimelineTime, timelineDuration, trackStates, updateAudioCue, updateCaption, updateGraphic, updateShot]);

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

  const splitClipAtTime = useCallback((timelineTime: number) => {
    if (trackStates.video.locked) {
      toast.warning(veText("toast.video_locked"));
      return;
    }
    const mapped = mapTimelineTime(timelineTime, clips);
    if (!mapped) return;
    const offset = timelineTime - mapped.timelineStart;
    const duration = getClipTimelineDuration(mapped.clip);
    if (offset < 0.12 || duration - offset < 0.12) {
      toast.warning(veText("toast.playhead_outside_clip"), veText("toast.playhead_outside_clip_detail"));
      return;
    }
    const splitSource = getClipSourceTime(mapped.clip, offset);
    const left: ClipSegment = {
      ...mapped.clip,
      id: makeId("clip"),
      label: `${mapped.clip.label} A`,
      sourceEnd: splitSource,
      fadeOut: 0,
      keyframes: mapped.clip.keyframes.filter((keyframe) => keyframe.time <= offset + 0.005),
    };
    const right: ClipSegment = {
      ...mapped.clip,
      id: makeId("clip"),
      label: `${mapped.clip.label} B`,
      sourceStart: splitSource,
      fadeIn: 0,
      keyframes: mapped.clip.keyframes
        .filter((keyframe) => keyframe.time >= offset - 0.005)
        .map((keyframe) => ({ ...keyframe, time: Math.max(0, keyframe.time - offset) })),
      color: CLIP_COLORS[(mapped.index + 1) % CLIP_COLORS.length],
    };
    setClips((current) => [
      ...current.slice(0, mapped.index),
      left,
      right,
      ...current.slice(mapped.index + 1),
    ]);
    setSelection({ type: "clip", id: right.id });
  }, [clips, toast, trackStates.video.locked]);

  const splitClipAtPlayhead = useCallback(() => {
    splitClipAtTime(playhead);
  }, [playhead, splitClipAtTime]);

  const razorSplitClip = useCallback((event: ReactPointerEvent<HTMLElement>, span: ClipTimelineSpan) => {
    if (event.button !== 0 || trackStates.video.locked) return;
    const rect = event.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    event.preventDefault();
    event.stopPropagation();
    const progress = clamp((event.clientX - rect.left) / rect.width, 0, 1);
    const splitTime = snapTimelineTime(span.start + span.duration * progress);
    setRazorPreview(null);
    setPlayhead(splitTime);
    splitClipAtTime(splitTime);
  }, [snapTimelineTime, splitClipAtTime, trackStates.video.locked]);

  const addTextLayer = useCallback((style: CaptionCue["style"]) => {
    if (trackStates.captions.locked) {
      toast.warning(veText("toast.captions_locked"));
      return;
    }
    const start = snapTimelineTime(playhead);
    const isTitle = style === "titleCard";
    const isLowerThird = style === "lowerThird";
    const cue: CaptionCue = {
      id: makeId("caption"),
      speaker: "",
      emotion: "",
      style,
      text: veText(isTitle ? "default.title_text" : isLowerThird ? "default.lower_third_text" : "default.caption_text"),
      start,
      end: clamp(start + (isTitle ? 3 : 2.2), start + 0.05, Math.max(start + 0.05, timelineDuration)),
      x: isLowerThird ? 8 : 50,
      y: isTitle ? 42 : isLowerThird ? 82 : 84,
      scale: 1,
      rotation: 0,
      opacity: 1,
      keyframes: [],
      size: isTitle ? 64 : isLowerThird ? 38 : 32,
      color: isTitle || isLowerThird ? "#ffffff" : "#1c1917",
      background: isTitle ? "rgba(15,23,42,0.56)" : isLowerThird ? "rgba(15,23,42,0.9)" : "rgba(255,255,255,0.94)",
      backgroundColor: isTitle || isLowerThird ? "#0f172a" : "#ffffff",
      backgroundOpacity: isTitle ? 0.56 : isLowerThird ? 0.9 : 0.94,
      align: isLowerThird ? "left" : "center",
      parentId: null,
      ...normalizeCaptionVisualStyle({
        fontFamily: isTitle ? "display" : "sans",
        fontWeight: isTitle ? 800 : 700,
        maxWidth: isTitle ? 72 : 78,
        reveal: isTitle ? "words" : "none",
      }),
    };
    setCaptions((current) => [...current, cue]);
    setSelection({ type: "caption", id: cue.id });
  }, [playhead, snapTimelineTime, timelineDuration, toast, trackStates.captions.locked]);

  const addCaption = useCallback(() => {
    addTextLayer("speechBubble");
  }, [addTextLayer]);

  const addGraphicLayer = useCallback((kind: Exclude<GraphicLayerKind, "image" | "video">) => {
    if (trackStates.graphics.locked) {
      toast.warning(veText("toast.graphics_locked"));
      return;
    }
    const start = snapTimelineTime(playhead);
    const index = graphicLayers.length + 1;
    const graphic: GraphicLayer = {
      id: makeId("graphic"),
      kind,
      label: `${graphicKindDisplayLabel(kind)} ${index}`,
      start,
      end: clamp(start + 3, start + 0.05, Math.max(start + 0.05, timelineDuration)),
      x: 50,
      y: 50,
      scale: 1,
      rotation: 0,
      opacity: kind === "group" || kind === "particle" || kind === "shader" ? 1 : kind === "ellipse" ? 0.88 : 0.82,
      keyframes: [],
      width: kind === "group" ? 72 : kind === "shader" ? 100 : kind === "particle" ? 54 : kind === "ellipse" ? 18 : kind === "line" ? 32 : 24,
      height: kind === "group" ? 72 : kind === "shader" ? 100 : kind === "particle" ? 54 : kind === "ellipse" ? 18 : kind === "line" ? 1.2 : 14,
      fill: kind === "shader" ? "#080b16" : kind === "particle" ? "#f8c95c" : kind === "ellipse" ? "#cf9b44" : "#5f928a",
      stroke: kind === "shader" ? "#f4a949" : kind === "particle" ? "#ff6b7a" : "#ffffff",
      strokeWidth: 0,
      cornerRadius: kind === "rectangle" ? 12 : 0,
      assetDocumentId: null,
      assetName: null,
      assetMimeType: null,
      parentId: null,
      clipChildren: kind === "group",
      isTemplate: false,
      instanceOf: null,
      ...normalizeGraphicVisualStyle({
        pathData: kind === "path" ? "M 50 4 L 96 92 L 68 82 L 50 58 L 32 82 L 4 92 Z" : "",
        zIndex: 10,
      }),
      ...(kind === "particle" ? normalizeParticleLayer({
        particleCount: 28,
        particleShape: "confetti",
        particleMotion: "burst",
        particleSeed: index * 137,
        particleLoop: true,
        fill: "#f8c95c",
        fillSecondary: "#65d6c4",
        stroke: "#ff6b7a",
      }) : {}),
      ...(kind === "shader" ? normalizeVideoEditorShaderStyle({
        ...DEFAULT_VIDEO_EDITOR_SHADER_STYLE,
        shaderSeed: index * 137,
      }) : {}),
    };
    setGraphicLayers((current) => [...current, graphic]);
    setSelection({ type: "graphic", id: graphic.id });
  }, [graphicLayers.length, playhead, snapTimelineTime, timelineDuration, toast, trackStates.graphics.locked]);

  const handleGraphicUpload = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || !doc) return;
    if (!file.type.startsWith("image/")) {
      toast.warning(veText("toast.image_required"));
      return;
    }
    if (trackStates.graphics.locked) {
      toast.warning(veText("toast.graphics_locked"));
      return;
    }
    setUploadingGraphic(true);
    try {
      const uploaded = await api.documents.upload(file, doc.folder_id ?? undefined);
      const image = await loadGraphicImageAsset(uploaded.id);
      const start = snapTimelineTime(playhead);
      const width = 32;
      const height = clamp(
        width * (image.naturalHeight / Math.max(1, image.naturalWidth)) * (mediaSize.width / Math.max(1, mediaSize.height)),
        4,
        70,
      );
      const graphic: GraphicLayer = {
        id: makeId("graphic"),
        kind: "image",
        label: baseName(uploaded.name),
        start,
        end: clamp(start + 4, start + 0.05, Math.max(start + 0.05, timelineDuration)),
        x: 50,
        y: 50,
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
        width,
        height,
        fill: "#ffffff",
        stroke: "#ffffff",
        strokeWidth: 0,
        cornerRadius: 0,
        assetDocumentId: uploaded.id,
        assetName: uploaded.name,
        assetMimeType: uploaded.mime_type || uploaded.file_type || file.type,
        ...normalizeGraphicVisualStyle({ assetFit: "cover", zIndex: 20 }),
      };
      setGraphicLayers((current) => [...current, graphic]);
      setGraphicAssetRevision((version) => version + 1);
      setSelection({ type: "graphic", id: graphic.id });
      await invalidateKnowledgeQueries(queryClient);
      toast.success(veText("toast.image_overlay_added"), uploaded.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.image_overlay_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingGraphic(false);
    }
  }, [doc, mediaSize.height, mediaSize.width, playhead, queryClient, snapTimelineTime, timelineDuration, toast, trackStates.graphics.locked]);

  const handleGraphicVideoUpload = useCallback(async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || !doc) return;
    if (!file.type.startsWith("video/")) {
      toast.warning(veText("toast.video_required"));
      return;
    }
    if (trackStates.graphics.locked) {
      toast.warning(veText("toast.graphics_locked"));
      return;
    }
    setUploadingGraphicVideo(true);
    try {
      const fileDuration = await readVideoFileDuration(file);
      const uploaded = await api.documents.upload(file, doc.folder_id ?? undefined);
      const video = await loadGraphicVideoAsset(uploaded.id);
      const assetDuration = fileDuration > 0
        ? fileDuration
        : Number.isFinite(video.duration) && video.duration > 0
          ? video.duration
          : 4;
      const start = snapTimelineTime(playhead);
      const visibleDuration = Math.min(assetDuration, Math.max(0.05, timelineDuration - start));
      const width = 36;
      const height = clamp(
        width * ((video.videoHeight || 9) / Math.max(1, video.videoWidth || 16)) * (mediaSize.width / Math.max(1, mediaSize.height)),
        6,
        72,
      );
      const graphic: GraphicLayer = {
        id: makeId("graphic-video"),
        kind: "video",
        label: baseName(uploaded.name),
        start,
        end: clamp(start + visibleDuration, start + 0.05, Math.max(start + 0.05, timelineDuration)),
        x: 50,
        y: 50,
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
        width,
        height,
        fill: "#ffffff",
        stroke: "#ffffff",
        strokeWidth: 0,
        cornerRadius: 5,
        assetDocumentId: uploaded.id,
        assetName: uploaded.name,
        assetMimeType: uploaded.mime_type || uploaded.file_type || file.type,
        assetDuration,
        sourceStart: 0,
        sourceEnd: assetDuration,
        speed: 1,
        loop: false,
        ...normalizeGraphicVisualStyle({ assetFit: "cover", zIndex: 20 }),
      };
      setGraphicLayers((current) => [...current, graphic]);
      setGraphicAssetRevision((version) => version + 1);
      setSelection({ type: "graphic", id: graphic.id });
      await invalidateKnowledgeQueries(queryClient);
      toast.success(veText("toast.video_overlay_added"), uploaded.name);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.video_overlay_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingGraphicVideo(false);
    }
  }, [doc, mediaSize.height, mediaSize.width, playhead, queryClient, snapTimelineTime, timelineDuration, toast, trackStates.graphics.locked]);

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
      x: 50,
      y: 50,
      scale: 1,
      rotation: 0,
      opacity: 1,
      keyframes: [],
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
      await invalidateKnowledgeQueries(queryClient);
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
        speed: 1,
        fadeIn: 0,
        fadeOut: 0,
        muted: false,
        color: CLIP_COLORS[clips.length % CLIP_COLORS.length],
        fit: "contain",
        x: 50,
        y: 50,
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
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
        setGraphicLayers(nextState.graphicLayers);
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
      sourceStart: 0,
      sourceEnd: null,
      assetDuration: null,
      assetDocumentId: null,
      assetName: null,
      assetMimeType: null,
      catalogTrackId: null,
      musicCreator: null,
      musicLicense: null,
      musicLicenseName: null,
      musicLicenseUrl: null,
      musicSourceUrl: null,
      musicAttribution: null,
      sourcePlan: type === "dialogue"
        ? veText("default.dialogue_source_plan")
        : veText("default.audio_source_plan"),
      prompt: "",
    };
    setAudioCues((current) => [...current, cue]);
    setSelection({ type: "audio", id: cue.id });
  }, [playhead, snapTimelineTime, timelineDuration, toast, trackStates.audio.locked]);

  const addAudioCueFromDocument = useCallback(async (
    asset: Document,
    startOverride?: number,
    musicTrack?: FreeMusicTrack,
  ) => {
    if (trackStates.audio.locked) {
      toast.warning(veText("toast.audio_locked"));
      return;
    }
    const type = musicTrack ? "music" : inferAudioCueType(asset.name);
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
      sourceStart: 0,
      sourceEnd: assetDuration > 0 ? assetDuration : null,
      assetDuration: assetDuration > 0 ? assetDuration : null,
      assetDocumentId: asset.id,
      assetName: asset.name,
      assetMimeType: asset.mime_type || asset.file_type || null,
      catalogTrackId: musicTrack?.id ?? null,
      musicCreator: musicTrack?.creator ?? null,
      musicLicense: musicTrack?.license ?? null,
      musicLicenseName: musicTrack?.license_name ?? null,
      musicLicenseUrl: musicTrack?.license_url ?? null,
      musicSourceUrl: musicTrack?.source_url ?? null,
      musicAttribution: musicTrack?.attribution ?? null,
      sourcePlan: musicTrack
        ? veText("free_music.source_plan", { source: musicTrack.source, license: musicTrack.license_name })
        : veText("default.asset_source_plan"),
      prompt: "",
    };
    setAudioCues((current) => [...current, cue]);
    setSelection({ type: "audio", id: cue.id });
    setPlayhead(start);
    toast.success(
      musicTrack ? veText("toast.free_music_added") : veText("toast.audio_asset_added"),
      musicTrack?.title || asset.name,
    );
  }, [playhead, snapTimelineTime, timelineDuration, toast, trackStates.audio.locked]);

  const stopFreeMusicPreview = useCallback(() => {
    const preview = freeMusicPreviewRef.current;
    if (preview) {
      preview.audio.ontimeupdate = null;
      preview.audio.onloadedmetadata = null;
      preview.audio.ondurationchange = null;
      preview.audio.onended = null;
      preview.audio.onerror = null;
      preview.audio.pause();
      preview.audio.removeAttribute("src");
      preview.audio.load();
    }
    freeMusicPreviewRef.current = null;
    setPreviewingFreeMusicId(null);
    setFreeMusicPreviewProgress(null);
  }, []);

  const toggleFreeMusicPreview = useCallback(async (track: FreeMusicTrack) => {
    if (freeMusicPreviewRef.current?.trackId === track.id) {
      stopFreeMusicPreview();
      return;
    }
    pausePlayback();
    stopMediaAssetPreview();
    stopFreeMusicPreview();
    const audio = new Audio(track.preview_url);
    audio.preload = "metadata";
    audio.volume = 0.82;
    const syncPreviewProgress = () => {
      if (freeMusicPreviewRef.current?.audio !== audio) return;
      const measuredDuration = Number.isFinite(audio.duration) && audio.duration > 0
        ? audio.duration
        : track.duration_seconds || 0;
      setFreeMusicPreviewProgress({
        trackId: track.id,
        currentTime: Number.isFinite(audio.currentTime) ? Math.max(0, audio.currentTime) : 0,
        duration: measuredDuration,
      });
    };
    audio.ontimeupdate = syncPreviewProgress;
    audio.onloadedmetadata = syncPreviewProgress;
    audio.ondurationchange = syncPreviewProgress;
    audio.onended = () => {
      if (freeMusicPreviewRef.current?.trackId === track.id) stopFreeMusicPreview();
    };
    audio.onerror = () => {
      if (freeMusicPreviewRef.current?.trackId === track.id) stopFreeMusicPreview();
      toast.error(veText("toast.free_music_preview_failed"));
    };
    freeMusicPreviewRef.current = { trackId: track.id, audio };
    setPreviewingFreeMusicId(track.id);
    setFreeMusicPreviewProgress({
      trackId: track.id,
      currentTime: 0,
      duration: track.duration_seconds || 0,
    });
    try {
      await audio.play();
    } catch (error) {
      console.warn("Free music preview could not play", error);
      stopFreeMusicPreview();
      toast.warning(veText("toast.preview_blocked"), veText("toast.preview_blocked_detail"));
    }
  }, [pausePlayback, stopFreeMusicPreview, stopMediaAssetPreview, toast]);

  const openFreeMusicLibrary = useCallback(() => {
    if (trackStates.audio.locked) {
      toast.warning(veText("toast.audio_locked"));
      return;
    }
    setFreeMusicOpen(true);
  }, [toast, trackStates.audio.locked]);

  const closeFreeMusicLibrary = useCallback(() => {
    stopFreeMusicPreview();
    setFreeMusicOpen(false);
  }, [stopFreeMusicPreview]);

  const submitFreeMusicSearch = useCallback(() => {
    const query = freeMusicSearch.trim();
    if (query.length < 2) return;
    if (query === submittedFreeMusicSearch) {
      void freeMusicQuery.refetch();
    } else {
      setSubmittedFreeMusicSearch(query);
    }
  }, [freeMusicQuery, freeMusicSearch, submittedFreeMusicSearch]);

  const chooseFreeMusicSearch = useCallback((query: string) => {
    setFreeMusicSearch(query);
    setSubmittedFreeMusicSearch(query);
  }, []);

  const importFreeMusic = useCallback(async (track: FreeMusicTrack) => {
    if (!doc) {
      toast.error(veText("toast.free_music_add_failed"));
      return;
    }
    if (importingFreeMusicId || trackStates.audio.locked || addedFreeMusicIds.has(track.id)) return;
    setImportingFreeMusicId(track.id);
    try {
      const result = await api.videoEditor.importFreeMusic(track.id, doc.id);
      await addAudioCueFromDocument(result.document, playhead, result.track);
      await queryClient.invalidateQueries({ queryKey: ["video-editor-assets"] });
      await invalidateKnowledgeQueries(queryClient);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.free_music_add_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setImportingFreeMusicId(null);
    }
  }, [addAudioCueFromDocument, addedFreeMusicIds, doc, importingFreeMusicId, playhead, queryClient, toast, trackStates.audio.locked]);

  useEffect(() => () => {
    const preview = freeMusicPreviewRef.current;
    if (preview) {
      preview.audio.ontimeupdate = null;
      preview.audio.onloadedmetadata = null;
      preview.audio.ondurationchange = null;
      preview.audio.onended = null;
      preview.audio.onerror = null;
      preview.audio.pause();
      preview.audio.removeAttribute("src");
    }
    freeMusicPreviewRef.current = null;
  }, []);

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
      await addAudioCueFromDocument(uploaded, playhead);
      await invalidateKnowledgeQueries(queryClient);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.add_audio_asset_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setUploadingAudio(false);
    }
  }, [addAudioCueFromDocument, doc, playhead, queryClient, toast, trackStates.audio.locked]);

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
      await invalidateKnowledgeQueries(queryClient);
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
      await invalidateKnowledgeQueries(queryClient);
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
    } else if (selection.type === "graphic") {
      setGraphicLayers((current) => current.filter((graphic) => graphic.id !== selection.id));
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
        keyframes: selectedShot.keyframes.map((keyframe) => ({ ...keyframe, id: makeId("motion") })),
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

    if (selectedGraphic) {
      const range = duplicateTimedRange(selectedGraphic, timelineDuration);
      const copy: GraphicLayer = {
        ...selectedGraphic,
        ...range,
        id: makeId("graphic"),
        label: veText("default.copy_label", { label: selectedGraphic.label }),
        keyframes: selectedGraphic.keyframes.map((keyframe) => ({ ...keyframe, id: makeId("motion") })),
      };
      setGraphicLayers((current) => sortTimedItems([...current, copy]));
      setSelection({ type: "graphic", id: copy.id });
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
    selectedGraphic,
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
      const duration = getClipTimelineDuration(clip);
      const hasManualEdit = clipHasManualEdit(clip, sourceDuration);
      if (hasManualEdit) {
        manualEdits.push({
          clip_id: clip.id,
          label: clip.label,
          timeline_start: cursor,
          timeline_end: cursor + duration,
          source_start: clip.sourceStart,
          source_end: clip.sourceEnd,
          playback_rate: normalizeVideoClipSpeed(clip.speed),
          visual: {
            fit: clip.fit,
            x: clip.x,
            y: clip.y,
            scale: clip.scale,
            rotation: clip.rotation,
            opacity: clip.opacity,
            fade_in: clip.fadeIn,
            fade_out: clip.fadeOut,
            keyframes: clip.keyframes,
          },
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
      version: 11,
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
        graphics: graphicLayers,
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
        tracks: ["markers", "shots", "replacement_clips", "graphics", "captions", "audio_cues"],
        expected_use: "Scene/shot planning, nested reusable layer groups, clip containers, keyframed SVG path drawing, parent scene-group animation, review markers, replacement clip patching, per-clip retiming, picture fades, frame-accurate deterministic easing, custom cubic Bézier timing, smooth spatial paths, independent axis and depth-tilt transforms, keyframed blur/effect intensity, video framing, production typography, gradients, vector paths, deterministic particle systems, native cinematic treatments, masks, timed graphics plus image and multi-video overlays, source-trimmed waveform audio, dialogue/SFX/BGM placement, and final MP4 render.",
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
        graphic_count: graphicLayers.length,
        audio_track_count: audioCues.length,
        editable_sources: ["clips", "shots", "graphics", "captions", "audio_cues", "markers"],
      },
      render_contract: {
        video: "Apply clip order, source/replacement asset trims, per-clip playback_rate, picture fade-in/fade-out, contain/cover framing, parent scene-group transforms, nested group transforms, clipChildren crops, reusable subscene instance timing, and frame-accurate seek-safe keyframes including pathProgress with independent scale axes, depth tilt, perspective strength, dynamic blur/effect intensity, easing IDs, custom cubic Bézier control points and spatial paths. Composite solid/gradient/vector/image layers, deterministic burst/drift/orbit particle systems, and multiple muted video overlays with independent sourceStart/sourceEnd trims, speed, loop, crop, z-order, shadow, blend, masks and native glass/glow/grain/scanline/chromatic/vignette/light-leak/film-burn/halation/anamorphic effects, then render production typography with deterministic word/character/wipe reveals.",
        audio: "Generate or attach cue stems, respect timeline start/end plus sourceStart/sourceEnd trims, loop only the selected source window, apply fade/ducking controls, track mute state, and volumeDb, then mix under dialogue.",
        export: "Server render should honor editor work area when enabled, produce final mp4 plus editable recipe sidecar, and preserve manual_edits.",
      },
    };
  }, [audioCues, captions, clips, doc, graphicLayers, markers, mediaSize.height, mediaSize.width, normalizedWorkArea, shotBeats, sourceDuration, timelineDuration, trackStates]);

  const editorLiveContent = useMemo(() => JSON.stringify({
    ...buildRecipe(),
    live_edit_context: {
      fps: VIDEO_EDITOR_FPS,
      playhead_seconds: snapMotionTimeToFrame(playhead, timelineDuration),
      playhead_frame: motionFrameNumberAtTime(playhead),
      selection: selection
        ? {
          ...selection,
          track: trackForSelectionType(selection.type),
          timeline_start: timelineTimeForSelection(selection, currentTrackState),
        }
        : null,
      animation_capabilities: [
        "clip_retiming",
        "picture_fades",
        "video_reframing",
        "graphics",
        "captions",
        "motion_keyframes",
        "scene_parent_group_keyframes",
        "nested_layer_groups",
        "clip_containers",
        "reusable_subscene_instances",
        "keyframed_path_drawing",
        "custom_cubic_bezier",
        "smooth_spatial_paths",
        "independent_axis_transforms",
        "depth_tilt_and_perspective",
        "keyframed_blur_and_effect_strength",
        "production_typography",
        "text_reveal_animation",
        "gradient_and_vector_layers",
        "layer_blur_shadow_and_blending",
        "native_cinematic_effects_and_masks",
        "media_crop_and_layer_order",
        "native_motion_design_generation",
        "source_trimmed_audio_waveforms",
        "multi_video_overlay_tracks",
        "deterministic_particle_layers",
      ],
      motion_design_presets: MOTION_DESIGN_PRESETS,
      motion_design_contract: {
        usage: "Set top-level motion_design with a preset and copy/theme fields. The editor consumes it into deterministic editable layers; omit it for ordinary layer edits.",
        fields: ["preset", "mode", "headline", "kicker", "subhead", "cta", "statValue", "statLabel", "background", "surface", "foreground", "accent", "accent2", "quality", "visualTone", "fontFamily"],
        modes: ["replace", "append"],
        quality_default: "production",
      },
      particle_contract: {
        usage: "Add or edit timeline.graphics items with kind='particle'. Particle state is a pure function of local timeline time and particleSeed, so preview, seek, and export match.",
        motions: PARTICLE_MOTIONS,
        shapes: PARTICLE_SHAPES,
        fields: ["particleCount", "particleShape", "particleMotion", "particleSeed", "particleSize", "particleSpeed", "particleGravity", "particleSpread", "particleLoop", "fill", "fillSecondary", "stroke"],
        count_range: [6, 40],
      },
      layer_group_contract: {
        usage: "Add timeline.graphics items with kind='group'. Set child graphics or captions parentId to the group id. Child x/y/width/height are percentages of the group bounds.",
        group_fields: ["parentId", "clipChildren", "isTemplate", "instanceOf", "start", "end", "x", "y", "width", "height", "keyframes"],
        path_draw_field: "pathProgress on a path base pose or motion keyframe, clamped from 0 to 1",
        reusable_subscene: "Set isTemplate=true on a source group, then instanceOf=<source group id> on another group. Instance time remaps to the source group's local timeline.",
      },
    },
  }, null, 2), [buildRecipe, currentTrackState, playhead, selection, timelineDuration]);
  editorLiveContentRef.current = editorLiveContent;

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
    if (!doc) return null;
    const result = await api.documents.list({
      folder_id: doc.folder_id ?? undefined,
      search: fileName,
      include_generated_assets: true,
      limit: 25,
    });
    return result.items.find((item) => item.name === fileName && isVideoEditRecipeDocument(item)) ?? null;
  }, [doc]);

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
      await api.videoEditor.linkRecipe(doc.id, uploaded.id);
      setRecipeDoc(uploaded);
      await invalidateKnowledgeQueries(queryClient);
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
    const sourceVideoSize = sourceVideo.videoWidth && sourceVideo.videoHeight
      ? { width: sourceVideo.videoWidth, height: sourceVideo.videoHeight }
      : { width: canvas.width, height: canvas.height };
    const activeClipMap = mapTimelineTime(timelineTime, clips);
    const activeClip = activeClipMap?.clip ?? null;
    const sceneGroup = shotBeats.find((shot) => timelineTime >= shot.start && timelineTime <= shot.end) ?? null;
    const sceneGroupPose = sceneGroup ? shotMotionPoseAtTimelineTime(sceneGroup, timelineTime) : null;
    const activeClipPose = activeClip && activeClipMap
      ? composeMotionPoses(
        clipMotionPoseAtLocalTime(activeClip, timelineTime - activeClipMap.timelineStart),
        sceneGroupPose,
      )
      : { x: 50, y: 50, scale: 1, rotation: 0, opacity: 1 };
    const activeClipEdgeFadeOpacity = activeClip && activeClipMap
      ? clipEdgeFadeOpacityAtLocalTime(activeClip, timelineTime - activeClipMap.timelineStart)
      : 1;
    const fittedSize = fittedMediaSize(
      { width: canvas.width, height: canvas.height },
      sourceVideoSize,
      activeClip?.fit ?? "contain",
    );
    if (trackStates.video.visible) {
      ctx.save();
      ctx.globalAlpha = activeClipPose.opacity * activeClipEdgeFadeOpacity;
      ctx.filter = (activeClipPose.blur ?? 0) > 0 ? `blur(${(activeClipPose.blur ?? 0) * (canvas.height / 1080)}px)` : "none";
      ctx.translate((activeClipPose.x / 100) * canvas.width, (activeClipPose.y / 100) * canvas.height);
      applyCanvasMotionTransform(ctx, activeClipPose);
      ctx.drawImage(sourceVideo, -fittedSize.width / 2, -fittedSize.height / 2, fittedSize.width, fittedSize.height);
      ctx.restore();
    }

    const drawNestedCaption = (
      caption: CaptionCue,
      frameTime: number,
      spaceWidth: number,
      spaceHeight: number,
    ) => {
      const motionPose = captionMotionPoseAtTimelineTime(caption, frameTime);
      const visualStyle = normalizeCaptionVisualStyle(caption);
      const fontSize = Math.round((caption.size / 1080) * canvas.height);
      const pixelScale = canvas.height / 1080;
      ctx.save();
      ctx.globalAlpha *= motionPose.opacity;
      ctx.filter = (motionPose.blur ?? 0) > 0 ? `blur(${(motionPose.blur ?? 0) * pixelScale}px)` : "none";
      ctx.translate((motionPose.x / 100) * spaceWidth, (motionPose.y / 100) * spaceHeight);
      applyCanvasMotionTransform(ctx, motionPose);
      ctx.font = `${visualStyle.fontWeight} ${fontSize}px ${captionFontStack(visualStyle.fontFamily)}`;
      (ctx as CanvasRenderingContext2D & { letterSpacing: string }).letterSpacing = `${visualStyle.letterSpacing * pixelScale}px`;
      ctx.textAlign = caption.align;
      ctx.textBaseline = "middle";
      const display = captionDisplay(caption);
      const rawText = caption.speaker ? `${caption.speaker}: ${caption.text}` : caption.text;
      const transformedText = transformCaptionText(rawText, visualStyle.textTransform);
      const localTime = Math.max(0, frameTime - caption.start);
      const captionText = revealedCaptionText(transformedText, visualStyle.reveal, localTime, visualStyle.revealDuration);
      const maxTextWidth = spaceWidth * (visualStyle.maxWidth / 100);
      const lines = wrapCanvasText(ctx, captionText, maxTextWidth);
      const lineHeight = fontSize * visualStyle.lineHeight;
      const paddingX = fontSize * visualStyle.paddingX;
      const paddingY = fontSize * visualStyle.paddingY;
      const blockWidth = Math.min(spaceWidth * 0.96, Math.max(...lines.map((line) => ctx.measureText(line).width), 0) + paddingX * 2);
      const blockHeight = lines.length * lineHeight + paddingY * 2;
      const boxX = caption.align === "left" ? -paddingX : caption.align === "right" ? -blockWidth + paddingX : -blockWidth / 2;
      const boxY = -blockHeight / 2;
      if (visualStyle.reveal === "wipe") {
        const wipeProgress = clamp(localTime / Math.max(0.05, visualStyle.revealDuration), 0, 1);
        ctx.beginPath();
        ctx.rect(boxX, boxY, blockWidth * wipeProgress, blockHeight);
        ctx.clip();
      }
      ctx.fillStyle = display.background;
      drawRoundedRect(ctx, boxX, boxY, blockWidth, blockHeight, fontSize * visualStyle.cornerRadius);
      ctx.fill();
      ctx.shadowColor = visualStyle.textShadowColor;
      ctx.shadowBlur = visualStyle.textShadowBlur * pixelScale;
      ctx.shadowOffsetX = visualStyle.textShadowOffsetX * pixelScale;
      ctx.shadowOffsetY = visualStyle.textShadowOffsetY * pixelScale;
      ctx.fillStyle = display.color;
      lines.forEach((line, index) => {
        const lineY = -((lines.length - 1) * lineHeight) / 2 + index * lineHeight;
        if (visualStyle.strokeWidth > 0) {
          ctx.strokeStyle = visualStyle.strokeColor;
          ctx.lineWidth = visualStyle.strokeWidth * pixelScale;
          ctx.lineJoin = "round";
          ctx.strokeText(line, 0, lineY);
        }
        ctx.fillText(line, 0, lineY);
      });
      ctx.restore();
    };

    const drawNestedGraphic = (
      graphic: GraphicLayer,
      frameTime: number,
      spaceWidth: number,
      spaceHeight: number,
      rootScenePose: MotionPose | null,
      ancestors = new Set<string>(),
    ) => {
      if (ancestors.has(graphic.id) || ancestors.size > 12) return;
      const nextAncestors = new Set(ancestors);
      nextAncestors.add(graphic.id);
      const localPose = graphicMotionPoseAtTimelineTime(graphic, frameTime);
      const motionPose = rootScenePose ? composeMotionPoses(localPose, rootScenePose) : localPose;
      const visualStyle = normalizeGraphicVisualStyle({
        ...graphic,
        blur: motionPose.blur ?? graphic.blur,
        effectStrength: motionPose.effectStrength ?? graphic.effectStrength,
      });
      const width = (graphic.width / 100) * spaceWidth;
      const height = (graphic.height / 100) * spaceHeight;
      const pixelScale = canvas.height / 1080;
      ctx.save();
      ctx.globalAlpha *= motionPose.opacity;
      ctx.globalCompositeOperation = canvasBlendMode(visualStyle.blendMode);
      ctx.filter = visualStyle.blur > 0 ? `blur(${visualStyle.blur * pixelScale}px)` : "none";
      ctx.shadowColor = visualStyle.effect === "glow" ? visualStyle.fillSecondary : visualStyle.shadowColor;
      ctx.shadowBlur = Math.max(visualStyle.shadowBlur, visualStyle.effect === "glow" ? 8 + visualStyle.effectStrength * 42 : 0) * pixelScale;
      ctx.shadowOffsetX = visualStyle.shadowOffsetX * pixelScale;
      ctx.shadowOffsetY = visualStyle.shadowOffsetY * pixelScale;
      ctx.translate((motionPose.x / 100) * spaceWidth, (motionPose.y / 100) * spaceHeight);
      applyCanvasMotionTransform(ctx, motionPose);

      if (graphic.kind === "group") {
        if (graphic.clipChildren || visualStyle.maskShape !== "none") {
          drawCanvasMaskPath(ctx, width, height, visualStyle.maskShape, (Math.min(width, height) * graphic.cornerRadius) / 100);
          ctx.clip();
        }
        const sourceGroup = graphic.instanceOf
          ? graphicLayers.find((candidate) => candidate.id === graphic.instanceOf && candidate.kind === "group") ?? graphic
          : graphic;
        if (sourceGroup.id !== graphic.id && nextAncestors.has(sourceGroup.id)) {
          ctx.restore();
          return;
        }
        if (sourceGroup.id !== graphic.id) nextAncestors.add(sourceGroup.id);
        const sourceTime = sourceGroup.id === graphic.id ? frameTime : sourceGroup.start + (frameTime - graphic.start);
        ctx.translate(-width / 2, -height / 2);
        graphicLayers
          .filter((candidate) => candidate.parentId === sourceGroup.id
            && !candidate.isTemplate
            && sourceTime >= candidate.start
            && sourceTime <= candidate.end)
          .sort((left, right) => layerZIndex(left) - layerZIndex(right))
          .forEach((child) => drawNestedGraphic(child, sourceTime, width, height, null, nextAncestors));
        if (trackStates.captions.visible) {
          captions
            .filter((caption) => caption.parentId === sourceGroup.id
              && sourceTime >= caption.start
              && sourceTime <= caption.end)
            .sort((left, right) => layerZIndex(left, 100) - layerZIndex(right, 100))
            .forEach((caption) => drawNestedCaption(caption, sourceTime, width, height));
        }
        ctx.restore();
        return;
      }

      if (graphic.kind === "shader") {
        let surface = graphicShaderRenderCache.get(graphic.id);
        if (!surface) {
          surface = createVideoEditorShaderSurface(width, height);
          graphicShaderRenderCache.set(graphic.id, surface);
        } else {
          resizeVideoEditorShaderSurface(surface, width, height);
        }
        renderVideoEditorShaderFrame(surface, {
          time: frameTime - graphic.start,
          duration: Math.max(0.05, graphic.end - graphic.start),
          colorA: graphic.fill,
          colorB: visualStyle.fillSecondary,
          colorC: graphic.stroke,
          style: graphic,
        });
        ctx.drawImage(surface.canvas, -width / 2, -height / 2, width, height);
        drawCanvasGraphicEffect(ctx, width, height, visualStyle, pixelScale);
        ctx.restore();
        return;
      }
      if ((graphic.kind === "image" || graphic.kind === "video") && graphic.assetDocumentId) {
        const image = graphic.kind === "image" ? graphicImageElementCache.get(graphic.assetDocumentId) : null;
        const overlayVideo = graphic.kind === "video" ? graphicVideoRenderElementCache.get(graphic.id)?.video ?? null : null;
        const media = image?.complete && image.naturalWidth > 0
          ? image
          : overlayVideo && overlayVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && overlayVideo.videoWidth > 0
            ? overlayVideo
            : null;
        if (media) {
          drawCanvasMaskPath(ctx, width, height, visualStyle.maskShape, (Math.min(width, height) * graphic.cornerRadius) / 100);
          ctx.clip();
          drawCanvasImageFitted(ctx, media, width, height, visualStyle.assetFit);
          drawCanvasGraphicEffect(ctx, width, height, visualStyle, pixelScale);
        }
        ctx.restore();
        return;
      }
      if (graphic.kind === "particle") {
        drawParticleLayer(ctx, graphic, frameTime - graphic.start, Math.max(0.05, graphic.end - graphic.start), width, height);
        ctx.restore();
        return;
      }
      ctx.beginPath();
      if (visualStyle.maskShape !== "none" && graphic.kind !== "path") {
        drawCanvasMaskPath(ctx, width, height, visualStyle.maskShape);
      } else if (graphic.kind === "ellipse") {
        ctx.ellipse(0, 0, width / 2, height / 2, 0, 0, Math.PI * 2);
      } else if (graphic.kind === "path" && visualStyle.pathData && typeof Path2D !== "undefined") {
        try {
          const vectorPath = new Path2D(visualStyle.pathData);
          ctx.save();
          ctx.translate(-width / 2, -height / 2);
          ctx.scale(width / 100, height / 100);
          const pathProgress = motionPose.pathProgress ?? 1;
          if (pathProgress >= 0.999) {
            ctx.fillStyle = canvasGraphicFill(ctx, 100, 100, graphic.fill, visualStyle);
            ctx.fill(vectorPath);
          }
          ctx.strokeStyle = graphic.stroke;
          ctx.lineWidth = Math.max(0.25, graphic.strokeWidth * (100 / Math.max(width, 1)) * pixelScale);
          if (graphic.strokeWidth > 0 && pathProgress >= 0.999) ctx.stroke(vectorPath);
          if (graphic.strokeWidth > 0 && pathProgress < 0.999 && drawCanvasPartialSvgPath(ctx, visualStyle.pathData, pathProgress)) {
            ctx.lineCap = "round";
            ctx.lineJoin = "round";
            ctx.stroke();
          }
          ctx.restore();
          ctx.restore();
          return;
        } catch {
          drawRoundedRect(ctx, -width / 2, -height / 2, width, height, 0);
        }
      } else {
        const radius = graphic.kind === "line" ? Math.min(width, height) / 2 : (Math.min(width, height) * graphic.cornerRadius) / 100;
        drawRoundedRect(ctx, -width / 2, -height / 2, width, height, radius);
      }
      ctx.fillStyle = canvasGraphicFill(ctx, width, height, graphic.fill, visualStyle);
      ctx.fill();
      if (graphic.strokeWidth > 0) {
        ctx.strokeStyle = graphic.stroke;
        ctx.lineWidth = Math.max(1, (graphic.strokeWidth / 1080) * canvas.height);
        ctx.stroke();
      }
      ctx.save();
      ctx.clip();
      drawCanvasGraphicEffect(ctx, width, height, visualStyle, pixelScale);
      ctx.restore();
      ctx.restore();
    };

    const activeGraphics = trackStates.graphics.visible
      ? graphicLayers
        .filter((graphic) => timelineTime >= graphic.start
          && timelineTime <= graphic.end
          && !graphic.isTemplate
          && (!graphic.parentId || !graphicGroupIds.has(graphic.parentId)))
        .sort((left, right) => layerZIndex(left) - layerZIndex(right))
      : [];
    for (const graphic of activeGraphics) {
      if (graphic.kind === "group") {
        drawNestedGraphic(graphic, timelineTime, canvas.width, canvas.height, sceneGroupPose);
        continue;
      }
      const motionPose = composeMotionPoses(
        graphicMotionPoseAtTimelineTime(graphic, timelineTime),
        sceneGroupPose,
      );
      const visualStyle = normalizeGraphicVisualStyle({
        ...graphic,
        blur: motionPose.blur ?? graphic.blur,
        effectStrength: motionPose.effectStrength ?? graphic.effectStrength,
      });
      const width = (graphic.width / 100) * canvas.width;
      const height = (graphic.height / 100) * canvas.height;
      const pixelScale = canvas.height / 1080;
      ctx.save();
      ctx.globalAlpha = motionPose.opacity;
      ctx.globalCompositeOperation = canvasBlendMode(visualStyle.blendMode);
      ctx.filter = visualStyle.blur > 0 ? `blur(${visualStyle.blur * pixelScale}px)` : "none";
      ctx.shadowColor = visualStyle.effect === "glow" ? visualStyle.fillSecondary : visualStyle.shadowColor;
      ctx.shadowBlur = Math.max(
        visualStyle.shadowBlur,
        visualStyle.effect === "glow" ? 8 + visualStyle.effectStrength * 42 : 0,
      ) * pixelScale;
      ctx.shadowOffsetX = visualStyle.shadowOffsetX * pixelScale;
      ctx.shadowOffsetY = visualStyle.shadowOffsetY * pixelScale;
      ctx.translate((motionPose.x / 100) * canvas.width, (motionPose.y / 100) * canvas.height);
      applyCanvasMotionTransform(ctx, motionPose);
      if (graphic.kind === "shader") {
        let surface = graphicShaderRenderCache.get(graphic.id);
        if (!surface) {
          surface = createVideoEditorShaderSurface(width, height);
          graphicShaderRenderCache.set(graphic.id, surface);
        } else {
          resizeVideoEditorShaderSurface(surface, width, height);
        }
        renderVideoEditorShaderFrame(surface, {
          time: timelineTime - graphic.start,
          duration: Math.max(0.05, graphic.end - graphic.start),
          colorA: graphic.fill,
          colorB: visualStyle.fillSecondary,
          colorC: graphic.stroke,
          style: graphic,
        });
        ctx.drawImage(surface.canvas, -width / 2, -height / 2, width, height);
        drawCanvasGraphicEffect(ctx, width, height, visualStyle, pixelScale);
        ctx.restore();
        continue;
      }
      if ((graphic.kind === "image" || graphic.kind === "video") && graphic.assetDocumentId) {
        const image = graphic.kind === "image"
          ? graphicImageElementCache.get(graphic.assetDocumentId)
          : null;
        const overlayVideo = graphic.kind === "video"
          ? graphicVideoRenderElementCache.get(graphic.id)?.video ?? null
          : null;
        const media = image?.complete && image.naturalWidth > 0
          ? image
          : overlayVideo && overlayVideo.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA && overlayVideo.videoWidth > 0
            ? overlayVideo
            : null;
        if (media) {
          const radius = (Math.min(width, height) * graphic.cornerRadius) / 100;
          drawCanvasMaskPath(ctx, width, height, visualStyle.maskShape, radius);
          ctx.clip();
          drawCanvasImageFitted(ctx, media, width, height, visualStyle.assetFit);
          drawCanvasGraphicEffect(ctx, width, height, visualStyle, pixelScale);
        }
      } else {
        if (graphic.kind === "particle") {
          drawParticleLayer(
            ctx,
            graphic,
            timelineTime - graphic.start,
            Math.max(0.05, graphic.end - graphic.start),
            width,
            height,
          );
          ctx.restore();
          continue;
        }
        ctx.beginPath();
        if (visualStyle.maskShape !== "none" && graphic.kind !== "path") {
          drawCanvasMaskPath(ctx, width, height, visualStyle.maskShape);
        } else if (graphic.kind === "ellipse") {
          ctx.ellipse(0, 0, width / 2, height / 2, 0, 0, Math.PI * 2);
        } else if (graphic.kind === "path" && visualStyle.pathData && typeof Path2D !== "undefined") {
          try {
            const vectorPath = new Path2D(visualStyle.pathData);
            ctx.save();
            ctx.translate(-width / 2, -height / 2);
            ctx.scale(width / 100, height / 100);
            const pathProgress = motionPose.pathProgress ?? 1;
            if (pathProgress >= 0.999) {
              ctx.fillStyle = canvasGraphicFill(ctx, 100, 100, graphic.fill, visualStyle);
              ctx.fill(vectorPath);
            }
            if (graphic.strokeWidth > 0 && pathProgress >= 0.999) {
              ctx.strokeStyle = graphic.stroke;
              ctx.lineWidth = Math.max(0.25, graphic.strokeWidth * (100 / Math.max(width, 1)) * pixelScale);
              ctx.stroke(vectorPath);
            }
            if (graphic.strokeWidth > 0 && pathProgress < 0.999 && drawCanvasPartialSvgPath(ctx, visualStyle.pathData, pathProgress)) {
              ctx.strokeStyle = graphic.stroke;
              ctx.lineWidth = Math.max(0.25, graphic.strokeWidth * (100 / Math.max(width, 1)) * pixelScale);
              ctx.lineCap = "round";
              ctx.lineJoin = "round";
              ctx.stroke();
            }
            if (pathProgress >= 0.999) {
              ctx.save();
              ctx.clip(vectorPath);
              drawCanvasGraphicEffect(ctx, 100, 100, visualStyle, Math.max(0.08, pixelScale * (100 / Math.max(height, 1))));
              ctx.restore();
            }
            ctx.restore();
            ctx.restore();
            continue;
          } catch {
            drawRoundedRect(ctx, -width / 2, -height / 2, width, height, 0);
          }
        } else {
          const radius = graphic.kind === "line"
            ? Math.min(width, height) / 2
            : (Math.min(width, height) * graphic.cornerRadius) / 100;
          drawRoundedRect(ctx, -width / 2, -height / 2, width, height, radius);
        }
        ctx.fillStyle = canvasGraphicFill(ctx, width, height, graphic.fill, visualStyle);
        ctx.fill();
        if (graphic.strokeWidth > 0) {
          ctx.strokeStyle = graphic.stroke;
          ctx.lineWidth = Math.max(1, (graphic.strokeWidth / 1080) * canvas.height);
          ctx.stroke();
        }
        ctx.save();
        ctx.clip();
        drawCanvasGraphicEffect(ctx, width, height, visualStyle, pixelScale);
        ctx.restore();
      }
      ctx.restore();
    }

    const active = trackStates.captions.visible
      ? captions.filter((caption) => timelineTime >= caption.start
        && timelineTime <= caption.end
        && (!caption.parentId || !graphicGroupIds.has(caption.parentId)))
      : [];
    for (const caption of active) {
      const motionPose = composeMotionPoses(
        captionMotionPoseAtTimelineTime(caption, timelineTime),
        sceneGroupPose,
      );
      const visualStyle = normalizeCaptionVisualStyle(caption);
      const fontSize = Math.round((caption.size / 1080) * canvas.height);
      const pixelScale = canvas.height / 1080;
      ctx.save();
      ctx.globalAlpha = motionPose.opacity;
      ctx.filter = (motionPose.blur ?? 0) > 0 ? `blur(${(motionPose.blur ?? 0) * pixelScale}px)` : "none";
      ctx.translate((motionPose.x / 100) * canvas.width, (motionPose.y / 100) * canvas.height);
      applyCanvasMotionTransform(ctx, motionPose);
      ctx.font = `${visualStyle.fontWeight} ${fontSize}px ${captionFontStack(visualStyle.fontFamily)}`;
      (ctx as CanvasRenderingContext2D & { letterSpacing: string }).letterSpacing = `${visualStyle.letterSpacing * pixelScale}px`;
      ctx.textAlign = caption.align;
      ctx.textBaseline = "middle";
      const display = captionDisplay(caption);
      const rawCaptionText = caption.speaker ? `${caption.speaker}: ${caption.text}` : caption.text;
      const transformedText = transformCaptionText(rawCaptionText, visualStyle.textTransform);
      const localTime = Math.max(0, timelineTime - caption.start);
      const captionText = revealedCaptionText(transformedText, visualStyle.reveal, localTime, visualStyle.revealDuration);
      const maxTextWidth = canvas.width * (visualStyle.maxWidth / 100);
      const lines = wrapCanvasText(ctx, captionText, maxTextWidth);
      const lineHeight = fontSize * visualStyle.lineHeight;
      const paddingX = fontSize * visualStyle.paddingX;
      const paddingY = fontSize * visualStyle.paddingY;
      const blockWidth = Math.min(
        canvas.width * 0.96,
        Math.max(...lines.map((line) => ctx.measureText(line).width), 0) + paddingX * 2,
      );
      const blockHeight = lines.length * lineHeight + paddingY * 2;
      const boxX = caption.align === "left"
        ? -paddingX
        : caption.align === "right"
          ? -blockWidth + paddingX
          : -blockWidth / 2;
      const boxY = -blockHeight / 2;
      if (visualStyle.reveal === "wipe") {
        const wipeProgress = clamp(localTime / Math.max(0.05, visualStyle.revealDuration), 0, 1);
        ctx.beginPath();
        ctx.rect(boxX, boxY, blockWidth * wipeProgress, blockHeight);
        ctx.clip();
      }
      ctx.fillStyle = display.background;
      drawRoundedRect(ctx, boxX, boxY, blockWidth, blockHeight, fontSize * visualStyle.cornerRadius);
      ctx.fill();
      if (caption.style === "speechBubble") {
        ctx.strokeStyle = "rgba(28,25,23,0.2)";
        ctx.lineWidth = Math.max(2, fontSize * 0.05);
        ctx.stroke();
      }
      ctx.shadowColor = visualStyle.textShadowColor;
      ctx.shadowBlur = visualStyle.textShadowBlur * pixelScale;
      ctx.shadowOffsetX = visualStyle.textShadowOffsetX * pixelScale;
      ctx.shadowOffsetY = visualStyle.textShadowOffsetY * pixelScale;
      ctx.fillStyle = display.color;
      lines.forEach((line, index) => {
        const lineY = -((lines.length - 1) * lineHeight) / 2 + index * lineHeight;
        if (visualStyle.strokeWidth > 0) {
          ctx.strokeStyle = visualStyle.strokeColor;
          ctx.lineWidth = visualStyle.strokeWidth * pixelScale;
          ctx.lineJoin = "round";
          ctx.strokeText(line, 0, lineY);
        }
        ctx.fillText(line, 0, lineY);
      });
      ctx.restore();
    }
  }, [captions, clips, graphicGroupIds, graphicLayers, shotBeats, trackStates.captions.visible, trackStates.graphics.visible, trackStates.video.visible]);

  const captureCurrentFrame = useCallback(async () => {
    const video = videoRef.current;
    if (!doc || !video || video.readyState < 2) {
      toast.warning(veText("capture_frame_failed"));
      return;
    }
    try {
      await Promise.all(graphicImageAssetIds.map((id) => loadGraphicImageAsset(id)));
      await syncGraphicVideoRenderers(graphicLayers, playhead, false);
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth || mediaSize.width || 1920;
      canvas.height = video.videoHeight || mediaSize.height || 1080;
      const ctx = canvas.getContext("2d");
      if (!ctx) throw new Error("Canvas renderer unavailable");
      drawExportFrame(ctx, canvas, video, playhead);
      const blob = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob((value) => value ? resolve(value) : reject(new Error("PNG encoder unavailable")), "image/png");
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${baseName(doc.name)}-${Math.round(playhead * 1000)}ms.png`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      revokeObjectUrlSoon(url);
      toast.success(veText("capture_frame_success"), formatTime(playhead));
    } catch (error) {
      console.error(error);
      toast.error(veText("capture_frame_failed"), error instanceof Error ? error.message : undefined);
    }
  }, [doc, drawExportFrame, graphicImageAssetIds, graphicLayers, mediaSize.height, mediaSize.width, playhead, toast]);

  const exportPreview = useCallback(async () => {
    if (!doc || !downloadUrl || timelineDuration <= 0 || exportRangeDuration <= 0) return;
    if (!("MediaRecorder" in window)) {
      toast.error(veText("toast.browser_export_unsupported"));
      return;
    }
    setExporting(true);
    setExportProgress(0);
    try {
      await document.fonts?.ready;
      await Promise.all(graphicImageAssetIds.map((id) => loadGraphicImageAsset(id)));
      await Promise.all(graphicLayers.filter((graphic) => graphic.kind === "video").map((graphic) => loadGraphicVideoRenderer(graphic)));
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
      const shouldCaptureAudio = !trackStates.audio.muted && (
        audioCues.some((cue) => !cue.muted)
        || (!trackStates.video.muted && clips.some((clip) => !clip.muted))
      );
      const useExplicitCanvasFrames = !trackStates.video.visible && !shouldCaptureAudio;
      const canvasStream = canvas.captureStream(useExplicitCanvasFrames ? 0 : 30);
      const canvasVideoTrack = canvasStream.getVideoTracks()[0] as MediaStreamTrack & { requestFrame?: () => void };
      const requestCanvasFrame = () => {
        if (useExplicitCanvasFrames) canvasVideoTrack.requestFrame?.();
      };

      const AudioContextCtor = window.AudioContext ?? (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      const audioContext = shouldCaptureAudio && AudioContextCtor ? new AudioContextCtor() : null;
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
            audio.loop = false;
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
      } else if (shouldCaptureAudio) {
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
          const sourceWindow = getAudioCueSourceWindow(node.cue, mediaDuration);
          const sourceInRange = node.cue.loop || cueOffset <= sourceWindow.duration;
          const active = realtime && cueOffset >= 0 && cueOffset <= duration && sourceInRange;
          node.gain.gain.value = getAudioCueGain(node.cue, timelineTime, audioCues);
          if (!active) {
            if (!node.audio.paused) node.audio.pause();
            node.wasActive = false;
            continue;
          }
          node.audio.loop = false;
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
      // MediaRecorder measures wall-clock time. Keep it paused while clips are
      // seeking or switching so those browser delays do not become frozen
      // frames and make the exported preview longer than the timeline.
      await setMediaRecorderPaused(recorder, true);

      const updateExportProgress = (timelineTime: number) => {
        const rangeOffset = clamp(timelineTime - exportRangeStart, 0, exportRangeDuration);
        setExportProgress(Math.round((rangeOffset / exportRangeDuration) * 100));
      };

      let cursor = 0;
      for (const clip of clips) {
        const duration = getClipTimelineDuration(clip);
        const clipStart = cursor;
        const clipEnd = cursor + duration;
        if (duration <= 0 || clipEnd <= exportRangeStart || clipStart >= exportRangeEnd) {
          cursor = clipEnd;
          continue;
        }
        const segmentStart = Math.max(clipStart, exportRangeStart);
        const segmentEnd = Math.min(clipEnd, exportRangeEnd);
        const sourceStart = getClipSourceTime(clip, segmentStart - clipStart);
        const sourceEnd = getClipSourceTime(clip, segmentEnd - clipStart);
        const activeVideo = getClipVideo(clip);
        activeVideo.playbackRate = normalizeVideoClipSpeed(clip.speed);
        activeVideo.muted = audioContext ? false : shouldMuteSourceVideoAudio(clip, segmentStart, audioCues, trackStates);
        syncClipAudioGain(clip, segmentStart);
        await seekVideo(activeVideo, sourceStart);
        await syncGraphicVideoRenderers(graphicLayers, segmentStart, false);
        drawExportFrame(ctx, canvas, activeVideo, segmentStart);
        // Pure canvas/motion-design projects do not need the carrier video to
        // run in real time. Frame stepping keeps every authored timeline frame
        // instead of letting browser playback outrun canvas capture.
        const shouldPlaySourceRealtime = trackStates.video.visible || shouldCaptureAudio;
        const playing = shouldPlaySourceRealtime
          ? await activeVideo.play().then(() => true).catch(() => false)
          : false;
        if (playing) {
          await setMediaRecorderPaused(recorder, false);
          let stalledFrames = 0;
          let lastTime = activeVideo.currentTime;
          while (activeVideo.currentTime < sourceEnd - 0.03 && stalledFrames < 45) {
            const timelineTime = clipStart + getClipTimelineOffset(clip, activeVideo.currentTime);
            activeVideo.muted = audioContext ? false : shouldMuteSourceVideoAudio(clip, timelineTime, audioCues, trackStates);
            syncClipAudioGain(clip, timelineTime);
            await syncAssetAudio(timelineTime, true);
            await syncGraphicVideoRenderers(graphicLayers, timelineTime, true);
            drawExportFrame(ctx, canvas, activeVideo, timelineTime);
            requestCanvasFrame();
            updateExportProgress(timelineTime);
            await nextFrame();
            stalledFrames = Math.abs(activeVideo.currentTime - lastTime) < 0.001 ? stalledFrames + 1 : 0;
            lastTime = activeVideo.currentTime;
          }
          // Some browsers stop advancing a media element under a complex
          // canvas workload before the requested source window has ended.
          // Recover the missing tail deterministically so motion layers and
          // resolve cards are never omitted from the delivered file.
          if (activeVideo.currentTime < sourceEnd - 0.03) {
            activeVideo.pause();
            await setMediaRecorderPaused(recorder, true);
            const frameStep = normalizeVideoClipSpeed(clip.speed) / 30;
            const recoveryStart = Math.max(sourceStart, activeVideo.currentTime + frameStep);
            for (let sourceTime = recoveryStart; sourceTime < sourceEnd; sourceTime += frameStep) {
              await seekVideo(activeVideo, sourceTime);
              const timelineTime = clipStart + getClipTimelineOffset(clip, sourceTime);
              await syncAssetAudio(timelineTime, false);
              await syncGraphicVideoRenderers(graphicLayers, timelineTime, false);
              drawExportFrame(ctx, canvas, activeVideo, timelineTime);
              requestCanvasFrame();
              updateExportProgress(timelineTime);
              await setMediaRecorderPaused(recorder, false);
              await nextFrame(1000 / 30);
              await setMediaRecorderPaused(recorder, true);
            }
          }
        } else {
          const frameStep = normalizeVideoClipSpeed(clip.speed) / 30;
          if (!shouldPlaySourceRealtime) {
            // Keep the recorder continuously active for code-only timelines.
            // Repeated pause/resume transitions can cause Chromium to discard
            // canvas frames even though every authored frame was drawn.
            await setMediaRecorderPaused(recorder, false);
            for (let sourceTime = sourceStart; sourceTime < sourceEnd; sourceTime += frameStep) {
              const timelineTime = clipStart + getClipTimelineOffset(clip, sourceTime);
              await syncAssetAudio(timelineTime, false);
              await syncGraphicVideoRenderers(graphicLayers, timelineTime, false);
              drawExportFrame(ctx, canvas, activeVideo, timelineTime);
              requestCanvasFrame();
              updateExportProgress(timelineTime);
              await nextFrame(1000 / 30);
            }
            await setMediaRecorderPaused(recorder, true);
          } else {
            for (let sourceTime = sourceStart; sourceTime < sourceEnd; sourceTime += frameStep) {
              if (trackStates.video.visible) await seekVideo(activeVideo, sourceTime);
              const timelineTime = clipStart + getClipTimelineOffset(clip, sourceTime);
              await syncAssetAudio(timelineTime, false);
              await syncGraphicVideoRenderers(graphicLayers, timelineTime, false);
              drawExportFrame(ctx, canvas, activeVideo, timelineTime);
              requestCanvasFrame();
              updateExportProgress(timelineTime);
              await setMediaRecorderPaused(recorder, false);
              await nextFrame(1000 / 30);
              await setMediaRecorderPaused(recorder, true);
            }
          }
        }
        activeVideo.pause();
        await setMediaRecorderPaused(recorder, true);
        await syncAssetAudio(segmentEnd, false);
        await syncGraphicVideoRenderers(graphicLayers, segmentEnd, false);
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
      graphicVideoRenderElementCache.forEach(({ video }) => video.pause());
      assetUrls.forEach((url) => revokeObjectUrlSoon(url));
      clipVideoUrls.forEach((url) => revokeObjectUrlSoon(url));
      await audioContext?.close().catch(() => undefined);
      setExportProgress(99);

      const blob = new Blob(chunks, { type: mimeType || "video/webm" });
      const previewFileName = normalizedWorkArea.enabled
        ? `${baseName(doc.name)}-range-preview.webm`
        : `${baseName(doc.name)}-preview.webm`;
      const finalFileName = normalizedWorkArea.enabled
        ? `${baseName(doc.name)}-range-edit.mp4`
        : `${baseName(doc.name)}-edited.mp4`;
      const previewFile = new File([blob], previewFileName, { type: blob.type || "video/webm" });
      const finalized = await api.videoEditor.finalizePreview(
        previewFile,
        doc.id,
        finalFileName,
        { fps: 30, crf: 18, preset: "veryfast", targetDurationSeconds: exportRangeDuration },
      );
      const exportRecipe = await saveExportRecipeSidecar(finalized.document);
      const linkedExport = await api.videoEditor.linkRecipe(finalized.document.id, exportRecipe.id);
      await invalidateKnowledgeQueries(queryClient);
      await queryClient.invalidateQueries({ queryKey: ["video-edit-recipe"] });
      await queryClient.invalidateQueries({ queryKey: ["video-editor-recipe-candidates"] });
      setExportProgress(100);
      setLastExportDoc(linkedExport.document);
      toast.success(veText("toast.preview_exported"), `${finalized.document.name} + ${exportRecipe.name}`);
    } catch (error) {
      console.error(error);
      toast.error(veText("toast.preview_export_failed"), error instanceof Error ? error.message : undefined);
    } finally {
      setExporting(false);
    }
  }, [audioCues, clips, doc, downloadUrl, drawExportFrame, exportRangeDuration, exportRangeEnd, exportRangeStart, graphicImageAssetIds, graphicLayers, mediaSize.height, mediaSize.width, normalizedWorkArea.enabled, queryClient, saveExportRecipeSidecar, timelineDuration, toast, trackStates.audio.muted, trackStates.video.muted]);

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
        setTimelineTool("select");
        setRazorPreview(null);
      } else if (key === "v") {
        event.preventDefault();
        setTimelineTool("select");
        setRazorPreview(null);
      } else if (key === "r") {
        event.preventDefault();
        setTimelineTool("razor");
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

  const addUnifiedMediaAsset = useCallback(async (asset: InsertableMediaAsset) => {
    if (asset.kind === "video") {
      await addVideoClipFromDocument(asset.document);
    } else {
      if (trackStates.graphics.locked) throw new Error(veText("toast.graphics_locked"));
      const image = await loadGraphicImageAsset(asset.document.id);
      const start = snapTimelineTime(playhead);
      const width = 32;
      const height = clamp(
        width * (image.naturalHeight / Math.max(1, image.naturalWidth)) * (mediaSize.width / Math.max(1, mediaSize.height)),
        4,
        70,
      );
      const graphic: GraphicLayer = {
        id: makeId("graphic"),
        kind: "image",
        label: baseName(asset.document.name),
        start,
        end: clamp(start + 4, start + 0.05, Math.max(start + 0.05, timelineDuration)),
        x: 50,
        y: 50,
        scale: 1,
        rotation: 0,
        opacity: 1,
        keyframes: [],
        width,
        height,
        fill: "#ffffff",
        stroke: "#ffffff",
        strokeWidth: 0,
        cornerRadius: 0,
        assetDocumentId: asset.document.id,
        assetName: asset.document.name,
        assetMimeType: asset.document.mime_type || asset.document.file_type || "image/png",
        ...normalizeGraphicVisualStyle({ assetFit: "cover", zIndex: 20 }),
      };
      setGraphicLayers((current) => [...current, graphic]);
      setGraphicAssetRevision((version) => version + 1);
      setSelection({ type: "graphic", id: graphic.id });
      toast.success(veText("toast.image_overlay_added"), asset.document.name);
    }
    await queryClient.invalidateQueries({ queryKey: ["video-editor-assets"] });
    await invalidateKnowledgeQueries(queryClient);
  }, [
    addVideoClipFromDocument,
    mediaSize.height,
    mediaSize.width,
    playhead,
    queryClient,
    snapTimelineTime,
    timelineDuration,
    toast,
    trackStates.graphics.locked,
  ]);

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

  const renderCaptionOverlay = (
    caption: CaptionCue,
    timelineTime: number,
    keyPrefix = "",
    nested = false,
  ) => {
    const previewFontSize = Math.max(1, Math.round((caption.size / 1080) * (previewMediaSize.height || mediaSize.height)));
    const localPose = captionMotionPoseAtTimelineTime(caption, timelineTime);
    const motionPose = nested ? localPose : composeMotionPoses(localPose, activeSceneGroupPose);
    const visualStyle = normalizeCaptionVisualStyle(caption);
    const rawCaptionText = caption.speaker ? `${caption.speaker}: ${caption.text}` : caption.text;
    const transformedText = transformCaptionText(rawCaptionText, visualStyle.textTransform);
    const localTime = Math.max(0, timelineTime - caption.start);
    const visibleText = revealedCaptionText(transformedText, visualStyle.reveal, localTime, visualStyle.revealDuration);
    const wipeProgress = visualStyle.reveal === "wipe"
      ? clamp(localTime / Math.max(0.05, visualStyle.revealDuration), 0, 1)
      : 1;
    return (
      <div
        key={`${keyPrefix}${caption.id}`}
        className={`ve-caption-overlay ve-caption-${caption.style} ${selection?.type === "caption" && selection.id === caption.id ? "is-selected" : ""} ${hasAiHighlight("caption", caption.id) ? "is-ai-highlighted" : ""}`}
        style={{
          left: `${motionPose.x}%`,
          top: `${motionPose.y}%`,
          opacity: motionPose.opacity,
          color: captionDisplay(caption).color,
          background: captionDisplay(caption).background,
          fontSize: `${previewFontSize}px`,
          fontFamily: captionFontStack(visualStyle.fontFamily),
          fontWeight: visualStyle.fontWeight,
          letterSpacing: `${visualStyle.letterSpacing * (previewFontSize / Math.max(1, caption.size))}px`,
          lineHeight: visualStyle.lineHeight,
          maxWidth: `${visualStyle.maxWidth}%`,
          padding: `${previewFontSize * visualStyle.paddingY}px ${previewFontSize * visualStyle.paddingX}px`,
          borderRadius: `${previewFontSize * visualStyle.cornerRadius}px`,
          textShadow: visualStyle.textShadowBlur > 0 || visualStyle.textShadowOffsetX !== 0 || visualStyle.textShadowOffsetY !== 0
            ? `${visualStyle.textShadowOffsetX}px ${visualStyle.textShadowOffsetY}px ${visualStyle.textShadowBlur}px ${visualStyle.textShadowColor}`
            : "none",
          WebkitTextStroke: visualStyle.strokeWidth > 0 ? `${visualStyle.strokeWidth}px ${visualStyle.strokeColor}` : "0 transparent",
          textAlign: caption.align,
          zIndex: 1000 + visualStyle.zIndex,
          filter: (motionPose.blur ?? 0) > 0 ? `blur(${motionPose.blur}px)` : "none",
          clipPath: wipeProgress < 1 ? `inset(0 ${(1 - wipeProgress) * 100}% 0 0)` : "none",
          transform: motionCssTransform(captionAnchorTransform(caption.align, previewFontSize), motionPose),
        }}
        onPointerDown={(event) => beginCaptionOverlayDrag(event, caption)}
      >
        <span>{visibleText}</span>
      </div>
    );
  };

  const renderGraphicOverlay = (
    graphic: GraphicLayer,
    timelineTime: number,
    keyPrefix = "",
    nested = false,
    ancestors = new Set<string>(),
  ): JSX.Element | null => {
    if (ancestors.has(graphic.id) || ancestors.size > 12) return null;
    const nextAncestors = new Set(ancestors);
    nextAncestors.add(graphic.id);
    const localPose = graphicMotionPoseAtTimelineTime(graphic, timelineTime);
    const motionPose = nested ? localPose : composeMotionPoses(localPose, activeSceneGroupPose);
    const imageUrl = graphic.assetDocumentId ? graphicImageUrls.get(graphic.assetDocumentId) : "";
    const videoUrl = graphic.assetDocumentId ? graphicVideoUrls.get(graphic.assetDocumentId) : "";
    const visualStyle = normalizeGraphicVisualStyle({
      ...graphic,
      blur: motionPose.blur ?? graphic.blur,
      effectStrength: motionPose.effectStrength ?? graphic.effectStrength,
    });
    const pathFillId = `${keyPrefix}${graphic.id}`.replace(/[^a-z0-9_-]/gi, "") || "graphic-fill";
    const isGroup = graphic.kind === "group";
    const sourceGroup = isGroup && graphic.instanceOf
      ? graphicLayers.find((candidate) => candidate.id === graphic.instanceOf && candidate.kind === "group") ?? graphic
      : graphic;
    if (sourceGroup.id !== graphic.id && nextAncestors.has(sourceGroup.id)) return null;
    if (sourceGroup.id !== graphic.id) nextAncestors.add(sourceGroup.id);
    const sourceTime = sourceGroup.id === graphic.id
      ? timelineTime
      : sourceGroup.start + (timelineTime - graphic.start);
    const childGraphics = isGroup
      ? graphicLayers
        .filter((candidate) => candidate.parentId === sourceGroup.id
          && !candidate.isTemplate
          && sourceTime >= candidate.start
          && sourceTime <= candidate.end)
        .sort((left, right) => layerZIndex(left) - layerZIndex(right))
      : [];
    const childCaptions = isGroup && trackStates.captions.visible
      ? captions
        .filter((caption) => caption.parentId === sourceGroup.id
          && sourceTime >= caption.start
          && sourceTime <= caption.end)
        .sort((left, right) => layerZIndex(left, 100) - layerZIndex(right, 100))
      : [];
    return (
      <div
        key={`${keyPrefix}${graphic.id}`}
        data-graphic-group-id={isGroup ? graphic.id : undefined}
        className={`ve-graphic-overlay ve-graphic-${graphic.kind} ${isGroup ? "ve-graphic-group" : ""} ${selection?.type === "graphic" && selection.id === graphic.id ? "is-selected" : ""} ${hasAiHighlight("graphic", graphic.id) ? "is-ai-highlighted" : ""}`}
        style={{
          left: `${motionPose.x}%`,
          top: `${motionPose.y}%`,
          width: `${graphic.width}%`,
          height: `${graphic.height}%`,
          opacity: motionPose.opacity,
          background: isGroup || graphic.kind === "image" || graphic.kind === "video" || graphic.kind === "path" || graphic.kind === "particle" || graphic.kind === "shader"
            ? "transparent"
            : graphicCssFill(graphic.fill, visualStyle),
          border: !isGroup && graphic.kind !== "particle" && graphic.kind !== "shader" && graphic.strokeWidth > 0 ? `${Math.max(1, graphic.strokeWidth)}px solid ${graphic.stroke}` : "none",
          borderRadius: graphic.kind === "ellipse" ? "50%" : graphic.kind === "rectangle" || graphic.kind === "image" || graphic.kind === "video" || isGroup ? `${graphic.cornerRadius}%` : graphic.kind === "line" ? "999px" : 0,
          boxShadow: isGroup ? "none" : graphicCssShadow(visualStyle),
          filter: graphicCssFilter(visualStyle),
          clipPath: graphicCssClipPath(visualStyle.maskShape),
          backdropFilter: visualStyle.effect === "glass" ? `blur(${6 + visualStyle.effectStrength * 18}px) saturate(${1.05 + visualStyle.effectStrength * 0.45})` : "none",
          mixBlendMode: visualStyle.blendMode,
          overflow: isGroup
            ? graphic.clipChildren || visualStyle.maskShape !== "none" ? "hidden" : "visible"
            : graphic.kind === "image"
              || graphic.kind === "video"
              || graphic.kind === "path"
              || graphic.kind === "particle"
              || graphic.kind === "shader"
              || visualStyle.maskShape !== "none"
              || ["glass", "grain", "scanlines", "chromatic", "vignette"].includes(visualStyle.effect)
              ? "hidden"
              : "visible",
          zIndex: 1000 + visualStyle.zIndex,
          transform: motionCssTransform("translate(-50%, -50%)", motionPose),
        }}
        role="button"
        tabIndex={trackStates.graphics.locked ? -1 : 0}
        aria-label={veText("graphic_overlay_aria", { label: graphic.label })}
        onPointerDown={(event) => beginGraphicOverlayDrag(event, graphic)}
        onKeyDown={(event) => {
          if (trackStates.graphics.locked || (event.key !== "Enter" && event.key !== " ")) return;
          event.preventDefault();
          setSelection({ type: "graphic", id: graphic.id });
        }}
      >
        {graphic.kind === "image" && imageUrl && (
          <img src={imageUrl} alt="" draggable={false} style={{ objectFit: visualStyle.assetFit }} />
        )}
        {graphic.kind === "image" && !imageUrl && (
          <span className="ve-graphic-loading"><LoadingSpinner /></span>
        )}
        {graphic.kind === "video" && videoUrl && (
          <video
            ref={(node) => {
              const previous = graphicVideoRefs.current.get(graphic.id);
              if (previous && previous !== node) previous.pause();
              if (node) graphicVideoRefs.current.set(graphic.id, node);
              else graphicVideoRefs.current.delete(graphic.id);
            }}
            src={videoUrl}
            muted
            playsInline
            preload="auto"
            aria-hidden="true"
            style={{ objectFit: visualStyle.assetFit }}
            onLoadedMetadata={(event) => {
              const video = event.currentTarget;
              video.currentTime = getGraphicVideoSourceTime(graphic, timelineTime, video.duration);
              const sourceWindow = getGraphicVideoSourceWindow(graphic, video.duration);
              const sourceElapsed = Math.max(0, timelineTime - graphic.start) * normalizeVideoClipSpeed(graphic.speed);
              if (isPlaying && (graphic.loop || sourceElapsed < sourceWindow.duration - 0.001)) {
                void video.play().catch(() => undefined);
              }
            }}
          />
        )}
        {graphic.kind === "video" && !videoUrl && (
          <span className="ve-graphic-loading"><LoadingSpinner /></span>
        )}
        {graphic.kind === "particle" && <ParticleGraphicCanvas graphic={graphic} timelineTime={timelineTime} />}
        {graphic.kind === "shader" && <ShaderGraphicCanvas graphic={graphic} timelineTime={timelineTime} />}
        {graphic.kind === "path" && visualStyle.pathData && (
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
            <defs>
              <linearGradient id={`${pathFillId}-fill`} x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stopColor={graphic.fill} />
                <stop offset="1" stopColor={visualStyle.fillSecondary} />
              </linearGradient>
              <radialGradient id={`${pathFillId}-radial`} cx="50%" cy="42%" r="68%">
                <stop offset="0" stopColor={graphic.fill} />
                <stop offset="1" stopColor={visualStyle.fillSecondary} />
              </radialGradient>
            </defs>
            <path
              d={visualStyle.pathData}
              pathLength={1}
              fill={(motionPose.pathProgress ?? 1) < 0.999 ? "none" : visualStyle.fillType === "linear" ? `url(#${pathFillId}-fill)` : visualStyle.fillType === "radial" ? `url(#${pathFillId}-radial)` : graphic.fill}
              stroke={graphic.strokeWidth > 0 ? graphic.stroke : "none"}
              strokeWidth={graphic.strokeWidth}
              strokeDasharray={(motionPose.pathProgress ?? 1) < 0.999 ? 1 : undefined}
              strokeDashoffset={(motionPose.pathProgress ?? 1) < 0.999 ? 1 - (motionPose.pathProgress ?? 1) : undefined}
              strokeLinecap="round"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
        )}
        {isGroup && childGraphics.map((child) => renderGraphicOverlay(
          child,
          sourceTime,
          `${keyPrefix}${graphic.id}/`,
          true,
          nextAncestors,
        ))}
        {isGroup && childCaptions.map((caption) => renderCaptionOverlay(
          caption,
          sourceTime,
          `${keyPrefix}${graphic.id}/`,
          true,
        ))}
        {!isGroup && visualStyle.effect !== "none" && (
          <span
            aria-hidden="true"
            style={{
              position: "absolute",
              inset: 0,
              borderRadius: "inherit",
              pointerEvents: "none",
              background: graphicEffectOverlayBackground(visualStyle),
              opacity: visualStyle.effectStrength,
              mixBlendMode: visualStyle.effect === "grain" || visualStyle.effect === "scanlines"
                ? "soft-light"
                : visualStyle.effect === "glow"
                  || visualStyle.effect === "lightLeak"
                  || visualStyle.effect === "filmBurn"
                  || visualStyle.effect === "halation"
                  || visualStyle.effect === "anamorphic"
                  ? "screen"
                  : "normal",
            }}
          />
        )}
      </div>
    );
  };

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
  const shortcutGroups = [
    {
      title: veText("shortcuts.playback"),
      items: [
        [veText("shortcuts.play_pause"), "Space"],
        [veText("shortcuts.start_end"), "Home / End"],
        [veText("shortcuts.range_points"), "I / O"],
        [veText("shortcuts.marker_navigation"), "J / K"],
      ],
    },
    {
      title: veText("shortcuts.editing"),
      items: [
        [veText("shortcuts.tools"), "V / R"],
        [veText("shortcuts.nudge"), "← / →"],
        [veText("shortcuts.split"), "S"],
        [veText("shortcuts.marker"), "M"],
        [veText("shortcuts.delete"), "Delete"],
      ],
    },
    {
      title: veText("shortcuts.project"),
      items: [
        [veText("shortcuts.undo_redo"), "⌘/Ctrl Z / ⇧⌘/Ctrl Z"],
        [veText("shortcuts.save"), "⌘/Ctrl S / ⇧⌘/Ctrl S"],
      ],
    },
  ];

  return (
    <div className="ve-shell">
      <style>{VIDEO_EDITOR_STYLES}</style>
      <header className="ve-topbar">
        <div className="ve-topbar-left">
          <button className="ve-icon-button" type="button" title={veText("back")} onClick={() => navigate(backTarget)}>
            <IconArrowLeft size={18} />
          </button>
          <div className="ve-title-block">
            <div className="ve-title-row">
              <PageHeaderTitle variant="editor" title={doc.name}>{doc.name}</PageHeaderTitle>
              <VideoEditorHelp
                titleKey="help.editor.title"
                bodyKey="help.editor.body"
                itemKeys={["help.editor.item1", "help.editor.item2", "help.editor.item3", "help.editor.item4"]}
              />
            </div>
            <PageHeaderSubtitle>{veText("title")}</PageHeaderSubtitle>
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
                instruction: veText("ai.instruction", { name: doc.name }),
                sessionLabel: veText("ai.session_label", { name: doc.name }),
                emptyDescription: veText("ai.empty_description"),
                placeholder: veText("ai.placeholder"),
                examples: [
                  veText("ai.example.camera_move"),
                  veText("ai.example.title_card"),
                  veText("ai.example.bezier"),
                  veText("ai.example.polish"),
                ],
                getContent: () => editorLiveContentRef.current,
                applyContent: (next) => {
                  try {
                    const recipe = JSON.parse(next) as VideoEditRecipe;
                    const before = cloneEditorTrackState(latestTrackStateRef.current ?? currentTrackState);
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
            <Link className="ve-link-button ve-open-export" to={`/viewer/${lastExportDoc.id}`} state={location.state} title={veText("open_export")}>
              <IconDocument size={15} />
              <span className="ve-action-label">{veText("open_export")}</span>
            </Link>
          )}
          <button className="ve-icon-button" type="button" title={veText("shortcut.undo")} onClick={undoHistory} disabled={!canUndo}>
            <IconUndo size={15} />
          </button>
          <button className="ve-icon-button" type="button" title={veText("shortcut.redo")} onClick={redoHistory} disabled={!canRedo}>
            <IconRedo size={15} />
          </button>
          <div className="ve-view-controls" role="group" aria-label={veText("workspace_views")}>
            <button
              className={`ve-view-toggle ${mediaPanelOpen ? "is-active" : ""}`}
              type="button"
              aria-pressed={mediaPanelOpen}
              title={veText(mediaPanelOpen ? "hide_media_panel" : "show_media_panel")}
              onClick={() => setMediaPanelOpen((open) => !open)}
            >
              <IconFolder size={14} />
              <span>{veText("media")}</span>
            </button>
            <button
              className={`ve-view-toggle ${inspectorPanelOpen ? "is-active" : ""}`}
              type="button"
              aria-pressed={inspectorPanelOpen}
              title={veText(inspectorPanelOpen ? "hide_inspector_panel" : "show_inspector_panel")}
              onClick={() => setInspectorPanelOpen((open) => !open)}
            >
              <IconEdit size={14} />
              <span>{veText("inspector")}</span>
            </button>
          </div>
          {(recipeDoc || recipeQuery.data) && (
            <button className="ve-icon-button" type="button" title={veText("reload_recipe")} onClick={() => loadSavedRecipe(true)} disabled={loadingRecipe}>
              <IconDocument size={15} />
            </button>
          )}
          <div className="ve-save-actions">
            <button className="ve-button ve-save-plan" type="button" title={veText("shortcut.save_recipe")} onClick={saveRecipe} disabled={saving || timelineDuration <= 0}>
              <IconEdit size={15} />
              <span className="ve-action-label">{saving ? veText("saving") : veText("save_recipe")}</span>
            </button>
            <button className="ve-button ve-button-primary ve-save-video" type="button" title={veText("shortcut.export_preview")} onClick={exportPreview} disabled={exporting || exportRangeDuration <= 0}>
              <IconDownload size={15} />
              <span className="ve-action-label">{exporting ? veText("exporting", { progress: exportProgress }) : veText("export_preview")}</span>
            </button>
          </div>
        </div>
      </header>

      <div className={`ve-workspace ${mediaPanelOpen ? "" : "is-media-panel-closed"} ${inspectorPanelOpen ? "" : "is-inspector-panel-closed"}`}>
        {mediaPanelOpen && <aside className="ve-panel ve-media-panel">
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
          <input
            ref={graphicFileInputRef}
            type="file"
            accept="image/*"
            hidden
            onChange={handleGraphicUpload}
          />
          <input
            ref={graphicVideoFileInputRef}
            type="file"
            accept="video/*"
            hidden
            onChange={handleGraphicVideoUpload}
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
            <button className="ve-track-action" type="button" disabled={trackStates.captions.locked} onClick={() => addTextLayer("titleCard")}>
              <IconText size={15} />
              {veText("add_title_card")}
            </button>
            <button className="ve-track-action" type="button" disabled={trackStates.captions.locked} onClick={() => addTextLayer("lowerThird")}>
              <IconText size={15} />
              {veText("add_lower_third")}
            </button>
            <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("rectangle")}>
              <IconGrid4 size={15} />
              {veText("add_rectangle")}
            </button>
            <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("ellipse")}>
              <IconPlus size={15} />
              {veText("add_ellipse")}
            </button>
            <button className="ve-track-action" type="button" disabled={uploadingGraphic || trackStates.graphics.locked} onClick={() => graphicFileInputRef.current?.click()}>
              <IconUpload size={15} />
              {uploadingGraphic ? veText("uploading_image") : veText("add_image_overlay")}
            </button>
            <button className="ve-track-action" type="button" disabled={uploadingGraphicVideo || trackStates.graphics.locked} onClick={() => graphicVideoFileInputRef.current?.click()}>
              <IconPlay size={15} />
              {uploadingGraphicVideo ? veText("uploading_video_overlay") : veText("add_video_overlay")}
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
              <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("group")}>
                <IconGrid4 size={15} />
                {veText("add_group")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("line")}>
                <IconPlus size={15} />
                {graphicKindDisplayLabel("line")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("path")}>
                <IconPlus size={15} />
                {graphicKindDisplayLabel("path")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("particle")}>
                <IconSparkles size={15} />
                {veText("add_particles")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.graphics.locked} onClick={() => addGraphicLayer("shader")}>
                <IconSparkles size={15} />
                WebGL shader
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={() => addAudioCue("dialogue")}>
                <IconPlus size={15} />
                {veText("add_dialogue_cue")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={() => addAudioCue("ambience")}>
                <IconPlus size={15} />
                {veText("add_ambience_cue")}
              </button>
              <button className="ve-track-action" type="button" disabled={trackStates.audio.locked} onClick={openFreeMusicLibrary}>
                <IconMusicNote size={15} />
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
            <button
              className="ve-open-media-picker"
              type="button"
              onClick={() => setUnifiedMediaInsertOpen(true)}
            >
              <IconSparkles size={16} />
              <span>
                <strong>{t("component.media_insert.generate")}</strong>
                <small>{t("component.media_insert.online")} · {t("component.media_insert.knowledge")}</small>
              </span>
              <IconChevronRight size={15} />
            </button>
            <button
              className="ve-open-media-picker ve-open-free-music"
              type="button"
              disabled={trackStates.audio.locked}
              onClick={openFreeMusicLibrary}
              title={veText("free_music.open_hint")}
            >
              <IconMusicNote size={16} />
              <span>
                <strong>{veText("free_music.open")}</strong>
                <small>{veText("free_music.open_detail")}</small>
              </span>
              <span className="ve-free-license-pill">CC0 · CC BY</span>
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
        </aside>}

        <main className="ve-center">
          <section ref={previewRef} className="ve-preview">
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
                <div
                  className="ve-preview-media-stage"
                  style={{
                    width: previewMediaSize.width || "100%",
                    height: previewMediaSize.height || "100%",
                  }}
                >
                  {selectedMotionPath.length > 1 && (
                    <svg
                      className="ve-motion-path-overlay"
                      viewBox="0 0 100 100"
                      preserveAspectRatio="none"
                      aria-hidden="true"
                    >
                      <polyline
                        points={selectedMotionPath.map((point) => `${point.x},${point.y}`).join(" ")}
                      />
                      {selectedMotionKeyframes.map((keyframe) => (
                        <circle
                          key={keyframe.id}
                          className={keyframe.id === activeMotionKeyframeId ? "is-active" : ""}
                          cx={keyframe.x}
                          cy={keyframe.y}
                          r={keyframe.id === activeMotionKeyframeId ? 1.45 : 1.05}
                        />
                      ))}
                      {selectedMotionPathPose && (
                        <circle
                          className="ve-motion-path-playhead"
                          cx={selectedMotionPathPose.x}
                          cy={selectedMotionPathPose.y}
                          r="1.5"
                        />
                      )}
                    </svg>
                  )}
                  <video
                    ref={videoRef}
                    className="ve-preview-video"
                    src={previewSourceUrl || downloadUrl}
                    style={{
                      left: `${activeVideoMotionPose.x}%`,
                      top: `${activeVideoMotionPose.y}%`,
                      opacity: trackStates.video.visible
                        ? activeVideoMotionPose.opacity * activeVideoEdgeFadeOpacity
                        : 0,
                      objectFit: activeVideoMap?.clip.fit ?? "contain",
                      filter: (activeVideoMotionPose.blur ?? 0) > 0 ? `blur(${activeVideoMotionPose.blur}px)` : "none",
                      transform: motionCssTransform("translate(-50%, -50%)", activeVideoMotionPose),
                      cursor: activeVideoMap && !trackStates.video.locked ? "grab" : "default",
                      touchAction: "none",
                    }}
                    preload="metadata"
                    playsInline
                    onLoadedMetadata={handleLoadedMetadata}
                    onDurationChange={handleLoadedMetadata}
                    onTimeUpdate={handleTimeUpdate}
                    onPause={handlePreviewPause}
                    onEnded={handleVideoEnded}
                    onPointerDown={(event) => {
                      if (activeVideoMap) beginClipOverlayDrag(event, activeVideoMap.clip);
                    }}
                  />
                  {activeGraphicLayers.map((graphic) => renderGraphicOverlay(graphic, playhead))}
                  {false && activeGraphicLayers.map((graphic) => {
                    const motionPose = composeMotionPoses(
                      graphicMotionPoseAtTimelineTime(graphic, playhead),
                      activeSceneGroupPose,
                    );
                    const imageUrl = graphic.assetDocumentId ? graphicImageUrls.get(graphic.assetDocumentId) : "";
                    const videoUrl = graphic.assetDocumentId ? graphicVideoUrls.get(graphic.assetDocumentId) : "";
                    const visualStyle = normalizeGraphicVisualStyle({
                      ...graphic,
                      blur: motionPose.blur ?? graphic.blur,
                      effectStrength: motionPose.effectStrength ?? graphic.effectStrength,
                    });
                    const pathFillId = `${graphic.id.replace(/[^a-z0-9_-]/gi, "") || "graphic"}-fill`;
                    return (
                      <div
                        key={graphic.id}
                        className={`ve-graphic-overlay ve-graphic-${graphic.kind} ${selection?.type === "graphic" && selection.id === graphic.id ? "is-selected" : ""} ${hasAiHighlight("graphic", graphic.id) ? "is-ai-highlighted" : ""}`}
                        style={{
                          left: `${motionPose.x}%`,
                          top: `${motionPose.y}%`,
                          width: `${graphic.width}%`,
                          height: `${graphic.height}%`,
                          opacity: motionPose.opacity,
                          background: graphic.kind === "image" || graphic.kind === "video" || graphic.kind === "path" || graphic.kind === "particle" || graphic.kind === "shader"
                            ? "transparent"
                            : graphicCssFill(graphic.fill, visualStyle),
                          border: graphic.kind !== "particle" && graphic.kind !== "shader" && graphic.strokeWidth > 0 ? `${Math.max(1, graphic.strokeWidth)}px solid ${graphic.stroke}` : "none",
                          borderRadius: graphic.kind === "ellipse" ? "50%" : graphic.kind === "rectangle" || graphic.kind === "image" || graphic.kind === "video" ? `${graphic.cornerRadius}%` : graphic.kind === "line" ? "999px" : 0,
                          boxShadow: graphicCssShadow(visualStyle),
                          filter: graphicCssFilter(visualStyle),
                          clipPath: graphicCssClipPath(visualStyle.maskShape),
                          backdropFilter: visualStyle.effect === "glass" ? `blur(${6 + visualStyle.effectStrength * 18}px) saturate(${1.05 + visualStyle.effectStrength * 0.45})` : "none",
                          mixBlendMode: visualStyle.blendMode,
                          overflow: graphic.kind === "image"
                            || graphic.kind === "video"
                            || graphic.kind === "path"
                            || graphic.kind === "particle"
                            || graphic.kind === "shader"
                            || visualStyle.maskShape !== "none"
                            || ["glass", "grain", "scanlines", "chromatic", "vignette"].includes(visualStyle.effect)
                            ? "hidden"
                            : "visible",
                          // Reserve the lower stacking plane for source footage and editor chrome.
                          // Recipe zIndex remains relative and is shared with the canvas exporter.
                          zIndex: 1000 + visualStyle.zIndex,
                          transform: motionCssTransform("translate(-50%, -50%)", motionPose),
                        }}
                        role="button"
                        tabIndex={trackStates.graphics.locked ? -1 : 0}
                        aria-label={veText("graphic_overlay_aria", { label: graphic.label })}
                        onPointerDown={(event) => beginGraphicOverlayDrag(event, graphic)}
                        onKeyDown={(event) => {
                          if (trackStates.graphics.locked || (event.key !== "Enter" && event.key !== " ")) return;
                          event.preventDefault();
                          setSelection({ type: "graphic", id: graphic.id });
                        }}
                      >
                        {graphic.kind === "image" && imageUrl && (
                          <img src={imageUrl} alt="" draggable={false} style={{ objectFit: visualStyle.assetFit }} />
                        )}
                        {graphic.kind === "image" && !imageUrl && (
                          <span className="ve-graphic-loading"><LoadingSpinner /></span>
                        )}
                        {graphic.kind === "video" && videoUrl && (
                          <video
                            ref={(node) => {
                              const previous = graphicVideoRefs.current.get(graphic.id);
                              if (previous && previous !== node) previous.pause();
                              if (node) graphicVideoRefs.current.set(graphic.id, node);
                              else graphicVideoRefs.current.delete(graphic.id);
                            }}
                            src={videoUrl}
                            muted
                            playsInline
                            preload="auto"
                            aria-hidden="true"
                            style={{ objectFit: visualStyle.assetFit }}
                            onLoadedMetadata={(event) => {
                              const video = event.currentTarget;
                              video.currentTime = getGraphicVideoSourceTime(graphic, playhead, video.duration);
                              const sourceWindow = getGraphicVideoSourceWindow(graphic, video.duration);
                              const sourceElapsed = Math.max(0, playhead - graphic.start) * normalizeVideoClipSpeed(graphic.speed);
                              if (isPlaying && (graphic.loop || sourceElapsed < sourceWindow.duration - 0.001)) {
                                void video.play().catch(() => undefined);
                              }
                            }}
                          />
                        )}
                        {graphic.kind === "video" && !videoUrl && (
                          <span className="ve-graphic-loading"><LoadingSpinner /></span>
                        )}
                        {graphic.kind === "particle" && (
                          <ParticleGraphicCanvas graphic={graphic} timelineTime={playhead} />
                        )}
                        {graphic.kind === "shader" && (
                          <ShaderGraphicCanvas graphic={graphic} timelineTime={playhead} />
                        )}
                        {graphic.kind === "path" && visualStyle.pathData && (
                          <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                            <defs>
                              <linearGradient id={pathFillId} x1="0" y1="0" x2="1" y2="1">
                                <stop offset="0" stopColor={graphic.fill} />
                                <stop offset="1" stopColor={visualStyle.fillSecondary} />
                              </linearGradient>
                              <radialGradient id={`${pathFillId}-radial`} cx="50%" cy="42%" r="68%">
                                <stop offset="0" stopColor={graphic.fill} />
                                <stop offset="1" stopColor={visualStyle.fillSecondary} />
                              </radialGradient>
                            </defs>
                            <path
                              d={visualStyle.pathData}
                              pathLength={1}
                              fill={(motionPose.pathProgress ?? 1) < 0.999 ? "none" : visualStyle.fillType === "linear" ? `url(#${pathFillId})` : visualStyle.fillType === "radial" ? `url(#${pathFillId}-radial)` : graphic.fill}
                              stroke={graphic.strokeWidth > 0 ? graphic.stroke : "none"}
                              strokeWidth={graphic.strokeWidth}
                              strokeDasharray={(motionPose.pathProgress ?? 1) < 0.999 ? 1 : undefined}
                              strokeDashoffset={(motionPose.pathProgress ?? 1) < 0.999 ? 1 - (motionPose.pathProgress ?? 1) : undefined}
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              vectorEffect="non-scaling-stroke"
                            />
                          </svg>
                        )}
                        {visualStyle.effect !== "none" && (
                          <span
                            aria-hidden="true"
                            style={{
                              position: "absolute",
                              inset: 0,
                              borderRadius: "inherit",
                              pointerEvents: "none",
                              background: graphicEffectOverlayBackground(visualStyle),
                              opacity: visualStyle.effectStrength,
                              mixBlendMode: visualStyle.effect === "grain" || visualStyle.effect === "scanlines"
                                ? "soft-light"
                                : visualStyle.effect === "glow"
                                  || visualStyle.effect === "lightLeak"
                                  || visualStyle.effect === "filmBurn"
                                  || visualStyle.effect === "halation"
                                  || visualStyle.effect === "anamorphic"
                                  ? "screen"
                                  : "normal",
                            }}
                          />
                        )}
                      </div>
                    );
                  })}
                  {activeCaptions.map((activeCaption) => renderCaptionOverlay(activeCaption, playhead))}
                  {false && activeCaptions.map((activeCaption) => {
                    const previewFontSize = Math.max(1, Math.round((activeCaption.size / 1080) * (previewMediaSize.height || mediaSize.height)));
                    const motionPose = composeMotionPoses(
                      captionMotionPoseAtTimelineTime(activeCaption, playhead),
                      activeSceneGroupPose,
                    );
                    const visualStyle = normalizeCaptionVisualStyle(activeCaption);
                    const rawCaptionText = activeCaption.speaker ? `${activeCaption.speaker}: ${activeCaption.text}` : activeCaption.text;
                    const transformedText = transformCaptionText(rawCaptionText, visualStyle.textTransform);
                    const localTime = Math.max(0, playhead - activeCaption.start);
                    const visibleText = revealedCaptionText(transformedText, visualStyle.reveal, localTime, visualStyle.revealDuration);
                    const wipeProgress = visualStyle.reveal === "wipe"
                      ? clamp(localTime / Math.max(0.05, visualStyle.revealDuration), 0, 1)
                      : 1;
                    return (
                      <div
                        key={activeCaption.id}
                        className={`ve-caption-overlay ve-caption-${activeCaption.style} ${selection?.type === "caption" && selection.id === activeCaption.id ? "is-selected" : ""} ${hasAiHighlight("caption", activeCaption.id) ? "is-ai-highlighted" : ""}`}
                        style={{
                          left: `${motionPose.x}%`,
                          top: `${motionPose.y}%`,
                          opacity: motionPose.opacity,
                          color: captionDisplay(activeCaption).color,
                          background: captionDisplay(activeCaption).background,
                          fontSize: `${previewFontSize}px`,
                          fontFamily: captionFontStack(visualStyle.fontFamily),
                          fontWeight: visualStyle.fontWeight,
                          letterSpacing: `${visualStyle.letterSpacing * (previewFontSize / Math.max(1, activeCaption.size))}px`,
                          lineHeight: visualStyle.lineHeight,
                          maxWidth: `${visualStyle.maxWidth}%`,
                          padding: `${previewFontSize * visualStyle.paddingY}px ${previewFontSize * visualStyle.paddingX}px`,
                          borderRadius: `${previewFontSize * visualStyle.cornerRadius}px`,
                          textShadow: visualStyle.textShadowBlur > 0 || visualStyle.textShadowOffsetX !== 0 || visualStyle.textShadowOffsetY !== 0
                            ? `${visualStyle.textShadowOffsetX}px ${visualStyle.textShadowOffsetY}px ${visualStyle.textShadowBlur}px ${visualStyle.textShadowColor}`
                            : "none",
                          WebkitTextStroke: visualStyle.strokeWidth > 0 ? `${visualStyle.strokeWidth}px ${visualStyle.strokeColor}` : "0 transparent",
                          textAlign: activeCaption.align,
                          zIndex: 1000 + visualStyle.zIndex,
                          filter: (motionPose.blur ?? 0) > 0 ? `blur(${motionPose.blur}px)` : "none",
                          clipPath: wipeProgress < 1 ? `inset(0 ${(1 - wipeProgress) * 100}% 0 0)` : "none",
                          transform: motionCssTransform(captionAnchorTransform(activeCaption.align, previewFontSize), motionPose),
                        }}
                        onPointerDown={(event) => beginCaptionOverlayDrag(event, activeCaption)}
                      >
                        <span>{visibleText}</span>
                      </div>
                    );
                  })}
                </div>
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
            <div className="ve-transport-primary">
              <button className="ve-round-button" type="button" title={isPlaying ? veText("shortcut.pause") : veText("shortcut.play")} onClick={togglePlayback}>
                {isPlaying ? <IconPause size={18} /> : <IconPlay size={18} />}
              </button>
              <button className="ve-round-button" type="button" title={veText("stop")} onClick={() => { pausePlayback(); seekTimeline(0); }}>
                <IconStop size={18} />
              </button>
              <div className="ve-timecode">{formatTime(playhead)} / {formatTime(timelineDuration)}</div>
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
            <div className="ve-transport-secondary">
              <div className="ve-preview-tools">
                <button
                  className={`ve-mini-button ${previewMuted ? "is-active" : ""}`}
                  type="button"
                  aria-pressed={previewMuted}
                  title={veText(previewMuted ? "unmute_preview" : "mute_preview")}
                  onClick={() => setPreviewMuted((muted) => !muted)}
                >
                  {previewMuted ? <IconEyeOff size={14} /> : <IconEye size={14} />}
                </button>
                <button
                  className={`ve-mini-button ${previewLoop ? "is-active" : ""}`}
                  type="button"
                  aria-pressed={previewLoop}
                  title={veText("loop_preview")}
                  onClick={() => setPreviewLoop((loop) => !loop)}
                >
                  <IconRefresh size={14} />
                </button>
                <label className="ve-playback-rate" title={veText("playback_speed")}>
                  <span className="sr-only">{veText("playback_speed")}</span>
                  <select value={playbackRate} onChange={(event) => setPlaybackRate(Number(event.currentTarget.value))}>
                    <option value={0.5}>0.5x</option>
                    <option value={1}>1x</option>
                    <option value={1.5}>1.5x</option>
                    <option value={2}>2x</option>
                  </select>
                </label>
                <button className="ve-mini-button" type="button" title={veText("capture_frame")} disabled={!downloadUrl} onClick={() => { void captureCurrentFrame(); }}>
                  <IconDownload size={14} />
                </button>
                <button className="ve-mini-button" type="button" title={veText(previewFullscreen ? "exit_fullscreen" : "enter_fullscreen")} onClick={() => { void togglePreviewFullscreen(); }}>
                  <IconGrid4 size={14} />
                </button>
              </div>
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
            </div>
          </div>
        </main>

        {inspectorPanelOpen && <aside className="ve-panel ve-inspector">
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
              <div className="ve-two-col">
                <NumberField
                  label={veText("field.source_duration")}
                  value={Math.max(0.05, selectedClip.sourceEnd - selectedClip.sourceStart)}
                  min={0.05}
                  max={Math.max(0.05, getClipMaxDuration(selectedClip, sourceDuration) - selectedClip.sourceStart)}
                  onChange={(value) => updateClip(selectedClip.id, { sourceEnd: selectedClip.sourceStart + Math.max(0.05, value) })}
                />
                <NumberField
                  label={veText("field.clip_speed")}
                  value={normalizeVideoClipSpeed(selectedClip.speed)}
                  min={MIN_VIDEO_CLIP_SPEED}
                  max={MAX_VIDEO_CLIP_SPEED}
                  step={0.25}
                  onChange={(value) => {
                    pausePlayback();
                    updateClip(selectedClip.id, { speed: value });
                  }}
                />
              </div>
              <p className="ve-field-note">
                {veText("clip_speed_summary", {
                  speed: normalizeVideoClipSpeed(selectedClip.speed).toFixed(2).replace(/\.00$/, ""),
                  duration: formatTime(getClipTimelineDuration(selectedClip)),
                })}
              </p>
              <div className="ve-field">
                <FieldLabel>{veText("field.frame_fit")}</FieldLabel>
                <Select
                  value={selectedClip.fit}
                  onChange={(value) => updateClip(selectedClip.id, { fit: value as ClipSegment["fit"] })}
                  options={videoFitOptions}
                  buttonStyle={{ boxShadow: "none" }}
                />
              </div>
              <div className="ve-two-col">
                <NumberField
                  label={veText("field.picture_fade_in")}
                  value={normalizeVideoClipFade(selectedClip.fadeIn, getClipTimelineDuration(selectedClip))}
                  min={0}
                  max={Math.min(MAX_VIDEO_CLIP_FADE_SECONDS, getClipTimelineDuration(selectedClip) / 2)}
                  step={0.1}
                  onChange={(value) => {
                    pausePlayback();
                    updateClip(selectedClip.id, { fadeIn: value });
                  }}
                />
                <NumberField
                  label={veText("field.picture_fade_out")}
                  value={normalizeVideoClipFade(selectedClip.fadeOut, getClipTimelineDuration(selectedClip))}
                  min={0}
                  max={Math.min(MAX_VIDEO_CLIP_FADE_SECONDS, getClipTimelineDuration(selectedClip) / 2)}
                  step={0.1}
                  onChange={(value) => {
                    pausePlayback();
                    updateClip(selectedClip.id, { fadeOut: value });
                  }}
                />
              </div>
              <p className="ve-field-note">{veText("picture_fade_hint")}</p>
              <section className="ve-motion-editor" aria-label={veText("motion.video_title")}>
                <div className="ve-motion-header">
                  <div>
                    <strong>{veText("motion.video_title")}</strong>
                    <span>{activeMotionKeyframe
                      ? veText("motion.editing_keyframe", { time: formatTime((selectedClipSpan?.start ?? 0) + activeMotionKeyframe.time) })
                      : autoRecordMotion
                        ? veText("motion.auto_record_hint")
                        : veText("motion.video_base_pose_hint")}</span>
                  </div>
                  <label className="ve-check ve-motion-auto-record">
                    <input
                      type="checkbox"
                      checked={autoRecordMotion}
                      disabled={trackStates.video.locked}
                      onChange={(event) => {
                        setAutoRecordMotion(event.currentTarget.checked);
                        if (event.currentTarget.checked) setActiveMotionKeyframeId(null);
                      }}
                    />
                    {veText("motion.auto_record")}
                  </label>
                </div>
                <div className="ve-motion-presets" aria-label={veText("motion.presets")}>
                  <span>{veText("motion.presets")}</span>
                  {(["fadeIn", "slideUp", "pop", "kenBurns"] as MotionPreset[]).map((preset) => (
                    <button key={preset} type="button" onClick={() => applyClipMotionPreset(preset)}>
                      {veText(`motion.preset.${preset}`)}
                    </button>
                  ))}
                  <button type="button" disabled={selectedClip.keyframes.length === 0} onClick={() => applyClipMotionPreset(null)}>
                    {veText("motion.clear")}
                  </button>
                </div>
                <div className="ve-two-col">
                  <NumberField label="X %" value={selectedClipMotionPose?.x ?? selectedClip.x} min={0} max={100} step={1} onChange={(value) => updateClipMotion(selectedClip.id, { x: value })} />
                  <NumberField label="Y %" value={selectedClipMotionPose?.y ?? selectedClip.y} min={0} max={100} step={1} onChange={(value) => updateClipMotion(selectedClip.id, { y: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label={veText("field.scale")} value={selectedClipMotionPose?.scale ?? selectedClip.scale} min={0.1} max={4} step={0.05} onChange={(value) => updateClipMotion(selectedClip.id, { scale: value })} />
                  <NumberField label={veText("field.rotation")} value={selectedClipMotionPose?.rotation ?? selectedClip.rotation} min={-360} max={360} step={1} onChange={(value) => updateClipMotion(selectedClip.id, { rotation: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Tilt X" value={selectedClipMotionPose?.rotationX ?? 0} min={-180} max={180} step={1} onChange={(value) => updateClipMotion(selectedClip.id, { rotationX: value })} />
                  <NumberField label="Tilt Y" value={selectedClipMotionPose?.rotationY ?? 0} min={-180} max={180} step={1} onChange={(value) => updateClipMotion(selectedClip.id, { rotationY: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Perspective" value={selectedClipMotionPose?.perspective ?? 1200} min={200} max={4000} step={50} onChange={(value) => updateClipMotion(selectedClip.id, { perspective: value })} />
                  <NumberField label="Motion blur" value={selectedClipMotionPose?.blur ?? 0} min={0} max={80} step={1} onChange={(value) => updateClipMotion(selectedClip.id, { blur: value })} />
                </div>
                <NumberField label={veText("field.layer_opacity")} value={selectedClipMotionPose?.opacity ?? selectedClip.opacity} min={0} max={1} step={0.05} onChange={(value) => updateClipMotion(selectedClip.id, { opacity: value })} />
                <div className="ve-motion-actions">
                  <button
                    className="ve-button"
                    type="button"
                    disabled={trackStates.video.locked || !selectedClipContainsPlayhead}
                    onClick={addClipKeyframe}
                  >
                    <IconKey size={14} />
                    {motionKeyframeAtPlayhead ? veText("motion.update_keyframe") : veText("motion.add_keyframe")}
                  </button>
                  {activeMotionKeyframe && (
                    <button className="ve-icon-button ve-danger" type="button" title={veText("motion.delete_keyframe")} onClick={deleteClipKeyframe}>
                      <IconTrash size={14} />
                    </button>
                  )}
                </div>
                {activeMotionKeyframe && (
                  <MotionKeyframeControls
                    keyframe={activeMotionKeyframe}
                    maxTime={selectedClipSpan?.duration ?? 0}
                    disabled={trackStates.video.locked}
                    onTimeChange={updateClipKeyframeTime}
                    onKeyframePatch={updateClipKeyframeOptions}
                  />
                )}
                {selectedClip.keyframes.length > 0 && (
                  <div className="ve-motion-keyframe-list" aria-label={veText("motion.keyframes")}>
                    {selectedClip.keyframes.map((keyframe, index) => (
                      <button
                        key={keyframe.id}
                        type="button"
                        className={keyframe.id === activeMotionKeyframeId ? "is-active" : ""}
                        onClick={() => {
                          setActiveMotionKeyframeId(keyframe.id);
                          seekTimeline((selectedClipSpan?.start ?? 0) + keyframe.time);
                        }}
                      >
                        <span aria-hidden="true" />
                        K{index + 1}
                        <small>{formatTime((selectedClipSpan?.start ?? 0) + keyframe.time)}</small>
                      </button>
                    ))}
                  </div>
                )}
              </section>
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
              <div className="ve-motion-section" aria-label="Scene group motion">
                <div className="ve-motion-section-heading">
                  <div>
                    <strong>Scene group motion</strong>
                    <small>Transforms video, graphics, and captions inside this shot as one parent group.</small>
                  </div>
                  <label className="ve-check ve-compact-check">
                    <input
                      type="checkbox"
                      checked={autoRecordMotion}
                      disabled={trackStates.shots.locked}
                      onChange={(event) => {
                        setAutoRecordMotion(event.currentTarget.checked);
                        if (event.currentTarget.checked) setActiveMotionKeyframeId(null);
                      }}
                    />
                    {veText("motion.auto_record")}
                  </label>
                </div>
                <div className="ve-motion-preset-row" aria-label="Scene motion presets">
                  <button type="button" onClick={() => applyShotMotionPreset("fadeIn")}>Fade</button>
                  <button type="button" onClick={() => applyShotMotionPreset("slideUp")}>Slide up</button>
                  <button type="button" onClick={() => applyShotMotionPreset("kenBurns")}>Cinematic drift</button>
                  <button type="button" disabled={selectedShot.keyframes.length === 0} onClick={() => applyShotMotionPreset(null)}>Clear</button>
                </div>
                <div className="ve-two-col">
                  <NumberField label="Group X %" value={selectedShotMotionPose?.x ?? 50} min={0} max={100} step={0.5} onChange={(value) => updateShotMotion(selectedShot.id, { x: value })} />
                  <NumberField label="Group Y %" value={selectedShotMotionPose?.y ?? 50} min={0} max={100} step={0.5} onChange={(value) => updateShotMotion(selectedShot.id, { y: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Scale" value={selectedShotMotionPose?.scale ?? 1} min={0.1} max={4} step={0.01} onChange={(value) => updateShotMotion(selectedShot.id, { scale: value })} />
                  <NumberField label="Rotation deg" value={selectedShotMotionPose?.rotation ?? 0} min={-360} max={360} step={1} onChange={(value) => updateShotMotion(selectedShot.id, { rotation: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Tilt X" value={selectedShotMotionPose?.rotationX ?? 0} min={-180} max={180} step={1} onChange={(value) => updateShotMotion(selectedShot.id, { rotationX: value })} />
                  <NumberField label="Tilt Y" value={selectedShotMotionPose?.rotationY ?? 0} min={-180} max={180} step={1} onChange={(value) => updateShotMotion(selectedShot.id, { rotationY: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Perspective" value={selectedShotMotionPose?.perspective ?? 1200} min={200} max={4000} step={50} onChange={(value) => updateShotMotion(selectedShot.id, { perspective: value })} />
                  <NumberField label="Motion blur" value={selectedShotMotionPose?.blur ?? 0} min={0} max={80} step={0.5} onChange={(value) => updateShotMotion(selectedShot.id, { blur: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Layer opacity" value={selectedShotMotionPose?.opacity ?? 1} min={0} max={1} step={0.05} onChange={(value) => updateShotMotion(selectedShot.id, { opacity: value })} />
                  <button className="ve-button" type="button" disabled={trackStates.shots.locked || !selectedMotionLayerContainsPlayhead} onClick={addShotKeyframe}>
                    <IconKey size={14} />
                    {veText("motion.add_keyframe")}
                  </button>
                </div>
                {activeMotionKeyframe && (
                  <div className="ve-motion-keyframe-active">
                    <span>{formatTime(selectedShot.start + activeMotionKeyframe.time)}</span>
                    <button type="button" disabled={trackStates.shots.locked} onClick={deleteShotKeyframe}>
                      <IconTrash size={13} />
                      {veText("motion.delete_keyframe")}
                    </button>
                  </div>
                )}
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

          {selectedGraphic && (
            <div className="ve-inspector-stack">
              <div className="ve-field">
                <FieldLabel>{veText("field.label")}</FieldLabel>
                <input className="ve-input" value={selectedGraphic.label} onChange={(event) => updateGraphic(selectedGraphic.id, { label: event.currentTarget.value })} />
              </div>
              <div className="ve-field">
                <FieldLabel>{veText("field.parent_group")}</FieldLabel>
                <Select
                  value={selectedGraphic.parentId ?? ""}
                  onChange={(value) => updateGraphic(selectedGraphic.id, { parentId: value || null })}
                  options={graphicGroupOptions}
                  buttonStyle={{ boxShadow: "none" }}
                />
                <span className="ve-field-note">{veText("group.child_note")}</span>
              </div>
              {selectedGraphic.kind === "group" && (
                <section className="ve-motion-section" aria-label="Nested layer group">
                  <div className="ve-motion-section-heading">
                    <div>
                      <strong>{veText("group.title")}</strong>
                      <small>{veText("group.description")}</small>
                    </div>
                    <span className="ve-feature-badge">{veText("group.seek_safe")}</span>
                  </div>
                  <label className="ve-check">
                    <input
                      type="checkbox"
                      checked={Boolean(selectedGraphic.clipChildren)}
                      onChange={(event) => updateGraphic(selectedGraphic.id, { clipChildren: event.currentTarget.checked })}
                    />
                    {veText("group.clip_children")}
                  </label>
                  <label className="ve-check">
                    <input
                      type="checkbox"
                      checked={Boolean(selectedGraphic.isTemplate)}
                      onChange={(event) => updateGraphic(selectedGraphic.id, {
                        isTemplate: event.currentTarget.checked,
                        ...(event.currentTarget.checked ? { instanceOf: null } : {}),
                      })}
                    />
                    {veText("group.reusable_template")}
                  </label>
                  {!selectedGraphic.isTemplate && (
                    <div className="ve-field">
                      <FieldLabel>{veText("group.subscene_source")}</FieldLabel>
                      <Select
                        value={selectedGraphic.instanceOf ?? ""}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { instanceOf: value || null })}
                        options={reusableGroupOptions}
                        buttonStyle={{ boxShadow: "none" }}
                      />
                      <span className="ve-field-note">{veText("group.instance_note")}</span>
                    </div>
                  )}
                </section>
              )}
              {selectedGraphic.kind !== "image" && selectedGraphic.kind !== "video" && (
                <div className="ve-field">
                  <FieldLabel>{veText("field.graphic_kind")}</FieldLabel>
                  <Select
                    value={selectedGraphic.kind}
                    onChange={(value) => updateGraphic(selectedGraphic.id, { kind: value as GraphicLayerKind })}
                    options={graphicKindOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
              )}
              {(selectedGraphic.kind === "image" || selectedGraphic.kind === "video") && (
                <div className="ve-asset-pill">
                  <strong>{veText(selectedGraphic.kind === "video" ? "video_overlay" : "image_overlay")}</strong>
                  <span>{selectedGraphic.assetName || selectedGraphic.assetDocumentId}</span>
                </div>
              )}
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>Effect</FieldLabel>
                  <Select
                    value={normalizeGraphicVisualStyle(selectedGraphic).effect}
                    onChange={(value) => updateGraphic(selectedGraphic.id, { effect: value as GraphicVisualStyle["effect"] })}
                    options={graphicEffectOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
                <div className="ve-field">
                  <FieldLabel>Mask</FieldLabel>
                  <Select
                    value={normalizeGraphicVisualStyle(selectedGraphic).maskShape}
                    onChange={(value) => updateGraphic(selectedGraphic.id, { maskShape: value as GraphicVisualStyle["maskShape"] })}
                    options={graphicMaskOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
              </div>
              {normalizeGraphicVisualStyle(selectedGraphic).effect !== "none" && (
                <NumberField label="Effect strength" value={normalizeGraphicVisualStyle(selectedGraphic).effectStrength} min={0} max={1} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { effectStrength: value })} />
              )}
              <div className="ve-two-col">
                <NumberField label={veText("field.start")} value={selectedGraphic.start} min={0} max={timelineDuration} onChange={(value) => updateGraphic(selectedGraphic.id, { start: value })} />
                <NumberField label={veText("field.end")} value={selectedGraphic.end} min={0} max={timelineDuration} onChange={(value) => updateGraphic(selectedGraphic.id, { end: value })} />
              </div>
              {selectedGraphic.kind === "video" && (() => {
                const sourceWindow = getGraphicVideoSourceWindow(selectedGraphic, selectedGraphic.assetDuration ?? 0);
                const maxDuration = Math.max(0.01, (selectedGraphic.assetDuration ?? sourceWindow.end) - sourceWindow.start);
                return (
                  <>
                    <div className="ve-two-col">
                      <NumberField
                        label={veText("field.source_start")}
                        value={sourceWindow.start}
                        min={0}
                        max={Math.max(0, (selectedGraphic.assetDuration ?? sourceWindow.end) - 0.01)}
                        step={1 / VIDEO_EDITOR_FPS}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { sourceStart: value })}
                      />
                      <NumberField
                        label={veText("field.source_duration")}
                        value={sourceWindow.duration}
                        min={0.01}
                        max={maxDuration}
                        step={1 / VIDEO_EDITOR_FPS}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { sourceEnd: sourceWindow.start + Math.max(0.01, value) })}
                      />
                    </div>
                    <div className="ve-two-col">
                      <NumberField
                        label={veText("field.clip_speed")}
                        value={normalizeVideoClipSpeed(selectedGraphic.speed)}
                        min={MIN_VIDEO_CLIP_SPEED}
                        max={MAX_VIDEO_CLIP_SPEED}
                        step={0.25}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { speed: value })}
                      />
                      <NumberField
                        label={veText("field.corner_radius")}
                        value={selectedGraphic.cornerRadius}
                        min={0}
                        max={100}
                        step={1}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { cornerRadius: value })}
                      />
                    </div>
                    <label className="ve-check">
                      <input type="checkbox" checked={Boolean(selectedGraphic.loop)} onChange={(event) => updateGraphic(selectedGraphic.id, { loop: event.currentTarget.checked })} />
                      {veText("loop_asset")}
                    </label>
                    <p className="ve-field-note">{veText("video_overlay_seek_hint")}</p>
                  </>
                );
              })()}
              <div className="ve-two-col">
                <NumberField label={veText("field.width_percent")} value={selectedGraphic.width} min={0.1} max={100} step={0.1} onChange={(value) => updateGraphic(selectedGraphic.id, { width: value })} />
                <NumberField label={veText("field.height_percent")} value={selectedGraphic.height} min={0.1} max={100} step={0.1} onChange={(value) => updateGraphic(selectedGraphic.id, { height: value })} />
              </div>
              {selectedGraphic.kind === "shader" && (() => {
                const shader = normalizeVideoEditorShaderStyle(selectedGraphic);
                const visual = normalizeGraphicVisualStyle(selectedGraphic);
                return (
                  <section className="ve-particle-editor" aria-label="WebGL shader controls">
                    <div className="ve-particle-editor-heading">
                      <div>
                        <strong>Procedural material</strong>
                        <span>Deterministic and frame-accurate in preview and export.</span>
                      </div>
                      <span className="ve-feature-badge">Native WebGL</span>
                    </div>
                    <div className="ve-field">
                      <FieldLabel>Material</FieldLabel>
                      <Select
                        value={shader.shaderPreset}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { shaderPreset: value as VideoEditorShaderStyle["shaderPreset"] })}
                        options={shaderPresetOptions}
                        buttonStyle={{ boxShadow: "none" }}
                      />
                    </div>
                    <div className="ve-two-col">
                      <NumberField label="Speed" value={shader.shaderSpeed} min={0} max={4} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderSpeed: value })} />
                      <NumberField label="Detail scale" value={shader.shaderScale} min={0.2} max={5} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderScale: value })} />
                    </div>
                    <div className="ve-two-col">
                      <NumberField label="Intensity" value={shader.shaderIntensity} min={0} max={2} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderIntensity: value })} />
                      <NumberField label="Bloom" value={shader.shaderBloom} min={0} max={2} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderBloom: value })} />
                    </div>
                    <div className="ve-two-col">
                      <NumberField label="Film grain" value={shader.shaderGrain} min={0} max={0.35} step={0.01} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderGrain: value })} />
                      <NumberField label="Seed" value={shader.shaderSeed} min={0} max={9999} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderSeed: value })} />
                    </div>
                    <div className="ve-two-col">
                      <NumberField label="Camera X" value={shader.shaderCameraX} min={-2} max={2} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderCameraX: value })} />
                      <NumberField label="Camera Y" value={shader.shaderCameraY} min={-2} max={2} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderCameraY: value })} />
                    </div>
                    <NumberField label="Camera depth" value={shader.shaderCameraZ} min={0.25} max={4} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { shaderCameraZ: value })} />
                    <div className="ve-three-color-row">
                      <div className="ve-field">
                        <FieldLabel>Base</FieldLabel>
                        <input className="ve-color" type="color" value={selectedGraphic.fill} onChange={(event) => updateGraphic(selectedGraphic.id, { fill: event.currentTarget.value })} />
                      </div>
                      <div className="ve-field">
                        <FieldLabel>Material</FieldLabel>
                        <input className="ve-color" type="color" value={visual.fillSecondary} onChange={(event) => updateGraphic(selectedGraphic.id, { fillSecondary: event.currentTarget.value })} />
                      </div>
                      <div className="ve-field">
                        <FieldLabel>Light</FieldLabel>
                        <input className="ve-color" type="color" value={selectedGraphic.stroke} onChange={(event) => updateGraphic(selectedGraphic.id, { stroke: event.currentTarget.value })} />
                      </div>
                    </div>
                  </section>
                );
              })()}
              {selectedGraphic.kind === "particle" && (() => {
                const particle = normalizeParticleLayer(selectedGraphic);
                return (
                  <section className="ve-particle-editor" aria-label={veText("particles.title")}>
                    <div className="ve-particle-editor-heading">
                      <div>
                        <strong>{veText("particles.title")}</strong>
                        <span>{veText("particles.seek_safe_hint")}</span>
                      </div>
                      <span className="ve-feature-badge">Canvas 2D</span>
                    </div>
                    <div className="ve-two-col">
                      <div className="ve-field">
                        <FieldLabel>{veText("particles.motion")}</FieldLabel>
                        <Select
                          value={particle.particleMotion}
                          onChange={(value) => updateGraphic(selectedGraphic.id, { particleMotion: value as ParticleMotion })}
                          options={particleMotionOptions}
                          buttonStyle={{ boxShadow: "none" }}
                        />
                      </div>
                      <div className="ve-field">
                        <FieldLabel>{veText("particles.shape")}</FieldLabel>
                        <Select
                          value={particle.particleShape}
                          onChange={(value) => updateGraphic(selectedGraphic.id, { particleShape: value as ParticleShape })}
                          options={particleShapeOptions}
                          buttonStyle={{ boxShadow: "none" }}
                        />
                      </div>
                    </div>
                    <div className="ve-two-col">
                      <NumberField label={veText("particles.count")} value={particle.particleCount} min={6} max={40} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { particleCount: value })} />
                      <NumberField label={veText("particles.size")} value={particle.particleSize} min={0.25} max={3} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { particleSize: value })} />
                    </div>
                    <div className="ve-two-col">
                      <NumberField label={veText("particles.speed")} value={particle.particleSpeed} min={0.25} max={3} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { particleSpeed: value })} />
                      <NumberField label={veText("particles.spread")} value={particle.particleSpread} min={0.1} max={1.5} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { particleSpread: value })} />
                    </div>
                    <div className="ve-two-col">
                      <NumberField label={veText("particles.gravity")} value={particle.particleGravity} min={-2} max={3} step={0.05} onChange={(value) => updateGraphic(selectedGraphic.id, { particleGravity: value })} />
                      <NumberField label={veText("particles.seed")} value={particle.particleSeed} min={0} max={9999} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { particleSeed: value })} />
                    </div>
                    <label className="ve-check">
                      <input type="checkbox" checked={particle.particleLoop} onChange={(event) => updateGraphic(selectedGraphic.id, { particleLoop: event.currentTarget.checked })} />
                      {veText("particles.loop")}
                    </label>
                    <div className="ve-three-color-row">
                      <div className="ve-field">
                        <FieldLabel>{veText("particles.color_a")}</FieldLabel>
                        <input className="ve-color" type="color" value={particle.fill} onChange={(event) => updateGraphic(selectedGraphic.id, { fill: event.currentTarget.value })} />
                      </div>
                      <div className="ve-field">
                        <FieldLabel>{veText("particles.color_b")}</FieldLabel>
                        <input className="ve-color" type="color" value={particle.fillSecondary} onChange={(event) => updateGraphic(selectedGraphic.id, { fillSecondary: event.currentTarget.value })} />
                      </div>
                      <div className="ve-field">
                        <FieldLabel>{veText("particles.color_c")}</FieldLabel>
                        <input className="ve-color" type="color" value={particle.stroke} onChange={(event) => updateGraphic(selectedGraphic.id, { stroke: event.currentTarget.value })} />
                      </div>
                    </div>
                  </section>
                );
              })()}
              {selectedGraphic.kind !== "group" && selectedGraphic.kind !== "image" && selectedGraphic.kind !== "video" && selectedGraphic.kind !== "particle" && selectedGraphic.kind !== "shader" && (
                <>
                  <div className="ve-two-col">
                    <div className="ve-field">
                      <FieldLabel>{veText("field.fill_color")}</FieldLabel>
                      <input className="ve-color" type="color" value={selectedGraphic.fill} onChange={(event) => updateGraphic(selectedGraphic.id, { fill: event.currentTarget.value })} />
                    </div>
                    <div className="ve-field">
                      <FieldLabel>{veText("field.stroke_color")}</FieldLabel>
                      <input className="ve-color" type="color" value={selectedGraphic.stroke} onChange={(event) => updateGraphic(selectedGraphic.id, { stroke: event.currentTarget.value })} />
                    </div>
                  </div>
                  <div className="ve-two-col">
                    <NumberField label={veText("field.stroke_width")} value={selectedGraphic.strokeWidth} min={0} max={20} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { strokeWidth: value })} />
                    {selectedGraphic.kind === "rectangle" && (
                      <NumberField label={veText("field.corner_radius")} value={selectedGraphic.cornerRadius} min={0} max={100} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { cornerRadius: value })} />
                    )}
                  </div>
                  <div className="ve-two-col">
                    <div className="ve-field">
                      <FieldLabel>Fill</FieldLabel>
                      <Select
                        value={normalizeGraphicVisualStyle(selectedGraphic).fillType}
                        onChange={(value) => updateGraphic(selectedGraphic.id, { fillType: value as GraphicVisualStyle["fillType"] })}
                        options={graphicFillOptions}
                        buttonStyle={{ boxShadow: "none" }}
                      />
                    </div>
                    {normalizeGraphicVisualStyle(selectedGraphic).fillType !== "solid" && (
                      <div className="ve-field">
                        <FieldLabel>Second color</FieldLabel>
                        <input className="ve-color" type="color" value={normalizeGraphicVisualStyle(selectedGraphic).fillSecondary} onChange={(event) => updateGraphic(selectedGraphic.id, { fillSecondary: event.currentTarget.value })} />
                      </div>
                    )}
                  </div>
                  {normalizeGraphicVisualStyle(selectedGraphic).fillType === "linear" && (
                    <NumberField label="Gradient angle" value={normalizeGraphicVisualStyle(selectedGraphic).gradientAngle} min={-360} max={360} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { gradientAngle: value })} />
                  )}
                  {selectedGraphic.kind === "path" && (
                    <>
                      <div className="ve-field">
                        <FieldLabel>SVG path</FieldLabel>
                        <textarea className="ve-textarea ve-mono-input" rows={4} value={normalizeGraphicVisualStyle(selectedGraphic).pathData} onChange={(event) => updateGraphic(selectedGraphic.id, { pathData: event.currentTarget.value })} />
                      </div>
                      <NumberField
                        label="Path draw progress"
                        value={selectedGraphicMotionPose?.pathProgress ?? 1}
                        min={0}
                        max={1}
                        step={0.01}
                        onChange={(value) => updateGraphicMotion(selectedGraphic.id, { pathProgress: value })}
                      />
                    </>
                  )}
                </>
              )}
              {(selectedGraphic.kind === "image" || selectedGraphic.kind === "video") && (
                <div className="ve-field">
                  <FieldLabel>{veText("media_fit")}</FieldLabel>
                  <Select
                    value={normalizeGraphicVisualStyle(selectedGraphic).assetFit}
                    onChange={(value) => updateGraphic(selectedGraphic.id, { assetFit: value as GraphicVisualStyle["assetFit"] })}
                    options={videoFitOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
              )}
              <div className="ve-two-col">
                <NumberField label="Shadow blur" value={normalizeGraphicVisualStyle(selectedGraphic).shadowBlur} min={0} max={160} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { shadowBlur: value })} />
                <NumberField label="Layer blur" value={normalizeGraphicVisualStyle(selectedGraphic).blur} min={0} max={80} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { blur: value })} />
              </div>
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>Blend</FieldLabel>
                  <Select
                    value={normalizeGraphicVisualStyle(selectedGraphic).blendMode}
                    onChange={(value) => updateGraphic(selectedGraphic.id, { blendMode: value as GraphicVisualStyle["blendMode"] })}
                    options={graphicBlendOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
                <NumberField label="Layer order" value={normalizeGraphicVisualStyle(selectedGraphic).zIndex} min={-1000} max={1000} step={1} onChange={(value) => updateGraphic(selectedGraphic.id, { zIndex: value })} />
              </div>
              <section className="ve-motion-editor" aria-label={veText("motion.title")}>
                <div className="ve-motion-header">
                  <div>
                    <strong>{veText("motion.title")}</strong>
                    <span>{activeMotionKeyframe
                      ? veText("motion.editing_keyframe", { time: formatTime(selectedGraphic.start + activeMotionKeyframe.time) })
                      : autoRecordMotion
                        ? veText("motion.auto_record_hint")
                        : veText("motion.base_pose_hint")}</span>
                  </div>
                  <label className="ve-check ve-motion-auto-record">
                    <input
                      type="checkbox"
                      checked={autoRecordMotion}
                      disabled={trackStates.graphics.locked}
                      onChange={(event) => {
                        setAutoRecordMotion(event.currentTarget.checked);
                        if (event.currentTarget.checked) setActiveMotionKeyframeId(null);
                      }}
                    />
                    {veText("motion.auto_record")}
                  </label>
                </div>
                <div className="ve-motion-presets" aria-label={veText("motion.presets")}>
                  <span>{veText("motion.presets")}</span>
                  {(["fadeIn", "slideUp", "pop"] as MotionPreset[]).map((preset) => (
                    <button key={preset} type="button" onClick={() => applyGraphicMotionPreset(preset)}>
                      {veText(`motion.preset.${preset}`)}
                    </button>
                  ))}
                  <button type="button" disabled={selectedGraphic.keyframes.length === 0} onClick={() => applyGraphicMotionPreset(null)}>
                    {veText("motion.clear")}
                  </button>
                </div>
                <div className="ve-two-col">
                  <NumberField label="X %" value={selectedGraphicMotionPose?.x ?? selectedGraphic.x} min={0} max={100} step={1} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { x: value })} />
                  <NumberField label="Y %" value={selectedGraphicMotionPose?.y ?? selectedGraphic.y} min={0} max={100} step={1} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { y: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label={veText("field.scale")} value={selectedGraphicMotionPose?.scale ?? selectedGraphic.scale} min={0.1} max={4} step={0.05} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { scale: value })} />
                  <NumberField label={veText("field.rotation")} value={selectedGraphicMotionPose?.rotation ?? selectedGraphic.rotation} min={-360} max={360} step={1} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { rotation: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Scale X" value={selectedGraphicMotionPose?.scaleX ?? 1} min={0.01} max={8} step={0.05} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { scaleX: value })} />
                  <NumberField label="Scale Y" value={selectedGraphicMotionPose?.scaleY ?? 1} min={0.01} max={8} step={0.05} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { scaleY: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Tilt X" value={selectedGraphicMotionPose?.rotationX ?? 0} min={-180} max={180} step={1} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { rotationX: value })} />
                  <NumberField label="Tilt Y" value={selectedGraphicMotionPose?.rotationY ?? 0} min={-180} max={180} step={1} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { rotationY: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Perspective" value={selectedGraphicMotionPose?.perspective ?? 1200} min={200} max={4000} step={50} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { perspective: value })} />
                  <NumberField label="Motion blur" value={selectedGraphicMotionPose?.blur ?? normalizeGraphicVisualStyle(selectedGraphic).blur} min={0} max={80} step={1} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { blur: value })} />
                </div>
                <NumberField label="Effect strength" value={selectedGraphicMotionPose?.effectStrength ?? normalizeGraphicVisualStyle(selectedGraphic).effectStrength} min={0} max={1} step={0.05} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { effectStrength: value })} />
                <NumberField label={veText("field.layer_opacity")} value={selectedGraphicMotionPose?.opacity ?? selectedGraphic.opacity} min={0} max={1} step={0.05} onChange={(value) => updateGraphicMotion(selectedGraphic.id, { opacity: value })} />
                <div className="ve-motion-actions">
                  <button className="ve-button" type="button" disabled={trackStates.graphics.locked || !selectedMotionLayerContainsPlayhead} onClick={addGraphicKeyframe}>
                    <IconKey size={14} />
                    {motionKeyframeAtPlayhead ? veText("motion.update_keyframe") : veText("motion.add_keyframe")}
                  </button>
                  {activeMotionKeyframe && (
                    <button className="ve-icon-button ve-danger" type="button" title={veText("motion.delete_keyframe")} onClick={deleteGraphicKeyframe}>
                      <IconTrash size={14} />
                    </button>
                  )}
                </div>
                {activeMotionKeyframe && (
                  <MotionKeyframeControls
                    keyframe={activeMotionKeyframe}
                    maxTime={Math.max(0, selectedGraphic.end - selectedGraphic.start)}
                    disabled={trackStates.graphics.locked}
                    onTimeChange={updateGraphicKeyframeTime}
                    onKeyframePatch={updateGraphicKeyframeOptions}
                  />
                )}
                {selectedGraphic.keyframes.length > 0 && (
                  <div className="ve-motion-keyframe-list" aria-label={veText("motion.keyframes")}>
                    {selectedGraphic.keyframes.map((keyframe, index) => (
                      <button
                        key={keyframe.id}
                        type="button"
                        className={keyframe.id === activeMotionKeyframeId ? "is-active" : ""}
                        onClick={() => {
                          setActiveMotionKeyframeId(keyframe.id);
                          seekTimeline(selectedGraphic.start + keyframe.time);
                        }}
                      >
                        <span aria-hidden="true" />
                        K{index + 1}
                        <small>{formatTime(selectedGraphic.start + keyframe.time)}</small>
                      </button>
                    ))}
                  </div>
                )}
              </section>
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
              <div className="ve-field">
                <FieldLabel>{veText("field.parent_group")}</FieldLabel>
                <Select
                  value={selectedCaption.parentId ?? ""}
                  onChange={(value) => updateCaption(selectedCaption.id, { parentId: value || null })}
                  options={graphicGroupOptions}
                  buttonStyle={{ boxShadow: "none" }}
                />
                <span className="ve-field-note">{veText("group.caption_note")}</span>
              </div>
              <div className="ve-two-col">
                <NumberField label={veText("field.start")} value={selectedCaption.start} min={0} max={timelineDuration} onChange={(value) => updateCaption(selectedCaption.id, { start: value })} />
                <NumberField label={veText("field.end")} value={selectedCaption.end} min={0} max={timelineDuration} onChange={(value) => updateCaption(selectedCaption.id, { end: value })} />
              </div>
              <section className="ve-motion-editor" aria-label={veText("motion.title")}>
                <div className="ve-motion-header">
                  <div>
                    <strong>{veText("motion.title")}</strong>
                    <span>{activeMotionKeyframe
                      ? veText("motion.editing_keyframe", { time: formatTime(selectedCaption.start + activeMotionKeyframe.time) })
                      : autoRecordMotion
                        ? veText("motion.auto_record_hint")
                        : veText("motion.base_pose_hint")}</span>
                  </div>
                  <label className="ve-check ve-motion-auto-record">
                    <input
                      type="checkbox"
                      checked={autoRecordMotion}
                      disabled={trackStates.captions.locked}
                      onChange={(event) => {
                        setAutoRecordMotion(event.currentTarget.checked);
                        if (event.currentTarget.checked) setActiveMotionKeyframeId(null);
                      }}
                    />
                    {veText("motion.auto_record")}
                  </label>
                </div>
                <div className="ve-motion-presets" aria-label={veText("motion.presets")}>
                  <span>{veText("motion.presets")}</span>
                  {(["fadeIn", "slideUp", "pop"] as MotionPreset[]).map((preset) => (
                    <button key={preset} type="button" onClick={() => applyCaptionMotionPreset(preset)}>
                      {veText(`motion.preset.${preset}`)}
                    </button>
                  ))}
                  <button type="button" disabled={selectedCaption.keyframes.length === 0} onClick={() => applyCaptionMotionPreset(null)}>
                    {veText("motion.clear")}
                  </button>
                </div>
                <div className="ve-two-col">
                  <NumberField label="X %" value={selectedCaptionMotionPose?.x ?? selectedCaption.x} min={0} max={100} step={1} onChange={(value) => updateCaptionMotion(selectedCaption.id, { x: value })} />
                  <NumberField label="Y %" value={selectedCaptionMotionPose?.y ?? selectedCaption.y} min={0} max={100} step={1} onChange={(value) => updateCaptionMotion(selectedCaption.id, { y: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label={veText("field.scale")} value={selectedCaptionMotionPose?.scale ?? selectedCaption.scale} min={0.1} max={4} step={0.05} onChange={(value) => updateCaptionMotion(selectedCaption.id, { scale: value })} />
                  <NumberField label={veText("field.rotation")} value={selectedCaptionMotionPose?.rotation ?? selectedCaption.rotation} min={-360} max={360} step={1} onChange={(value) => updateCaptionMotion(selectedCaption.id, { rotation: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Scale X" value={selectedCaptionMotionPose?.scaleX ?? 1} min={0.01} max={8} step={0.05} onChange={(value) => updateCaptionMotion(selectedCaption.id, { scaleX: value })} />
                  <NumberField label="Scale Y" value={selectedCaptionMotionPose?.scaleY ?? 1} min={0.01} max={8} step={0.05} onChange={(value) => updateCaptionMotion(selectedCaption.id, { scaleY: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Tilt X" value={selectedCaptionMotionPose?.rotationX ?? 0} min={-180} max={180} step={1} onChange={(value) => updateCaptionMotion(selectedCaption.id, { rotationX: value })} />
                  <NumberField label="Tilt Y" value={selectedCaptionMotionPose?.rotationY ?? 0} min={-180} max={180} step={1} onChange={(value) => updateCaptionMotion(selectedCaption.id, { rotationY: value })} />
                </div>
                <div className="ve-two-col">
                  <NumberField label="Perspective" value={selectedCaptionMotionPose?.perspective ?? 1200} min={200} max={4000} step={50} onChange={(value) => updateCaptionMotion(selectedCaption.id, { perspective: value })} />
                  <NumberField label="Motion blur" value={selectedCaptionMotionPose?.blur ?? 0} min={0} max={80} step={1} onChange={(value) => updateCaptionMotion(selectedCaption.id, { blur: value })} />
                </div>
                <NumberField label={veText("field.layer_opacity")} value={selectedCaptionMotionPose?.opacity ?? selectedCaption.opacity} min={0} max={1} step={0.05} onChange={(value) => updateCaptionMotion(selectedCaption.id, { opacity: value })} />
                <div className="ve-motion-actions">
                  <button
                    className="ve-button"
                    type="button"
                    disabled={trackStates.captions.locked || !selectedCaptionContainsPlayhead}
                    onClick={addCaptionKeyframe}
                  >
                    <IconKey size={14} />
                    {motionKeyframeAtPlayhead ? veText("motion.update_keyframe") : veText("motion.add_keyframe")}
                  </button>
                  {activeMotionKeyframe && (
                    <button className="ve-icon-button ve-danger" type="button" title={veText("motion.delete_keyframe")} onClick={deleteCaptionKeyframe}>
                      <IconTrash size={14} />
                    </button>
                  )}
                </div>
                {activeMotionKeyframe && (
                  <MotionKeyframeControls
                    keyframe={activeMotionKeyframe}
                    maxTime={Math.max(0, selectedCaption.end - selectedCaption.start)}
                    disabled={trackStates.captions.locked}
                    onTimeChange={updateCaptionKeyframeTime}
                    onKeyframePatch={updateCaptionKeyframeOptions}
                  />
                )}
                {selectedCaption.keyframes.length > 0 && (
                  <div className="ve-motion-keyframe-list" aria-label={veText("motion.keyframes")}>
                    {selectedCaption.keyframes.map((keyframe, index) => (
                      <button
                        key={keyframe.id}
                        type="button"
                        className={keyframe.id === activeMotionKeyframeId ? "is-active" : ""}
                        onClick={() => {
                          setActiveMotionKeyframeId(keyframe.id);
                          seekTimeline(selectedCaption.start + keyframe.time);
                        }}
                      >
                        <span aria-hidden="true" />
                        K{index + 1}
                        <small>{formatTime(selectedCaption.start + keyframe.time)}</small>
                      </button>
                    ))}
                  </div>
                )}
              </section>
              <NumberField label={veText("field.size")} value={selectedCaption.size} min={10} max={480} step={1} onChange={(value) => updateCaption(selectedCaption.id, { size: value })} />
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>Typeface</FieldLabel>
                  <Select
                    value={normalizeCaptionVisualStyle(selectedCaption).fontFamily}
                    onChange={(value) => updateCaption(selectedCaption.id, { fontFamily: value as CaptionVisualStyle["fontFamily"] })}
                    options={captionFontOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
                <NumberField label="Weight" value={normalizeCaptionVisualStyle(selectedCaption).fontWeight} min={100} max={900} step={100} onChange={(value) => updateCaption(selectedCaption.id, { fontWeight: value })} />
              </div>
              <div className="ve-two-col">
                <NumberField label="Letter spacing" value={normalizeCaptionVisualStyle(selectedCaption).letterSpacing} min={-8} max={32} step={0.5} onChange={(value) => updateCaption(selectedCaption.id, { letterSpacing: value })} />
                <NumberField label="Line height" value={normalizeCaptionVisualStyle(selectedCaption).lineHeight} min={0.8} max={2.4} step={0.05} onChange={(value) => updateCaption(selectedCaption.id, { lineHeight: value })} />
              </div>
              <div className="ve-two-col">
                <NumberField label="Text width %" value={normalizeCaptionVisualStyle(selectedCaption).maxWidth} min={10} max={96} step={1} onChange={(value) => updateCaption(selectedCaption.id, { maxWidth: value })} />
                <NumberField label="Layer order" value={normalizeCaptionVisualStyle(selectedCaption).zIndex} min={-1000} max={1000} step={1} onChange={(value) => updateCaption(selectedCaption.id, { zIndex: value })} />
              </div>
              <div className="ve-two-col">
                <div className="ve-field">
                  <FieldLabel>Text transform</FieldLabel>
                  <Select
                    value={normalizeCaptionVisualStyle(selectedCaption).textTransform}
                    onChange={(value) => updateCaption(selectedCaption.id, { textTransform: value as CaptionVisualStyle["textTransform"] })}
                    options={captionTransformOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
                <div className="ve-field">
                  <FieldLabel>Reveal</FieldLabel>
                  <Select
                    value={normalizeCaptionVisualStyle(selectedCaption).reveal}
                    onChange={(value) => updateCaption(selectedCaption.id, { reveal: value as CaptionVisualStyle["reveal"] })}
                    options={captionRevealOptions}
                    buttonStyle={{ boxShadow: "none" }}
                  />
                </div>
              </div>
              {normalizeCaptionVisualStyle(selectedCaption).reveal !== "none" && (
                <NumberField label="Reveal duration" value={normalizeCaptionVisualStyle(selectedCaption).revealDuration} min={0.05} max={8} step={0.05} onChange={(value) => updateCaption(selectedCaption.id, { revealDuration: value })} />
              )}
              <div className="ve-two-col">
                <NumberField label="Text shadow" value={normalizeCaptionVisualStyle(selectedCaption).textShadowBlur} min={0} max={80} step={1} onChange={(value) => updateCaption(selectedCaption.id, { textShadowBlur: value })} />
                <NumberField label="Text outline" value={normalizeCaptionVisualStyle(selectedCaption).strokeWidth} min={0} max={16} step={0.5} onChange={(value) => updateCaption(selectedCaption.id, { strokeWidth: value })} />
              </div>
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
              {selectedAudio.musicLicenseName && (
                <div className="ve-music-license-card">
                  <div>
                    <IconMusicNote size={15} />
                    <span>
                      <strong>{selectedAudio.musicCreator || veText("free_music.unknown_creator")}</strong>
                      <small>{selectedAudio.musicLicenseName}</small>
                    </span>
                  </div>
                  <p>{selectedAudio.musicAttribution}</p>
                  <div className="ve-music-license-actions">
                    {selectedAudio.musicSourceUrl && (
                      <a href={selectedAudio.musicSourceUrl} target="_blank" rel="noreferrer">
                        {veText("free_music.source")} <IconExternalLink size={12} />
                      </a>
                    )}
                    {selectedAudio.musicLicenseUrl && (
                      <a href={selectedAudio.musicLicenseUrl} target="_blank" rel="noreferrer">
                        {veText("free_music.license")} <IconExternalLink size={12} />
                      </a>
                    )}
                    {selectedAudio.musicAttribution && (
                      <button
                        type="button"
                        onClick={() => {
                          if (!navigator.clipboard) {
                            toast.error(veText("toast.music_credit_copy_failed"));
                            return;
                          }
                          void navigator.clipboard.writeText(selectedAudio.musicAttribution || "")
                            .then(() => toast.success(veText("toast.music_credit_copied")))
                            .catch(() => toast.error(veText("toast.music_credit_copy_failed")));
                        }}
                      >
                        <IconCopy size={12} /> {veText("free_music.copy_credit")}
                      </button>
                    )}
                  </div>
                </div>
              )}
              <div className="ve-two-col">
                <NumberField label={veText("field.start")} value={selectedAudio.start} min={0} max={timelineDuration} onChange={(value) => updateAudioCue(selectedAudio.id, { start: value })} />
                <NumberField label={veText("field.end")} value={selectedAudio.end} min={0} max={timelineDuration} onChange={(value) => updateAudioCue(selectedAudio.id, { end: value })} />
              </div>
              {selectedAudio.assetDocumentId && (
                <>
                  <div className="ve-two-col">
                    <NumberField
                      label="Source in"
                      value={selectedAudio.sourceStart ?? 0}
                      min={0}
                      max={Math.max(0, (selectedAudio.assetDuration ?? selectedAudio.sourceEnd ?? selectedAudio.end - selectedAudio.start) - 0.01)}
                      step={1 / VIDEO_EDITOR_FPS}
                      onChange={(value) => updateAudioCue(selectedAudio.id, { sourceStart: value })}
                    />
                    <NumberField
                      label="Source duration"
                      value={Math.max(0.01, (selectedAudio.sourceEnd ?? selectedAudio.assetDuration ?? selectedAudio.end - selectedAudio.start) - (selectedAudio.sourceStart ?? 0))}
                      min={0.01}
                      max={Math.max(0.01, (selectedAudio.assetDuration ?? selectedAudio.sourceEnd ?? selectedAudio.end - selectedAudio.start) - (selectedAudio.sourceStart ?? 0))}
                      step={1 / VIDEO_EDITOR_FPS}
                      onChange={(value) => updateAudioCue(selectedAudio.id, {
                        sourceEnd: (selectedAudio.sourceStart ?? 0) + Math.max(0.01, value),
                      })}
                    />
                  </div>
                  <p className="ve-field-note">Source trim is seeked identically in preview and final export. Loop repeats only the selected source window.</p>
                </>
              )}
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
        </aside>}
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
            <span>{veText("graphics_count", { count: graphicLayers.length })}</span>
            <span>{veText("captions_count", { count: captions.length })}</span>
            <span>{veText("audio_cues_count", { count: audioCues.length })}</span>
          </div>
          <div className="ve-timeline-tools">
            <div className="ve-tool-switcher" role="toolbar" aria-label={veText("timeline_tools")}>
              <button
                className={timelineTool === "select" ? "is-active" : ""}
                type="button"
                aria-pressed={timelineTool === "select"}
                title={veText("shortcut.select_tool")}
                onClick={() => {
                  setTimelineTool("select");
                  setRazorPreview(null);
                }}
              >
                <IconEdit size={13} />
                {veText("tool.select")}
              </button>
              <button
                className={timelineTool === "razor" ? "is-active" : ""}
                type="button"
                aria-pressed={timelineTool === "razor"}
                title={veText("shortcut.razor_tool")}
                onClick={() => setTimelineTool("razor")}
              >
                <IconClock size={13} />
                {veText("tool.razor")}
              </button>
            </div>
            <button
              className="ve-mini-text-button ve-keyframe-button"
              type="button"
              title={veText("motion.add_keyframe_at_playhead")}
              disabled={!selectedMotionLayer || selectedTrackLocked || !selectedMotionLayerContainsPlayhead}
              onClick={selectedClip
                ? addClipKeyframe
                : selectedShot
                  ? addShotKeyframe
                  : selectedGraphic
                    ? addGraphicKeyframe
                    : addCaptionKeyframe}
            >
              <IconKey size={13} />
              {veText("motion.keyframe")}
            </button>
            <label className="ve-check ve-compact-check" title={veText("motion.auto_record_hint")}>
              <input
                type="checkbox"
                checked={autoRecordMotion}
                disabled={!selectedMotionLayer || selectedTrackLocked}
                onChange={(event) => {
                  setAutoRecordMotion(event.currentTarget.checked);
                  if (event.currentTarget.checked) setActiveMotionKeyframeId(null);
                }}
              />
              {veText("motion.auto")}
            </label>
            <button className="ve-mini-text-button" type="button" title={veText("shortcut.split")} disabled={trackStates.video.locked} onClick={splitClipAtPlayhead}>{veText("split_at_playhead")}</button>
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
            <button className="ve-mini-button" type="button" title={veText("shortcuts")} onClick={() => setShortcutsOpen(true)}>
              <IconHelp size={15} />
            </button>
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
                      {shot.keyframes.map((keyframe) => (
                        <span
                          key={keyframe.id}
                          className={`ve-keyframe-diamond ${selection?.type === "shot" && selection.id === shot.id && activeMotionKeyframeId === keyframe.id ? "is-active" : ""}`}
                          style={{ left: `${((keyframe.time) / Math.max(0.05, shot.end - shot.start)) * 100}%` }}
                          role="button"
                          tabIndex={trackStates.shots.locked ? -1 : 0}
                          aria-label={`Scene group keyframe at ${formatTime(shot.start + keyframe.time)}`}
                          onPointerDown={(event) => event.stopPropagation()}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelection({ type: "shot", id: shot.id });
                            setActiveMotionKeyframeId(keyframe.id);
                            seekTimeline(shot.start + keyframe.time);
                          }}
                          onKeyDown={(event) => {
                            if (trackStates.shots.locked || (event.key !== "Enter" && event.key !== " ")) return;
                            event.preventDefault();
                            event.stopPropagation();
                            setSelection({ type: "shot", id: shot.id });
                            setActiveMotionKeyframeId(keyframe.id);
                            seekTimeline(shot.start + keyframe.time);
                          }}
                        />
                      ))}
                      <span className="ve-block-label">{shot.scene} · {shot.title}</span>
                      <span className="ve-resize-handle ve-resize-end" onPointerDown={(event) => beginCueResize(event, "shot", shot.id, "end")} />
                    </button>
                  );
                })}
              </div>
            </div>
            <div className="ve-track ve-video-track">
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
                  const filmstripPixelWidth = duration * timelineEffectivePixelsPerSecond;
                  const filmstripFrameCount = Math.min(24, Math.max(2, Math.ceil(filmstripPixelWidth / 64)));
                  const fadeInWidth = (normalizeVideoClipFade(clip.fadeIn, duration) / duration) * 100;
                  const fadeOutWidth = (normalizeVideoClipFade(clip.fadeOut, duration) / duration) * 100;
                  return (
                    <div
                      key={clip.id}
                      role="button"
                      tabIndex={trackStates.video.locked ? -1 : 0}
                      aria-disabled={trackStates.video.locked}
                      className={`ve-clip-block ${timelineTool === "razor" ? "is-razor" : ""} ${trackStates.video.visible ? "" : "is-hidden-track"} ${trackStates.video.locked ? "is-locked-track" : ""} ${trackStates.video.muted ? "is-muted-track" : ""} ${clipHasManualEdit(clip, sourceDuration) ? "is-manual" : ""} ${selection?.type === "clip" && selection.id === clip.id ? "is-selected" : ""} ${draggingClipId === clip.id ? "is-dragging" : ""} ${hasAiHighlight("clip", clip.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${width}%`, background: clip.color }}
                      title={veText(timelineTool === "razor" ? "razor_click_hint" : "clip_drag_hint")}
                      aria-label={veText("clip_aria_label", { label, duration: formatTime(duration) })}
                      aria-grabbed={draggingClipId === clip.id}
                      draggable={false}
                      onPointerDown={(event) => {
                        if (timelineTool === "razor") razorSplitClip(event, span);
                        else beginClipDrag(event, clip.id);
                      }}
                      onDragStart={(event) => event.preventDefault()}
                      onPointerMove={(event) => {
                        if (timelineTool !== "razor") return;
                        const rect = event.currentTarget.getBoundingClientRect();
                        if (rect.width <= 0) return;
                        setRazorPreview({
                          clipId: clip.id,
                          percent: clamp(((event.clientX - rect.left) / rect.width) * 100, 0, 100),
                        });
                      }}
                      onPointerLeave={() => {
                        if (razorPreview?.clipId === clip.id) setRazorPreview(null);
                      }}
                      onClick={() => {
                        if (timelineTool === "select") setSelection({ type: "clip", id: clip.id });
                      }}
                      onKeyDown={(event) => {
                        if (trackStates.video.locked || (event.key !== "Enter" && event.key !== " ")) return;
                        event.preventDefault();
                        setSelection({ type: "clip", id: clip.id });
                        setActiveMotionKeyframeId(null);
                      }}
                    >
                      <TimelineClipFilmstrip
                        assetId={clip.assetDocumentId || doc.id}
                        sourceStart={clip.sourceStart}
                        sourceEnd={clip.sourceEnd}
                        frameCount={filmstripFrameCount}
                      />
                      {fadeInWidth > 0 && (
                        <span className="ve-clip-fade-edge is-in" style={{ width: `${fadeInWidth}%` }} aria-hidden="true" />
                      )}
                      {fadeOutWidth > 0 && (
                        <span className="ve-clip-fade-edge is-out" style={{ width: `${fadeOutWidth}%` }} aria-hidden="true" />
                      )}
                      {timelineTool === "razor" && razorPreview?.clipId === clip.id && (
                        <span className="ve-razor-preview" style={{ left: `${razorPreview.percent}%` }} aria-hidden="true" />
                      )}
                      {clip.keyframes.map((keyframe) => (
                        <button
                          key={keyframe.id}
                          className={`ve-keyframe-diamond ${selection?.type === "clip" && selection.id === clip.id && activeMotionKeyframeId === keyframe.id ? "is-active" : ""}`}
                          type="button"
                          disabled={trackStates.video.locked}
                          title={veText("motion.keyframe_at", { time: formatTime(span.start + keyframe.time) })}
                          style={{ left: `${clamp((keyframe.time / duration) * 100, 0, 100)}%` }}
                          onPointerDown={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                          }}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelection({ type: "clip", id: clip.id });
                            setActiveMotionKeyframeId(keyframe.id);
                            seekTimeline(span.start + keyframe.time);
                          }}
                        />
                      ))}
                      <span className="ve-resize-handle ve-resize-start" title={veText("trim_start")} onPointerDown={(event) => beginClipTrim(event, clip.id, "start")} />
                      <span className="ve-clip-grip" aria-hidden="true"><IconDragHandle size={13} /></span>
                      <span className="ve-clip-title">{label}</span>
                      <small>{formatTime(duration)} · {normalizeVideoClipSpeed(clip.speed).toFixed(2).replace(/\.00$/, "")}×</small>
                      <span className="ve-resize-handle ve-resize-end" title={veText("trim_end")} onPointerDown={(event) => beginClipTrim(event, clip.id, "end")} />
                    </div>
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
              {renderTrackLabel("graphics", veText("graphics"))}
              <div className={`ve-track-lane ${trackStates.graphics.visible ? "" : "is-hidden-track"} ${trackStates.graphics.locked ? "is-locked-track" : ""}`}>
                {graphicLayers.map((graphic) => {
                  const left = timelineDuration ? (graphic.start / timelineDuration) * 100 : 0;
                  const width = timelineDuration ? ((graphic.end - graphic.start) / timelineDuration) * 100 : 0;
                  const graphicDuration = Math.max(0.05, graphic.end - graphic.start);
                  const videoSourceWindow = graphic.kind === "video"
                    ? getGraphicVideoSourceWindow(graphic, graphic.assetDuration ?? 0)
                    : null;
                  const videoFilmstripFrameCount = Math.floor(clamp(
                    Math.ceil((graphicDuration * timelineEffectivePixelsPerSecond) / 88),
                    2,
                    12,
                  ));
                  return (
                    <div
                      key={graphic.id}
                      role="button"
                      tabIndex={trackStates.graphics.locked ? -1 : 0}
                      aria-disabled={trackStates.graphics.locked}
                      className={`ve-graphic-block ve-graphic-block-${graphic.kind} ${trackStates.graphics.visible ? "" : "is-hidden-track"} ${trackStates.graphics.locked ? "is-locked-track" : ""} ${selection?.type === "graphic" && selection.id === graphic.id ? "is-selected" : ""} ${hasAiHighlight("graphic", graphic.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${Math.max(1.5, width)}%` }}
                      onPointerDown={(event) => {
                        if (!trackStates.graphics.locked) beginCueDrag(event, "graphic", graphic.id);
                      }}
                      onClick={() => {
                        if (trackStates.graphics.locked) return;
                        setSelection({ type: "graphic", id: graphic.id });
                        setActiveMotionKeyframeId(null);
                      }}
                      onKeyDown={(event) => {
                        if (trackStates.graphics.locked || (event.key !== "Enter" && event.key !== " ")) return;
                        event.preventDefault();
                        setSelection({ type: "graphic", id: graphic.id });
                        setActiveMotionKeyframeId(null);
                      }}
                    >
                      <span className="ve-resize-handle ve-resize-start" onPointerDown={(event) => beginCueResize(event, "graphic", graphic.id, "start")} />
                      {graphic.kind === "video" && graphic.assetDocumentId && videoSourceWindow && (
                        <TimelineClipFilmstrip
                          assetId={graphic.assetDocumentId}
                          sourceStart={videoSourceWindow.start}
                          sourceEnd={videoSourceWindow.end}
                          frameCount={videoFilmstripFrameCount}
                        />
                      )}
                      {graphic.keyframes.map((keyframe) => (
                        <button
                          key={keyframe.id}
                          className={`ve-keyframe-diamond ${selection?.type === "graphic" && selection.id === graphic.id && activeMotionKeyframeId === keyframe.id ? "is-active" : ""}`}
                          type="button"
                          disabled={trackStates.graphics.locked}
                          title={veText("motion.keyframe_at", { time: formatTime(graphic.start + keyframe.time) })}
                          style={{ left: `${clamp((keyframe.time / graphicDuration) * 100, 0, 100)}%` }}
                          onPointerDown={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                          }}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelection({ type: "graphic", id: graphic.id });
                            setActiveMotionKeyframeId(keyframe.id);
                            seekTimeline(graphic.start + keyframe.time);
                          }}
                        />
                      ))}
                      <span className="ve-block-label">{graphic.label}</span>
                      <span className="ve-resize-handle ve-resize-end" onPointerDown={(event) => beginCueResize(event, "graphic", graphic.id, "end")} />
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="ve-track">
              {renderTrackLabel("captions", veText("captions"))}
              <div className={`ve-track-lane ${trackStates.captions.visible ? "" : "is-hidden-track"} ${trackStates.captions.locked ? "is-locked-track" : ""}`}>
                {captions.map((caption) => {
                  const left = timelineDuration ? (caption.start / timelineDuration) * 100 : 0;
                  const width = timelineDuration ? ((caption.end - caption.start) / timelineDuration) * 100 : 0;
                  const captionDuration = Math.max(0.05, caption.end - caption.start);
                  return (
                    <div
                      key={caption.id}
                      role="button"
                      tabIndex={trackStates.captions.locked ? -1 : 0}
                      aria-disabled={trackStates.captions.locked}
                      className={`ve-caption-block ${trackStates.captions.visible ? "" : "is-hidden-track"} ${trackStates.captions.locked ? "is-locked-track" : ""} ${selection?.type === "caption" && selection.id === caption.id ? "is-selected" : ""} ${hasAiHighlight("caption", caption.id) ? "is-ai-highlighted" : ""}`}
                      style={{ left: `${left}%`, width: `${Math.max(1.5, width)}%` }}
                      onPointerDown={(event) => {
                        if (!trackStates.captions.locked) beginCueDrag(event, "caption", caption.id);
                      }}
                      onClick={() => {
                        if (trackStates.captions.locked) return;
                        setSelection({ type: "caption", id: caption.id });
                        setActiveMotionKeyframeId(null);
                      }}
                      onKeyDown={(event) => {
                        if (trackStates.captions.locked || (event.key !== "Enter" && event.key !== " ")) return;
                        event.preventDefault();
                        setSelection({ type: "caption", id: caption.id });
                        setActiveMotionKeyframeId(null);
                      }}
                    >
                      <span className="ve-resize-handle ve-resize-start" onPointerDown={(event) => beginCueResize(event, "caption", caption.id, "start")} />
                      {caption.keyframes.map((keyframe) => (
                        <button
                          key={keyframe.id}
                          className={`ve-keyframe-diamond ${selection?.type === "caption" && selection.id === caption.id && activeMotionKeyframeId === keyframe.id ? "is-active" : ""}`}
                          type="button"
                          disabled={trackStates.captions.locked}
                          title={veText("motion.keyframe_at", { time: formatTime(caption.start + keyframe.time) })}
                          style={{ left: `${clamp((keyframe.time / captionDuration) * 100, 0, 100)}%` }}
                          onPointerDown={(event) => {
                            event.preventDefault();
                            event.stopPropagation();
                          }}
                          onClick={(event) => {
                            event.stopPropagation();
                            setSelection({ type: "caption", id: caption.id });
                            setActiveMotionKeyframeId(keyframe.id);
                            seekTimeline(caption.start + keyframe.time);
                          }}
                        />
                      ))}
                      <span className="ve-block-label">{caption.speaker ? `${caption.speaker}: ${caption.text}` : caption.text}</span>
                      <span className="ve-resize-handle ve-resize-end" onPointerDown={(event) => beginCueResize(event, "caption", caption.id, "end")} />
                    </div>
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
                      <TimelineAudioWaveform
                        cue={cue}
                        barCount={Math.min(120, Math.max(8, ((cue.end - cue.start) * timelineEffectivePixelsPerSecond) / 5))}
                      />
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
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
        title={veText("shortcuts")}
        width="min(720px, calc(100vw - 48px))"
        maxWidth="720px"
        footer={(
          <button className="ve-button ve-button-primary" type="button" onClick={() => setShortcutsOpen(false)}>{t("action.done")}</button>
        )}
      >
        <div className="ve-shortcuts-grid">
          {shortcutGroups.map((group) => (
            <section className="ve-shortcut-group" key={group.title}>
              <h3>{group.title}</h3>
              {group.items.map(([label, keys]) => (
                <div className="ve-shortcut-row" key={label}>
                  <span>{label}</span>
                  <kbd>{keys}</kbd>
                </div>
              ))}
            </section>
          ))}
        </div>
      </Modal>

      <Modal
        open={freeMusicOpen}
        onClose={closeFreeMusicLibrary}
        title={veText("free_music.title")}
        className="ve-free-music-dialog"
        bodyClassName="ve-free-music-dialog-body"
        width="min(980px, calc(100vw - 48px))"
        height="min(760px, calc(100dvh - 48px))"
        maxWidth="980px"
        footer={(
          <div className="ve-free-music-footer">
            <span>{veText("free_music.footer_note")}</span>
            <button className="ve-button" type="button" onClick={closeFreeMusicLibrary}>{t("action.done")}</button>
          </div>
        )}
      >
        <div className="ve-free-music-library">
          <header>
            <div>
              <strong>{veText("free_music.subtitle")}</strong>
              <span>{veText("free_music.description")}</span>
            </div>
            <a href="https://openverse.org/" target="_blank" rel="noreferrer">
              Openverse <IconExternalLink size={12} />
            </a>
          </header>
          <form
            className="ve-free-music-search"
            onSubmit={(event) => {
              event.preventDefault();
              submitFreeMusicSearch();
            }}
          >
            <div className="ve-media-search">
              <IconSearch size={16} />
              <input
                className="ve-input"
                value={freeMusicSearch}
                onChange={(event) => setFreeMusicSearch(event.currentTarget.value)}
                placeholder={veText("free_music.search_placeholder")}
                autoFocus
              />
            </div>
            <button className="ve-button ve-button-primary" type="submit" disabled={freeMusicSearch.trim().length < 2}>
              {veText("free_music.search")}
            </button>
          </form>
          <div className="ve-free-music-presets" aria-label={veText("free_music.moods")}>
            {FREE_MUSIC_SEARCH_PRESETS.map((preset) => (
              <button
                key={preset.key}
                type="button"
                className={submittedFreeMusicSearch === preset.query ? "is-active" : ""}
                onClick={() => chooseFreeMusicSearch(preset.query)}
              >
                {veText(`free_music.preset.${preset.key}`)}
              </button>
            ))}
          </div>
          <div className="ve-free-music-summary">
            <span>{veText("free_music.commercial_safe")}</span>
            {freeMusicQuery.data && (
              <small>{veText("free_music.results_count", { count: freeMusicQuery.data.total })}</small>
            )}
          </div>
          <div className="ve-free-music-results" aria-live="polite">
            {freeMusicQuery.isLoading && (
              <div className="ve-free-music-state"><LoadingSpinner size={24} /><span>{veText("free_music.loading")}</span></div>
            )}
            {freeMusicQuery.isError && (
              <div className="ve-free-music-state is-error">
                <IconMusicNote size={24} />
                <strong>{veText("free_music.error")}</strong>
                <button className="ve-button" type="button" onClick={() => { void freeMusicQuery.refetch(); }}>
                  {veText("free_music.retry")}
                </button>
              </div>
            )}
            {!freeMusicQuery.isLoading && !freeMusicQuery.isError && freeMusicQuery.data?.items.length === 0 && (
              <div className="ve-free-music-state">
                <IconMusicNote size={24} />
                <strong>{veText("free_music.empty")}</strong>
                <span>{veText("free_music.empty_detail")}</span>
              </div>
            )}
            {!freeMusicQuery.isLoading && !freeMusicQuery.isError && freeMusicQuery.data?.items.map((track) => {
              const previewing = previewingFreeMusicId === track.id;
              const importing = importingFreeMusicId === track.id;
              const added = addedFreeMusicIds.has(track.id);
              const previewProgress = freeMusicPreviewProgress?.trackId === track.id
                ? freeMusicPreviewProgress
                : null;
              const previewDuration = previewProgress?.duration || track.duration_seconds || 0;
              const previewRatio = previewDuration > 0
                ? clamp((previewProgress?.currentTime || 0) / previewDuration, 0, 1)
                : 0;
              const previewTimeLabel = previewing
                ? `${formatTime(previewProgress?.currentTime || 0)} / ${previewDuration > 0 ? formatTime(previewDuration) : veText("free_music.duration_unknown")}`
                : track.duration_seconds
                  ? formatTime(track.duration_seconds)
                  : veText("free_music.duration_unknown");
              return (
                <article className="ve-free-music-card" key={track.id}>
                  <button
                    className={`ve-free-music-preview ${previewing ? "is-playing" : ""}`}
                    type="button"
                    aria-label={previewing ? `${veText("free_music.stop_preview")}. ${previewTimeLabel}` : veText("free_music.preview")}
                    title={previewing ? veText("free_music.stop_preview") : veText("free_music.preview")}
                    onClick={() => { void toggleFreeMusicPreview(track); }}
                  >
                    {previewing && (
                      <svg className="ve-free-music-preview-ring" viewBox="0 0 40 40" aria-hidden="true">
                        <circle className="ve-free-music-preview-ring-track" cx="20" cy="20" r="17" />
                        <circle
                          className="ve-free-music-preview-ring-value"
                          cx="20"
                          cy="20"
                          r="17"
                          strokeDasharray={FREE_MUSIC_PREVIEW_RING_CIRCUMFERENCE}
                          strokeDashoffset={FREE_MUSIC_PREVIEW_RING_CIRCUMFERENCE * (1 - previewRatio)}
                        />
                      </svg>
                    )}
                    {previewing ? (
                      <span className="ve-free-music-meter" aria-hidden="true"><span /><span /><span /></span>
                    ) : <IconPlay size={17} />}
                  </button>
                  <div className="ve-free-music-card-copy">
                    <strong title={track.title}>{track.title}</strong>
                    <span>{track.creator}</span>
                    <small className={previewing ? "is-previewing" : ""}>
                      <span className="ve-free-music-time">{previewTimeLabel}</span>
                      {track.genres.length > 0 ? ` · ${track.genres.slice(0, 2).join(" · ")}` : ""}
                    </small>
                  </div>
                  <div className="ve-free-music-license">
                    <strong>{track.license_name}</strong>
                    <span>{track.license === "by" ? veText("free_music.credit_required") : veText("free_music.no_credit_required")}</span>
                  </div>
                  <div className="ve-free-music-card-actions">
                    <a href={track.source_url} target="_blank" rel="noreferrer" title={veText("free_music.open_source")}>
                      <IconExternalLink size={14} />
                    </a>
                    <button
                      className={`ve-button ${added ? "is-added" : "ve-button-primary"}`}
                      type="button"
                      disabled={Boolean(importingFreeMusicId) || trackStates.audio.locked || added}
                      onClick={() => { void importFreeMusic(track); }}
                    >
                      {importing
                        ? <span className="ve-audio-preview-spinner" aria-hidden="true" />
                        : added
                          ? <IconCheck size={14} />
                          : <IconPlus size={14} />}
                      {importing
                        ? veText("free_music.adding")
                        : added
                          ? veText("free_music.added")
                          : veText("free_music.add")}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      </Modal>

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
      <MediaInsertDialog
        open={unifiedMediaInsertOpen}
        onClose={() => setUnifiedMediaInsertOpen(false)}
        allowedKinds={["image", "video"]}
        defaultKind="video"
        onInsert={addUnifiedMediaAsset}
      />
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
  --ve-border: rgba(214, 211, 209, 0.68);
  --ve-border-soft: rgba(28, 25, 23, 0.07);
  --ve-surface: var(--glass-panel);
  --ve-surface-solid: var(--surface-panel);
  --ve-muted-surface: var(--surface-muted);
  --ve-teal: var(--accent);
  box-sizing: border-box;
  container-name: video-editor;
  container-type: inline-size;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overflow: hidden;
  padding: 12px 14px 14px;
  background: var(--surface-muted);
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
  padding: 9px 10px;
  border: 0;
  border-radius: var(--radius-card);
  background: var(--ve-surface);
  box-shadow: var(--shadow-sm);
  backdrop-filter: var(--glass-blur-sm);
}
.ve-topbar-left, .ve-topbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}
.ve-topbar-left {
  flex: 1 1 240px;
  overflow: hidden;
}
.ve-topbar-actions {
  flex: 0 1 auto;
  flex-wrap: nowrap;
  justify-content: flex-end;
  gap: 8px;
  max-width: calc(100% - 220px);
  padding: 2px;
  overflow-x: auto;
  scrollbar-width: none;
}
.ve-topbar-actions::-webkit-scrollbar {
  display: none;
}
.ve-topbar-actions > * {
  flex: 0 0 auto;
}
.ve-save-actions {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}
.ve-view-controls {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 0;
  border-radius: 9px;
  background: var(--ve-muted-surface);
}
.ve-view-toggle {
  min-height: 28px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 8px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #78716c;
  font-size: 11px;
  font-weight: 800;
  cursor: pointer;
}
.ve-view-toggle:hover,
.ve-view-toggle.is-active {
  background: var(--ve-surface-solid);
  color: #292524;
  box-shadow: 0 1px 4px rgba(28,25,23,.08);
}
.ve-view-toggle:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-ring);
}
.ve-title-block {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
}
.ve-title-row, .ve-section-title, .ve-track-label-name {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.ve-title-row .page-header-title, .ve-section-title h2, .ve-track-label-name > span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ve-title-row {
  display: flex;
  max-width: 100%;
}
.ve-title-row .page-header-title {
  flex: 1 1 auto;
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
.ve-title-block .page-header-title {
  white-space: nowrap;
  max-width: 100%;
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
.ve-workspace.is-media-panel-closed {
  grid-template-columns: minmax(420px, 1fr) minmax(284px, 330px);
}
.ve-workspace.is-inspector-panel-closed {
  grid-template-columns: minmax(250px, 280px) minmax(420px, 1fr);
}
.ve-workspace.is-media-panel-closed.is-inspector-panel-closed {
  grid-template-columns: minmax(420px, 1fr);
}
.ve-panel, .ve-preview, .ve-timeline {
  border: 0;
  border-radius: var(--radius-card);
  background: var(--ve-surface);
  box-shadow: var(--shadow-sm);
  backdrop-filter: var(--glass-blur-sm);
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
  background: rgba(28, 25, 23, .035);
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
  border: 0;
  border-radius: 10px;
  background: rgba(28, 25, 23, .035);
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
  border: 0;
  border-radius: 12px;
  background: rgba(28, 25, 23, .035);
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
  background: rgba(28, 25, 23, .06);
  box-shadow: 0 0 0 3px var(--accent-ring);
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
.ve-open-media-picker:disabled {
  opacity: .55;
  cursor: not-allowed;
}
.ve-open-free-music {
  color: var(--text-default);
}
.ve-free-license-pill {
  display: inline-flex !important;
  width: max-content;
  padding: 4px 6px;
  border-radius: 999px;
  background: var(--surface-panel);
  box-shadow: var(--shadow-sm);
  color: var(--text-muted);
  font-size: 9px;
  font-weight: 900;
  line-height: 1;
  white-space: nowrap;
}
.ve-media-tabs {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px;
}
.ve-media-tabs button, .ve-media-filter-row button {
  min-width: 0;
  border: 0;
  background: rgba(28, 25, 23, .035);
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
  background: var(--ve-surface-solid);
  color: #292524;
  box-shadow: var(--shadow-sm);
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
  border: 0;
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
  box-shadow: 0 9px 18px rgba(28,25,23,.06);
  outline: 2px solid var(--accent-ring);
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
  border: 0;
  background: rgba(28, 25, 23, .04);
  color: #57534e;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  text-decoration: none;
  font-size: 11px;
  font-weight: 850;
  cursor: pointer;
}
.ve-media-action:hover:not(:disabled), .ve-media-action.is-active {
  background: rgba(28, 25, 23, .09);
  color: #1c1917;
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
.ve-free-music-dialog .manor-dialog-header,
.ve-free-music-dialog .manor-dialog-footer {
  flex: 0 0 auto;
}
.ve-free-music-dialog-body {
  display: flex;
  min-height: 0;
  overflow: hidden;
}
.ve-free-music-library {
  width: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ve-free-music-library > header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.ve-free-music-library > header > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.ve-free-music-library > header strong {
  color: var(--text-strong);
  font-size: 14px;
}
.ve-free-music-library > header span {
  max-width: 690px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.5;
}
.ve-free-music-library > header > a,
.ve-music-license-actions a,
.ve-music-license-actions button {
  border: 0;
  background: transparent;
  color: var(--text-muted);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 800;
  text-decoration: none;
  white-space: nowrap;
  cursor: pointer;
}
.ve-free-music-library > header > a:hover,
.ve-music-license-actions a:hover,
.ve-music-license-actions button:hover {
  color: var(--text-strong);
}
.ve-free-music-search {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}
.ve-free-music-search .ve-input {
  min-height: 40px;
}
.ve-free-music-search .ve-button {
  min-width: 94px;
}
.ve-free-music-presets {
  flex: 0 0 auto;
  min-height: 32px;
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding: 1px 1px 3px;
}
.ve-free-music-presets button {
  flex: 0 0 auto;
  min-height: 28px;
  padding: 0 10px;
  border: 0;
  border-radius: 999px;
  background: var(--surface-muted);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 800;
  cursor: pointer;
}
.ve-free-music-presets button:hover,
.ve-free-music-presets button.is-active {
  background: var(--surface-panel);
  box-shadow: var(--shadow-sm);
  color: var(--text-strong);
}
.ve-free-music-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 750;
}
.ve-free-music-summary span::before {
  content: "";
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 999px;
  background: var(--accent);
  vertical-align: 1px;
}
.ve-free-music-results {
  min-height: 0;
  flex: 1 1 auto;
  overflow: auto;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-content: start;
  gap: 8px;
  padding: 1px 2px 4px 1px;
}
.ve-free-music-state {
  grid-column: 1 / -1;
  min-height: 190px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  gap: 8px;
  border-radius: var(--radius-card);
  background: var(--surface-muted);
  color: var(--text-muted);
  text-align: center;
}
.ve-free-music-state strong {
  color: var(--text-strong);
}
.ve-free-music-card {
  min-width: 0;
  min-height: 92px;
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) 106px auto;
  align-items: center;
  gap: 9px;
  padding: 10px;
  border-radius: var(--radius-card);
  background: var(--glass-card);
  box-shadow: var(--shadow-sm);
  backdrop-filter: var(--glass-blur-sm);
}
.ve-free-music-preview {
  width: 36px;
  height: 36px;
  position: relative;
  flex: 0 0 auto;
  border: 0;
  border-radius: 999px;
  background: var(--surface-sunken);
  color: var(--text-default);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}
.ve-free-music-preview-ring {
  width: 44px;
  height: 44px;
  position: absolute;
  inset: -4px;
  overflow: visible;
  pointer-events: none;
  transform: rotate(-90deg);
}
.ve-free-music-preview-ring-track,
.ve-free-music-preview-ring-value {
  fill: none;
  stroke-linecap: round;
  stroke-width: 2.2;
}
.ve-free-music-preview-ring-track {
  stroke: var(--border-default);
}
.ve-free-music-preview-ring-value {
  stroke: var(--accent);
  transition: stroke-dashoffset .22s linear;
}
.ve-free-music-meter {
  width: 15px;
  height: 15px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
}
.ve-free-music-meter > span {
  width: 2px;
  height: 13px;
  border-radius: 999px;
  background: currentColor;
  transform-origin: center;
  animation: ve-free-music-meter 720ms ease-in-out infinite;
}
.ve-free-music-meter > span:nth-child(2) {
  animation-delay: -360ms;
}
.ve-free-music-meter > span:nth-child(3) {
  animation-delay: -180ms;
}
@keyframes ve-free-music-meter {
  0%, 100% { transform: scaleY(.32); }
  50% { transform: scaleY(1); }
}
.ve-free-music-preview:hover,
.ve-free-music-preview.is-playing {
  background: var(--ink);
  color: var(--surface-panel);
}
.ve-free-music-card-copy,
.ve-free-music-license {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-free-music-card-copy strong {
  color: var(--text-strong);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-free-music-card-copy span,
.ve-free-music-card-copy small,
.ve-free-music-license span {
  color: var(--text-muted);
  font-size: 10px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ve-free-music-card-copy small.is-previewing,
.ve-free-music-card-copy small.is-previewing .ve-free-music-time {
  color: var(--text-default);
  font-variant-numeric: tabular-nums;
  font-weight: 750;
}
.ve-free-music-license strong {
  color: var(--text-default);
  font-size: 10px;
}
.ve-free-music-card-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ve-free-music-card-actions > a {
  width: 30px;
  height: 30px;
  border-radius: var(--radius-control);
  background: var(--surface-muted);
  color: var(--text-muted);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.ve-free-music-card-actions > a:hover {
  color: var(--text-strong);
}
.ve-free-music-card-actions .ve-button {
  min-width: 74px;
  gap: 5px;
}
.ve-free-music-card-actions .ve-button.is-added,
.ve-free-music-card-actions .ve-button.is-added:disabled {
  background: var(--surface-sunken);
  color: var(--text-default);
  cursor: default;
  opacity: 1;
}
.ve-free-music-footer {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.ve-free-music-footer span {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}
.ve-music-license-card {
  padding: 10px;
  border-radius: var(--radius-control);
  background: var(--surface-muted);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ve-music-license-card > div:first-child {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ve-music-license-card > div:first-child > span {
  min-width: 0;
  display: flex;
  flex-direction: column;
}
.ve-music-license-card strong {
  color: var(--text-strong);
  font-size: 11px;
}
.ve-music-license-card small {
  color: var(--text-muted);
  font-size: 10px;
}
.ve-music-license-card p {
  margin: 0;
  color: var(--text-muted);
  font-size: 10px;
  line-height: 1.45;
}
.ve-music-license-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 9px;
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
  background: rgba(28, 25, 23, .035);
  border-color: transparent;
}
.ve-track-action:hover:not(:disabled), .ve-button:hover:not(:disabled), .ve-link-button:hover {
  background: rgba(28, 25, 23, .065);
  border-color: transparent;
}
.ve-track-action:disabled {
  opacity: .55;
  cursor: not-allowed;
}
.ve-button, .ve-link-button {
  padding: 6px 11px;
  border-color: transparent;
  background: rgba(28, 25, 23, .035);
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
  border-color: var(--accent);
  background: var(--accent);
  color: #ffffff;
}
.ve-button-primary:hover:not(:disabled) {
  border-color: var(--accent-hover);
  background: var(--accent-hover);
  color: #ffffff;
}
.ve-button:focus-visible,
.ve-link-button:focus-visible,
.ve-icon-button:focus-visible,
.ve-round-button:focus-visible,
.ve-mini-button:focus-visible,
.ve-media-tabs button:focus-visible,
.ve-media-filter-row button:focus-visible,
.ve-media-action:focus-visible,
.ve-open-media-picker:focus-visible,
.ve-free-music-preview:focus-visible,
.ve-free-music-presets button:focus-visible,
.ve-free-music-card-actions a:focus-visible,
.ve-music-license-actions a:focus-visible,
.ve-music-license-actions button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-ring);
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
  box-shadow: var(--shadow-md);
  backdrop-filter: none;
}
.ve-preview:fullscreen {
  width: 100vw;
  height: 100vh;
  border: 0;
  border-radius: 0;
}
.ve-preview:fullscreen .ve-preview-media-stage {
  max-width: 100vw;
  max-height: 100vh;
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
.ve-preview-media-stage {
  position: relative;
  flex: 0 0 auto;
  max-width: 100%;
  max-height: 100%;
  overflow: hidden;
}
.ve-motion-path-overlay {
  position: absolute;
  inset: 0;
  z-index: 6;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
}
.ve-motion-path-overlay polyline {
  fill: none;
  stroke: rgba(103, 232, 249, .88);
  stroke-width: 1.75;
  stroke-dasharray: 5 4;
  vector-effect: non-scaling-stroke;
  filter: drop-shadow(0 1px 3px rgba(2, 6, 23, .72));
}
.ve-motion-path-overlay circle {
  fill: #f8fafc;
  stroke: #0f766e;
  stroke-width: 1.5;
  vector-effect: non-scaling-stroke;
}
.ve-motion-path-overlay circle.is-active {
  fill: #fef3c7;
  stroke: #d97706;
  stroke-width: 2;
}
.ve-motion-path-overlay .ve-motion-path-playhead {
  fill: #67e8f9;
  stroke: #083344;
  stroke-width: 2;
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
  position: absolute;
  z-index: 0;
  left: 50%;
  top: 50%;
  width: 100%;
  height: 100%;
  max-height: 62vh;
  object-fit: contain;
  display: block;
  transform-origin: center;
}
.ve-preview-media-stage .ve-preview-video {
  max-height: none;
}
.ve-graphic-overlay {
  position: absolute;
  z-index: 2;
  box-sizing: border-box;
  overflow: hidden;
  cursor: grab;
  touch-action: none;
  user-select: none;
  transform-origin: center;
}

.ve-graphic-group {
  isolation: isolate;
  transform-style: preserve-3d;
}

.ve-graphic-group > .ve-graphic-overlay,
.ve-graphic-group > .ve-caption-overlay {
  position: absolute;
}
.ve-graphic-overlay:focus-visible,
.ve-graphic-overlay.is-selected {
  outline: 2px solid rgba(95,146,138,.9);
  outline-offset: 3px;
}
.ve-graphic-overlay.is-ai-highlighted {
  box-shadow: 0 0 0 2px rgba(125,211,252,.72), 0 0 24px rgba(56,189,248,.42);
}
.ve-graphic-overlay img,
.ve-graphic-overlay video {
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
}
.ve-graphic-overlay svg {
  width: 100%;
  height: 100%;
  display: block;
  overflow: visible;
  pointer-events: none;
}
.ve-particle-canvas {
  width: 100%;
  height: 100%;
  display: block;
  pointer-events: none;
}
.ve-graphic-loading {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  background: rgba(28,25,23,.34);
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
  font-size: 1em;
  color: inherit;
  opacity: 1;
}
.ve-caption-speechBubble {
  border: 1px solid rgba(28,25,23,.16);
  box-shadow: 0 10px 24px rgba(28,25,23,.18);
}
.ve-caption-narrationBox {
  border: 1px solid rgba(250,250,249,.22);
  box-shadow: 0 10px 24px rgba(28,25,23,.24);
}
.ve-caption-titleCard {
  max-width: 86%;
  font-weight: 900;
  letter-spacing: -.025em;
  text-wrap: balance;
  box-shadow: 0 18px 46px rgba(2,6,23,.26);
}
.ve-caption-lowerThird {
  max-width: 58%;
  border-left: 4px solid var(--ve-accent);
  box-shadow: 0 12px 30px rgba(2,6,23,.28);
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
	  min-height: 0;
	  display: flex;
	  flex-direction: column;
	  align-items: stretch;
	  gap: 5px;
	  padding: 7px 9px;
	  border: 0;
	  border-radius: 12px;
	  background: var(--glass-card);
	  box-shadow: var(--shadow-sm);
	  backdrop-filter: var(--glass-blur-sm);
	  overflow: hidden;
	}
.ve-transport-primary,
.ve-transport-secondary {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.ve-transport-secondary {
  justify-content: space-between;
  overflow-x: auto;
  scrollbar-width: none;
}
.ve-transport-secondary .ve-mini-button {
  width: 28px;
  height: 28px;
}
.ve-transport-secondary .ve-compact-check {
  min-height: 28px;
  padding: 0 5px;
}
.ve-transport-secondary .ve-mini-text-button {
  padding: 0 5px;
}
.ve-transport-secondary::-webkit-scrollbar {
  display: none;
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
	  justify-content: flex-end;
	  gap: 5px;
	  flex: 1 0 auto;
	  min-width: 0;
	  flex-wrap: nowrap;
	}
.ve-preview-tools {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 0;
  border-radius: 9px;
  background: var(--ve-muted-surface);
}
.ve-preview-tools .ve-mini-button.is-active {
  border-color: rgba(67,107,101,.32);
  background: #eaf3f0;
  color: #365c56;
}
.ve-playback-rate select {
  height: 28px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: #44403c;
  font: inherit;
  font-size: 11px;
  font-weight: 850;
  cursor: pointer;
}
.ve-workarea-controls > span {
  color: #78716c;
  font-size: 10px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
	.ve-scrubber {
	  flex: 1 1 120px;
	  min-width: 90px;
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
.ve-field-note {
  margin: -4px 0 0;
  color: #78716c;
  font-size: 12px;
  line-height: 1.45;
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
  border: 0;
  border-radius: var(--radius-control);
  background: rgba(28, 25, 23, .045);
  color: #1c1917;
  font-size: 13px;
  transition: background .12s ease, box-shadow .12s ease;
}
.ve-input {
  height: 34px;
  padding: 0 9px;
}
.ve-input:focus, .ve-textarea:focus {
  outline: none;
  background: var(--ve-surface-solid);
  box-shadow: 0 0 0 3px var(--accent-ring);
}
.ve-input:disabled {
  cursor: not-allowed;
  opacity: .52;
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
.ve-particle-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px;
  border-radius: 12px;
  background: linear-gradient(145deg, rgba(73, 63, 103, .09), rgba(95, 146, 138, .06));
}
.ve-particle-editor-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.ve-particle-editor-heading > div {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-particle-editor-heading strong {
  color: #292524;
  font-size: 12px;
}
.ve-particle-editor-heading span:not(.ve-feature-badge) {
  color: #78716c;
  font-size: 10px;
  line-height: 1.35;
}
.ve-three-color-row {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.ve-motion-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 9px;
  border-radius: 12px;
  background: rgba(28, 25, 23, .035);
  box-shadow: none;
}
.ve-motion-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.ve-motion-header > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-motion-header strong {
  color: #292524;
  font-size: 12px;
}
.ve-motion-header span {
  color: #78716c;
  font-size: 10px;
  line-height: 1.35;
}
.ve-motion-auto-record {
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 800;
}
.ve-motion-presets {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}
.ve-motion-presets > span {
  margin-right: 2px;
  color: #78716c;
  font-size: 10px;
  font-weight: 900;
}
.ve-motion-presets button {
  min-height: 24px;
  padding: 0 7px;
  border: 0;
  border-radius: 7px;
  background: rgba(255, 255, 255, .78);
  color: #57534e;
  font-size: 10px;
  font-weight: 850;
  cursor: pointer;
  box-shadow: var(--shadow-sm);
}
.ve-motion-presets button:hover:not(:disabled) {
  color: #1c1917;
  box-shadow: 0 0 0 2px var(--accent-ring);
}
.ve-motion-presets button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-ring);
}
.ve-motion-presets button:disabled {
  opacity: .45;
  cursor: not-allowed;
}
.ve-motion-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ve-motion-actions .ve-button {
  flex: 1 1 auto;
}
.ve-motion-keyframe-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
  border-radius: 10px;
  background: rgba(255, 255, 255, .58);
  box-shadow: var(--shadow-sm);
}
.ve-motion-curve {
  display: grid;
  grid-template-columns: 116px minmax(0, 1fr);
  align-items: center;
  gap: 9px;
  min-width: 0;
}
.ve-motion-curve svg {
  width: 116px;
  height: 92px;
  overflow: visible;
  touch-action: none;
}
.ve-motion-curve-grid {
  fill: none;
  stroke: rgba(120, 113, 108, .18);
  stroke-width: 1;
}
.ve-motion-curve polyline {
  fill: none;
  stroke: var(--ve-teal);
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 2.4;
}
.ve-motion-bezier-guides {
  fill: none;
  stroke: rgba(120, 113, 108, .56);
  stroke-width: 1;
  stroke-dasharray: 3 3;
  vector-effect: non-scaling-stroke;
}
.ve-motion-curve circle {
  fill: var(--ve-teal);
}
.ve-motion-curve .ve-motion-bezier-handle {
  fill: #fef3c7;
  stroke: #b45309;
  stroke-width: 1.5;
  cursor: grab;
  vector-effect: non-scaling-stroke;
  pointer-events: all;
}
.ve-motion-curve .ve-motion-bezier-handle:active {
  cursor: grabbing;
}
.ve-motion-curve > div {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ve-motion-curve strong {
  color: #292524;
  font-size: 10px;
}
.ve-motion-curve span {
  color: #78716c;
  font-size: 9px;
  line-height: 1.35;
}
.ve-motion-curve small {
  color: #57534e;
  font: 600 9px/1.35 "JetBrains Mono", ui-monospace, monospace;
}
.ve-motion-bezier-fields {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
}
.ve-motion-bezier-fields .ve-input {
  padding-inline: 6px;
}
.ve-motion-keyframe-list {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.ve-motion-keyframe-list button {
  min-height: 26px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 0 7px;
  border: 0;
  border-radius: 7px;
  background: rgba(255, 255, 255, .78);
  color: #57534e;
  font-size: 10px;
  font-weight: 900;
  cursor: pointer;
  box-shadow: var(--shadow-sm);
}
.ve-motion-keyframe-list button > span {
  width: 7px;
  height: 7px;
  transform: rotate(45deg);
  border-radius: 1px;
  background: #a8a29e;
}
.ve-motion-keyframe-list button small {
  color: #a8a29e;
  font: 500 9px/1 "JetBrains Mono", ui-monospace, monospace;
}
.ve-motion-keyframe-list button:hover,
.ve-motion-keyframe-list button.is-active {
  color: #1c1917;
  box-shadow: 0 0 0 2px var(--accent-ring), 0 3px 8px rgba(28,25,23,.08);
}
.ve-motion-keyframe-list button.is-active > span {
  background: var(--ve-teal);
}
.ve-motion-keyframe-list button:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--accent-ring);
}
.ve-timeline {
  flex: 0 0 clamp(278px, 35vh, 318px);
  min-height: 0;
  position: relative;
  padding: 10px 12px 12px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
.ve-timeline-header {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: center;
  column-gap: 12px;
  row-gap: 7px;
  margin-bottom: 8px;
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
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex-wrap: nowrap;
  overflow-x: auto;
  scrollbar-width: none;
}
.ve-timeline-summary::-webkit-scrollbar {
  display: none;
}
.ve-timeline-summary span {
  min-height: 26px;
  padding: 5px 8px;
  flex: 0 0 auto;
  border: 0;
  border-radius: 999px;
  background: rgba(28, 25, 23, .04);
  color: #78716c;
  font-size: 11px;
  font-weight: 850;
}
.ve-timeline-tools {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: flex-start;
  gap: 6px;
  min-width: 0;
  padding: 1px 2px 3px;
  flex-wrap: nowrap;
  overflow-x: auto;
  scrollbar-width: none;
}
.ve-timeline-tools::-webkit-scrollbar {
  display: none;
}
.ve-timeline-tools > * {
  flex: 0 0 auto;
}
.ve-tool-switcher {
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 0;
  border-radius: 9px;
  background: var(--ve-muted-surface);
}
.ve-tool-switcher button {
  height: 24px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0 7px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: #78716c;
  font-size: 11px;
  font-weight: 850;
  cursor: pointer;
}
.ve-tool-switcher button:hover,
.ve-tool-switcher button.is-active {
  background: var(--ve-surface-solid);
  color: #292524;
  box-shadow: 0 1px 4px rgba(28,25,23,.08);
}
.ve-tool-switcher button:focus-visible {
  outline: 2px solid rgba(67,107,101,.42);
  outline-offset: 1px;
}
.ve-zoom-control {
  min-height: 30px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 8px;
  border: 0;
  border-radius: 8px;
  background: var(--ve-muted-surface);
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
  border: 0;
  border-radius: 8px;
  background: var(--ve-muted-surface);
}
.ve-shell .manor-select-trigger {
  border: 0 !important;
  background: var(--ve-muted-surface);
  box-shadow: none !important;
}
.ve-shell .manor-select-trigger:focus-visible,
.ve-shell .manor-select-trigger[aria-expanded="true"] {
  box-shadow: 0 0 0 3px var(--accent-ring) !important;
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
  border-color: transparent;
  background: rgba(28, 25, 23, .06);
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
.ve-keyframe-button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
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
.ve-shortcuts-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}
.ve-shortcut-group {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--ve-border-soft);
  border-radius: 12px;
  background: var(--ve-muted-surface);
}
.ve-shortcut-group h3 {
  margin: 0 0 10px;
  color: #292524;
  font-size: 12px;
}
.ve-shortcut-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 32px;
  color: #57534e;
  font-size: 12px;
}
.ve-shortcut-row + .ve-shortcut-row {
  border-top: 1px solid var(--ve-border-soft);
}
.ve-shortcut-row kbd {
  flex: 0 0 auto;
  padding: 3px 6px;
  border: 1px solid var(--ve-border);
  border-radius: 6px;
  background: var(--ve-surface-solid);
  color: #292524;
  font-family: inherit;
  font-size: 10px;
  font-weight: 850;
  box-shadow: 0 1px 2px rgba(28,25,23,.08);
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
.ve-video-track,
.ve-video-track .ve-track-lane {
  min-height: 64px;
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
.ve-clip-block, .ve-shot-block, .ve-graphic-block, .ve-caption-block, .ve-audio-block {
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
.ve-clip-block[aria-disabled="true"] {
  cursor: not-allowed;
}
.ve-clip-block {
  position: absolute;
  top: 6px;
  height: 52px;
  border-radius: 7px;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: flex-start;
  padding: 4px 14px 4px 31px;
  text-align: left;
  cursor: grab;
  touch-action: none;
  user-select: none;
  transition: transform .12s ease, box-shadow .12s ease, filter .12s ease;
}
.ve-clip-block.is-razor {
  cursor: crosshair;
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
.ve-clip-filmstrip {
  position: absolute;
  inset: 0;
  z-index: 0;
  display: flex;
  max-width: none !important;
  border-radius: inherit;
  pointer-events: none;
  background: rgba(28,25,23,.16);
}
.ve-clip-filmstrip::after {
  content: "";
  position: absolute;
  inset: 0;
  background: linear-gradient(180deg, rgba(28,25,23,.02) 30%, rgba(28,25,23,.76) 100%);
}
.ve-clip-filmstrip.is-loading {
  background-image: repeating-linear-gradient(90deg, rgba(255,255,255,.10) 0 18%, rgba(28,25,23,.10) 18% 20%);
}
.ve-clip-filmstrip img {
  min-width: 0;
  flex: 1 1 0;
  height: 100%;
  object-fit: cover;
  border-right: 1px solid rgba(255,255,255,.18);
}
.ve-clip-filmstrip img:last-child {
  border-right: 0;
}
.ve-clip-fade-edge {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 1;
  max-width: 50% !important;
  pointer-events: none;
}
.ve-clip-fade-edge.is-in {
  left: 0;
  background: linear-gradient(90deg, rgba(2,6,23,.9), rgba(2,6,23,0));
}
.ve-clip-fade-edge.is-out {
  right: 0;
  background: linear-gradient(270deg, rgba(2,6,23,.9), rgba(2,6,23,0));
}
.ve-razor-preview {
  position: absolute;
  top: 0;
  bottom: 0;
  z-index: 4;
  width: 2px;
  max-width: none !important;
  transform: translateX(-1px);
  overflow: visible !important;
  background: #ffffff;
  box-shadow: 0 0 0 1px rgba(28,25,23,.48), 0 0 0 4px rgba(255,255,255,.18);
  pointer-events: none;
}
.ve-razor-preview::before,
.ve-razor-preview::after {
  content: "";
  position: absolute;
  left: 50%;
  width: 7px;
  height: 7px;
  transform: translateX(-50%) rotate(45deg);
  background: #ffffff;
  box-shadow: 0 0 0 1px rgba(28,25,23,.42);
}
.ve-razor-preview::before {
  top: -3px;
}
.ve-razor-preview::after {
  bottom: -3px;
}
.ve-clip-title,
.ve-clip-block small,
.ve-clip-grip {
  position: relative;
  z-index: 2;
  text-shadow: 0 1px 3px rgba(28,25,23,.72);
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
.ve-shot-block, .ve-graphic-block, .ve-caption-block, .ve-audio-block {
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
.ve-graphic-block {
  background: #5f928a;
}
.ve-graphic-block-image {
  background: #7c6f64;
}
.ve-graphic-block-video {
  overflow: hidden;
  background: #365e66;
}
.ve-graphic-block-video .ve-clip-filmstrip {
  opacity: .78;
}
.ve-graphic-block-video::after {
  position: absolute;
  inset: 0;
  content: "";
  pointer-events: none;
  background: linear-gradient(90deg, rgba(28,25,23,.42), transparent 32%, rgba(28,25,23,.18));
}
.ve-graphic-block-ellipse {
  background: #a97935;
}
.ve-graphic-block-particle {
  overflow: hidden;
  background-color: #493f67;
  background-image:
    radial-gradient(circle at 12px 14px, #f8c95c 0 2px, transparent 2.5px),
    radial-gradient(circle at 34px 8px, #65d6c4 0 1.7px, transparent 2.2px),
    radial-gradient(circle at 50px 23px, #ff6b7a 0 2.2px, transparent 2.7px);
  background-size: 58px 28px;
}
.ve-caption-block:focus-visible,
.ve-graphic-block:focus-visible {
  outline: 3px solid var(--accent-ring);
  outline-offset: 1px;
}
.ve-caption-block .ve-block-label {
  position: relative;
  z-index: 1;
  pointer-events: none;
}
.ve-graphic-block .ve-block-label {
  position: relative;
  z-index: 1;
  pointer-events: none;
}
.ve-keyframe-diamond {
  position: absolute;
  top: 50%;
  z-index: 4;
  width: 12px;
  height: 12px;
  padding: 0;
  transform: translate(-50%, -50%) rotate(45deg);
  border: 2px solid rgba(28,25,23,.68);
  border-radius: 2px;
  background: #ffffff;
  cursor: pointer;
  box-shadow: 0 0 0 1px rgba(255,255,255,.7);
}
.ve-keyframe-diamond:hover,
.ve-keyframe-diamond.is-active {
  background: #82ada4;
  border-color: #ffffff;
  box-shadow: 0 0 0 2px rgba(67,107,101,.72);
}
.ve-keyframe-diamond:focus-visible {
  outline: 2px solid #ffffff;
  outline-offset: 2px;
}
.ve-keyframe-diamond:disabled {
  cursor: not-allowed;
  opacity: .58;
}
.ve-shot-block {
  background: #57534e;
}
.ve-audio-block {
  background: #4f7e87;
}
.ve-audio-waveform {
  position: absolute;
  inset: 4px 8px;
  z-index: 0;
  display: flex;
  align-items: center;
  gap: 1px;
  max-width: none !important;
  overflow: hidden;
  pointer-events: none;
  opacity: .58;
}
.ve-audio-waveform i {
  min-width: 1px;
  flex: 1 1 0;
  border-radius: 999px;
  background: rgba(255,255,255,.82);
}
.ve-audio-waveform.is-loading {
  opacity: .28;
}
.ve-audio-block .ve-block-label {
  position: relative;
  z-index: 2;
  max-width: 100%;
  padding: 2px 5px;
  overflow: hidden;
  border-radius: 5px;
  background: rgba(28,25,23,.36);
  text-overflow: ellipsis;
  text-shadow: 0 1px 2px rgba(28,25,23,.5);
}
.ve-clip-block.is-ai-highlighted,
.ve-shot-block.is-ai-highlighted,
.ve-graphic-block.is-ai-highlighted,
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
@media (max-width: 1340px) {
  .ve-view-toggle {
    width: 28px;
    justify-content: center;
    padding: 0;
  }
  .ve-view-toggle span,
  .ve-open-export .ve-action-label {
    display: none;
  }
}
@media (max-width: 1100px) {
  .ve-topbar {
    gap: 10px;
  }
  .ve-topbar-actions {
    max-width: calc(100% - 180px);
  }
  .ve-save-plan {
    width: 34px;
    justify-content: center;
    padding: 0;
  }
  .ve-save-plan .ve-action-label {
    display: none;
  }
}
@container video-editor (max-width: 920px) {
  .ve-workspace {
    grid-template-columns: 210px minmax(320px, 1fr);
    grid-auto-rows: minmax(280px, auto);
    overflow: auto;
    padding-right: 2px;
  }
  .ve-workspace.is-media-panel-closed {
    grid-template-columns: minmax(320px, 1fr);
  }
  .ve-workspace.is-inspector-panel-closed {
    grid-template-columns: 210px minmax(320px, 1fr);
  }
  .ve-workspace.is-media-panel-closed.is-inspector-panel-closed {
    grid-template-columns: minmax(320px, 1fr);
  }
  .ve-inspector {
    grid-column: 1 / -1;
    min-height: 280px;
  }
}
@container video-editor (max-width: 640px) {
  .ve-workspace,
  .ve-workspace.is-media-panel-closed,
  .ve-workspace.is-inspector-panel-closed,
  .ve-workspace.is-media-panel-closed.is-inspector-panel-closed {
    grid-template-columns: minmax(0, 1fr);
  }
  .ve-inspector {
    grid-column: auto;
  }
  .ve-timeline-header {
    grid-template-columns: minmax(0, 1fr);
  }
  .ve-timeline-summary,
  .ve-timeline-tools {
    grid-column: 1;
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
  .ve-topbar-left,
  .ve-topbar-actions {
    width: 100%;
  }
  .ve-topbar-actions {
    max-width: 100%;
    justify-content: flex-start;
  }
  .ve-title-block .page-header-title {
    max-width: 82vw;
  }
  .ve-workspace,
  .ve-workspace.is-media-panel-closed,
  .ve-workspace.is-inspector-panel-closed,
  .ve-workspace.is-media-panel-closed.is-inspector-panel-closed {
    grid-template-columns: 1fr;
    flex: 0 0 auto;
    min-height: auto;
  }
  .ve-timeline {
    flex-basis: 320px;
  }
  .ve-transport {
    align-items: flex-start;
    overflow: hidden;
  }
  .ve-transport-primary,
  .ve-transport-secondary {
    width: 100%;
  }
  .ve-transport-primary {
    flex-wrap: wrap;
  }
  .ve-timecode {
    flex: 1 1 120px;
  }
  .ve-workarea-controls {
    flex-basis: 100%;
  }
  .ve-scrubber {
    flex-basis: 100%;
    min-width: 100%;
  }
  .ve-track {
    grid-template-columns: 132px var(--ve-lane-width, 1fr);
  }
  .ve-track-toggle {
    width: 20px;
    height: 20px;
  }
  .ve-timeline-summary {
    width: 100%;
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
  .ve-free-music-library > header,
  .ve-free-music-footer {
    align-items: stretch;
    flex-direction: column;
  }
  .ve-free-music-search {
    grid-template-columns: minmax(0, 1fr);
  }
  .ve-free-music-search .ve-button {
    width: 100%;
  }
  .ve-free-music-results {
    grid-template-columns: 1fr;
  }
  .ve-free-music-card {
    min-height: 118px;
    grid-template-columns: 40px minmax(0, 1fr) auto;
    grid-template-rows: auto auto;
  }
  .ve-free-music-card-copy,
  .ve-free-music-preview {
    align-self: start;
  }
  .ve-free-music-preview {
    margin-top: 2px;
  }
  .ve-free-music-license {
    grid-column: 2;
  }
  .ve-free-music-card-actions {
    grid-column: 3;
    grid-row: 1 / 3;
    flex-direction: column;
  }
  .ve-shortcuts-grid {
    grid-template-columns: 1fr;
  }
  .ve-view-toggle span {
    display: none;
  }
}
@media (prefers-reduced-motion: reduce) {
  .ve-open-media-picker,
  .ve-media-card,
  .ve-clip-block,
  .ve-track-action,
  .ve-button,
  .ve-link-button,
  .ve-icon-button,
  .ve-round-button,
  .ve-free-music-preview-ring-value {
    transition: none;
  }
  .ve-media-thumb.is-loading::after,
  .ve-preview-audio-meter,
  .ve-free-music-meter > span,
  .ve-clip-block.is-ai-highlighted,
  .ve-caption-block.is-ai-highlighted,
  .ve-audio-block.is-ai-highlighted {
    animation: none;
  }
  .ve-free-music-meter > span {
    transform: scaleY(.68);
  }
}
`;
