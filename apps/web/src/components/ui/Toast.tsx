import { useEffect, useState } from "react";

type ToastVariant = "success" | "warning" | "error" | "info";

const ICONS: Record<ToastVariant, string> = {
  success: "\u2713",
  warning: "\u26A0",
  error: "\u2716",
  info: "\u2139",
};

interface ToastProps {
  message: string;
  variant?: ToastVariant;
  duration?: number;
  onClose?: () => void;
}

export default function Toast({ message, variant = "info", duration = 3000, onClose }: ToastProps) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const timer = setTimeout(() => {
      setVisible(false);
      setTimeout(() => onClose?.(), 200);
    }, duration);
    return () => clearTimeout(timer);
  }, [duration, onClose]);

  return (
    <div
      className={`manor-toast manor-toast-${variant}`}
      style={{
        position: "fixed",
        top: "24px",
        right: "24px",
        zIndex: 100,
        opacity: visible ? 1 : 0,
        transform: visible ? "translateY(0)" : "translateY(-12px)",
        transition: "all 0.2s ease",
      }}
    >
      <span style={{ fontSize: "16px", flexShrink: 0 }}>{ICONS[variant]}</span>
      {message}
    </div>
  );
}
