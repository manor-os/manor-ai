import {
  IconAudioWave,
  IconClock,
  IconDocument,
  IconPalette,
  IconPlay,
  IconSparkles,
  IconTag,
} from "./icons";
import {
  getChatBoxModeConfig,
  type ChatBoxMode,
} from "./ChatModeSelector";
import { t } from "../lib/i18n";

export type ChatModePayload = {
  task?: string;
  aspect_ratio?: string;
  clip_duration_seconds?: number;
  audio_policy?: string;
  generate_audio?: boolean;
  caption_policy?: string;
  reference_policy?: string;
  reference_slots?: Array<{
    role: string;
    label: string;
    required?: boolean;
  }>;
  reference_role_hints?: Record<string, string>;
  resolution?: string;
  text_policy?: string;
  model?: string;
  output_type?: string;
  purpose?: string;
  format?: string;
  depth?: string;
  source_policy?: string;
  render?: string;
};

type Option = {
  value: string;
  label: string;
};

const VIDEO_CLIP_DURATION_OPTIONS = [4, 5, 8, 10, 15] as const;
export const VIDEO_RESOLUTION_OPTIONS = [
  { value: "720p", label: "720p" },
  { value: "1080p", label: "1080p" },
] as const;
export const VIDEO_ASPECT_RATIO_OPTIONS = [
  { value: "adaptive", labelKey: "component.chat_mode.brief_auto" },
  { value: "21:9", label: "21:9" },
  { value: "16:9", label: "16:9" },
  { value: "4:3", label: "4:3" },
  { value: "3:4", label: "3:4" },
  { value: "1:1", label: "1:1" },
  { value: "9:16", label: "9:16" },
] as const;
const FIRST_LAST_FRAME_REFERENCE_SLOTS = [
  { role: "first_frame", label: "First frame / 首帧", required: true },
  { role: "last_frame", label: "Last frame / 尾帧", required: true },
];

function coerceVideoDuration(value?: string | number) {
  const raw = Number(value || 4);
  if (
    VIDEO_CLIP_DURATION_OPTIONS.includes(
      raw as (typeof VIDEO_CLIP_DURATION_OPTIONS)[number],
    )
  ) {
    return raw;
  }
  return VIDEO_CLIP_DURATION_OPTIONS.reduce((closest, candidate) =>
    Math.abs(candidate - raw) < Math.abs(closest - raw) ? candidate : closest,
  );
}

function coerceVideoAspectRatio(value?: string) {
  const normalized = String(value || "").trim().toLowerCase();
  if (normalized === "auto") return "adaptive";
  const match = VIDEO_ASPECT_RATIO_OPTIONS.find(
    (option) => option.value.toLowerCase() === normalized,
  );
  return match?.value || "16:9";
}

function coerceVideoResolution(value?: string) {
  const normalized = String(value || "").trim().toLowerCase();
  const match = VIDEO_RESOLUTION_OPTIONS.find(
    (option) => option.value.toLowerCase() === normalized,
  );
  return match?.value || "720p";
}

const MODE_DEFAULT_PAYLOADS: Partial<Record<ChatBoxMode, ChatModePayload>> = {
  image: {
    task: "generate",
    aspect_ratio: "auto",
    resolution: "2k",
    reference_policy: "smart_references",
    text_policy: "avoid_text",
    model: "image_5_lite",
  },
  video: {
    output_type: "single_clip",
    aspect_ratio: "16:9",
    resolution: "720p",
    clip_duration_seconds: 4,
    audio_policy: "native_if_supported",
    generate_audio: true,
    caption_policy: "editable_or_burned",
    reference_policy: "hash_references",
    model: "seedance_official",
  },
  audio: {
    purpose: "dialogue_or_narration",
    clip_duration_seconds: 15,
    model: "openrouter_tts",
  },
  document: {
    task: "draft",
    format: "structured_doc",
    source_policy: "use_references",
  },
  slides: {
    format: "presentation_deck",
    depth: "full_deck",
    render: "editable",
    source_policy: "use_references",
  },
  sheet: {
    task: "create",
    format: "workbook",
    output_type: "formulas_and_charts",
  },
  website: {
    task: "build_or_edit",
    format: "app_prototype",
    depth: "responsive",
    source_policy: "use_references",
  },
  research: {
    depth: "source_backed",
    format: "brief",
    source_policy: "web_and_references",
  },
};

export function getDefaultChatModePayload(mode: ChatBoxMode): ChatModePayload {
  return { ...(MODE_DEFAULT_PAYLOADS[mode] || {}) };
}

export function getChatModeInputPlaceholder(
  mode: ChatBoxMode,
  payload?: ChatModePayload,
): string {
  if (mode === "video") {
    const referencePolicy =
      payload?.reference_policy ||
      MODE_DEFAULT_PAYLOADS.video?.reference_policy ||
      "hash_references";
    if (referencePolicy === "first_last_frames") {
      return t("component.chat_mode.video_placeholder_first_last_frames");
    }
    if (referencePolicy === "smart_multiframe") {
      return t("component.chat_mode.video_placeholder_smart_multiframe");
    }
    return t("component.chat_mode.video_placeholder_all_refs");
  }
  return getChatBoxModeConfig(mode).placeholder;
}

function nextPayload(mode: ChatBoxMode, value?: ChatModePayload): ChatModePayload {
  return {
    ...getDefaultChatModePayload(mode),
    ...(value || {}),
  };
}

function coerceVideoPayload(payload: ChatModePayload): ChatModePayload {
  const audioPolicy = String(payload.audio_policy || "").trim().toLowerCase();
  const generateAudio =
    payload.generate_audio !== undefined
      ? Boolean(payload.generate_audio)
      : audioPolicy
        ? audioPolicy !== "silent_visual"
        : true;
  const next = {
    ...payload,
    clip_duration_seconds: coerceVideoDuration(payload.clip_duration_seconds),
    aspect_ratio: coerceVideoAspectRatio(payload.aspect_ratio),
    resolution: coerceVideoResolution(payload.resolution),
    generate_audio: generateAudio,
    audio_policy: generateAudio ? "native_if_supported" : "silent_visual",
  };
  if (next.reference_policy === "first_last_frames") {
    return {
      ...next,
      reference_slots: FIRST_LAST_FRAME_REFERENCE_SLOTS,
      reference_role_hints: {
        first_frame: "#first_frame / #首帧",
        last_frame: "#last_frame / #尾帧",
      },
    };
  }
  delete next.reference_slots;
  delete next.reference_role_hints;
  return next;
}

export function normalizeChatModePayload(
  mode: ChatBoxMode,
  payload: ChatModePayload,
): ChatModePayload {
  return mode === "video" ? coerceVideoPayload(payload) : payload;
}

function SegmentGroup({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value?: string;
  options: Option[];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <label className="chat-mode-brief-group">
      <span>{label}</span>
      <div className="chat-mode-brief-segments">
        {options.map((option) => (
          <button
            key={option.value}
            type="button"
            disabled={disabled}
            className={
              option.value === value
                ? "chat-mode-brief-chip chat-mode-brief-chip--active"
                : "chat-mode-brief-chip"
            }
            onClick={() => onChange(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>
    </label>
  );
}

function DurationGroup({
  value,
  onChange,
  disabled,
}: {
  value?: number;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  const current = coerceVideoDuration(value);
  return (
    <label className="chat-mode-brief-group">
      <span>
        <IconClock size={12} />
        {t("component.chat_mode.brief_clip_duration")}
      </span>
      <div className="chat-mode-brief-segments">
        {VIDEO_CLIP_DURATION_OPTIONS.map((seconds) => (
          <button
            key={seconds}
            type="button"
            disabled={disabled}
            className={
              seconds === current
                ? "chat-mode-brief-chip chat-mode-brief-chip--active"
                : "chat-mode-brief-chip"
            }
            onClick={() => onChange(seconds)}
          >
            {seconds}s
          </button>
        ))}
      </div>
    </label>
  );
}

export default function ChatModeBriefPanel({
  mode,
  value,
  onChange,
  disabled = false,
}: {
  mode: ChatBoxMode;
  value?: ChatModePayload;
  onChange: (payload: ChatModePayload) => void;
  disabled?: boolean;
}) {
  if (mode === "auto") return null;

  const payload =
    mode === "video"
      ? coerceVideoPayload(nextPayload(mode, value))
      : nextPayload(mode, value);

  const update = (patch: ChatModePayload) => {
    const merged = { ...payload, ...patch };
    onChange(mode === "video" ? coerceVideoPayload(merged) : merged);
  };

  if (mode === "video") {
    return (
      <div className="chat-mode-brief chat-mode-brief--video">
        <div className="chat-mode-brief-header">
          <IconPlay size={15} />
          <span>{t("component.chat_mode.brief_video_title")}</span>
        </div>
        <SegmentGroup
          label={t("component.chat_mode.brief_output")}
          value={payload.output_type}
          disabled={disabled}
          onChange={(output_type) => update({ output_type })}
          options={[
            { value: "single_clip", label: t("component.chat_mode.brief_single_clip") },
            { value: "multi_clip_final", label: t("component.chat_mode.brief_final_video") },
            { value: "edit_existing", label: t("component.chat_mode.brief_edit_existing") },
          ]}
        />
        <DurationGroup
          value={payload.clip_duration_seconds}
          disabled={disabled}
          onChange={(clip_duration_seconds) => update({ clip_duration_seconds })}
        />
        <SegmentGroup
          label={t("component.chat_mode.brief_aspect")}
          value={payload.aspect_ratio}
          disabled={disabled}
          onChange={(aspect_ratio) => update({ aspect_ratio })}
          options={VIDEO_ASPECT_RATIO_OPTIONS.map((option) => ({
            value: option.value,
            label: "labelKey" in option ? t(option.labelKey) : option.label,
          }))}
        />
        <SegmentGroup
          label={t("component.chat_mode.brief_audio")}
          value={payload.audio_policy}
          disabled={disabled}
          onChange={(audio_policy) =>
            update({
              audio_policy,
              generate_audio: audio_policy !== "silent_visual",
            })
          }
          options={[
            { value: "native_if_supported", label: t("component.chat_mode.brief_audio_native") },
            { value: "silent_visual", label: t("component.chat_mode.brief_audio_silent") },
          ]}
        />
        <div className="chat-mode-brief-hint">
          <IconTag size={13} />
          <span>{t("component.chat_mode.brief_hash_reference_hint")}</span>
        </div>
        <div className="chat-mode-brief-hint chat-mode-brief-hint--warn">
          {t("component.chat_mode.brief_video_duration_limit")}
        </div>
      </div>
    );
  }

  if (mode === "image") {
    return (
      <div className="chat-mode-brief">
        <div className="chat-mode-brief-header">
          <IconPalette size={15} />
          <span>{t("component.chat_mode.brief_image_title")}</span>
        </div>
        <SegmentGroup
          label={t("component.chat_mode.brief_task")}
          value={payload.task}
          disabled={disabled}
          onChange={(task) => update({ task })}
          options={[
            { value: "generate", label: t("component.chat_mode.brief_generate") },
            { value: "edit", label: t("component.chat_mode.brief_edit") },
            { value: "variant", label: t("component.chat_mode.brief_variant") },
          ]}
        />
        <SegmentGroup
          label={t("component.chat_mode.brief_aspect")}
          value={payload.aspect_ratio}
          disabled={disabled}
          onChange={(aspect_ratio) => update({ aspect_ratio })}
          options={[
            { value: "auto", label: t("component.chat_mode.brief_auto") },
            { value: "21:9", label: "21:9" },
            { value: "16:9", label: "16:9" },
            { value: "3:2", label: "3:2" },
            { value: "4:3", label: "4:3" },
            { value: "1:1", label: "1:1" },
            { value: "3:4", label: "3:4" },
            { value: "2:3", label: "2:3" },
            { value: "9:16", label: "9:16" },
          ]}
        />
        <SegmentGroup
          label={t("component.chat_mode.toolbar_resolution")}
          value={payload.resolution}
          disabled={disabled}
          onChange={(resolution) => update({ resolution })}
          options={[
            { value: "2k", label: t("component.chat_mode.toolbar_resolution_2k") },
            { value: "4k", label: t("component.chat_mode.toolbar_resolution_4k") },
          ]}
        />
        <SegmentGroup
          label={t("component.chat_mode.toolbar_text")}
          value={payload.text_policy}
          disabled={disabled}
          onChange={(text_policy) => update({ text_policy })}
          options={[
            { value: "avoid_text", label: t("component.chat_mode.toolbar_text_avoid_short") },
            { value: "text_if_requested", label: t("component.chat_mode.toolbar_text_if_requested_short") },
            { value: "typography", label: t("component.chat_mode.toolbar_text_typography_short") },
          ]}
        />
        <div className="chat-mode-brief-hint">
          <IconTag size={13} />
          <span>{t("component.chat_mode.brief_hash_reference_hint")}</span>
        </div>
      </div>
    );
  }

  if (mode === "audio") {
    return (
      <div className="chat-mode-brief">
        <div className="chat-mode-brief-header">
          <IconAudioWave size={15} />
          <span>{t("component.chat_mode.brief_audio_title")}</span>
        </div>
        <SegmentGroup
          label={t("component.chat_mode.brief_purpose")}
          value={payload.purpose}
          disabled={disabled}
          onChange={(purpose) => update({ purpose })}
          options={[
            { value: "dialogue_or_narration", label: t("component.chat_mode.brief_dialogue") },
            { value: "ambience", label: t("component.chat_mode.brief_ambience") },
            { value: "music", label: t("component.chat_mode.brief_music") },
            { value: "sfx", label: t("component.chat_mode.brief_sfx") },
          ]}
        />
        <DurationGroup
          value={payload.clip_duration_seconds}
          disabled={disabled}
          onChange={(clip_duration_seconds) => update({ clip_duration_seconds })}
        />
      </div>
    );
  }

  return (
    <div className="chat-mode-brief chat-mode-brief--compact">
      <div className="chat-mode-brief-header">
        {mode === "document" || mode === "slides" || mode === "sheet" ? (
          <IconDocument size={15} />
        ) : (
          <IconSparkles size={15} />
        )}
        <span>{t("component.chat_mode.brief_source_hint")}</span>
      </div>
      <div className="chat-mode-brief-hint">
        <IconTag size={13} />
        <span>{t("component.chat_mode.brief_hash_reference_hint")}</span>
      </div>
    </div>
  );
}
