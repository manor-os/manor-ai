import { useId, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import Modal from "../ui/Modal";
import Textarea from "../ui/Textarea";
import { formatDateLong } from "../../lib/format";
import { IconRefresh } from "../icons";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import { WorkflowSchemaFields, parseWorkflowSchemaDraft, setWorkflowValueAtPath } from "./WorkflowSchemaFields";
import { workflowRetrySchemaIsCompatible, type WorkflowHistoryRun } from "./workflowRunDisplay";

export default function WorkflowRunRetryControl({ runId, workspaceId, bindingId, workflowId, businessOutcome, disabled, title, reviewRequired = false, size = "md", onRetried }: {
  runId: string;
  workspaceId?: string | null;
  bindingId?: string;
  workflowId?: string;
  businessOutcome?: string;
  disabled?: boolean;
  title?: string;
  reviewRequired?: boolean;
  size?: "sm" | "md" | "lg";
  onRetried: (run: WorkflowHistoryRun) => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<WorkflowHistoryRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const reviewId = useId();
  const [reviewNote, setReviewNote] = useState("");
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  const [reviewDecision, setReviewDecision] = useState<"not_applied" | "applied">("not_applied");
  const [resultDraft, setResultDraft] = useState<unknown>({});
  const [resultErrors, setResultErrors] = useState<Record<string, string>>({});

  const openReview = async () => {
    if (disabled || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setOpen(true);
    setDetail(null);
    setError("");
    setReviewNote("");
    setReviewConfirmed(false);
    setResultErrors({});
    try {
      const current = await api.workflows.getRun(runId, false, AbortSignal.timeout(15_000));
      const action = current.intervention;
      const effectReview = action?.kind === "workflow_effect_review"
        && action.options?.some((option: string) => option === "review" || option === "review_applied") && typeof action.effect_fingerprint === "string"
        && Boolean(action.step_id);
      const ordinaryRetry = action?.kind === "workflow_retry" && action.options?.includes("retry")
        && Boolean(action.retry_from_step_id || action.step_id)
        && workflowRetrySchemaIsCompatible(action.editable_input_schema);
      const retryable = ["failed", "cancelled"].includes(current.status) || (current.status === "completed"
        && ["needs_input", "revision_required"].includes(current.business_outcome));
      if (current.id !== runId || (current.workspace_id || "") !== (workspaceId || "")
        || (current.binding_id || "") !== (bindingId || "") || current.workflow_id !== workflowId
        || !retryable || !current.capabilities?.can_control
        || !action || (!effectReview && !ordinaryRetry)) {
        throw new Error(t("component.workspace_app_view.retry_unavailable"));
      }
      setDetail({ ...current, intervention: action
        ? { ...action, options: effectReview ? action.options?.filter((option: string) => option === "review" || option === "review_applied") : ordinaryRetry ? ["retry"] : [] }
        : null });
      setReviewDecision(action?.options?.includes("review") ? "not_applied" : "applied");
      // A new attested result has no implicit values. Untouched optional
      // output fields must stay absent, especially alternative failure fields.
      setResultDraft({});
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
    const effectReview = action.kind === "workflow_effect_review";
    if (!(effectReview ? action.options?.includes(reviewDecision === "applied" ? "review_applied" : "review") : action.kind === "workflow_retry" && action.options?.includes("retry"))) return;
    if (effectReview && (!reviewConfirmed || reviewNote.trim().length < 20 || reviewNote.trim().length > 2000)) return;
    let verifiedResult: Record<string, unknown> | undefined;
    if (effectReview && reviewDecision === "applied") {
      if (!action.result_schema) return;
      const errors: Record<string, string> = {};
      verifiedResult = parseWorkflowSchemaDraft(action.result_schema, resultDraft, "result", true, errors) as Record<string, unknown>;
      setResultErrors(errors);
      if (Object.keys(errors).length) return;
    }
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const retried = await api.workflows.retryRun(detail.id, {
        from_step_id: action.retry_from_step_id || action.step_id,
        variables: payload?.variables as Record<string, unknown> | undefined,
        execute: true,
        ...(effectReview ? { effect_review: {
          step_id: action.step_id!, effect_fingerprint: String(action.effect_fingerprint),
          decision: reviewDecision, confirmed: true as const, note: reviewNote.trim(),
          ...(verifiedResult ? { result: verifiedResult } : {}),
          ...(reviewDecision === "applied" ? { project_revisions: action.project_revisions } : {}),
        } } : {}),
      });
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
      for (const key of ["workflow-runs", "workspace-workflow-runs", "workflow-run", "workspace-chat-workflow-run-family", "workflow-run-history-detail", "workflow-run-history-family", "workflow-run-history-control", "chat-messages", "workspace-app-recent-run", "workspace-app-run", "workspace-app-run-detail"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      onRetried(retried);
      setOpen(false);
    } catch (cause) {
      setError(translateApiError(cause, t("component.workflow_run_history.retry_error")));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const action = detail?.intervention;
  const retryStep = action?.retry_from_step_id || action?.step_id || detail?.current_step_id || "";
  // The compact run omits the definition snapshot; the action names its own resume step.
  const retryStepName = action?.retry_from_step_name
    || detail?.definition_snapshot?.nodes?.find((node) => node.id === retryStep)?.name
    || (detail?.current_step_id === retryStep ? detail.current_step_name : undefined)
    || retryStep
    || t("component.workflow_run.continue_fields");
  const needsInput = detail ? detail.status === "completed" && detail.business_outcome === "needs_input" : businessOutcome === "needs_input";
  const effectReview = action?.kind === "workflow_effect_review" && action.options?.some((option) => option === "review" || option === "review_applied");
  const appliedReview = effectReview && reviewDecision === "applied";
  const blocked = Boolean(detail && !effectReview && !(action?.kind === "workflow_retry" && action.options?.includes("retry")));
  const actionLabel = t((detail ? effectReview : reviewRequired) ? "component.workflow_run_history.review_retry" : needsInput ? "component.workflow_run.action.provide_input" : "component.workflow_run.action.retry");
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
      {detail && <p className="workflow-run-input-context">{needsInput ? detail.workflow_name : `${t("component.workflow_run.retry_from")}: ${retryStepName}`}</p>}
      {blocked ? <div className="workflow-run-intervention-fields">
        <p role="status">{t(action?.blocked_reason === "external_effect_requires_reconciliation"
          ? "component.workflow_run_history.external_effect_blocked"
          : "component.workspace_app_view.data_retry_review_required")}</p>
        {typeof action?.deadline_at === "string" && <p>{t("component.workflow_run_history.effect_review_deadline", { time: formatDateLong(action.deadline_at) })}</p>}
        <p>{t("component.workflow_run_history.retry_blocked_help")}</p>
        <div className="workflow-run-intervention-actions">
          <Button variant="ghost" onClick={() => setOpen(false)}>{t("action.close")}</Button>
          <Button variant="outline" loading={busy} onClick={() => void openReview()}>{t("component.workflow_run_history.retry_recheck")}</Button>
          <Button disabled>{t("component.workflow_run.action.retry")}</Button>
        </div>
      </div> : detail && action && effectReview ? <form className="workflow-run-intervention-fields" onSubmit={(event) => { event.preventDefault(); void resolve("retry"); }}>
        {action.options?.includes("review") && action.options?.includes("review_applied") && <label>
          {t("component.workflow_run_history.effect_review_result")}
          <select value={reviewDecision} disabled={busy} onChange={(event) => { setReviewDecision(event.target.value as "not_applied" | "applied"); setReviewConfirmed(false); }}>
            <option value="not_applied">{t("component.workflow_run_history.effect_not_applied")}</option>
            <option value="applied">{t("component.workflow_run_history.effect_applied")}</option>
          </select>
        </label>}
        <p>{t(appliedReview ? "component.workflow_run_history.effect_applied_help" : "component.workflow_run_history.effect_review_help")}</p>
        {appliedReview && Object.keys(action.project_revisions || {}).length > 0 && <p>{t("component.workflow_run_history.effect_project_refresh")}</p>}
        {typeof action.deadline_at === "string" && <p>{t("component.workflow_run_history.effect_review_deadline", { time: formatDateLong(action.deadline_at) })}</p>}
        {appliedReview && action.result_schema && <WorkflowSchemaFields
          rootKey="result" schema={action.result_schema} value={resultDraft} errors={resultErrors}
          disabled={busy} workspaceId={workspaceId || undefined}
          onChange={(path, value) => setResultDraft((current: unknown) => setWorkflowValueAtPath(current, path, value))}
        />}
        <Textarea id={`${reviewId}-note`} label={t("component.workflow_run_history.effect_review_note")} value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} rows={3} required disabled={busy} />
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={reviewConfirmed} onChange={(event) => setReviewConfirmed(event.target.checked)} disabled={busy} />
          {t(appliedReview ? "component.workflow_run_history.effect_applied_confirm" : "component.workflow_run_history.effect_review_confirm")}
        </label>
        <div className="workflow-run-intervention-actions">
          <Button variant="ghost" disabled={busy} onClick={() => setOpen(false)}>{t("common.cancel")}</Button>
          <Button type="submit" loading={busy} disabled={!reviewConfirmed || reviewNote.trim().length < 20 || reviewNote.trim().length > 2000}>{t(appliedReview ? "component.workflow_run_history.effect_applied_submit" : "component.workflow_run_history.effect_review_submit")}</Button>
        </div>
      </form> : detail && action && <WorkflowRunIntervention
        run={{ id: detail.id, title: detail.workflow_name || "Workflow", status: detail.status === "failed" || detail.status === "cancelled" ? detail.status : "completed", businessOutcome: detail.business_outcome, currentNodeId: retryStep, error: detail.error,
          nodes: [{ id: retryStep, name: retryStepName, status: detail.status === "failed" || detail.status === "cancelled" ? detail.status : "completed" }] }}
        action={action}
        presentation="dialog"
        workspaceId={workspaceId || undefined}
        disabled={disabled}
        loading={busy}
        onResolve={resolve}
      />}
    </Modal>
  </>;
}
