import { type CSSProperties, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  'audio[controls]:not([tabindex="-1"])',
  'video[controls]:not([tabindex="-1"])',
  'iframe:not([tabindex="-1"])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

type ModalLayerId = string;

const MODAL_LAYER_ATTRIBUTE = "data-manor-modal-layer-id";
let nextModalLayerId = 0;
let bodyScrollLockCount = 0;
let bodyOverflowBeforeLock = "";

function isTopModalLayer(layerId: ModalLayerId): boolean {
  const renderedLayers = document.body.querySelectorAll<HTMLElement>(
    `[${MODAL_LAYER_ATTRIBUTE}]`,
  );
  return renderedLayers.item(renderedLayers.length - 1)?.dataset.manorModalLayerId
    === layerId;
}

function lockBodyScroll(): () => void {
  if (bodyScrollLockCount === 0) {
    bodyOverflowBeforeLock = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  bodyScrollLockCount += 1;

  let released = false;
  return () => {
    if (released) return;
    released = true;
    bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1);
    if (bodyScrollLockCount === 0) {
      document.body.style.overflow = bodyOverflowBeforeLock;
      bodyOverflowBeforeLock = "";
    }
  };
}

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => {
      if (
        element.hidden
        || element.matches(":disabled")
        || element.getAttribute("aria-disabled") === "true"
        || element.closest('[aria-hidden="true"], [hidden]')
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

export function trapDialogTabKey(event: KeyboardEvent, dialog: HTMLElement): void {
  if (event.key !== "Tab") return;

  const focusableElements = getFocusableElements(dialog);
  if (focusableElements.length === 0) {
    event.preventDefault();
    dialog.focus();
    return;
  }

  const first = focusableElements[0];
  const last = focusableElements[focusableElements.length - 1];
  const activeIndex = focusableElements.indexOf(document.activeElement as HTMLElement);
  if (event.shiftKey && activeIndex <= 0) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && activeIndex === focusableElements.length - 1) {
    event.preventDefault();
    first.focus();
  } else if (activeIndex === -1) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
}

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
  overlayClassName?: string;
  bodyClassName?: string;
  width?: string;
  height?: string;
  maxWidth?: string;
  restoreFocusFallback?: () => void;
}

export default function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  className,
  overlayClassName,
  bodyClassName,
  width,
  height,
  maxWidth,
  restoreFocusFallback,
}: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const [modalLayerId] = useState<ModalLayerId>(
    () => `modal-layer-${++nextModalLayerId}`,
  );
  const previouslyFocusedElementRef = useRef<HTMLElement | null>(null);
  const restoreFocusFallbackRef = useRef(restoreFocusFallback);
  restoreFocusFallbackRef.current = restoreFocusFallback;

  useEffect(() => {
    if (!open) return;
    const unlockBodyScroll = lockBodyScroll();
    return unlockBodyScroll;
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const activeElement = document.activeElement;
    previouslyFocusedElementRef.current = activeElement instanceof HTMLElement
      ? activeElement
      : null;
    const focusFrame = window.requestAnimationFrame(() => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusableElements = getFocusableElements(dialog);
      (focusableElements[0] ?? dialog).focus();
    });

    return () => {
      window.cancelAnimationFrame(focusFrame);
      const previouslyFocusedElement = previouslyFocusedElementRef.current;
      previouslyFocusedElementRef.current = null;
      if (previouslyFocusedElement?.isConnected) {
        previouslyFocusedElement.focus();
      } else {
        restoreFocusFallbackRef.current?.();
      }
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleKey(event: KeyboardEvent) {
      const dialog = dialogRef.current;
      if (!dialog) return;
      if (!isTopModalLayer(modalLayerId)) return;
      if (event.key === "Escape") {
        const target = event.target;
        if (
          target instanceof Element
          && target.closest('[data-manor-popup-open="true"]')
        ) {
          return;
        }
        event.preventDefault();
        onClose();
        return;
      }
      trapDialogTabKey(event, dialog);
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [modalLayerId, open, onClose]);

  if (!open) return null;

  const overlayStyle: CSSProperties = {
    position: "fixed",
    top: 0,
    right: 0,
    bottom: 0,
    left: 0,
    width: "100vw",
    height: "100vh",
    minHeight: "100dvh",
    zIndex: 20000,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    padding: 24,
    overflowY: "auto",
    background: "var(--modal-overlay-bg)",
    backdropFilter: "blur(5px)",
    WebkitBackdropFilter: "blur(5px)",
    opacity: 1,
    pointerEvents: "auto",
  };

  const dialogStyle: CSSProperties = {
    position: "relative",
    zIndex: 20001,
    width: width || "min(100%, calc(100vw - 32px))",
    height,
    maxWidth: maxWidth || "520px",
    maxHeight: "calc(100dvh - 48px)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    background: "var(--modal-bg)",
    backdropFilter: "blur(20px) saturate(1.08)",
    WebkitBackdropFilter: "blur(20px) saturate(1.08)",
    borderRadius: 24,
    border: "1px solid var(--modal-border)",
    boxShadow: "var(--modal-shadow)",
  };

  return createPortal(
    <div
      data-manor-modal-layer-id={modalLayerId}
      className={["manor-dialog-overlay", overlayClassName].filter(Boolean).join(" ")}
      style={overlayStyle}
      onClick={onClose}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        className={["manor-dialog", className].filter(Boolean).join(" ")}
        style={dialogStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="manor-dialog-header">
          <h2 className="manor-dialog-title" title={title}>{title}</h2>
          <button type="button" className="manor-dialog-close" onClick={onClose} aria-label="Close" title="Close">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          </button>
        </div>
        <div className={["manor-dialog-body", bodyClassName].filter(Boolean).join(" ")}>{children}</div>
        {footer && <div className="manor-dialog-footer">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
