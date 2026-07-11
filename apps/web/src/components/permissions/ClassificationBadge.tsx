/**
 * ClassificationBadge — P0 global element.
 *
 * Shown on every list row, file detail, RAG citation, share dialog. Defines
 * what visual constraints the resource carries (watermark, download, share).
 *
 * See docs/PERMISSIONS_UX_DESIGN_ZH.md §1.1.
 */
import type { Classification } from "../../lib/types";
import { t } from "../../lib/i18n";

type Size = "sm" | "md";

// Style + i18n-key map. Labels and tooltips are resolved at render-time
// so locale changes take effect without a remount.
const CLASSIFICATION_STYLES: Record<
  Classification,
  { bg: string; fg: string; labelKey: string; tooltipKey: string; pulse?: boolean }
> = {
  public: {
    bg: "#f5f5f4",
    fg: "#57534e",
    labelKey: "permissions.classification.public.label",
    tooltipKey: "permissions.classification.public.tooltip",
  },
  internal: {
    bg: "#f3f6fa",
    fg: "#3f57a0",
    labelKey: "permissions.classification.internal.label",
    tooltipKey: "permissions.classification.internal.tooltip",
  },
  confidential: {
    bg: "#f9f4ec",
    fg: "#9a5630",
    labelKey: "permissions.classification.confidential.label",
    tooltipKey: "permissions.classification.confidential.tooltip",
  },
  restricted: {
    bg: "#f8f0ef",
    fg: "#a23e38",
    labelKey: "permissions.classification.restricted.label",
    tooltipKey: "permissions.classification.restricted.tooltip",
    pulse: true,
  },
};

interface Props {
  level?: Classification | string | null;
  size?: Size;
  showTooltip?: boolean;
}

export default function ClassificationBadge({
  level,
  size = "md",
  showTooltip = true,
}: Props) {
  if (!level) return null;
  const cfg =
    CLASSIFICATION_STYLES[level as Classification] ??
    CLASSIFICATION_STYLES.internal;
  const padding = size === "sm" ? "2px 8px" : "3px 10px";
  const fontSize = size === "sm" ? 10 : 11;

  return (
    <span
      title={showTooltip ? t(cfg.tooltipKey) : undefined}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding,
        fontSize,
        fontWeight: 600,
        background: cfg.bg,
        color: cfg.fg,
        borderRadius: 6,
        whiteSpace: "nowrap",
        cursor: showTooltip ? "help" : "default",
      }}
    >
      {cfg.pulse && (
        <span
          aria-hidden
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: cfg.fg,
            animation: "manor-classification-pulse 1.6s ease-in-out infinite",
          }}
        />
      )}
      {t(cfg.labelKey)}
    </span>
  );
}

// Inject the keyframes once. Idempotent — safe across HMR.
if (typeof document !== "undefined") {
  const STYLE_ID = "manor-classification-badge-style";
  if (!document.getElementById(STYLE_ID)) {
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
@keyframes manor-classification-pulse {
  0%, 100% { opacity: 1; }
  50%      { opacity: 0.35; }
}`;
    document.head.appendChild(style);
  }
}
