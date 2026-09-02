export interface PublicChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string | null;
  stream_status?: string | null;
  client_turn_id?: string | null;
  local_status?: "pending" | "notice";
  attachments?: { name?: string; filename?: string; original_name?: string }[] | null;
}

const ASSISTANT_STREAM_STARTED_CONTENT =
  "The assistant started this response and is still working. If this remains after a reload, the stream was interrupted before it could finish.";

export function hasAssistantStreamPlaceholder(content: string): boolean {
  return (content || "").startsWith(ASSISTANT_STREAM_STARTED_CONTENT);
}

export function removeAssistantStreamPlaceholder(content: string): string {
  if (!hasAssistantStreamPlaceholder(content)) return content || "";
  return (content || "").slice(ASSISTANT_STREAM_STARTED_CONTENT.length).replace(/^\s+/, "");
}

export function publicChatMessageNeedsRefresh(message: PublicChatMessage): boolean {
  return message.role === "assistant" && (
    message.stream_status === "running" || message.stream_status === "streaming" ||
    (!message.stream_status && hasAssistantStreamPlaceholder(message.content))
  );
}

function publicMessageContentMatches(localContent: string, serverContent: string): boolean {
  const normalize = (value: string) =>
    value
      .replace(/\n\n\[Attached:[\s\S]*?\]$/i, "")
      .replace(/\n\[Image:[\s\S]*?\]$/i, "")
      .trim();
  const local = normalize(localContent || "");
  const server = normalize(serverContent || "");
  if (local === server) return true;
  if (local && server && (local.startsWith(server) || server.startsWith(local))) return true;
  return local.startsWith("[Attached:") && server.startsWith("Attached file");
}

export function mergePublicChatMessages(
  previous: PublicChatMessage[],
  incoming: PublicChatMessage[],
): PublicChatMessage[] {
  if (!incoming.length) return previous;
  const next = [...previous];
  let changed = false;
  for (const server of incoming) {
    const stripped = server.role === "assistant" ? removeAssistantStreamPlaceholder(server.content) : server.content;
    const message = stripped && stripped !== server.content ? { ...server, content: stripped } : server;
    const existingIndex = next.findIndex((item) => item.id === message.id);
    if (existingIndex >= 0) {
      const existing = next[existingIndex];
      // A checkpoint can lag behind text already received over SSE. Preserve
      // that text, but let server progress replace a local transport notice.
      if (publicChatMessageNeedsRefresh(server) && existing.local_status !== "notice" && existing.content && (
        !stripped || existing.content.startsWith(stripped)
      )) continue;
      if (existing.content !== message.content || existing.stream_status !== message.stream_status ||
        existing.client_turn_id !== message.client_turn_id ||
        existing.local_status || JSON.stringify(existing.attachments) !== JSON.stringify(message.attachments)) {
        next[existingIndex] = message;
        changed = true;
      }
      continue;
    }
    // Correlate both rows even if SSE disconnected before delivering their IDs.
    // Only legacy user messages without a turn ID may fall back to text matching.
    const optimisticIndex = next.findIndex((item) =>
      item.id.startsWith("tmp-") && item.role === message.role && (
        message.client_turn_id
          ? item.client_turn_id === message.client_turn_id
          : item.role === "user" && publicMessageContentMatches(item.content, message.content)
      )
    );
    if (optimisticIndex >= 0) next[optimisticIndex] = message;
    else next.push(message);
    changed = true;
  }
  return changed ? next : previous;
}
