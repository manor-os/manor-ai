import { AiEditPreviewStatus } from "../../lib/editorLiveChat";
import { t } from "../../lib/i18n";
import Button from "./Button";

type AiEditPreviewControlsProps = {
  status: AiEditPreviewStatus;
  changeCount: number;
  onAccept: () => void | Promise<void>;
  onDiscard: () => void | Promise<void>;
  onReview?: () => void;
  accepting?: boolean;
  acceptDisabled?: boolean;
  className?: string;
  reviewLabel?: string;
  workingLabel?: string;
  workingDescription?: string;
  readyDescription?: string;
};

/** Blocks editor mutations while the Accept/Discard transaction is open. */
export function AiEditPreviewInteractionShield({ className = "" }: { className?: string }) {
  return (
    <div
      className={`ai-edit-preview-interaction-shield ${className}`.trim()}
      aria-hidden="true"
    />
  );
}

/** Shared transaction controls for editor-native AI previews. */
export default function AiEditPreviewControls({
  status,
  changeCount,
  onAccept,
  onDiscard,
  onReview,
  accepting = false,
  acceptDisabled = false,
  className = "",
  reviewLabel,
  workingLabel,
  workingDescription,
  readyDescription,
}: AiEditPreviewControlsProps) {
  const isReady = status === AiEditPreviewStatus.Ready;
  const countLabel = t(
    changeCount === 1
      ? "page.doc_editor.ai_edit_pending_change"
      : "page.doc_editor.ai_edit_pending_changes",
    { count: Math.max(1, changeCount) },
  );

  return (
    <section
      className={`ai-edit-preview-controls ${className}`.trim()}
      aria-label={t("page.doc_editor.ai_edit_review_preview")}
      aria-live="polite"
      aria-busy={!isReady || accepting || acceptDisabled}
      data-status={status}
    >
      <div className="ai-edit-preview-controls__copy">
        <span className="ai-edit-preview-controls__indicator" aria-hidden="true" />
        <span>
          <strong>
            {isReady
              ? countLabel
              : workingLabel || t("page.doc_editor.ai_edit_editing_document")}
          </strong>
          <small>
            {isReady
              ? readyDescription || t("page.doc_editor.ai_edit_review_preview")
              : workingDescription || t("page.doc_editor.ai_edit_review_preview")}
          </small>
        </span>
      </div>
      <div className="ai-edit-preview-controls__actions">
        {onReview && (
          <Button
            variant="ghost"
            size="sm"
            disabled={accepting || !isReady}
            onClick={onReview}
          >
            {reviewLabel || t("page.doc_editor.ai_edit_review")}
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          disabled={accepting || !isReady}
          onClick={() => { void onDiscard(); }}
        >
          {t("page.doc_editor.ai_edit_discard")}
        </Button>
        <Button
          size="sm"
          loading={accepting}
          disabled={acceptDisabled || accepting || !isReady}
          onClick={() => { void onAccept(); }}
        >
          {t("page.doc_editor.ai_edit_accept")}
        </Button>
      </div>
    </section>
  );
}
