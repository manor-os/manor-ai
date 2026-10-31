import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { uploadPendingInputFiles } from "../../lib/pendingInputUploads";
import ChatActionCard from "../ui/ChatActionCard";
import Modal from "../ui/Modal";
import { WORKFLOW_REVIEW_FRAMES_DIALOG_WIDTH, workflowReviewHasFrames } from "../workflows/WorkflowApprovalReview";
import { InlineRowsSkeleton } from "../ui/Skeleton";

/** A waiting decision answered inside the App: the same card the chat shows, without leaving the page. */
export default function WorkspaceAppDecisionDialog({ workspaceId, messageId, title, onClose }: {
  workspaceId: string;
  messageId: string;
  title: string;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [resetToken, setResetToken] = useState(0);
  const message = useQuery({
    queryKey: ["workspace-attention-message", workspaceId, messageId],
    queryFn: ({ signal }) => api.workspaces.chat.getMessage(workspaceId, messageId, signal),
    staleTime: 0,
  });
  const resolve = useMutation({
    mutationFn: async ({ choice, note, payload, files }: {
      choice: string; note?: string; payload?: Record<string, any>; files?: File[];
    }) => {
      const uploaded = files?.length ? await uploadPendingInputFiles({ workspaceId, messageId }, files) : [];
      const resolvedPayload = uploaded.length
        ? { ...(payload || {}), attachments: [...(Array.isArray(payload?.attachments) ? payload.attachments : []), ...uploaded] }
        : payload;
      return api.workspaces.chat.resolveAction(workspaceId, messageId, choice, note, resolvedPayload);
    },
    onSuccess: () => {
      // Needs you, the run and the chat card all follow the answer at once.
      for (const queryKey of [
        ["workspace-app-section", workspaceId], ["workspace-app-view", workspaceId], ["workspace-chat", workspaceId],
        ["workspace-attention-message", workspaceId], ["workflow-run"], ["workspace-workflow-runs", workspaceId],
      ]) void queryClient.invalidateQueries({ queryKey });
      window.dispatchEvent(new CustomEvent("manor:workspace-actions-refresh", { detail: { workspaceId } }));
      onClose();
    },
    // The card unlocks so the same answer can be sent again.
    onError: () => setResetToken((token) => token + 1),
  });
  const data = message.data;
  // A storyboard's frames need the width to sit side by side; other decisions keep their size.
  const frames = workflowReviewHasFrames(data?.pending_action?.review);
  return <Modal open onClose={onClose} title={title} bodyClassName="workspace-app-decision"
    width={frames ? WORKFLOW_REVIEW_FRAMES_DIALOG_WIDTH : "min(760px, calc(100vw - 32px))"}
    {...(frames ? { maxWidth: WORKFLOW_REVIEW_FRAMES_DIALOG_WIDTH } : {})}>
    {message.isPending ? <InlineRowsSkeleton rows={3} />
      : !data?.pending_action?.kind && !data?.resolved_at
        ? <p className="workspace-app-section__note">{t("component.workspace_app_view.queue_answered")}</p>
        : <ChatActionCard
          workspaceId={workspaceId}
          action={data.pending_action || { kind: "unknown" }}
          resolved={Boolean(data.resolved_at)}
          resolution={data.resolution ?? null}
          resetToken={resetToken}
          disabled={resolve.isPending}
          onResolve={(choice, note, payload, files) => resolve.mutate({ choice, note, payload, files })}
        />}
  </Modal>;
}
