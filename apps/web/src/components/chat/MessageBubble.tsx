import { type CSSProperties, type ReactNode } from "react";

/**
 * The rounded message bubble shared by the Manor AI chat and Support
 * panels — one source of truth for padding, the asymmetric corner radius,
 * the teal "mine" / slate "other" colours, border and soft shadow.
 *
 * The Manor AI chat passes `className="chat-bubble chat-bubble--user|bot"`
 * so its markdown / meta-chip child styles (scoped under those classes in
 * index.css) keep working; the inline style here owns the box look itself.
 */
export default function MessageBubble({
  role,
  className,
  style,
  children,
}: {
  role: "user" | "other";
  className?: string;
  /** Escape hatch for per-caller tweaks; merged last. */
  style?: CSSProperties;
  children: ReactNode;
}) {
  const mine = role === "user";
  return (
    <div
      className={className}
      style={{
        padding: "10px 14px",
        borderRadius: mine ? "14px 14px 4px 14px" : "14px 14px 14px 4px",
        background: mine ? "var(--accent)" : "var(--surface-muted)",
        color: mine ? "#fff" : "var(--text-strong)",
        border: `1px solid ${mine ? "var(--accent)" : "var(--border-subtle)"}`,
        boxShadow: "var(--shadow-sm)",
        fontSize: 13,
        lineHeight: 1.5,
        wordBreak: "break-word",
        ...style,
      }}
    >
      {children}
    </div>
  );
}
