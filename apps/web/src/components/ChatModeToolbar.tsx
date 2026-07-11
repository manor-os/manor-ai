import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import {
  IconAudioWave,
  IconBolt,
  IconAspectRatio,
  IconChevronDown,
  IconChecklist,
  IconCode,
  IconDocument,
  IconClock,
  IconEdit,
  IconExcelGrid,
  IconGlobe,
  IconGrid4,
  IconImage,
  IconLayers,
  IconMicrophone,
  IconMusicNote,
  IconPalette,
  IconPaperclip,
  IconReport,
  IconResolution,
  IconRefresh,
  IconSearch,
  IconSparkles,
  IconText,
  IconWorkspace,
} from "./icons";
import ChatModeSelector, {
  type ChatBoxMode,
} from "./ChatModeSelector";
import {
  getDefaultChatModePayload,
  normalizeChatModePayload,
  VIDEO_ASPECT_RATIO_OPTIONS,
  VIDEO_RESOLUTION_OPTIONS,
  type ChatModePayload,
} from "./ChatModeBriefPanel";
import { t } from "../lib/i18n";

const VIDEO_DURATIONS = [4, 5, 8, 10, 15] as const;
const AUDIO_DURATIONS = [5, 15, 30, 60] as const;

type ToolbarOption = {
  value: string;
  label: string;
  buttonLabel?: string;
  icon?: ReactNode;
};

function withDefaults(mode: ChatBoxMode, value?: ChatModePayload) {
  return {
    ...getDefaultChatModePayload(mode),
    ...(value || {}),
  };
}

function coerceVideoDuration(value?: string | number) {
  const raw = Number(value || VIDEO_DURATIONS[0]);
  if (VIDEO_DURATIONS.includes(raw as (typeof VIDEO_DURATIONS)[number])) {
    return raw;
  }
  return VIDEO_DURATIONS.reduce((closest, candidate) =>
    Math.abs(candidate - raw) < Math.abs(closest - raw) ? candidate : closest,
  );
}

function updatePayload(
  mode: ChatBoxMode,
  current: ChatModePayload | undefined,
  onChange: (payload: ChatModePayload) => void,
  patch: ChatModePayload,
) {
  const next = {
    ...withDefaults(mode, current),
    ...patch,
  };
  if (mode === "video") {
    onChange(normalizeChatModePayload(mode, next));
    return;
  }
  onChange(next);
}

function FirstLastFrameSlots() {
  return (
    <div
      className="chat-mode-frame-slots"
      aria-label={t("component.chat_mode.frame_slots")}
      title={t("component.chat_mode.frame_slots_hint")}
    >
      <span
        className="chat-mode-frame-slot"
        title={t("component.chat_mode.first_frame_hint")}
      >
        <IconImage size={12} />
        <span>{t("component.chat_mode.first_frame_short")}</span>
      </span>
      <span className="chat-mode-frame-arrow" aria-hidden="true">
        →
      </span>
      <span
        className="chat-mode-frame-slot"
        title={t("component.chat_mode.last_frame_hint")}
      >
        <IconImage size={12} />
        <span>{t("component.chat_mode.last_frame_short")}</span>
      </span>
    </div>
  );
}

function ToolbarDropdownPill({
  icon,
  controlLabel,
  value,
  options,
  disabled,
  onChange,
}: {
  icon?: ReactNode;
  controlLabel: string;
  value?: string | number;
  options: ToolbarOption[];
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [menuCoords, setMenuCoords] = useState<{
    top: number;
    left: number;
    width: number;
    maxHeight: number;
    placement: "above" | "below";
  } | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const selected =
    options.find((option) => option.value === String(value)) || options[0];
  const selectedIcon = selected?.icon || icon;
  const selectedLabel = selected?.buttonLabel ?? selected?.label;

  const updateMenuCoords = useCallback(() => {
    if (typeof window === "undefined" || !buttonRef.current) return;
    const rect = buttonRef.current.getBoundingClientRect();
    const width = Math.min(240, Math.max(168, rect.width));
    const left = Math.min(
      Math.max(12, rect.left),
      Math.max(12, window.innerWidth - width - 12),
    );
    const spaceAbove = Math.max(0, rect.top - 10);
    const spaceBelow = Math.max(0, window.innerHeight - rect.bottom - 10);
    const placement =
      spaceAbove >= 180 || spaceAbove >= spaceBelow ? "above" : "below";
    const maxHeight = Math.min(
      320,
      Math.max(130, placement === "above" ? spaceAbove : spaceBelow),
    );
    setMenuCoords({
      top: placement === "above" ? rect.top - 6 : rect.bottom + 6,
      left,
      width,
      maxHeight,
      placement,
    });
  }, []);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !buttonRef.current?.contains(target) &&
        !menuRef.current?.contains(target)
      ) {
        setOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    updateMenuCoords();
    window.addEventListener("resize", updateMenuCoords);
    window.addEventListener("scroll", updateMenuCoords, true);
    return () => {
      window.removeEventListener("resize", updateMenuCoords);
      window.removeEventListener("scroll", updateMenuCoords, true);
    };
  }, [open, updateMenuCoords]);

  const menuStyle: CSSProperties | undefined = menuCoords
    ? {
        position: "fixed",
        top: menuCoords.top,
        left: menuCoords.left,
        width: menuCoords.width,
        maxHeight: menuCoords.maxHeight,
        transform:
          menuCoords.placement === "above" ? "translateY(-100%)" : undefined,
      }
    : undefined;

  const menu =
    open && menuCoords
      ? createPortal(
          <div
            ref={menuRef}
            className="chat-mode-toolbar-menu chat-mode-toolbar-menu--portal"
            role="listbox"
            aria-label={controlLabel}
            style={menuStyle}
          >
            {options.map((option) => {
              const active = option.value === selected?.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={`chat-mode-toolbar-menu-item ${
                    active ? "chat-mode-toolbar-menu-item--active" : ""
                  }`}
                  onClick={() => {
                    onChange(option.value);
                    setOpen(false);
                  }}
                >
                  <span className="chat-mode-toolbar-menu-icon">
                    {option.icon || icon}
                  </span>
                  <span className="chat-mode-toolbar-menu-label">
                    {option.label}
                  </span>
                </button>
              );
            })}
          </div>,
          document.body,
        )
      : null;

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        disabled={disabled || options.length < 1}
        className="chat-mode-toolbar-dropdown"
        onClick={() => setOpen((value) => !value)}
        title={`${controlLabel}: ${selected?.label || ""}`}
        aria-label={`${controlLabel}: ${selected?.label || ""}`}
        aria-expanded={open}
        aria-haspopup="listbox"
      >
        {selectedIcon ? (
          <span className="chat-mode-toolbar-button-icon">{selectedIcon}</span>
        ) : null}
        {selectedLabel ? (
          <span className="chat-mode-toolbar-button-label">
            {selectedLabel}
          </span>
        ) : null}
        <IconChevronDown size={12} />
      </button>
      {menu}
    </>
  );
}

export default function ChatModeToolbar({
  mode,
  payload,
  onModeChange,
  onPayloadChange,
  disabled = false,
}: {
  mode: ChatBoxMode;
  payload?: ChatModePayload;
  onModeChange: (mode: ChatBoxMode) => void;
  onPayloadChange: (payload: ChatModePayload) => void;
  disabled?: boolean;
}) {
  const current = withDefaults(mode, payload);
  if (mode === "video") {
    current.clip_duration_seconds = coerceVideoDuration(
      current.clip_duration_seconds,
    );
  }
  const patch = (next: ChatModePayload) =>
    updatePayload(mode, current, onPayloadChange, next);
  const referenceOptions = [
    {
      value: "hash_references",
      label: t("component.chat_mode.toolbar_all_refs"),
      buttonLabel: t("component.chat_mode.toolbar_all_refs_short"),
      icon: <IconPaperclip size={13} />,
    },
    {
      value: "smart_references",
      label: t("component.chat_mode.toolbar_smart_refs"),
      buttonLabel: t("component.chat_mode.toolbar_smart_refs_short"),
      icon: <IconPaperclip size={13} />,
    },
  ];
  const videoReferenceOptions = [
    {
      value: "hash_references",
      label: t("component.chat_mode.toolbar_all_refs"),
      buttonLabel: t("component.chat_mode.toolbar_all_refs_short"),
      icon: <IconPaperclip size={13} />,
    },
    {
      value: "first_last_frames",
      label: t("component.chat_mode.toolbar_first_last_frames"),
      buttonLabel: t("component.chat_mode.toolbar_first_last_frames_short"),
      icon: <IconLayers size={13} />,
    },
    {
      value: "smart_multiframe",
      label: t("component.chat_mode.toolbar_smart_multiframe"),
      buttonLabel: t("component.chat_mode.toolbar_smart_multiframe_short"),
      icon: <IconGrid4 size={13} />,
    },
  ];
  const videoDurationOptions = VIDEO_DURATIONS.map((duration) => ({
    value: String(duration),
    label: `${duration}s`,
    icon: <IconClock size={13} />,
  }));
  const audioDurationOptions = AUDIO_DURATIONS.map((duration) => ({
    value: String(duration),
    label: `${duration}s`,
    icon: <IconClock size={13} />,
  }));
  const videoAspectOptions = VIDEO_ASPECT_RATIO_OPTIONS.map((option) => ({
    value: option.value,
    label: "labelKey" in option ? t(option.labelKey) : option.label,
    icon: <IconAspectRatio size={13} />,
  }));
  const videoResolutionOptions = VIDEO_RESOLUTION_OPTIONS.map((option) => ({
    value: option.value,
    label:
      option.value === "1080p"
        ? t("component.chat_mode.toolbar_video_resolution_1080p")
        : t("component.chat_mode.toolbar_video_resolution_720p"),
    buttonLabel: option.label,
    icon: <IconResolution size={13} />,
  }));
  const videoAudioOptions = [
    {
      value: "true",
      label: t("component.chat_mode.brief_audio_native"),
      buttonLabel: t("component.chat_mode.brief_audio_native_short"),
      icon: <IconAudioWave size={13} />,
    },
    {
      value: "false",
      label: t("component.chat_mode.brief_audio_silent"),
      buttonLabel: t("component.chat_mode.brief_audio_silent_short"),
      icon: <IconAudioWave size={13} />,
    },
  ];
  const imageAspectOptions = [
    {
      value: "auto",
      label: t("component.chat_mode.brief_auto"),
      icon: <IconAspectRatio size={13} />,
    },
    { value: "21:9", label: "21:9", icon: <IconAspectRatio size={13} /> },
    { value: "16:9", label: "16:9", icon: <IconAspectRatio size={13} /> },
    { value: "3:2", label: "3:2", icon: <IconAspectRatio size={13} /> },
    { value: "4:3", label: "4:3", icon: <IconAspectRatio size={13} /> },
    { value: "1:1", label: "1:1", icon: <IconAspectRatio size={13} /> },
    { value: "3:4", label: "3:4", icon: <IconAspectRatio size={13} /> },
    { value: "2:3", label: "2:3", icon: <IconAspectRatio size={13} /> },
    { value: "9:16", label: "9:16", icon: <IconAspectRatio size={13} /> },
  ];
  const imageResolutionOptions = [
    {
      value: "2k",
      label: t("component.chat_mode.toolbar_resolution_2k"),
      buttonLabel: "2K",
      icon: <IconResolution size={13} />,
    },
    {
      value: "4k",
      label: t("component.chat_mode.toolbar_resolution_4k"),
      buttonLabel: "4K",
      icon: <IconResolution size={13} />,
    },
  ];
  const imageTextOptions = [
    {
      value: "avoid_text",
      label: t("component.chat_mode.toolbar_text_avoid"),
      buttonLabel: t("component.chat_mode.toolbar_text_avoid_short"),
      icon: <IconText size={13} />,
    },
    {
      value: "text_if_requested",
      label: t("component.chat_mode.toolbar_text_if_requested"),
      buttonLabel: t("component.chat_mode.toolbar_text_if_requested_short"),
      icon: <IconText size={13} />,
    },
    {
      value: "typography",
      label: t("component.chat_mode.toolbar_text_typography"),
      buttonLabel: t("component.chat_mode.toolbar_text_typography_short"),
      icon: <IconText size={13} />,
    },
  ];
  const imageTaskOptions = [
    {
      value: "generate",
      label: t("component.chat_mode.brief_generate"),
      icon: <IconImage size={13} />,
    },
    {
      value: "edit",
      label: t("component.chat_mode.brief_edit"),
      icon: <IconEdit size={13} />,
    },
    {
      value: "variant",
      label: t("component.chat_mode.brief_variant"),
      icon: <IconRefresh size={13} />,
    },
  ];
  const audioPurposeOptions = [
    {
      value: "dialogue_or_narration",
      label: t("component.chat_mode.brief_dialogue"),
      icon: <IconMicrophone size={13} />,
    },
    {
      value: "ambience",
      label: t("component.chat_mode.brief_ambience"),
      icon: <IconAudioWave size={13} />,
    },
    {
      value: "music",
      label: t("component.chat_mode.brief_music"),
      icon: <IconMusicNote size={13} />,
    },
    { value: "sfx", label: t("component.chat_mode.brief_sfx"), icon: <IconBolt size={13} /> },
  ];
  const sourceOptions = [
    {
      value: "use_references",
      label: t("component.chat_mode.toolbar_sources_refs"),
      buttonLabel: t("component.chat_mode.toolbar_sources_refs_short"),
      icon: <IconPaperclip size={13} />,
    },
    {
      value: "prompt_only",
      label: t("component.chat_mode.toolbar_sources_prompt"),
      buttonLabel: t("component.chat_mode.toolbar_sources_prompt_short"),
      icon: <IconText size={13} />,
    },
  ];
  const documentTaskOptions = [
    {
      value: "draft",
      label: t("component.chat_mode.toolbar_doc_task_draft"),
      buttonLabel: t("component.chat_mode.toolbar_doc_task_draft_short"),
      icon: <IconDocument size={13} />,
    },
    {
      value: "edit",
      label: t("component.chat_mode.toolbar_doc_task_edit"),
      buttonLabel: t("component.chat_mode.toolbar_doc_task_edit_short"),
      icon: <IconEdit size={13} />,
    },
    {
      value: "summarize",
      label: t("component.chat_mode.toolbar_doc_task_summary"),
      buttonLabel: t("component.chat_mode.toolbar_doc_task_summary_short"),
      icon: <IconReport size={13} />,
    },
  ];
  const documentFormatOptions = [
    {
      value: "structured_doc",
      label: t("component.chat_mode.toolbar_doc_format_structured"),
      buttonLabel: t("component.chat_mode.toolbar_doc_format_structured_short"),
      icon: <IconChecklist size={13} />,
    },
    {
      value: "report",
      label: t("component.chat_mode.toolbar_doc_format_report"),
      buttonLabel: t("component.chat_mode.toolbar_doc_format_report_short"),
      icon: <IconReport size={13} />,
    },
    {
      value: "memo",
      label: t("component.chat_mode.toolbar_doc_format_memo"),
      buttonLabel: t("component.chat_mode.toolbar_doc_format_memo_short"),
      icon: <IconText size={13} />,
    },
  ];
  const slidesFormatOptions = [
    {
      value: "presentation_deck",
      label: t("component.chat_mode.toolbar_slides_format_deck"),
      buttonLabel: t("component.chat_mode.toolbar_slides_format_deck_short"),
      icon: <IconLayers size={13} />,
    },
    {
      value: "pitch_deck",
      label: t("component.chat_mode.toolbar_slides_format_pitch"),
      buttonLabel: t("component.chat_mode.toolbar_slides_format_pitch_short"),
      icon: <IconSparkles size={13} />,
    },
    {
      value: "lesson_deck",
      label: t("component.chat_mode.toolbar_slides_format_lesson"),
      buttonLabel: t("component.chat_mode.toolbar_slides_format_lesson_short"),
      icon: <IconDocument size={13} />,
    },
  ];
  const slidesDepthOptions = [
    {
      value: "outline",
      label: t("component.chat_mode.toolbar_slides_depth_outline"),
      buttonLabel: t("component.chat_mode.toolbar_slides_depth_outline_short"),
      icon: <IconChecklist size={13} />,
    },
    {
      value: "full_deck",
      label: t("component.chat_mode.toolbar_slides_depth_full"),
      buttonLabel: t("component.chat_mode.toolbar_slides_depth_full_short"),
      icon: <IconLayers size={13} />,
    },
    {
      value: "speaker_notes",
      label: t("component.chat_mode.toolbar_slides_depth_notes"),
      buttonLabel: t("component.chat_mode.toolbar_slides_depth_notes_short"),
      icon: <IconText size={13} />,
    },
  ];
  const slidesRenderOptions = [
    {
      value: "editable",
      label: t("component.chat_mode.toolbar_slides_render_editable"),
      buttonLabel: t("component.chat_mode.toolbar_slides_render_editable_short"),
      icon: <IconEdit size={13} />,
    },
    {
      value: "full_page_image",
      label: t("component.chat_mode.toolbar_slides_render_image"),
      buttonLabel: t("component.chat_mode.toolbar_slides_render_image_short"),
      icon: <IconSparkles size={13} />,
    },
  ];
  const sheetFormatOptions = [
    {
      value: "workbook",
      label: t("component.chat_mode.toolbar_sheet_format_workbook"),
      buttonLabel: t("component.chat_mode.toolbar_sheet_format_workbook_short"),
      icon: <IconExcelGrid size={13} />,
    },
    {
      value: "tracker",
      label: t("component.chat_mode.toolbar_sheet_format_tracker"),
      buttonLabel: t("component.chat_mode.toolbar_sheet_format_tracker_short"),
      icon: <IconChecklist size={13} />,
    },
    {
      value: "dashboard",
      label: t("component.chat_mode.toolbar_sheet_format_dashboard"),
      buttonLabel: t("component.chat_mode.toolbar_sheet_format_dashboard_short"),
      icon: <IconGrid4 size={13} />,
    },
  ];
  const sheetOutputOptions = [
    {
      value: "formulas_and_charts",
      label: t("component.chat_mode.toolbar_sheet_output_formulas"),
      buttonLabel: t("component.chat_mode.toolbar_sheet_output_formulas_short"),
      icon: <IconSparkles size={13} />,
    },
    {
      value: "clean_table",
      label: t("component.chat_mode.toolbar_sheet_output_table"),
      buttonLabel: t("component.chat_mode.toolbar_sheet_output_table_short"),
      icon: <IconExcelGrid size={13} />,
    },
    {
      value: "charts",
      label: t("component.chat_mode.toolbar_sheet_output_charts"),
      buttonLabel: t("component.chat_mode.toolbar_sheet_output_charts_short"),
      icon: <IconGrid4 size={13} />,
    },
  ];
  const websiteTaskOptions = [
    {
      value: "build_or_edit",
      label: t("component.chat_mode.toolbar_website_task_build"),
      buttonLabel: t("component.chat_mode.toolbar_website_task_build_short"),
      icon: <IconCode size={13} />,
    },
    {
      value: "landing_page",
      label: t("component.chat_mode.toolbar_website_task_landing"),
      buttonLabel: t("component.chat_mode.toolbar_website_task_landing_short"),
      icon: <IconGlobe size={13} />,
    },
    {
      value: "app_prototype",
      label: t("component.chat_mode.toolbar_website_task_app"),
      buttonLabel: t("component.chat_mode.toolbar_website_task_app_short"),
      icon: <IconWorkspace size={13} />,
    },
  ];
  const websiteDepthOptions = [
    {
      value: "responsive",
      label: t("component.chat_mode.toolbar_website_depth_responsive"),
      buttonLabel: t("component.chat_mode.toolbar_website_depth_responsive_short"),
      icon: <IconAspectRatio size={13} />,
    },
    {
      value: "polished_ui",
      label: t("component.chat_mode.toolbar_website_depth_polished"),
      buttonLabel: t("component.chat_mode.toolbar_website_depth_polished_short"),
      icon: <IconPalette size={13} />,
    },
    {
      value: "interactive",
      label: t("component.chat_mode.toolbar_website_depth_interactive"),
      buttonLabel: t("component.chat_mode.toolbar_website_depth_interactive_short"),
      icon: <IconBolt size={13} />,
    },
  ];
  const researchDepthOptions = [
    {
      value: "quick",
      label: t("component.chat_mode.toolbar_research_depth_quick"),
      buttonLabel: t("component.chat_mode.toolbar_research_depth_quick_short"),
      icon: <IconBolt size={13} />,
    },
    {
      value: "source_backed",
      label: t("component.chat_mode.toolbar_research_depth_sources"),
      buttonLabel: t("component.chat_mode.toolbar_research_depth_sources_short"),
      icon: <IconSearch size={13} />,
    },
    {
      value: "deep",
      label: t("component.chat_mode.toolbar_research_depth_deep"),
      buttonLabel: t("component.chat_mode.toolbar_research_depth_deep_short"),
      icon: <IconReport size={13} />,
    },
  ];
  const researchFormatOptions = [
    {
      value: "brief",
      label: t("component.chat_mode.toolbar_research_format_brief"),
      buttonLabel: t("component.chat_mode.toolbar_research_format_brief_short"),
      icon: <IconDocument size={13} />,
    },
    {
      value: "comparison",
      label: t("component.chat_mode.toolbar_research_format_compare"),
      buttonLabel: t("component.chat_mode.toolbar_research_format_compare_short"),
      icon: <IconGrid4 size={13} />,
    },
    {
      value: "report",
      label: t("component.chat_mode.toolbar_research_format_report"),
      buttonLabel: t("component.chat_mode.toolbar_research_format_report_short"),
      icon: <IconReport size={13} />,
    },
  ];

  return (
    <div className={`chat-mode-toolbar chat-mode-toolbar--${mode}`}>
      <ChatModeSelector
        value={mode}
        onChange={onModeChange}
        disabled={disabled}
      />

      {mode === "video" ? (
        <>
          <ToolbarDropdownPill
            icon={<IconPaperclip size={12} />}
            controlLabel={t("component.chat_mode.brief_references")}
            value={current.reference_policy}
            options={videoReferenceOptions}
            disabled={disabled}
            onChange={(reference_policy) => patch({ reference_policy })}
          />
          {current.reference_policy === "first_last_frames" ? (
            <FirstLastFrameSlots />
          ) : null}
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.brief_aspect")}
            value={current.aspect_ratio}
            options={videoAspectOptions}
            disabled={disabled}
            onChange={(aspect_ratio) => patch({ aspect_ratio })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_resolution")}
            value={current.resolution}
            options={videoResolutionOptions}
            disabled={disabled}
            onChange={(resolution) => patch({ resolution })}
          />
          <ToolbarDropdownPill
            icon={<IconAudioWave size={12} />}
            controlLabel={t("component.chat_mode.brief_audio")}
            value={current.generate_audio === false ? "false" : "true"}
            options={videoAudioOptions}
            disabled={disabled}
            onChange={(generate_audio) => {
              const enabled = generate_audio === "true";
              patch({
                generate_audio: enabled,
                audio_policy: enabled ? "native_if_supported" : "silent_visual",
              });
            }}
          />
          <ToolbarDropdownPill
            icon={<IconClock size={12} />}
            controlLabel={t("component.chat_mode.brief_clip_duration")}
            value={current.clip_duration_seconds || VIDEO_DURATIONS[0]}
            options={videoDurationOptions}
            disabled={disabled}
            onChange={(clip_duration_seconds) =>
              patch({ clip_duration_seconds: Number(clip_duration_seconds) })
            }
          />
        </>
      ) : null}

      {mode === "image" ? (
        <>
          <ToolbarDropdownPill
            icon={<IconImage size={12} />}
            controlLabel={t("component.chat_mode.brief_task")}
            value={current.task}
            options={imageTaskOptions}
            disabled={disabled}
            onChange={(task) => patch({ task })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.brief_aspect")}
            value={current.aspect_ratio}
            options={imageAspectOptions}
            disabled={disabled}
            onChange={(aspect_ratio) => patch({ aspect_ratio })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_resolution")}
            value={current.resolution}
            options={imageResolutionOptions}
            disabled={disabled}
            onChange={(resolution) => patch({ resolution })}
          />
          <ToolbarDropdownPill
            icon={<IconPaperclip size={12} />}
            controlLabel={t("component.chat_mode.brief_references")}
            value={current.reference_policy}
            options={referenceOptions}
            disabled={disabled}
            onChange={(reference_policy) => patch({ reference_policy })}
          />
          <ToolbarDropdownPill
            icon={<IconText size={12} />}
            controlLabel={t("component.chat_mode.toolbar_text")}
            value={current.text_policy}
            options={imageTextOptions}
            disabled={disabled}
            onChange={(text_policy) => patch({ text_policy })}
          />
        </>
      ) : null}

      {mode === "audio" ? (
        <>
          <ToolbarDropdownPill
            icon={<IconAudioWave size={12} />}
            controlLabel={t("component.chat_mode.brief_purpose")}
            value={current.purpose}
            options={audioPurposeOptions}
            disabled={disabled}
            onChange={(purpose) => patch({ purpose })}
          />
          <ToolbarDropdownPill
            icon={<IconClock size={12} />}
            controlLabel={t("component.chat_mode.brief_clip_duration")}
            value={current.clip_duration_seconds || 15}
            options={audioDurationOptions}
            disabled={disabled}
            onChange={(clip_duration_seconds) =>
              patch({ clip_duration_seconds: Number(clip_duration_seconds) })
            }
          />
        </>
      ) : null}

      {mode === "document" ? (
        <>
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.brief_task")}
            value={current.task}
            options={documentTaskOptions}
            disabled={disabled}
            onChange={(task) => patch({ task })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_format")}
            value={current.format}
            options={documentFormatOptions}
            disabled={disabled}
            onChange={(format) => patch({ format })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_sources")}
            value={current.source_policy}
            options={sourceOptions}
            disabled={disabled}
            onChange={(source_policy) => patch({ source_policy })}
          />
        </>
      ) : null}

      {mode === "slides" ? (
        <>
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_format")}
            value={current.format}
            options={slidesFormatOptions}
            disabled={disabled}
            onChange={(format) => patch({ format })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_depth")}
            value={current.depth}
            options={slidesDepthOptions}
            disabled={disabled}
            onChange={(depth) => patch({ depth })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_slides_render")}
            value={current.render || "editable"}
            options={slidesRenderOptions}
            disabled={disabled}
            onChange={(render) => patch({ render })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_sources")}
            value={current.source_policy}
            options={sourceOptions}
            disabled={disabled}
            onChange={(source_policy) => patch({ source_policy })}
          />
        </>
      ) : null}

      {mode === "sheet" ? (
        <>
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_format")}
            value={current.format}
            options={sheetFormatOptions}
            disabled={disabled}
            onChange={(format) => patch({ format })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_output")}
            value={current.output_type}
            options={sheetOutputOptions}
            disabled={disabled}
            onChange={(output_type) => patch({ output_type })}
          />
        </>
      ) : null}

      {mode === "website" ? (
        <>
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.brief_task")}
            value={current.task}
            options={websiteTaskOptions}
            disabled={disabled}
            onChange={(task) => patch({ task })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_depth")}
            value={current.depth}
            options={websiteDepthOptions}
            disabled={disabled}
            onChange={(depth) => patch({ depth })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_sources")}
            value={current.source_policy}
            options={sourceOptions}
            disabled={disabled}
            onChange={(source_policy) => patch({ source_policy })}
          />
        </>
      ) : null}

      {mode === "research" ? (
        <>
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_depth")}
            value={current.depth}
            options={researchDepthOptions}
            disabled={disabled}
            onChange={(depth) => patch({ depth })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_format")}
            value={current.format}
            options={researchFormatOptions}
            disabled={disabled}
            onChange={(format) => patch({ format })}
          />
          <ToolbarDropdownPill
            controlLabel={t("component.chat_mode.toolbar_sources")}
            value={current.source_policy}
            options={[
              {
                value: "web_and_references",
                label: t("component.chat_mode.toolbar_sources_web_refs"),
                buttonLabel: t("component.chat_mode.toolbar_sources_web_refs_short"),
                icon: <IconSearch size={13} />,
              },
              ...sourceOptions,
            ]}
            disabled={disabled}
            onChange={(source_policy) => patch({ source_policy })}
          />
        </>
      ) : null}
    </div>
  );
}
