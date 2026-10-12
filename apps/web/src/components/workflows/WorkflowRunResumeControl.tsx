import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import type { WorkflowHistoryRun } from "./workflowRunDisplay";

/** Review the current frozen wait before sending a decision to normal resume. */
export default function WorkflowRunResumeControl({ runId, disabled, onResumed }: {
  runId: string;
  disabled?: boolean;
  onResumed?: (run: WorkflowHistoryRun) => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<WorkflowHistoryRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const openReview = async () => {
    if (disabled || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setOpen(true);
    setDetail(null);
    setError("");
    try {
      const current = await api.workflows.getRun(runId);
      if (current.status !== "paused" || !current.capabilities?.can_control || !current.resume_action) {
        throw new Error(current.status === "paused"
          ? "Nothing needs your response right now. This run is waiting on background work and continues on its own."
          : `Nothing needs your response. This run is ${current.status}.`);
      }
      setDetail(current);
    } catch (cause) {
      setError(translateApiError(cause, "Couldn't load the current workflow request."));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const resolve = async (choice: string, note?: string, payload?: Record<string, unknown>) => {
    const action = detail?.resume_action;
    if (!detail || !action || busyRef.current || !action.options?.includes(choice)) return;
    busyRef.current = true;
    setBusy(true);
    try {
      let resumed: WorkflowHistoryRun;
      if (action.kind === "workflow_approval" && typeof action.runtime_approval_id === "string" && action.runtime_approval_id) {
        if (choice !== "approve" && choice !== "reject") return;
        const approvalRunId = typeof action.workflow_run_id === "string" && action.workflow_run_id.trim()
          ? action.workflow_run_id.trim()
          : detail.id;
        resumed = await api.workflows.resolveRuntimeApproval(approvalRunId, action.runtime_approval_id, choice);
        if (approvalRunId !== detail.id) resumed = await api.workflows.getRun(detail.id);
      } else if (typeof action.message_id === "string" && action.message_id) {
        if (typeof action.message_workspace_id === "string" && action.message_workspace_id) {
          await api.workspaces.chat.resolveAction(action.message_workspace_id, action.message_id, choice, note, payload);
        } else {
          await api.chat.resolveAction(action.message_id, choice, note, payload);
        }
        resumed = await api.workflows.getRun(detail.id);
      } else {
        // A note (revision instructions) travels with the choice; the runner
        // reads `.choice` from either shape.
        const variables = action.kind === "workflow_approval"
          ? { [String(action.response_variable)]: note ? { choice, note } : choice }
          : action.kind === "workflow_input"
            ? payload
          : undefined;
        resumed = await api.workflows.resumeRun(detail.id, variables, action.step_id);
      }
      for (const key of ["workflow-run", "workflow-runs", "workspace-workflow-runs", "workflow-run-history-detail", "workflow-run-history-family", "workflow-run-review", "workspace-chat-workflow-run-family", "workspace-chat", "workspace-chat-messages", "chat-messages"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      onResumed?.(resumed);
      setOpen(false);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  return <>
    <Button size="sm" variant="outline" disabled={disabled || busy} onClick={() => void openReview()}>{t("component.workflow_review.open_request")}</Button>
    <Modal
      open={open}
      onClose={() => { if (!busy) setOpen(false); }}
      title={detail?.resume_action?.kind === "workflow_approval" ? t("component.workflow_review.dialog_title") : t("component.workflow_review.response_title")}
      maxWidth="760px"
      className="workflow-response-dialog"
    >
      {error && <p role="alert">{error}</p>}
      {busy && !detail && <p role="status">Loading current request…</p>}
      {detail?.resume_action && <WorkflowRunIntervention
        run={{ id: detail.id, title: detail.workflow_name || "Workflow", status: "paused", currentNodeId: detail.current_step_id, nodes: [{ id: detail.current_step_id || "", name: String(detail.resume_action.title || "Wait"), status: "paused" }] }}
        action={detail.resume_action}
        presentation="dialog"
        workspaceId={detail.workspace_id}
        loading={busy}
        onResolve={resolve}
      />}
    </Modal>
  </>;
}
