import { VectorStatus } from "../../lib/format";
import { t } from "../../lib/i18n";

type Variant = "generating" | "uploading" | "indexing";

interface CardStatusOverlayProps {
  /** Explicit variant, or pass vectorStatus to auto-detect. */
  variant?: Variant;
  vectorStatus?: string;
  /** Indexing progress from API (0–100). Shows determinate bar when provided. */
  progress?: number;
  /** Indexing step label from API (e.g. "reading", "embedding 3/10"). */
  stepLabel?: string;
}

const VARIANT_LABELS: Record<Variant, string> = {
  generating: t("component.card_status_overlay.generating"),
  uploading: t("component.card_status_overlay.uploading"),
  indexing: t("component.card_status_overlay.indexing"),
};

function resolveVariant(vectorStatus?: string): Variant | null {
  if (!vectorStatus) return null;
  if (vectorStatus === VectorStatus.GENERATING) return "generating";
  // Only show indexing animation for "processing" — "pending" is just queued
  if (vectorStatus === VectorStatus.PROCESSING) return "indexing";
  return null;
}

function stepDescription(step: string | undefined, current: number, total: number): string {
  if (!step) return "";
  switch (step) {
    case "reading": return t("component.card_status_overlay.reading_file");
    case "chunking": return t("component.card_status_overlay.chunking_text");
    case "embedding":
      return total > 1
        ? t("component.card_status_overlay.embedding_progress")
            .replace("{current}", String(current))
            .replace("{total}", String(total))
        : t("component.card_status_overlay.embedding");
    case "storing": return t("component.card_status_overlay.storing");
    default: return "";
  }
}

/**
 * Non-blocking status indicator for cards in a loading state.
 * Shows a bottom progress bar + inline status chip — document info stays visible.
 *
 * Usage:
 *   <div className={`my-card ${cardStatusClass(doc.vector_status)}`}>
 *     <CardStatusOverlay vectorStatus={doc.vector_status}
 *       progress={doc.indexing_progress?.progress}
 *       stepLabel={doc.indexing_progress?.step} />
 *     ...card content...
 *   </div>
 */
export default function CardStatusOverlay({
  variant,
  vectorStatus,
  progress,
  stepLabel,
}: CardStatusOverlayProps) {
  const v = variant ?? resolveVariant(vectorStatus);
  if (!v) return null;

  const hasDeterminate = v === "indexing" && typeof progress === "number" && progress > 0;
  const pct = hasDeterminate ? Math.min(Math.max(progress!, 0), 100) : 0;

  return (
    <>
      {/* Bottom progress bar */}
      <div
        className={`card-progress-bar${hasDeterminate ? " determinate" : ""}`}
        style={hasDeterminate ? { "--progress": pct } as React.CSSProperties : undefined}
      />
      {/* Inline status chip — replaces the StatusBadge for in-progress items */}
      <div className="card-status-chip">
        <div
          className="status-spinner"
          style={{ width: 12, height: 12, borderWidth: 2 }}
        />
        {hasDeterminate ? (
          <span>{pct}%</span>
        ) : (
          <span>{stepLabel || VARIANT_LABELS[v]}</span>
        )}
      </div>
    </>
  );
}

/**
 * Extracts display info from the API's indexing_progress field.
 */
export function parseIndexingProgress(
  indexing?: { step?: string; progress?: number; total_chunks?: number; current_chunk?: number } | null,
): { progress?: number; stepLabel?: string } {
  if (!indexing) return {};
  return {
    progress: indexing.progress,
    stepLabel: stepDescription(
      indexing.step,
      indexing.current_chunk ?? 0,
      indexing.total_chunks ?? 0,
    ),
  };
}

/**
 * Returns the CSS class to add to the card container based on vector_status.
 * Returns "" when no animation class is needed.
 */
export function cardStatusClass(vectorStatus?: string): string {
  if (!vectorStatus) return "";
  if (vectorStatus === VectorStatus.GENERATING) return "card-generating";
  if (vectorStatus === VectorStatus.PROCESSING) return "card-indexing";
  return "";
}
