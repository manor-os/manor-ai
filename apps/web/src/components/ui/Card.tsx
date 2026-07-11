/**
 * Card — clean, composable surface for structured items (integrations,
 * agents, accounts, anything that needs an icon + title + body + action
 * footer).
 *
 * Defining traits:
 *  - Calm visual: single neutral border, subtle hover (no translateY)
 *  - Optional brand-tinted IconTile in the header with a status dot
 *  - Pinned footer via flex column + minHeight, so cards in a grid line
 *    up regardless of body length
 *  - No internal overflow clipping — keeps row dropdowns un-clipped
 */
import IconTile from "./IconTile";

/* Shared card sizing — keep every card grid (agents, skills, apps,
 * integrations, team) on one rhythm so they line up across the product. */
export const CARD_MIN_HEIGHT = 248;
export const CARD_ICON_SIZE = 40;

interface CardProps {
  // ── Header pieces (all optional; if none are set the header is skipped)
  /** Visual to render in the header's leading slot. When ``brandColor``
   *  is also set, the node is wrapped in an ``IconTile`` (brand-tinted
   *  square + status dot). When ``brandColor`` is omitted the node is
   *  rendered as-is — use this to drop in an ``<Avatar>`` or any other
   *  circular / non-square visual. */
  icon?: React.ReactNode;
  /** Brand colour for the tile + soft tint. */
  brandColor?: string;
  /** Status dot — attached to the IconTile when ``brandColor`` is set,
   *  otherwise ignored (the caller can render their own status chip in
   *  ``badges`` instead). */
  status?: { color: string; label?: string };
  title?: React.ReactNode;
  /** Sub-line under the title, line-clamped to 2 lines. */
  description?: React.ReactNode;
  /** Inline chips/tags rendered under the description. */
  badges?: React.ReactNode;
  /** Trailing slot in the header row (e.g. menu button). */
  headerAction?: React.ReactNode;

  // ── Body / footer
  children?: React.ReactNode;
  /** Footer slot — gets a top divider; flex row by default. */
  footer?: React.ReactNode;

  // ── Layout
  /** Minimum card height so a grid of cards lines up. Default 240. */
  minHeight?: number | string;
  /** Base border colour, useful for warning/error cards that still use the same shell. */
  borderColor?: string;
  /** Show subtle hover affordance (border darken + tiny shadow). Default true. */
  hoverable?: boolean;
  onClick?: () => void;
  className?: string;
  style?: React.CSSProperties;
}

export default function Card({
  icon, brandColor, status, title, description, badges, headerAction,
  children, footer, minHeight = CARD_MIN_HEIGHT, borderColor = "var(--glass-border)",
  hoverable = true, onClick, className = "", style,
}: CardProps) {
  const hasHeader = !!(icon || title || description || badges || headerAction);

  const restShadow = "var(--shadow-sm)";
  const customBg = style?.background as string | undefined;
  const restBg = customBg ?? "var(--glass-card)";
  const hoverClass = hoverable ? "card-hover-surface" : "";

  return (
    <div
      className={[hoverClass, className].filter(Boolean).join(" ")}
      onClick={onClick}
      style={{
        position: "relative" as const,
        backdropFilter: "var(--glass-blur-sm)",
        WebkitBackdropFilter: "var(--glass-blur-sm)",
        border: `1px solid ${borderColor}`,
        borderRadius: 14,
        boxShadow: restShadow,
        display: "flex",
        flexDirection: "column",
        minHeight,
        cursor: onClick ? "pointer" : undefined,
        transition:
          "background 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease",
        ...style,
        background: restBg,
      }}
    >
      <div style={{
        padding: hasHeader || children ? "16px 16px 12px" : 0,
        flex: 1, display: "flex", flexDirection: "column",
      }}>
        {hasHeader && (
          <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
            {icon && (
              brandColor ? (
                <IconTile color={brandColor} status={status}>
                  {icon}
                </IconTile>
              ) : (
                <div style={{ flexShrink: 0 }}>{icon}</div>
              )
            )}
            <div style={{ flex: 1, minWidth: 0 }}>
              {title && (
                <div style={{
                  fontSize: 14, fontWeight: 700, color: "#1c1917",
                  overflow: "hidden",
                  display: "-webkit-box",
                  WebkitLineClamp: 2,
                  WebkitBoxOrient: "vertical" as const,
                  lineHeight: 1.35,
                  marginBottom: description ? 2 : 0,
                }}>
                  {title}
                </div>
              )}
              {description && (
                <p style={{
                  fontSize: 12, color: "#78716c", margin: 0, lineHeight: 1.45,
                  display: "-webkit-box",
                  WebkitLineClamp: 2, WebkitBoxOrient: "vertical" as const,
                  overflow: "hidden",
                }}>
                  {description}
                </p>
              )}
              {badges && (
                <div style={{
                  display: "flex", flexWrap: "wrap" as const, gap: 6, marginTop: 8,
                }}>
                  {badges}
                </div>
              )}
            </div>
            {headerAction && (
              <div style={{ flexShrink: 0 }}>{headerAction}</div>
            )}
          </div>
        )}

        {children}
      </div>

      {footer && (
        <div style={{
          display: "flex", alignItems: "center", gap: 6,
          padding: "4px 14px 14px",
          flexShrink: 0,
        }}>
          {footer}
        </div>
      )}
    </div>
  );
}
