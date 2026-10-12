import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import { IconRefresh } from "../icons";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import { workflowRetrySchemaIsCompatible, type WorkflowHistoryRun } from "./workflowRunDisplay";

export default function WorkflowRunRetryControl({ runId, workspaceId, bindingId, workflowId, businessOutcome, disabled, title, size = "md", onRetried }: {
  runId: string;
  workspaceId: string;
  bindingId?: string;
  workflowId?: string;
  businessOutcome?: string;
  disabled?: boolean;
  title?: string;
  size?: "sm" | "md" | "lg";
  onRetried: (run: WorkflowHistoryRun) => void;
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
      const current = await api.workflows.getRun(runId, false, AbortSignal.timeout(15_000));
      const action = current.intervention;
      const retryable = current.status === "failed" || (current.status === "completed"
        && ["needs_input", "revision_required"].includes(current.business_outcome));
      if (current.id !== runId || current.workspace_id !== workspaceId
        || current.binding_id !== bindingId || current.workflow_id !== workflowId
        || !retryable || !current.capabilities?.can_control || action?.kind !== "workflow_retry"
        || !action.options?.includes("retry")
        || !(action.retry_from_step_id || action.step_id)
        || !workflowRetrySchemaIsCompatible(action.editable_input_schema)) {
        throw new Error(t("component.workspace_app_view.retry_unavailable"));
      }
      setDetail({ ...current, intervention: { ...action, options: ["retry"] } });
    } catch (cause) {
      setError(translateApiError(cause, t("component.workflow_run_history.control_load_error")));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const resolve = async (choice: string, _note?: string, payload?: Record<string, unknown>) => {
    const action = detail?.intervention;
    if (disabled || !detail || !action || busyRef.current || choice !== "retry") return;
    busyRef.current = true;
    setBusy(true);
    try {
      const retried = await api.workflows.retryRun(detail.id, {
        from_step_id: action.retry_from_step_id || action.step_id,
        variables: payload?.variables as Record<string, unknown> | undefined,
        execute: true,
      });
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
      for (const key of ["workflow-runs", "workspace-workflow-runs", "workflow-run", "workspace-chat-workflow-run-family", "workflow-run-history-detail", "workflow-run-history-family", "workflow-run-history-control", "chat-messages", "workspace-app-recent-run", "workspace-app-run", "workspace-app-run-detail"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      onRetried(retried);
      setOpen(false);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const action = detail?.intervention;
  const retryStep = action?.retry_from_step_id || action?.step_id || "";
  const retryStepName = detail?.definition_snapshot?.nodes?.find((node) => node.id === retryStep)?.name
    || (detail?.current_step_id === retryStep ? detail.current_step_name : undefined)
    || t("component.workflow_run.continue_fields");
  const needsInput = detail ? detail.status === "completed" && detail.business_outcome === "needs_input" : businessOutcome === "needs_input";
  const actionLabel = t(needsInput ? "component.workflow_run.action.provide_input" : "component.workflow_run.action.retry");
  return <>
    <Button variant="primary" size={size} disabled={disabled} loading={busy} title={title} aria-haspopup="dialog" onClick={() => void openReview()}>
      {!busy && !needsInput && <IconRefresh size={16} aria-hidden="true" />}{actionLabel}
    </Button>
    <Modal
      open={open}
      onClose={() => { if (!busy) setOpen(false); }}
      title={actionLabel}
      maxWidth="760px"
      className="workflow-response-dialog"
    >
      {error && <p role="alert">{error}</p>}
      {busy && !detail && <p role="status">{t("component.workflow_run_history.loading_controls")}</p>}
      {detail && action && <p className="workflow-run-input-context">{needsInput ? detail.workflow_name : `${t("component.workflow_run.retry_from")}: ${retryStepName}`}</p>}
      {detail && action && <WorkflowRunIntervention
        run={{ id: detail.id, title: detail.workflow_name || "Workflow", status: detail.status === "failed" ? "failed" : "completed", businessOutcome: detail.business_outcome, currentNodeId: retryStep, error: detail.error,
          nodes: [{ id: retryStep, name: retryStepName, status: detail.status === "failed" ? "failed" : "completed" }] }}
        action={action}
        presentation="dialog"
        workspaceId={workspaceId}
        disabled={disabled}
        loading={busy}
        onResolve={resolve}
      />}
    </Modal>
  </>;
}
