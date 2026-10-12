import { hasActivePersistedChatStream } from "./chatStream";

interface WorkspaceStreamMessage {
  id: string;
  conversation_id: string;
  created_at: string;
  author_kind: string;
  meta?: Record<string, unknown> | null;
}

export function hasActiveWorkspaceChatStream(
  messages: WorkspaceStreamMessage[],
  conversationId: string | null | undefined,
  hasActiveRuntimeRun?: boolean | null,
): boolean {
  if (!conversationId) return false;
  if (typeof hasActiveRuntimeRun === "boolean") return hasActiveRuntimeRun;
  return hasActivePersistedChatStream(
    messages
      .filter((message) => message.conversation_id === conversationId
        && (message.author_kind === "user" || message.author_kind === "agent"))
      .sort((left, right) => {
        const timeDelta = Date.parse(left.created_at) - Date.parse(right.created_at);
        if (Number.isFinite(timeDelta) && timeDelta !== 0) return timeDelta;
        const rankDelta = Number(left.author_kind !== "user") - Number(right.author_kind !== "user");
        return rankDelta || left.id.localeCompare(right.id);
      })
      .map((message) => ({
        role: message.author_kind === "user" ? "user" : "assistant",
        meta: message.meta,
      })),
  );
}
