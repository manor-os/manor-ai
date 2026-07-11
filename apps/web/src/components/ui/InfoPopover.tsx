/**
 * Generic "?" info popover.
 *
 * Renders a small ``?`` icon button that, on click, reveals a panel
 * anchored beneath it. Designed for the trailing slot of cards
 * (Card.headerAction) where it sits top-right and unobtrusive at
 * rest, glowing brand-colored on hover, opening on click.
 *
 * Behaviour:
 *  - Click toggles open/closed
 *  - Click outside closes
 *  - Esc closes
 *  - Hover shows a soft brand-colored glow ring (no popover yet)
 *  - Keyboard-tabbable, Enter/Space opens
 *
 * Positioning:
 *  - The panel is rendered into ``document.body`` via portal and
 *    positioned with viewport-fixed coordinates anchored to the icon.
 *    This avoids being clipped by ancestors with ``overflow: hidden``
 *    or trapped under sibling cards in the integrations grid (the
 *    bug that prompted this rewrite — a card-local ``position:
 *    absolute`` panel was painted behind the next row of cards even
 *    with z-index because parent stacking contexts swallowed it).
 *  - On scroll/resize we recompute so the panel tracks its anchor
 *    instead of detaching.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { IconHelp } from "../icons";

interface InfoPopoverProps {
  /** Content rendered inside the popover panel. */
  children: React.ReactNode;
  /** Brand color for the glow ring + accent border. */
  brandColor?: string;
  /** Override the default panel width (320px). */
  width?: number;
  /** Override the default icon size (16px). */
  size?: number;
  /** ARIA label for screen readers. */
  ariaLabel?: string;
  /** Render position of the panel relative to the icon. */
  align?: "right" | "left";
}

export default function InfoPopover({
  children,
  brandColor = "#436b65",
  width = 320,
  size = 16,
  ariaLabel = "More information",
  align = "right",
}: InfoPopoverProps) {
  const [open, setOpen] = useState(false);
  const [hover, setHover] = useState(false);
  const wrapRef = useRef<HTMLSpanElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [panelPos, setPanelPos] = useState<{ top: number; left: number; maxHeight: number } | null>(null);

  // Place the panel relative to the icon, using viewport-fixed coords
  // since we portal into <body>. Width is fixed; we anchor by ``align``
  // (right → panel's right edge = icon's right edge; left → panel's
  // left = icon's left). Clamp into the viewport so an icon near the
  // right edge of a narrow window doesn't push the panel offscreen.
  const recompute = useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const rect = wrap.getBoundingClientRect();
    const viewportGutter = 8;
    const preferredGap = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left =
      align === "right" ? rect.right - width : rect.left;
    // Clamp horizontally with a viewport gutter.
    if (left + width > vw - viewportGutter) left = vw - viewportGutter - width;
    if (left < viewportGutter) left = viewportGutter;

    const preferredMaxHeight = Math.min(480, Math.max(180, vh * 0.6));
    const measuredHeight = panelRef.current?.getBoundingClientRect().height || preferredMaxHeight;
    const below = vh - rect.bottom - preferredGap - viewportGutter;
    const above = rect.top - preferredGap - viewportGutter;
    const openAbove = below < Math.min(measuredHeight, preferredMaxHeight) && above > below;
    const maxHeight = Math.max(140, Math.min(preferredMaxHeight, openAbove ? above : below));
    const unclampedTop = openAbove ? rect.top - preferredGap - Math.min(measuredHeight, maxHeight) : rect.bottom + preferredGap;
    const top = Math.max(viewportGutter, Math.min(unclampedTop, vh - viewportGutter - maxHeight));
    setPanelPos({ top, left, maxHeight });
  }, [align, width]);

  useLayoutEffect(() => {
    if (!open) return;
    recompute();
  }, [open, recompute]);

  useEffect(() => {
    if (!open) return;
    const onScrollOrResize = () => recompute();
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [open, recompute]);

  // Close on outside click and Esc. The panel lives in a portal, so
  // it isn't a DOM descendant of the wrapper — check both the wrapper
  // and the panel before dismissing.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as Node;
      if (wrapRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const ringActive = open || hover;

  return (
    <span
      ref={wrapRef}
      style={{ position: "relative", display: "inline-flex", alignItems: "center" }}
    >
      <button
        type="button"
        aria-label={ariaLabel}
        aria-expanded={open}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        style={{
          all: "unset",
          width: size + 6,
          height: size + 6,
          borderRadius: "50%",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          color: "var(--text-faint)",
          background: open ? `${brandColor}14` : "transparent",
          cursor: "pointer",
          boxShadow: ringActive
            ? `0 0 0 3px ${brandColor}1a, 0 0 10px 1px ${brandColor}55`
            : "none",
          transition:
            "box-shadow 180ms ease, color 120ms ease, background 120ms ease",
        }}
      >
        <IconHelp size={size} />
      </button>

      {open && panelPos && typeof document !== "undefined" &&
        createPortal(
          <div
            ref={panelRef}
            role="dialog"
            aria-label={ariaLabel}
            onClick={(e) => e.stopPropagation()}
            style={{
              position: "fixed",
              top: panelPos.top,
              left: panelPos.left,
              zIndex: 1000,
              width,
              maxHeight: panelPos.maxHeight,
              overflowY: "auto",
              background: "var(--surface-panel)",
              backdropFilter: "blur(12px)",
              border: "1px solid var(--border-default)",
              borderRadius: 10,
              boxShadow: "var(--shadow-lg)",
              padding: 14,
              fontSize: 12.5,
              lineHeight: 1.55,
              color: "var(--text-default)",
              textAlign: "left",
            }}
          >
            {children}
          </div>,
          document.body,
        )}
    </span>
  );
}
