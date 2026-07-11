import { type ReactNode } from "react";

/**
 * One message row in a bottom-right chat-style panel (Manor AI chat,
 * Support, …). Owns the shared shell only: left/right placement by author,
 * the optional avatar on the "other" side, and the vertically-stacked
 * content column. Callers drop whatever belongs in the column as
 * `children` — a <MessageBubble>, a meta line, attachments, tool cards,
 * a streaming cursor, a timestamp, etc.
 */
export default function MessageRow({
  role,
  avatar,
  children,
}: {
  /** "user" = the signed-in person (right side, no avatar). */
  role: "user" | "other";
  /** Rendered on the "other" side only; omitted for the user's own rows. */
  avatar?: ReactNode;
  children: ReactNode;
}) {
  const mine = role === "user";
  return (
    <div
      className="chat-message-shell"
      style={{
        display: "flex",
        flexDirection: mine ? "row-reverse" : "row",
        gap: 8,
        alignItems: "flex-end",
      }}
    >
      {!mine && avatar}
      <div
        style={{
          maxWidth: "82%",
          minWidth: 0,
          display: "flex",
          flexDirection: "column",
          alignItems: mine ? "flex-end" : "flex-start",
          gap: 3,
          overflow: "hidden",
        }}
      >
        {children}
      </div>
    </div>
  );
}
