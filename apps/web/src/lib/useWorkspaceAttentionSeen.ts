import { useEffect, type RefObject } from "react";
import type { QueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { observeWorkspaceAttention } from "./observeWorkspaceAttention";

export function useWorkspaceAttentionSeen(
  workspaceId: string,
  unseenKeys: Record<string, Record<string, string>> | undefined,
  root: RefObject<HTMLDivElement | null>,
  queryClient: QueryClient,
) {
  useEffect(() => {
    if (!root.current || !unseenKeys || !Object.keys(unseenKeys).length) return;
    return observeWorkspaceAttention(root.current, async (keys) => {
      await api.workspaces.chat.markAttentionSeen(workspaceId, keys);
      // Refresh the badge without reprocessing the chat page or moving its
      // scroll anchor. The observer retains successful receipts until refetch.
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-unread-attention", workspaceId] });
    });
  }, [workspaceId, unseenKeys, root, queryClient]);
}
