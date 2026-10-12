import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { uploadPendingInputFiles } from "../../lib/pendingInputUploads";
import { t } from "../../lib/i18n";
import type { Task, TaskInputRequest } from "../../lib/types";
import { useToastStore } from "../../stores/toast";

/** Task detail and drawer resolve the original Chat card, not a second HITL. */
export function useTaskInputResolution(task?: Task) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [resetToken, setResetToken] = useState(0);
  const mutation = useMutation({
    mutationFn: async ({ workspaceId, request, choice, note, payload, files }: {
      taskId: string;
      workspaceId: string;
      request: TaskInputRequest;
      choice: string;
      note?: string;
      payload?: Record<string, any>;
      files?: File[];
    }) => {
      const uploads = files?.length
        ? await uploadPendingInputFiles({ workspaceId, messageId: request.message_id }, files)
        : [];
      return api.workspaces.chat.resolveAction(
        workspaceId, request.message_id, choice, note,
        uploads.length ? {
          ...payload,
          attachments: [...(Array.isArray(payload?.attachments) ? payload.attachments : []), ...uploads],
        } : payload,
      );
    },
    onSuccess: (_result, { taskId, workspaceId, request }) => {
      // A post-commit response closes exactly this card, not a sibling wait.
      queryClient.setQueryData<Task>(["task", taskId], (current) => current ? {
        ...current,
        pending_input_requests: current.pending_input_requests?.filter((item) => item.request_id !== request.request_id),
      } : current);
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] });
      queryClient.invalidateQueries({ queryKey: ["task-plans", taskId] });
      queryClient.invalidateQueries({ queryKey: ["plan-steps", request.plan_id] });
      queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
      window.dispatchEvent(new CustomEvent("manor:workspace-actions-refresh", { detail: { workspaceId } }));
      toast.success(t("page.tasks.input_sent"));
    },
    onError: (err: any, { taskId }) => {
      setResetToken((token) => token + 1);
      queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      toast.error(t("page.task_detail.resume_failed"), err?.message || t("page.tasks.could_not_resume_this_task"));
    },
  });
  return {
    isPending: mutation.isPending,
    resetToken,
    resolve: (request: TaskInputRequest, choice: string, note?: string, payload?: Record<string, any>, files?: File[]) => {
      if (!task?.workspace_id || request.pending_action.task_id !== task.id) return;
      mutation.mutate({ taskId: task.id, workspaceId: task.workspace_id, request, choice, note, payload, files });
    },
  };
}
