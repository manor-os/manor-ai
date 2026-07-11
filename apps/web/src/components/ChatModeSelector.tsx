import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import {
  IconChevronDown,
  IconAudioWave,
  IconDocument,
  IconGrid4,
  IconImage,
  IconLayers,
  IconPlay,
  IconReport,
  IconSparkles,
  IconWorkspace,
  type IconProps,
} from "./icons";
import { t } from "../lib/i18n";

export type ChatBoxMode =
  | "auto"
  | "image"
  | "video"
  | "audio"
  | "document"
  | "slides"
  | "sheet"
  | "website"
  | "research";

export type ChatBoxModeConfig = {
  key: ChatBoxMode;
  icon: (props: IconProps) => JSX.Element;
  label: string;
  helper: string;
  placeholder: string;
};

export const CHAT_BOX_MODES: ChatBoxModeConfig[] = [
  {
    key: "auto",
    icon: IconSparkles,
    label: t("component.chat_mode.auto"),
    helper: t("component.chat_mode.auto_helper"),
    placeholder: t("component.chat_mode.auto_placeholder"),
  },
  {
    key: "image",
    icon: IconImage,
    label: t("component.chat_mode.image"),
    helper: t("component.chat_mode.image_helper"),
    placeholder: t("component.chat_mode.image_placeholder"),
  },
  {
    key: "video",
    icon: IconPlay,
    label: t("component.chat_mode.video"),
    helper: t("component.chat_mode.video_helper"),
    placeholder: t("component.chat_mode.video_placeholder"),
  },
  {
    key: "audio",
    icon: IconAudioWave,
    label: t("component.chat_mode.audio"),
    helper: t("component.chat_mode.audio_helper"),
    placeholder: t("component.chat_mode.audio_placeholder"),
  },
  {
    key: "document",
    icon: IconDocument,
    label: t("component.chat_mode.document"),
    helper: t("component.chat_mode.document_helper"),
    placeholder: t("component.chat_mode.document_placeholder"),
  },
  {
    key: "slides",
    icon: IconLayers,
    label: t("component.chat_mode.slides"),
    helper: t("component.chat_mode.slides_helper"),
    placeholder: t("component.chat_mode.slides_placeholder"),
  },
  {
    key: "sheet",
    icon: IconGrid4,
    label: t("component.chat_mode.sheet"),
    helper: t("component.chat_mode.sheet_helper"),
    placeholder: t("component.chat_mode.sheet_placeholder"),
  },
  {
    key: "website",
    icon: IconWorkspace,
    label: t("component.chat_mode.website"),
    helper: t("component.chat_mode.website_helper"),
    placeholder: t("component.chat_mode.website_placeholder"),
  },
  {
    key: "research",
    icon: IconReport,
    label: t("component.chat_mode.research"),
    helper: t("component.chat_mode.research_helper"),
    placeholder: t("component.chat_mode.research_placeholder"),
  },
];

export function getChatBoxModeConfig(mode: ChatBoxMode): ChatBoxModeConfig {
  return CHAT_BOX_MODES.find((item) => item.key === mode) || CHAT_BOX_MODES[0];
}

export function chatModeFromCapability(capability: string): ChatBoxMode {
  if (capability === "docs") return "document";
  if (capability === "sheets") return "sheet";
  if (capability === "slides") return "slides";
  if (
    capability === "image" ||
    capability === "video" ||
    capability === "website" ||
    capability === "research"
  ) {
    return capability;
  }
  return "auto";
}

export default function ChatModeSelector({
  value,
  onChange,
  disabled = false,
}: {
  value: ChatBoxMode;
  onChange: (mode: ChatBoxMode) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [menuCoords, setMenuCoords] = useState<{
    top: number;
    left: number;
    width: number;
    maxHeight: number;
    placement: "above" | "below";
  } | null>(null);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const selected = getChatBoxModeConfig(value);
  const SelectedIcon = selected.icon;

  const updateMenuCoords = useCallback(() => {
    if (typeof window === "undefined" || !buttonRef.current) return;
    const rect = buttonRef.current.getBoundingClientRect();
    const width = Math.min(286, Math.max(220, window.innerWidth - 24));
    const desiredLeft = rect.left;
    const left = Math.min(
      Math.max(12, desiredLeft),
      Math.max(12, window.innerWidth - width - 12),
    );
    const spaceAbove = Math.max(0, rect.top - 12);
    const spaceBelow = Math.max(0, window.innerHeight - rect.bottom - 12);
    const placement =
      spaceAbove >= 240 || spaceAbove >= spaceBelow ? "above" : "below";
    const maxHeight = Math.min(
      438,
      Math.max(180, placement === "above" ? spaceAbove : spaceBelow),
    );
    setMenuCoords({
      top: placement === "above" ? rect.top - 8 : rect.bottom + 8,
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
        !rootRef.current?.contains(target) &&
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

  const title = useMemo(
    () => `${t("component.chat_mode.mode")}: ${selected.label}`,
    [selected.label],
  );

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
            className="chat-mode-menu chat-mode-menu--portal"
            role="listbox"
            aria-label={t("component.chat_mode.choose_mode")}
            style={menuStyle}
          >
            {CHAT_BOX_MODES.map((mode) => {
              const ModeIcon = mode.icon;
              const active = mode.key === value;
              return (
                <button
                  key={mode.key}
                  className={`chat-mode-option ${active ? "chat-mode-option--active" : ""}`}
                  type="button"
                  role="option"
                  aria-selected={active}
                  onClick={() => {
                    onChange(mode.key);
                    setOpen(false);
                  }}
                >
                  <span className="chat-mode-option-icon">
                    <ModeIcon size={15} />
                  </span>
                  <span className="chat-mode-option-main">
                    <strong>{mode.label}</strong>
                    <small>{mode.helper}</small>
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
      <div className="chat-mode-selector" ref={rootRef}>
        <button
          ref={buttonRef}
          className={`chat-mode-trigger chat-mode-trigger--${value}`}
          type="button"
          disabled={disabled}
          title={title}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-label={title}
          onClick={() => {
            updateMenuCoords();
            setOpen((next) => !next);
          }}
        >
          <SelectedIcon size={14} />
          <span>{selected.label}</span>
          <IconChevronDown size={12} />
        </button>
      </div>
      {menu}
    </>
  );
}
