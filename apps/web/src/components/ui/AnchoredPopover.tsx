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

interface AnchoredPopoverRenderContext {
  close: () => void;
}

interface AnchoredPopoverProps {
  trigger: ReactElement;
  children: ReactNode | ((context: AnchoredPopoverRenderContext) => ReactNode);
  ariaLabel: string;
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
  const [open, setOpen] = useState(false);
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

  const setPopoverOpen = useCallback((nextOpen: boolean) => {
    if (openRef.current === nextOpen) return;
    openRef.current = nextOpen;
    setOpen(nextOpen);
    onOpenChange?.(nextOpen);
  }, [onOpenChange]);

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

    const measuredHeight = panelRef.current?.getBoundingClientRect().height || 360;
    const maxAvailableHeight = Math.max(180, viewportHeight - viewportGutter * 2);
    const desiredHeight = Math.min(measuredHeight, maxAvailableHeight);
    const roomBelow = viewportHeight - rect.bottom - gap - viewportGutter;
    const roomAbove = rect.top - gap - viewportGutter;
    const openAbove = roomBelow < desiredHeight && roomAbove > roomBelow;
    const maxHeight = Math.max(160, Math.min(maxAvailableHeight, openAbove ? roomAbove : roomBelow));
    const desiredTop = openAbove
      ? rect.top - gap - Math.min(measuredHeight, maxHeight)
      : rect.bottom + gap;
    const desiredLeft = align === "right" ? rect.right - effectiveWidth : rect.left;
    setPosition({
      top: Math.max(viewportGutter, Math.min(desiredTop, viewportHeight - viewportGutter - maxHeight)),
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
          if (openOnHover) {
            openFromTrigger();
          } else {
            setPopoverOpen(!openRef.current);
          }
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
                if (openOnHover) {
                  openFromTrigger();
                } else {
                  setPopoverOpen(!openRef.current);
                }
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
