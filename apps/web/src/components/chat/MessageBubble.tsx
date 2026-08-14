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
        padding: mine
          ? "var(--message-bubble-user-padding, 10px 14px)"
          : "var(--message-bubble-other-padding, 10px 14px)",
        borderRadius: mine
          ? "var(--message-bubble-user-radius, 16px 0 16px 16px)"
          : "var(--message-bubble-other-radius, 0 16px 16px 16px)",
        background: mine
          ? "var(--message-bubble-user-bg, var(--accent))"
          : "var(--message-bubble-other-bg, var(--surface-muted))",
        color: mine
          ? "var(--message-bubble-user-fg, #fff)"
          : "var(--message-bubble-other-fg, var(--text-strong))",
        border: mine
          ? "var(--message-bubble-user-border, 1px solid var(--accent))"
          : "var(--message-bubble-other-border, 1px solid var(--border-subtle))",
        boxShadow: mine
          ? "var(--message-bubble-user-shadow, var(--shadow-sm))"
          : "var(--message-bubble-other-shadow, var(--shadow-sm))",
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
