import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { t } from "../../lib/i18n";
import { integrationCatalogQueryOptions } from "../../lib/integrationCatalog";
import IntegrationConnectRow from "../integrations/IntegrationConnectRow";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import { WORKFLOW_REVIEW_FRAMES_DIALOG_WIDTH, workflowReviewHasFrames } from "./WorkflowApprovalReview";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import type { WorkflowHistoryRun, WorkflowRunAction } from "./workflowRunDisplay";
import { PageLoading } from "../ui/Skeleton";
import { loadWaitingRun } from "./workflowWaitingRun";

const RESUMED_RUN_QUERY_KEYS = ["workflow-run", "workflow-runs", "workspace-workflow-runs", "workflow-run-history-detail", "workflow-run-history-family", "workflow-run-history-control", "workflow-run-review", "workspace-chat-workflow-run-family", "workspace-chat", "workspace-chat-messages", "chat-messages", "workspace-app-data", "workspace-app-data-contexts", "workspace-app-section", "workspace-app-recent-run"];

/** Review the current frozen wait before sending a decision to normal resume.
 *
 * A run waiting for an integration (``action.connection_provider``) is
 * answered by connecting it over this page; the run then continues from its
 * step. */
export default function WorkflowRunResumeControl({ runId, action: waitAction, disabled, onResumed, label, variant = "outline" }: {
  runId: string;
  action?: WorkflowRunAction | null;
  disabled?: boolean;
  /** The button's words where the wait is known ("Review"); defaults to the generic request. */
  label?: string;
  variant?: "outline" | "primary";
  onResumed?: (run: WorkflowHistoryRun) => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<WorkflowHistoryRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);

  const connectionProvider = String(waitAction?.connection_provider || "");
  const connectionLabel = String(waitAction?.connection_label || connectionProvider);
  const catalog = useQuery(integrationCatalogQueryOptions(Boolean(connectionProvider)));
  const connectionReady = Boolean(catalog.data?.find((server) => server.server_key === connectionProvider)?.agent_can_use);
  const continueRun = async () => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const waitingRunId = typeof waitAction?.workflow_run_id === "string" && waitAction.workflow_run_id.trim()
        ? waitAction.workflow_run_id.trim()
        : runId;
      let resumed = await api.workflows.resumeRun(waitingRunId, undefined, waitAction?.step_id);
      if (waitingRunId !== runId) resumed = await api.workflows.getRun(runId);
      for (const key of RESUMED_RUN_QUERY_KEYS) void queryClient.invalidateQueries({ queryKey: [key] });
      onResumed?.(resumed);
    } catch (cause) {
      setError(translateApiError(cause, t("component.workflow_run_history.control_load_error")));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  if (connectionProvider && !connectionReady) {
    // Connected with the same row as everywhere; the run then continues from its waiting step.
    return <>
      {error && <span role="alert">{error}</span>}
      <IntegrationConnectRow as="div" requirement={{ kind: "integration", provider: connectionProvider, label: connectionLabel }}
        hint={t("component.integration_connect.run_waiting", { provider: connectionLabel, step: String(waitAction?.title || waitAction?.step_id || "") })}
        onConnected={() => void continueRun()} />
    </>;
  }
  if (connectionProvider) {
    return <>
      {error && <span role="alert">{error}</span>}
      <Button
        size="sm"
        variant="primary"
        disabled={disabled || busy}
        loading={busy}
        title={t("component.integration_connect.run_waiting", { provider: connectionLabel, step: String(waitAction?.title || waitAction?.step_id || "") })}
        onClick={() => void continueRun()}
      >
        {t("recommendation.label.integration.continue")}
      </Button>
    </>;
  }

  const openReview = async () => {
    if (disabled || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setOpen(true);
    setDetail(null);
    setError("");
    try {
      // A retried result's first run id still answers its latest attempt's request.
      const current = await loadWaitingRun<WorkflowHistoryRun>(runId, (id, full = true) => api.workflows.getRun(id, full));
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
        resumed = await api.workflows.resolveRuntimeApproval(approvalRunId, action.runtime_approval_id, choice, payload?.review);
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
        const approvalResponse = note || payload?.review ? {
          choice,
          ...(note ? { note } : {}),
          ...(payload?.review ? { review: payload.review } : {}),
        } : choice;
        const variables = action.kind === "workflow_approval"
          ? { [String(action.response_variable)]: approvalResponse }
          : action.kind === "workflow_input"
            ? payload
          : undefined;
        // A run paused on a nested Flow shows the decision its descendant waits on;
        // that descendant is the run answered, and this run is read again after.
        const waitingRunId = typeof action.workflow_run_id === "string" && action.workflow_run_id.trim()
          ? action.workflow_run_id.trim()
          : detail.id;
        resumed = await api.workflows.resumeRun(waitingRunId, variables, action.step_id);
        if (waitingRunId !== detail.id) resumed = await api.workflows.getRun(detail.id);
      }
      for (const key of ["workflow-run", "workflow-runs", "workspace-workflow-runs", "workflow-run-history-detail", "workflow-run-history-family", "workflow-run-history-control", "workflow-run-review", "workspace-chat-workflow-run-family", "workspace-chat", "workspace-chat-messages", "chat-messages", "workspace-app-data", "workspace-app-data-contexts", "workspace-app-section", "workspace-app-recent-run"]) {
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
    <Button size="sm" variant={variant} disabled={disabled || busy} onClick={() => void openReview()}>{label || t("component.workflow_review.open_request")}</Button>
    <Modal
      open={open}
      onClose={() => { if (!busy) setOpen(false); }}
      title={detail?.resume_action?.kind === "workflow_approval" ? t("component.workflow_review.dialog_title") : t("component.workflow_review.response_title")}
      maxWidth={workflowReviewHasFrames(detail?.resume_action?.review) ? WORKFLOW_REVIEW_FRAMES_DIALOG_WIDTH : "760px"}
      className="workflow-response-dialog"
    >
      {error && <p role="alert">{error}</p>}
      {busy && !detail && <PageLoading label={t("ui.loading_current_request")} minHeight={96} />}
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
