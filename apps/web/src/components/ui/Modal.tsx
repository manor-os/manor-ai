import { type CSSProperties, useEffect } from "react";
import { createPortal } from "react-dom";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  width?: string;
  height?: string;
  maxWidth?: string;
}

export default function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  className,
  bodyClassName,
  width,
  height,
  maxWidth,
}: ModalProps) {
  useEffect(() => {
    if (open) {
      document.body.style.overflow = "hidden";
      return () => { document.body.style.overflow = ""; };
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onClose]);

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
    <div className="manor-dialog-overlay" style={overlayStyle} onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={["manor-dialog", className].filter(Boolean).join(" ")}
        style={dialogStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="manor-dialog-header">
          <h2 className="manor-dialog-title">{title}</h2>
          <button className="manor-dialog-close" onClick={onClose} aria-label="Close" title="Close">
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
