import { useState, useEffect } from "react";
import { useToastStore, type ToastItem } from "../stores/toast";

/* ── Icon SVGs matching original Element UI notification icons ── */

const ICON_COLORS: Record<ToastItem["type"], string> = {
  success: "#436b65",
  warning: "#cf9b44",
  error: "#d65f59",
  info: "#5f84bd",
};

const ICON_BG: Record<ToastItem["type"], string> = {
  success: "#f2f6f5",
  warning: "#faf7ef",
  error: "#f8f0ef",
  info: "#f4f7fa",
};

function ToastIcon({ type }: { type: ToastItem["type"] }) {
  const color = ICON_COLORS[type];
  return (
    <div
      style={{
        width: 28,
        height: 28,
        borderRadius: 8,
        background: ICON_BG[type],
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
      }}
    >
      {type === "success" && (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
        </svg>
      )}
      {type === "error" && (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      )}
      {type === "warning" && (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
        </svg>
      )}
      {type === "info" && (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />
        </svg>
      )}
    </div>
  );
}

/* ── Single Toast Card (glass notification style) ── */

function ToastCard({ toast }: { toast: ToastItem }) {
  const removeToast = useToastStore((s) => s.removeToast);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const raf = requestAnimationFrame(() => setVisible(true));
    return () => cancelAnimationFrame(raf);
  }, []);

  const handleClose = () => {
    setVisible(false);
    setTimeout(() => removeToast(toast.id), 250);
  };

  const handleAction = () => {
    toast.onAction?.();
  };

  return (
    <div
      style={{
        background: "rgba(255,255,255,0.95)",
        backdropFilter: "blur(20px)",
        WebkitBackdropFilter: "blur(20px)",
        borderRadius: 16,
        border: "1px solid rgba(255,255,255,0.5)",
        boxShadow: "0 8px 16px -4px rgba(0,0,0,0.1), 0 4px 6px -2px rgba(0,0,0,0.04)",
        padding: toast.actionLabel ? "10px 32px 12px 12px" : "10px 32px 10px 12px",
        width: "min(calc(100vw - 24px), 320px)",
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        position: "relative",
        overflow: "visible",
        opacity: visible ? 1 : 0,
        transform: visible ? "translateX(0)" : "translateX(24px)",
        transition: "all 0.3s cubic-bezier(0.16,1,0.3,1)",
        fontFamily: '"Inter", system-ui, sans-serif',
      }}
    >
      <ToastIcon type={toast.type} />

      <div style={{ flex: 1, minWidth: 0, paddingTop: 1 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 700,
            color: "#292524",
            lineHeight: 1.35,
            marginBottom: toast.message ? 2 : 0,
          }}
        >
          {toast.title}
        </div>
        {toast.message && (
          <div
            style={{
              fontSize: 12,
              color: "#78716c",
              lineHeight: 1.4,
              margin: 0,
            }}
          >
            {toast.message}
          </div>
        )}
        {toast.actionLabel && (
          <button
            type="button"
            onClick={handleAction}
            style={{
              marginTop: 10,
              padding: "6px 10px",
              borderRadius: 10,
              border: "1px solid rgba(95,132,189,0.18)",
              background: "#f3f6fa",
              color: "#3f57a0",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
            }}
          >
            {toast.actionLabel}
          </button>
        )}
      </div>

      <button
        onClick={handleClose}
        style={{
          position: "absolute",
          top: 10,
          right: 10,
          width: 20,
          height: 20,
          borderRadius: "50%",
          background: "#f5f5f4",
          border: "none",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
          color: "#a8a29e",
          fontSize: 12,
          transition: "all 0.2s",
          padding: 0,
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "#e7e5e4";
          e.currentTarget.style.color = "#57534e";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "#f5f5f4";
          e.currentTarget.style.color = "#a8a29e";
        }}
      >
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={3}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  );
}

/* ── Toast Container (top-right stack) ── */

export default function ToastContainer() {
  const toasts = useToastStore((s) => s.toasts);

  if (toasts.length === 0) return null;

  return (
    <div
      style={{
        position: "fixed",
        top: 24,
        right: 24,
        // Above modals (overlay 10000 / dialog 10001 in ui/Modal) so success
        // and error toasts aren't hidden behind an open popup.
        zIndex: 10050,
        display: "flex",
        flexDirection: "column",
        gap: 12,
        pointerEvents: "none",
      }}
    >
      {toasts.map((toast) => (
        <div key={toast.id} style={{ pointerEvents: "auto" }}>
          <ToastCard toast={toast} />
        </div>
      ))}
    </div>
  );
}
