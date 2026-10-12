import { useEffect, useId, useMemo, useRef, useState } from "react";
import { IconChevronDown, IconError, IconList, IconPause } from "../icons";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";
import Textarea from "../ui/Textarea";
import Modal from "../ui/Modal";
import WorkflowApprovalReview, {
  EditableWorkflowApprovalReview,
  workflowReviewIsEditable,
} from "./WorkflowApprovalReview";
import {
  WorkflowSchemaFields,
  parseWorkflowSchemaDraft,
  setWorkflowValueAtPath,
  workflowInputErrorPath,
  workflowSchemaDraft,
  type WorkflowInputSchema,
} from "./WorkflowSchemaFields";
import {
  actionIdentity,
  formatWorkflowError,
  interventionNodeIndex,
  workflowRunActionLabelKey,
  workflowRunInputFieldsLabelKey,
  workflowRetrySchemaIsCompatible,
  workflowRunNeedsInput,
  workflowInputGuidance,
  type WorkflowRunAction,
  type WorkflowRunActionInput,
  type WorkflowRunView,
} from "./workflowRunDisplay";

function schemaForStarterInput(input: WorkflowRunActionInput): WorkflowInputSchema {
  const base: WorkflowInputSchema = input.schema
    ? { ...input.schema }
    : input.type === "json"
      ? { type: "string", "x-workflow-type": "json", "x-ui": { control: "textarea", rows: 3 } }
      : { type: input.type || "string" };
  return {
    ...base,
    title: base.title || input.label,
    description: base.description || input.description,
    default: base.default ?? input.default,
    "x-ui": {
      ...(base["x-ui"] || {}),
      ...(input.hidden ? { hidden: true } : {}),
    },
  };
}

function actionSchema(action: WorkflowRunAction): WorkflowInputSchema {
  if (action.kind === "workflow_retry") {
    return action.editable_input_schema || { type: "object", properties: {} };
  }
  if (action.input_schema) return action.input_schema;
  const inputs = (action.inputs || []).filter((input) => Boolean(input?.key));
  return {
    type: "object",
    properties: Object.fromEntries(inputs.map((input) => [
      input.key,
      schemaForStarterInput(input),
    ])),
    required: inputs.filter((input) => input.required).map((input) => input.key),
    "x-ui": { order: inputs.map((input) => input.key) },
  };
}

function suppliedActionValues(
  action: WorkflowRunAction,
  schema: WorkflowInputSchema,
): Record<string, unknown> {
  const values = { ...(action.values || {}) };
  if (
    action.kind === "workflow_retry"
    && schema.properties?.retry_segment_ids
    && action.retry_segment_ids
  ) {
    values.retry_segment_ids = action.retry_segment_ids;
  }
  return values;
}

function actionOptionLabel(run: WorkflowRunView, option: string): string {
  const key = workflowRunActionLabelKey(run, option);
  return key ? t(key) : formatUserFacingLabel(option);
}

function isPreviewScaffold(line: string): boolean {
  return line.endsWith(":") || /^[\[\]{},]+$/.test(line);
}

function compactReasonPreview(value: unknown, fallback: string, truncationText: string): string {
  const candidates = Array.isArray(value) ? value : [value];
  for (const candidate of candidates) {
    const formatted = formatWorkflowError(candidate, truncationText);
    const lines = formatted.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
    const line = lines.find((line) => !isPreviewScaffold(line)) || lines[0];
    if (line) return line;
  }
  const fallbackLines = fallback.split(/\r?\n/).map((part) => part.trim()).filter(Boolean);
  return fallbackLines.find((line) => !isPreviewScaffold(line)) || fallbackLines[0] || "";
}

/** Approval choices that send the work back and therefore need instructions. */
const WORKFLOW_REVISION_CHOICES = new Set(["revise", "request_changes", "changes_requested"]);

export interface WorkflowRunInterventionProps {
  run: WorkflowRunView;
  action: WorkflowRunAction;
  onResolve: (
    choice: string,
    note?: string,
    payload?: Record<string, unknown>,
  ) => void | Promise<void>;
  disabled?: boolean;
  loading?: boolean;
  error?: unknown;
  historyHref?: string;
  historyReview?: unknown;
  historyReviewTitle?: string;
  historyReviewLoading?: boolean;
  historyReviewError?: unknown;
  workspaceId?: string;
  presentation?: "inline" | "dialog";
}

export default function WorkflowRunIntervention({
  run,
  action,
  onResolve,
  disabled = false,
  loading = false,
  error,
  historyHref,
  historyReview,
  historyReviewTitle,
  historyReviewLoading = false,
  historyReviewError,
  workspaceId,
  presentation = "inline",
}: WorkflowRunInterventionProps) {
  const stableActionIdentity = JSON.stringify([actionIdentity(action), action.runtime_approval_id]);
  const schema = useMemo(() => actionSchema(action), [stableActionIdentity]);
  const initialValues = useMemo(
    () => suppliedActionValues(action, schema),
    [stableActionIdentity],
  );
  const [values, setValues] = useState<unknown>(() => workflowSchemaDraft(schema, initialValues));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submittingOption, setSubmittingOption] = useState("");
  const [submissionError, setSubmissionError] = useState<unknown>();
  const [fileBusy, setFileBusy] = useState(false);
  const [reviewDraft, setReviewDraft] = useState<Record<string, unknown> | null>(null);
  const [reviewDraftIsValid, setReviewDraftIsValid] = useState(true);
  const needsInput = action.kind === "workflow_retry" && workflowRunNeedsInput({ ...run, action });
  const [inputDialogOpen, setInputDialogOpen] = useState(false);
  const [inputDialogBusy, setInputDialogBusy] = useState(false);
  const dialogInput = presentation === "dialog" && needsInput;
  const inputGuidance = needsInput ? workflowInputGuidance(action) : [];
  const dialogReview = presentation === "dialog" && action.kind === "workflow_approval";
  const reviewPrompt = typeof action.prompt === "string" ? action.prompt : action.description;
  const hasFields = Boolean(Object.keys(schema.properties || {}).length);
  const startsExpanded = dialogInput || dialogReview || (presentation === "dialog" && action.kind === "workflow_retry" && hasFields)
    || action.kind === "workflow_starter_input" || action.kind === "workflow_input";
  const [expanded, setExpanded] = useState(startsExpanded);
  const [validationFocusRequest, setValidationFocusRequest] = useState(0);
  // A revise decision is only useful with instructions, so it opens a note
  // box first and sends the note with the choice.
  const [revisionOption, setRevisionOption] = useState("");
  const [revisionNote, setRevisionNote] = useState("");
  const [revisionNoteError, setRevisionNoteError] = useState("");
  const detailsId = useId();
  const detailsBodyRef = useRef<HTMLDivElement>(null);
  const attentionNode = run.nodes[interventionNodeIndex(run, action)];
  const retrySchemaCompatible = action.kind !== "workflow_retry"
    || workflowRetrySchemaIsCompatible(action.editable_input_schema);
  const isFailed = !needsInput && (attentionNode?.status === "failed" || run.status === "failed");
  const displayedReview = action.review ?? historyReview;
  const editableReview = action.kind === "workflow_approval"
    && action.review_read_only !== true
    && workflowReviewIsEditable(displayedReview);
  const allowedOptions = (action.options || [])
    .map((option) => `${option}`.trim())
    .filter((option) => (
      Boolean(option)
      && !(editableReview && option.toLowerCase().replace(/[-\s]+/g, "_") === "revise")
    ));
  const preservedCount = Array.isArray(action.preserved_receipts)
    ? action.preserved_receipts.length
    : 0;
  const truncationText = t("component.workflow_run.error_truncated");
  const problem = action.observed_problem
    ?? action.prompt
    ?? action.description
    ?? attentionNode?.error
    ?? run.error;
  const reason = formatWorkflowError(
    problem,
    truncationText,
  );
  const visibleReason = dialogReview && displayedReview != null && reviewPrompt && reason.trim() === formatWorkflowError(reviewPrompt, truncationText).trim()
    ? ""
    : reason;
  const requiredChange = formatWorkflowError(action.required_change, truncationText);
  const reasonPreview = compactReasonPreview(
    problem,
    reason || requiredChange,
    truncationText,
  );
  const shownError = formatWorkflowError(submissionError ?? error, truncationText);
  const validationErrorCount = Object.values(errors).filter(Boolean).length;
  const previewText = validationErrorCount > 0
    ? t("component.workflow_run.input_validation_error", { count: validationErrorCount })
    : needsInput ? inputGuidance.join(" ") || t("component.workflow_run.input_guidance") : reasonPreview;
  const rootKey = action.kind === "workflow_retry" ? "variables" : "inputs";
  const reviewFromHistory = action.kind === "workflow_approval"
    && action.review_location === "workflow_history";
  const displayedReviewTitle = action.review != null
    ? action.review_title as string | undefined
    : historyReviewTitle || action.review_title as string | undefined;
  const hasReview = action.kind === "workflow_approval" && displayedReview != null;
  const reviewInHistory = reviewFromHistory && Boolean(historyHref);
  const busy = loading || Boolean(submittingOption) || fileBusy;
  const primaryOption = allowedOptions.find((option) => {
    const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
    return normalized !== "cancel" && normalized !== "revise";
  });
  const secondaryOptions = allowedOptions.filter((option) => option !== primaryOption);
  const hasDetails = Boolean(
    hasReview
    || visibleReason
    || requiredChange
    || preservedCount > 0
    || hasFields
    || shownError
    || reviewFromHistory
    || historyReviewLoading
    || Boolean(historyReviewError)
    || secondaryOptions.length > 0
  );

  const primaryActions = (
    <div className="workflow-run-intervention-summary-actions">
      {reviewInHistory && (
        <a className="approval-action-bar__review-link workflow-run-intervention-history-link" href={historyHref}>
          <IconList size={13} aria-hidden="true" />
          {t("component.workflow_run.review_in_history")}
        </a>
      )}
      {hasDetails && !dialogReview && !needsInput && (
        <Button size="sm" variant="outline" aria-expanded={expanded} aria-controls={detailsId} onClick={() => setExpanded((current) => !current)}>
          {t(expanded ? "component.workflow_run.hide_details" : "component.workflow_run.details")}
          <IconChevronDown size={13} className="workflow-run-intervention-chevron" aria-hidden="true" />
        </Button>
      )}
      {primaryOption && (
        <Button size={dialogReview || dialogInput ? "md" : "sm"} variant="primary" disabled={disabled || busy} loading={submittingOption === primaryOption} ariaBusy={submittingOption === primaryOption} aria-haspopup={needsInput && !dialogInput ? "dialog" : undefined} onClick={() => needsInput && !dialogInput ? setInputDialogOpen(true) : void resolve(primaryOption)}>
          {needsInput ? t(dialogInput ? "component.workflow_run.action.continue" : "component.workflow_run.action.provide_input") : actionOptionLabel(run, primaryOption)}
        </Button>
      )}
    </div>
  );

  const secondaryActions = secondaryOptions.map((option) => (
    <Button key={option} size={dialogReview ? "md" : "sm"} variant="outline" disabled={disabled || busy} loading={submittingOption === option} ariaBusy={submittingOption === option} onClick={() => void resolve(option)}>
      {actionOptionLabel(run, option)}
    </Button>
  ));

  useEffect(() => {
    setValues(workflowSchemaDraft(schema, initialValues));
    setErrors({});
    setSubmissionError(undefined);
    setSubmittingOption("");
    setFileBusy(false);
    setReviewDraft(null);
    setReviewDraftIsValid(true);
    setExpanded(startsExpanded);
    setValidationFocusRequest(0);
    setRevisionOption("");
    setRevisionNote("");
    setRevisionNoteError("");
    setInputDialogOpen(false);
    setInputDialogBusy(false);
  }, [stableActionIdentity]);

  useEffect(() => {
    if (!expanded || validationFocusRequest === 0) return;
    const detailsBody = detailsBodyRef.current;
    const firstInvalidField = detailsBody?.querySelector<HTMLElement>('[aria-invalid="true"]');
    if (!detailsBody || !firstInvalidField) return;
    let collapsedGroup = firstInvalidField.closest<HTMLDetailsElement>('details:not([open])');
    while (collapsedGroup) {
      collapsedGroup.open = true;
      collapsedGroup = collapsedGroup.parentElement?.closest<HTMLDetailsElement>(
        'details:not([open])',
      ) ?? null;
    }
    firstInvalidField.focus({ preventScroll: true });
    const detailsBounds = detailsBody.getBoundingClientRect();
    const fieldBounds = firstInvalidField.getBoundingClientRect();
    detailsBody.scrollTo({
      top: Math.max(0, detailsBody.scrollTop + fieldBounds.top - detailsBounds.top - 12),
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }, [expanded, validationFocusRequest]);

  const resolve = async (option: string, note?: string) => {
    if (disabled || busy) return;
    const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
    if (
      action.kind === "workflow_approval"
      && WORKFLOW_REVISION_CHOICES.has(normalized)
      && note === undefined
    ) {
      setRevisionOption(option);
      setRevisionNoteError("");
      setExpanded(true);
      return;
    }
    const submitsFields = (
      (action.kind === "workflow_starter_input" && normalized === "run")
    ) || (
      action.kind === "workflow_input" && normalized === "submit"
    ) || (
      action.kind === "workflow_input" && normalized === "respond"
    ) || (
      action.kind === "workflow_retry" && ["retry", "retry_now"].includes(normalized)
    );
    let payload: Record<string, unknown> | undefined;
    if (submitsFields) {
      const nextErrors: Record<string, string> = {};
      const parsed = parseWorkflowSchemaDraft(schema, values, rootKey, false, nextErrors);
      setErrors(nextErrors);
      if (Object.keys(nextErrors).length) {
        setExpanded(true);
        setValidationFocusRequest((current) => current + 1);
        return;
      }
      payload = action.kind === "workflow_retry"
        ? { variables: parsed }
        : action.kind === "workflow_input"
          ? { [String(action.response_variable || "inputs")]: parsed }
          : { inputs: parsed };
    } else if (
      action.kind === "workflow_approval"
      && editableReview
      && !["cancel", "reject", "revise"].includes(normalized)
    ) {
      if (!reviewDraftIsValid) {
        setExpanded(true);
        return;
      }
      payload = { review: reviewDraft ?? displayedReview };
    }
    setSubmittingOption(option);
    setSubmissionError(undefined);
    try {
      await onResolve(option, note, payload);
    } catch (resolveError) {
      setSubmissionError(resolveError);
      setExpanded(true);
    } finally {
      setSubmittingOption("");
    }
  };

  if (!retrySchemaCompatible) return null;

  return (
    <section
      className={`workflow-run-intervention${dialogReview ? " workflow-run-intervention--dialog-review" : ""}${needsInput ? " workflow-run-intervention--needs-input" : ""}`}
      aria-label={t("component.workflow_run.intervention")}
      aria-busy={busy}
    >
      <div className="workflow-run-intervention-summary" data-status={isFailed ? "failed" : "paused"}>
        <span className="workflow-run-intervention-kicker">
          <span className="workflow-run-intervention-icon" aria-hidden="true">
            {isFailed ? <IconError size={13} /> : <IconPause size={13} />}
          </span>
          {needsInput ? t("component.workflow_run.status.needs_input") : isFailed
            ? t("component.workflow_run.failed_node")
            : t("component.workflow_run.attention_required")}
        </span>

        {!dialogReview && !dialogInput && previewText && (
          <p
            className={`workflow-run-intervention-preview${validationErrorCount > 0 ? " is-error" : ""}`}
            role={validationErrorCount > 0 ? "alert" : undefined}
          >
            {previewText}
          </p>
        )}

        {!dialogReview && !dialogInput && primaryActions}
      </div>

      {expanded && (!needsInput || dialogInput) && (
        <div
          ref={detailsBodyRef}
          className="workflow-run-intervention-body"
          id={detailsId}
        >
          {hasReview && editableReview && (
            <EditableWorkflowApprovalReview
              key={stableActionIdentity}
              prompt={reviewPrompt}
              review={displayedReview}
              reviewTitle={displayedReviewTitle}
              disabled={disabled || busy}
              onDraftState={(draft, valid) => {
                setReviewDraft(draft);
                setReviewDraftIsValid(valid);
              }}
            />
          )}

          {hasReview && !editableReview && (
            <WorkflowApprovalReview
              review={displayedReview}
              reviewTitle={displayedReviewTitle}
              prompt={reviewPrompt}
            />
          )}

          {!hasReview && historyReviewLoading && (
            <div className="workspace-workflow-run-query-state" role="status">
              <LoadingSpinner size={13} />
              <span>{t("component.workflow_run.loading_review")}</span>
            </div>
          )}

          {!hasReview && Boolean(historyReviewError) && (
            <p className="workflow-run-intervention-error" role="alert">
              {t("component.workflow_run.review_load_error")}
            </p>
          )}

          {dialogInput && <div className="workflow-run-input-guidance">
            {inputGuidance.length > 0 ? inputGuidance.map((line, index) => <p key={index}>{line}</p>) : <p>{t("component.workflow_run.input_guidance")}</p>}
          </div>}

          {!needsInput && (visibleReason || requiredChange || preservedCount > 0) && (
            <dl className="workflow-run-intervention-details">
              {visibleReason && (
                <div>
                  <dt>{t("component.workflow_run.reason")}</dt>
                  <dd>{visibleReason}</dd>
                </div>
              )}
              {requiredChange && (
                <div>
                  <dt>{t("component.workflow_run.required_change")}</dt>
                  <dd>{requiredChange}</dd>
                </div>
              )}
              {preservedCount > 0 && (
                <div>
                  <dt>{t("component.workflow_run.preserved_artifacts")}</dt>
                  <dd className="mono">{preservedCount}</dd>
                </div>
              )}
            </dl>
          )}

          {hasFields && (
            <div className="workflow-run-intervention-fields">
              <h3>
                {needsInput ? t("component.workflow_run.continue_fields") : action.kind === "workflow_retry"
                  ? t(workflowRunInputFieldsLabelKey(action))
                  : t("component.workflow_run.input_fields")}
              </h3>
              <WorkflowSchemaFields
                rootKey={rootKey}
                schema={schema}
                value={values}
                errors={errors}
                disabled={disabled || busy}
                workspaceId={workspaceId}
                onFileBusyChange={setFileBusy}
                onChange={(path, nextValue) => {
                  setValues((current: unknown) => setWorkflowValueAtPath(current, path, nextValue));
                  setErrors((current) => ({
                    ...current,
                    [workflowInputErrorPath(rootKey, path)]: "",
                  }));
                }}
              />
            </div>
          )}

          {shownError && (
            <p className="workflow-run-intervention-error" role="alert">{shownError}</p>
          )}

          {revisionOption && (
            <div className="workflow-run-intervention-revision">
              <Textarea
                label="What should change?"
                value={revisionNote}
                rows={3}
                required
                disabled={disabled || busy}
                placeholder="For example: make the video brighter and show the product in the first second."
                error={revisionNoteError || undefined}
                onChange={(event) => {
                  setRevisionNote(event.target.value);
                  setRevisionNoteError("");
                }}
              />
              <div className="workflow-run-intervention-actions">
                <Button
                  size="sm"
                  disabled={disabled || busy}
                  loading={submittingOption === revisionOption}
                  onClick={() => {
                    const note = revisionNote.trim();
                    if (!note) {
                      setRevisionNoteError("Describe what should change before sending it back.");
                      return;
                    }
                    void resolve(revisionOption, note);
                  }}
                >
                  Send for revision
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={disabled || busy}
                  onClick={() => setRevisionOption("")}
                >
                  Back
                </Button>
              </div>
            </div>
          )}

          {!dialogReview && !dialogInput && !revisionOption && secondaryOptions.length > 0 && (
            <div className="workflow-run-intervention-actions">
              {secondaryActions}
            </div>
          )}
        </div>
      )}
      {(dialogReview || dialogInput) && <div className="workflow-run-intervention-dialog-actions">
        {!revisionOption && secondaryActions.length > 0 && <div className="workflow-run-intervention-actions">{secondaryActions}</div>}
        {primaryActions}
      </div>}
      {needsInput && presentation === "inline" && <Modal
        open={inputDialogOpen}
        onClose={() => { if (!inputDialogBusy && !loading) setInputDialogOpen(false); }}
        title={t("component.workflow_run.action.provide_input")}
        maxWidth="760px"
        className="workflow-response-dialog"
      >
        <p className="workflow-run-input-context">{run.title}</p>
        <WorkflowRunIntervention
          key={stableActionIdentity}
          run={run}
          action={action}
          presentation="dialog"
          workspaceId={workspaceId}
          disabled={disabled}
          loading={loading}
          error={error}
          onResolve={async (choice, note, payload) => {
            setInputDialogBusy(true);
            try {
              await onResolve(choice, note, payload);
              setInputDialogOpen(false);
            } finally {
              setInputDialogBusy(false);
            }
          }}
        />
      </Modal>}
    </section>
  );
}
