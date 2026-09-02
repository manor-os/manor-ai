import {
  type ReactNode,
  type RefObject,
  useEffect,
  useRef,
} from "react";

type FloatingPanelCloseHandler = () => void | Promise<void>;

interface FloatingPanelEscapeEntry {
  token: symbol;
  zIndex: number;
  close: FloatingPanelCloseHandler;
}

const floatingPanelEscapeEntries: FloatingPanelEscapeEntry[] = [];

function topFloatingPanelEscapeEntry(): FloatingPanelEscapeEntry | undefined {
  return floatingPanelEscapeEntries.reduce<FloatingPanelEscapeEntry | undefined>(
    (top, entry) => !top || entry.zIndex >= top.zIndex ? entry : top,
    undefined,
  );
}

function removeFloatingPanelEscapeEntry(token: symbol) {
  const index = floatingPanelEscapeEntries.findIndex((entry) => entry.token === token);
  if (index >= 0) floatingPanelEscapeEntries.splice(index, 1);
}

const FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[contenteditable='true']",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/**
 * Shared chrome and interaction lifecycle for bottom-right floating panels.
 *
 * The panel stays mounted for its exit animation, but a closed panel is inert
 * and therefore cannot leave invisible controls in the keyboard tab order.
 * Escape is coordinated across floating panels so only the highest layer
 * closes, and focus returns to the control that opened the panel.
 */
export default function FloatingPanel({
  id,
  open,
  children,
  zIndex = 1001,
  ariaLabel,
  onClose,
  initialFocusRef,
  restoreFocusRef,
}: {
  id?: string;
  open: boolean;
  children: ReactNode;
  /** Stack order. Chat uses 1001; layer others above it if both can show. */
  zIndex?: number;
  ariaLabel?: string;
  onClose?: FloatingPanelCloseHandler;
  initialFocusRef?: RefObject<HTMLElement | null>;
  restoreFocusRef?: RefObject<HTMLElement | null>;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocusedElementRef = useRef<HTMLElement | null>(null);
  const wasOpenRef = useRef(false);
  const escapeCloseRef = useRef<Promise<void> | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const closeEnabled = Boolean(onClose);

  useEffect(() => {
    const wasOpen = wasOpenRef.current;
    wasOpenRef.current = open;

    if (open && !wasOpen) {
      previouslyFocusedElementRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      const animationFrame = window.requestAnimationFrame(() => {
        const initialFocus = initialFocusRef?.current
          || panelRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR)
          || panelRef.current;
        initialFocus?.focus();
      });
      return () => window.cancelAnimationFrame(animationFrame);
    }

    if (!open && wasOpen) {
      const panel = panelRef.current;
      const activeElement = document.activeElement;
      const focusWasInsidePanel =
        activeElement === document.body
        || (activeElement instanceof Node && Boolean(panel?.contains(activeElement)));
      if (!focusWasInsidePanel) return undefined;

      const restoreTarget = restoreFocusRef?.current
        || previouslyFocusedElementRef.current;
      const animationFrame = window.requestAnimationFrame(() => {
        if (restoreTarget?.isConnected) restoreTarget.focus();
      });
      return () => window.cancelAnimationFrame(animationFrame);
    }

    return undefined;
  }, [initialFocusRef, open, restoreFocusRef]);

  useEffect(() => {
    if (!open || !closeEnabled) return undefined;

    const requestEscapeClose = () => {
      if (escapeCloseRef.current) return escapeCloseRef.current;
      const close = onCloseRef.current;
      if (!close) return Promise.resolve();
      const pending = Promise.resolve()
        .then(close)
        .catch((error) => {
          console.warn("Floating panel close failed", error);
        })
        .finally(() => {
          if (escapeCloseRef.current === pending) escapeCloseRef.current = null;
        });
      escapeCloseRef.current = pending;
      return pending;
    };

    const entry: FloatingPanelEscapeEntry = {
      token: Symbol("floating-panel"),
      zIndex,
      close: requestEscapeClose,
    };
    floatingPanelEscapeEntries.push(entry);

    const handleKeyDown = (event: KeyboardEvent) => {
      if (
        event.key !== "Escape"
        || event.isComposing
        || topFloatingPanelEscapeEntry() !== entry
        || document.body.querySelector("[data-manor-modal-layer-id]")
      ) return;
      event.preventDefault();
      event.stopPropagation();
      void entry.close();
    };

    // Capture lets AI Edit close from its composer, which intentionally stops
    // bubbling keyboard shortcuts so they do not reach the underlying editor.
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown, true);
      removeFloatingPanelEscapeEntry(entry.token);
    };
  }, [closeEnabled, open, zIndex]);

  return (
    <div
      ref={panelRef}
      id={id}
      role="dialog"
      aria-label={ariaLabel}
      aria-hidden={!open}
      {...(!open ? { inert: "" } : {})}
      tabIndex={-1}
      data-open={open ? "true" : "false"}
      className="floating-panel"
      style={{
        position: "fixed",
        zIndex,
        borderRadius: "var(--radius-panel)",
        background: "var(--modal-bg)",
        backdropFilter: "blur(24px)",
        WebkitBackdropFilter: "blur(24px)",
        border: "none",
        boxShadow: "var(--modal-shadow)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        fontFamily: '"Inter", system-ui, sans-serif',
        color: "var(--text-default)",
      }}
    >
      {children}
    </div>
  );
}
