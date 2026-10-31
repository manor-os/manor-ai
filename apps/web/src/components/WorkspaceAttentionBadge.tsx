import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { useToastStore } from "../stores/toast";

export default function WorkspaceAttentionBadge({ workspaceId, name, count, compact = false }: {
  workspaceId: string;
  name: string;
  count: number;
  compact?: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const jumpingRef = useRef(false);
  const [jumping, setJumping] = useState(false);
  const label = t("component.workspace_attention.open", { count: String(count), name });
  const jumpToPendingMessage = async () => {
    if (jumpingRef.current) return;
    jumpingRef.current = true;
    setJumping(true);
    try {
      const reminders = await queryClient.fetchQuery({
        queryKey: ["workspace-pending-attention", workspaceId],
        queryFn: ({ signal }) => api.workspaces.chat.listPendingAttention(workspaceId, signal),
        staleTime: 0,
      });
      // Read receipts never remove outstanding work from this navigation.
      // Start with the oldest open request.
      const next = reminders.items.at(-1);
      if (next) {
        navigate(`/chat?workspace=${encodeURIComponent(workspaceId)}&attention=${encodeURIComponent(next.key)}#workspace-chat-message-${encodeURIComponent(next.message_id)}`);
      } else {
        void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
        toast.info(t("component.workspace_attention.empty"));
      }
    } catch {
      toast.error(t("component.workspace_attention.error"));
    } finally {
      jumpingRef.current = false;
      setJumping(false);
    }
  };
  return (
    <button type="button" className={`conv-badge workspace-attention-badge${compact ? " workspace-attention-badge--compact" : ""}`}
      aria-label={label} title={label} aria-busy={jumping} disabled={jumping}
      onClick={(event) => { event.stopPropagation(); void jumpToPendingMessage(); }}>
      {compact && count > 9 ? "9+" : count}
    </button>
  );
}
