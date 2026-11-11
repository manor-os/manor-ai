import type { QueryClient } from "@tanstack/react-query";
import { api } from "./api";

// About three screens: enough to read at once, and older pages load on scroll.
export const WORKSPACE_CHAT_PAGE_SIZE = 30;

type ThreadRef = { kind?: string; id?: string } | null | undefined;

export function workspaceChatMessagesQuery(workspaceId: string, threadRef?: ThreadRef) {
  return {
    queryKey: ["workspace-chat", workspaceId, threadRef?.kind || "main", threadRef?.id || ""],
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      api.workspaces.chat.listMessagesPage(
        workspaceId,
        {
          limit: WORKSPACE_CHAT_PAGE_SIZE,
          thread_ref_kind: threadRef?.kind,
          thread_ref_id: threadRef?.id,
        },
        signal,
      ),
  };
}

/** Starts reading a Workspace's messages while the chat's own code is still loading. */
export function prefetchWorkspaceChatMessages(queryClient: QueryClient, workspaceId: string) {
  void queryClient.prefetchQuery(workspaceChatMessagesQuery(workspaceId));
}
