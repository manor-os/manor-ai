import type { ReactNode, KeyboardEvent } from "react";

/**
 * CompactCard — a small, dense, clickable card (Manus / Codex style).
 *
 * Shows just the essentials — a leading visual, title, one-line subtitle and
 * trailing meta — and opens the global detail pop-up on click (wire `onClick`
 * to `openDetail(...)`). The **key action** can live on the card itself
 * (`action`); secondary / other actions go in the detail pop-up.
 *
 * The root is a focusable `div[role=button]` (not a `<button>`) so the inner
 * action button isn't an invalid nested button. Borderless frosted glass per
 * the design system.
 */
interface CompactCardProps {
  /** Leading visual — Avatar / IconTile / monogram (≈34px). */
  icon?: ReactNode;
  title: ReactNode;
  /** One-line, truncated. */
  subtitle?: ReactNode;
  /** Trailing meta (status dot, count, etc.) — kept compact. */
  meta?: ReactNode;
  /** Visual tone for connection/readiness metadata. */
  metaTone?: "muted" | "connected";
  /** Key action surfaced on the card itself (e.g. a primary/icon button).
   *  Its clicks are isolated and do NOT open the detail pop-up. */
  action?: ReactNode;
  selected?: boolean;
  /** Opens the detail pop-up (clicking the card body). */
  onClick?: () => void;
  className?: string;
}

export default function CompactCard({
  icon,
  title,
  subtitle,
  meta,
  metaTone = "muted",
  action,
  selected = false,
  onClick,
  className = "",
}: CompactCardProps) {
  const handleKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!onClick) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onClick();
    }
  };

  return (
    <div
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={handleKey}
      className={`compact-card ${selected ? "is-selected" : ""} ${className}`}
    >
      {icon && <div className="compact-card-icon">{icon}</div>}
      <div className="compact-card-body">
        <div className="compact-card-title">{title}</div>
        {subtitle && <div className="compact-card-subtitle">{subtitle}</div>}
      </div>
      <div className={`compact-card-meta compact-card-meta--${metaTone}`}>
        {meta}
        {action && (
          <span
            className="compact-card-action"
            onClick={(e) => e.stopPropagation()}
          >
            {action}
          </span>
        )}
        {onClick && (
          <span className="compact-card-chevron" aria-hidden>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 6l6 6-6 6" />
            </svg>
          </span>
        )}
      </div>
    </div>
  );
}
