import { useEffect, useId, useRef, useState } from "react";
import { t } from "../lib/i18n";
import Button from "./ui/Button";
import Modal from "./ui/Modal";
import ThemeAwareImage from "./ui/ThemeAwareImage";
import {
  IconChevronLeft,
  IconChevronRight,
  IconClose,
  IconDownload,
  IconSparkles,
} from "./icons";
import type { ChatBoxMode } from "./ChatModeSelector";

export type ChatModeTemplatePreviewContent = {
  label?: string;
  title?: string;
  lines?: string[];
  chips?: string[];
  imageSrc?: string;
  imageAlt?: string;
  previewImageSrc?: string;
  previewImageDarkSrc?: string;
  previewImageAlt?: string;
  detailImageSrcs?: string[];
  detailImageAlt?: string;
  sampleSrc?: string;
  sampleLabel?: string;
  videoSrc?: string;
};

export type ChatModeTemplateSample = {
  title: string;
  outcome: string;
  prompt: string;
  previewContent?: ChatModeTemplatePreviewContent;
};

function previewSrc(sample: ChatModeTemplateSample) {
  const content = sample.previewContent;
  return (
    content?.previewImageSrc ||
    content?.imageSrc ||
    content?.detailImageSrcs?.[0]
  );
}

function previewKey(sample: ChatModeTemplateSample) {
  return `${sample.title}:${previewSrc(sample) || ""}`;
}

function ChatModeTemplateQuickPreview({
  sample,
  disabled,
  onClose,
  onUse,
}: {
  sample: ChatModeTemplateSample | null;
  disabled: boolean;
  onClose: () => void;
  onUse: (sample: ChatModeTemplateSample) => void;
}) {
  const [activePage, setActivePage] = useState(0);
  const content = sample?.previewContent;
  const singlePreview = sample ? previewSrc(sample) : undefined;
  const pages = content?.detailImageSrcs?.length
    ? content.detailImageSrcs
    : singlePreview
      ? [singlePreview]
      : [];
  const activeImage = pages[activePage];
  const activeDarkImage =
    activePage === 0 && pages.length === 1
      ? content?.previewImageDarkSrc
      : undefined;

  useEffect(() => {
    setActivePage(0);
  }, [sample]);

  return (
    <Modal
      open={Boolean(sample)}
      onClose={onClose}
      title={
        sample
          ? `${t("component.embedded_chat.quick_preview")} · ${sample.title}`
          : t("component.embedded_chat.quick_preview")
      }
      className="workspace-sample-quick-preview-modal"
      bodyClassName="workspace-sample-quick-preview-body"
      maxWidth="980px"
      footer={
        sample ? (
          <div className="workspace-sample-quick-preview-footer">
            {content?.sampleSrc && (
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  window.open(content.sampleSrc, "_blank", "noopener,noreferrer")
                }
              >
                <IconDownload size={14} />
                {t("component.embedded_chat.open_artifact")}
              </Button>
            )}
            <Button
              size="sm"
              variant="primary"
              disabled={disabled}
              onClick={() => onUse(sample)}
            >
              <IconSparkles size={14} />
              {t("component.embedded_chat.remix")}
            </Button>
          </div>
        ) : null
      }
    >
      {sample && (
        <div className="workspace-sample-quick-preview workspace-blueprint-quick-preview">
          <figure>
            {content?.videoSrc ? (
              <video
                src={content.videoSrc}
                poster={activeImage}
                controls
                playsInline
                preload="metadata"
                aria-label={content.previewImageAlt || sample.title}
              />
            ) : activeImage ? (
              <ThemeAwareImage
                src={activeImage}
                darkSrc={activeDarkImage}
                alt={content?.detailImageAlt || content?.previewImageAlt || sample.title}
              />
            ) : null}
          </figure>
          <aside className="workspace-blueprint-preview-details">
            <span className="workspace-blueprint-preview-eyebrow">
              {content?.sampleLabel || t("component.embedded_chat.quick_preview")}
            </span>
            <section className="workspace-blueprint-preview-section">
              <h3>{sample.title}</h3>
              <p className="workspace-blueprint-preview-summary">{sample.outcome}</p>
            </section>
            {content?.lines && content.lines.length > 0 && (
              <section className="workspace-blueprint-preview-section workspace-blueprint-preview-includes">
                <h3>{t("component.embedded_chat.workspace_contents")}</h3>
                <ul>
                  {content.lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              </section>
            )}
            {pages.length > 1 && (
              <div
                className="workspace-sample-quick-preview-pages"
                aria-label={t("component.embedded_chat.preview_pages")}
              >
                <button
                  type="button"
                  disabled={activePage === 0}
                  aria-label={t("component.embedded_chat.previous_preview_page")}
                  onClick={() => setActivePage((page) => Math.max(0, page - 1))}
                >
                  <IconChevronLeft size={14} />
                </button>
                <span>
                  {activePage + 1} / {pages.length}
                </span>
                <button
                  type="button"
                  disabled={activePage === pages.length - 1}
                  aria-label={t("component.embedded_chat.next_preview_page")}
                  onClick={() =>
                    setActivePage((page) => Math.min(pages.length - 1, page + 1))
                  }
                >
                  <IconChevronRight size={14} />
                </button>
              </div>
            )}
          </aside>
        </div>
      )}
    </Modal>
  );
}

export default function ChatModeTemplateGallery({
  mode,
  disabled = false,
  samples,
  onSelect,
}: {
  mode: ChatBoxMode;
  disabled?: boolean;
  samples: ChatModeTemplateSample[];
  onSelect: (sample: ChatModeTemplateSample) => void | Promise<void>;
}) {
  const [failedPreviewKeys, setFailedPreviewKeys] = useState<Set<string>>(
    () => new Set(),
  );
  const templates = samples.filter((sample) => {
    return (
      Boolean(previewSrc(sample)) && !failedPreviewKeys.has(previewKey(sample))
    );
  });
  const [dismissedMode, setDismissedMode] = useState<ChatBoxMode | null>(null);
  const [previewSample, setPreviewSample] = useState<ChatModeTemplateSample | null>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const headingId = useId();

  useEffect(() => {
    setDismissedMode(null);
    setPreviewSample(null);
    setFailedPreviewKeys(new Set());
    railRef.current?.scrollTo({ left: 0 });
  }, [mode]);

  if (templates.length === 0 || dismissedMode === mode) return null;

  return (
    <>
      <section
        className="chat-mode-template-gallery"
        aria-labelledby={headingId}
        data-mode={mode}
      >
        <div className="chat-mode-template-gallery-header">
          <h2 id={headingId}>{t("page.flows.templates")}</h2>
          <div className="chat-mode-template-gallery-actions">
            <button
              type="button"
              className="chat-mode-template-gallery-icon-button"
              aria-label={t("action.close")}
              title={t("action.close")}
              onClick={() => setDismissedMode(mode)}
            >
              <IconClose size={15} />
            </button>
          </div>
        </div>

        <div ref={railRef} className="chat-mode-template-gallery-rail">
          {templates.map((item, index) => {
            const imageSrc = previewSrc(item);
            const imageKey = previewKey(item);
            return (
              <button
                key={`${item.title}:${index}`}
                type="button"
                className="chat-mode-template-card"
                aria-label={`${t("component.embedded_chat.quick_preview")}: ${item.title}`}
                title={item.title}
                onClick={() => setPreviewSample(item)}
              >
                <span className="chat-mode-template-card-preview">
                  <img
                    src={imageSrc}
                    alt=""
                    loading="lazy"
                    draggable={false}
                    onError={() => {
                      setFailedPreviewKeys((current) => {
                        const next = new Set(current);
                        next.add(imageKey);
                        return next;
                      });
                    }}
                  />
                </span>
                <span className="chat-mode-template-card-title">{item.title}</span>
              </button>
            );
          })}
        </div>
      </section>
      <ChatModeTemplateQuickPreview
        sample={previewSample}
        disabled={disabled}
        onClose={() => setPreviewSample(null)}
        onUse={(sample) => {
          void onSelect(sample);
          setPreviewSample(null);
        }}
      />
    </>
  );
}
