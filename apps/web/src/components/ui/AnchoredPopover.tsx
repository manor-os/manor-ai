import {
  cloneElement,
  isValidElement,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

const POPOVER_FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

function visibleFocusableElements(root: ParentNode): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(POPOVER_FOCUSABLE_SELECTOR)).filter(
    (element) => {
      if (
        element.hidden
        || element.getAttribute("aria-disabled") === "true"
        || element.closest('[aria-hidden="true"], [hidden], [inert]')
      ) {
        return false;
      }
      const style = window.getComputedStyle(element);
      return style.display !== "none"
        && style.visibility !== "hidden"
        && element.getClientRects().length > 0;
    },
  );
}

function focusAfterTrigger(anchor: HTMLElement, panel: HTMLElement): HTMLElement | null {
  const trigger = anchor.querySelector<HTMLElement>(POPOVER_FOCUSABLE_SELECTOR);
  if (!trigger) return null;
  const pageControls = visibleFocusableElements(document).filter(
    (element) => !panel.contains(element),
  );
  const triggerIndex = pageControls.indexOf(trigger);
  return triggerIndex >= 0 ? pageControls[triggerIndex + 1] ?? null : null;
}

interface AnchoredPopoverRenderContext {
  close: () => void;
}

interface AnchoredPopoverProps {
  trigger: ReactElement;
  children: ReactNode | ((context: AnchoredPopoverRenderContext) => ReactNode);
  ariaLabel: string;
  open?: boolean;
  align?: "left" | "right";
  width?: number;
  panelClassName?: string;
  openOnHover?: boolean;
  onOpenChange?: (open: boolean) => void;
  persistentOnWide?: boolean;
  persistentBreakpoint?: number;
  persistentPlacement?: "align-anchor" | "below-anchor";
  persistentInlineInset?: number;
}

/**
 * Shared non-modal popover anchored to a trigger control.
 *
 * The panel portals to document.body so it is not clipped by app-shell or chat
 * overflow. It follows the trigger during scroll/resize and closes on outside
 * click or Escape while preserving normal page tab order. Consumers can opt
 * into a persistent wide-screen mode that keeps the panel open during normal
 * page interaction while preserving its alignment with the trigger.
 */
export default function AnchoredPopover({
  trigger,
  children,
  ariaLabel,
  open: controlledOpen,
  align = "right",
  width = 360,
  panelClassName = "",
  openOnHover = false,
  onOpenChange,
  persistentOnWide = false,
  persistentBreakpoint = 1280,
  persistentPlacement = "align-anchor",
  persistentInlineInset = 0,
}: AnchoredPopoverProps) {
  const [internalOpen, setInternalOpen] = useState(false);
  const open = controlledOpen ?? internalOpen;
  const [persistent, setPersistent] = useState(false);
  const [position, setPosition] = useState<{
    top: number;
    left: number;
    maxHeight: number;
    width: number;
  } | null>(null);
  const panelId = useId();
  const anchorRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const hoverCloseTimerRef = useRef<number | null>(null);
  const interactionPinnedRef = useRef(false);
  const openRef = useRef(false);

  useLayoutEffect(() => {
    openRef.current = open;
  }, [open]);

  const setPopoverOpen = useCallback((nextOpen: boolean) => {
    if (openRef.current === nextOpen) return;
    openRef.current = nextOpen;
    if (controlledOpen === undefined) setInternalOpen(nextOpen);
    onOpenChange?.(nextOpen);
  }, [controlledOpen, onOpenChange]);

  const clearHoverCloseTimer = useCallback(() => {
    if (hoverCloseTimerRef.current === null) return;
    window.clearTimeout(hoverCloseTimerRef.current);
    hoverCloseTimerRef.current = null;
  }, []);
  const close = useCallback(() => {
    clearHoverCloseTimer();
    interactionPinnedRef.current = false;
    setPopoverOpen(false);
  }, [clearHoverCloseTimer, setPopoverOpen]);
  const openFromHover = useCallback(() => {
    if (!openOnHover) return;
    clearHoverCloseTimer();
    setPopoverOpen(true);
  }, [clearHoverCloseTimer, openOnHover, setPopoverOpen]);
  const closeAfterHover = useCallback(() => {
    if (!openOnHover || interactionPinnedRef.current) return;
    clearHoverCloseTimer();
    hoverCloseTimerRef.current = window.setTimeout(() => {
      hoverCloseTimerRef.current = null;
      setPopoverOpen(false);
    }, 140);
  }, [clearHoverCloseTimer, openOnHover, setPopoverOpen]);
  const openFromTrigger = useCallback(() => {
    clearHoverCloseTimer();
    interactionPinnedRef.current = true;
    setPopoverOpen(true);
    window.requestAnimationFrame(() => {
      panelRef.current
        ?.querySelector<HTMLElement>(
          "button:not(:disabled), a, input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex='-1'])",
        )
        ?.focus();
    });
  }, [clearHoverCloseTimer, setPopoverOpen]);
  const toggleFromTrigger = useCallback(() => {
    if (openOnHover) {
      openFromTrigger();
      return;
    }
    if (openRef.current) {
      close();
      return;
    }
    openFromTrigger();
  }, [close, openFromTrigger, openOnHover]);
  const recomputePosition = useCallback(() => {
    const anchor = anchorRef.current;
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    const viewportGutter = 8;
    const gap = 8;
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    const effectiveWidth = Math.min(width, viewportWidth - viewportGutter * 2);
    const shouldPersist = persistentOnWide && viewportWidth >= persistentBreakpoint;
    setPersistent(shouldPersist);

    if (shouldPersist) {
      const persistentGutter = 16;
      const top = Math.max(
        persistentGutter,
        persistentPlacement === "below-anchor" ? rect.bottom + gap : rect.top,
      );
      const desiredLeft = align === "right"
        ? rect.right - effectiveWidth - persistentInlineInset
        : rect.left + persistentInlineInset;
      setPosition({
        top,
        left: Math.max(
          persistentGutter,
          Math.min(desiredLeft, viewportWidth - persistentGutter - effectiveWidth),
        ),
        maxHeight: Math.max(160, viewportHeight - top - persistentGutter),
        width: effectiveWidth,
      });
      return;
    }

    // Use content height: the rendered panel may already be capped by maxHeight.
    const measuredHeight = panelRef.current?.scrollHeight || 360;
    const maxAvailableHeight = Math.max(1, viewportHeight - viewportGutter * 2);
    const desiredHeight = Math.min(measuredHeight, maxAvailableHeight);
    const roomBelow = viewportHeight - rect.bottom - gap - viewportGutter;
    const roomAbove = rect.top - gap - viewportGutter;
    const openBelow = roomBelow >= desiredHeight;
    const openAbove = !openBelow && roomAbove >= desiredHeight;
    // When neither side fits, overlay within the viewport instead of clipping to one side.
    const maxHeight = openBelow ? roomBelow : openAbove ? roomAbove : maxAvailableHeight;
    const desiredTop = openBelow ? rect.bottom + gap
      : openAbove ? rect.top - gap - desiredHeight
      : viewportGutter;
    const desiredLeft = align === "right" ? rect.right - effectiveWidth : rect.left;
    setPosition({
      top: desiredTop,
      left: Math.max(viewportGutter, Math.min(desiredLeft, viewportWidth - viewportGutter - effectiveWidth)),
      maxHeight,
      width: effectiveWidth,
    });
  }, [
    align,
    persistentBreakpoint,
    persistentInlineInset,
    persistentOnWide,
    persistentPlacement,
    width,
  ]);

  useEffect(() => {
    return clearHoverCloseTimer;
  }, [clearHoverCloseTimer]);

  useEffect(() => {
    if (!persistentOnWide) {
      setPersistent(false);
      return;
    }
    const updatePersistentMode = () => {
      setPersistent(window.innerWidth >= persistentBreakpoint);
    };
    updatePersistentMode();
    window.addEventListener("resize", updatePersistentMode);
    return () => window.removeEventListener("resize", updatePersistentMode);
  }, [persistentBreakpoint, persistentOnWide]);

  useLayoutEffect(() => {
    if (!open) return;
    recomputePosition();
  }, [open, recomputePosition]);

  useEffect(() => {
    if (!open || controlledOpen === undefined) return;
    const frame = window.requestAnimationFrame(() => {
      panelRef.current?.querySelector<HTMLElement>(POPOVER_FOCUSABLE_SELECTOR)?.focus();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [controlledOpen, open]);

  useEffect(() => {
    if (!open) return;
    const handleOutsideClick = (event: MouseEvent) => {
      if (persistent) return;
      const target = event.target as Node;
      if (anchorRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      close();
    };
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      close();
      anchorRef.current?.querySelector<HTMLElement>("button, a, [tabindex]")?.focus();
    };
    const handleViewportChange = () => recomputePosition();
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(handleViewportChange);
    if (panelRef.current) resizeObserver?.observe(panelRef.current);
    document.addEventListener("mousedown", handleOutsideClick);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("resize", handleViewportChange);
    window.addEventListener("scroll", handleViewportChange, true);
    return () => {
      resizeObserver?.disconnect();
      document.removeEventListener("mousedown", handleOutsideClick);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("resize", handleViewportChange);
      window.removeEventListener("scroll", handleViewportChange, true);
    };
  }, [close, open, persistent, recomputePosition]);

  const typedTrigger = trigger as ReactElement<any>;
  const triggerControl = isValidElement(trigger)
    ? cloneElement(typedTrigger, {
      "aria-haspopup": "dialog",
      "aria-expanded": open,
      "aria-controls": open ? panelId : undefined,
      "data-popover-persistent": persistent || undefined,
      onClick: (event: ReactMouseEvent<HTMLElement>) => {
        typedTrigger.props.onClick?.(event);
        if (!event.defaultPrevented) {
          toggleFromTrigger();
        }
      },
      ...(
        typeof typedTrigger.type === "string" && !["button", "a", "input"].includes(typedTrigger.type)
          ? {
            role: "button",
            tabIndex: 0,
            onKeyDown: (event: KeyboardEvent<HTMLElement>) => {
              typedTrigger.props.onKeyDown?.(event);
              if (!event.defaultPrevented && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                toggleFromTrigger();
              }
            },
          }
          : {}
      ),
    } as Record<string, unknown>)
    : trigger;

  return (
    <span
      ref={anchorRef}
      className="anchored-popover-anchor"
      onMouseEnter={openOnHover ? openFromHover : undefined}
      onMouseLeave={openOnHover ? closeAfterHover : undefined}
    >
      {triggerControl}
      {open && typeof document !== "undefined" && createPortal(
        <div
          id={panelId}
          ref={panelRef}
          role="dialog"
          aria-label={ariaLabel}
          onMouseEnter={openOnHover ? clearHoverCloseTimer : undefined}
          onMouseLeave={openOnHover ? closeAfterHover : undefined}
          onFocusCapture={openOnHover ? () => {
            clearHoverCloseTimer();
            interactionPinnedRef.current = true;
          } : undefined}
          onMouseDown={openOnHover ? () => {
            clearHoverCloseTimer();
            interactionPinnedRef.current = true;
          } : undefined}
          onKeyDown={(event) => {
            if (persistent || event.key !== "Tab") return;
            const panel = panelRef.current;
            const anchor = anchorRef.current;
            if (!panel || !anchor) return;
            const focusableElements = visibleFocusableElements(panel);
            const first = focusableElements[0];
            const last = focusableElements[focusableElements.length - 1];
            const activeElement = document.activeElement;
            if (event.shiftKey && activeElement === first) {
              event.preventDefault();
              event.stopPropagation();
              close();
              anchor.querySelector<HTMLElement>(POPOVER_FOCUSABLE_SELECTOR)?.focus();
            } else if (!event.shiftKey && activeElement === last) {
              event.preventDefault();
              event.stopPropagation();
              const nextControl = focusAfterTrigger(anchor, panel);
              close();
              (nextControl
                ?? anchor.querySelector<HTMLElement>(POPOVER_FOCUSABLE_SELECTOR))
                ?.focus();
            }
          }}
          className={`anchored-popover-panel${persistent ? " anchored-popover-panel--persistent" : ""}${panelClassName ? ` ${panelClassName}` : ""}`}
          style={{
            top: position?.top ?? 0,
            left: position?.left ?? 0,
            width: position?.width ?? width,
            maxHeight: position?.maxHeight ?? 360,
            visibility: position ? "visible" : "hidden",
          }}
        >
          {typeof children === "function" ? children({ close }) : children}
        </div>,
        document.body,
      )}
    </span>
  );
}
