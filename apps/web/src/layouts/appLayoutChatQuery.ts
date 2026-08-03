export type AppLayoutChatTarget =
  | { type: "conversation"; conversationId: string }
  | { type: "workspace"; workspaceId: string }
  | null;

export function parseAppLayoutChatTarget(search: string): AppLayoutChatTarget {
  const params = new URLSearchParams(search);
  const conversationId = (params.get("conversation") || params.get("conversationId") || "").trim();
  if (conversationId) {
    return { type: "conversation", conversationId };
  }

  const workspaceId = (params.get("workspace") || params.get("workspaceId") || "").trim();
  if (workspaceId) {
    return { type: "workspace", workspaceId };
  }

  return null;
}
