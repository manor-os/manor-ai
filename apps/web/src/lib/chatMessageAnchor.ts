export function chatMessageAnchorId(
  messageId: string | undefined,
  index: number,
): string {
  const raw = messageId || `index-${index}`;
  return `chat-message-${String(raw).replace(/[^A-Za-z0-9_-]/g, "-")}`;
}
