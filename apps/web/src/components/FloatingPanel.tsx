import { type CSSProperties, type ReactNode } from "react";

/**
 * Shared chrome for bottom-right floating panels (Manor AI chat, Support, …).
 *
 * Owns only the *shell*: fixed bottom-right placement, rounded corners,
 * translucent blur background, soft shadow, and the slide-up + scale
 * open/close transition. Each feature renders its own header/body as
 * `children`. The panel stays mounted and animates on `open` so callers
 * get a smooth exit (and may preserve their own internal state).
 */
export default function FloatingPanel({
  open,
  children,
  zIndex = 1001,
  width = 380,
  height = 560,
  ariaLabel,
  style,
}: {
  open: boolean;
  children: ReactNode;
  /** Stack order. Chat uses 1001; layer others above it if both can show. */
  zIndex?: number;
  /** Shared default size for all floating panels — override only if needed. */
  width?: number;
  height?: number;
  ariaLabel?: string;
  /** Escape hatch for per-panel tweaks; merged last. */
  style?: CSSProperties;
}) {
  return (
    <div
      role="dialog"
      aria-label={ariaLabel}
      aria-hidden={!open}
      style={{
        position: "fixed",
        bottom: 24,
        right: 24,
        zIndex,
        width,
        height,
        maxHeight: "calc(100vh - 48px)",
        maxWidth: "calc(100vw - 48px)",
        borderRadius: 20,
        background: "var(--modal-bg)",
        backdropFilter: "blur(24px)",
        WebkitBackdropFilter: "blur(24px)",
        border: "1px solid var(--modal-border)",
        boxShadow: "var(--modal-shadow)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        transform: open
          ? "translateY(0) scale(1)"
          : "translateY(16px) scale(0.95)",
        opacity: open ? 1 : 0,
        pointerEvents: open ? "auto" : "none",
        transition: "all 0.3s cubic-bezier(0.16,1,0.3,1)",
        fontFamily: '"Inter", system-ui, sans-serif',
        color: "var(--text-default)",
        ...style,
      }}
    >
      {children}
    </div>
  );
}
