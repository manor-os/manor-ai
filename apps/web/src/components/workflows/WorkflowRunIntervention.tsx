import { useEffect, useId, useMemo, useState } from "react";
import { IconChevronDown, IconError, IconPause } from "../icons";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import Button from "../ui/Button";
import WorkflowApprovalReview from "./WorkflowApprovalReview";
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
}

export default function WorkflowRunIntervention({
  run,
  action,
  onResolve,
  disabled = false,
  loading = false,
  error,
}: WorkflowRunInterventionProps) {
  const stableActionIdentity = actionIdentity(action);
  const schema = useMemo(() => actionSchema(action), [stableActionIdentity]);
  const initialValues = useMemo(
    () => suppliedActionValues(action, schema),
    [stableActionIdentity],
  );
  const [values, setValues] = useState<unknown>(() => workflowSchemaDraft(schema, initialValues));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submittingOption, setSubmittingOption] = useState("");
  const [submissionError, setSubmissionError] = useState<unknown>();
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const attentionNode = run.nodes[interventionNodeIndex(run, action)];
  const retrySchemaCompatible = action.kind !== "workflow_retry"
    || workflowRetrySchemaIsCompatible(action.editable_input_schema);
  const isFailed = attentionNode?.status === "failed" || run.status === "failed";
  const allowedOptions = (action.options || [])
    .map((option) => `${option}`.trim())
    .filter(Boolean);
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
  const requiredChange = formatWorkflowError(action.required_change, truncationText);
  const reasonPreview = compactReasonPreview(
    problem,
    reason || requiredChange,
    truncationText,
  );
  const shownError = formatWorkflowError(submissionError ?? error, truncationText);
  const rootKey = action.kind === "workflow_retry" ? "variables" : "inputs";
  const hasFields = Boolean(Object.keys(schema.properties || {}).length);
  const hasReview = action.kind === "workflow_approval" && action.review != null;
  const busy = loading || Boolean(submittingOption);
  const primaryOption = allowedOptions.find((option) => {
    const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
    return normalized !== "cancel" && normalized !== "revise";
  });
  const secondaryOptions = allowedOptions.filter((option) => option !== primaryOption);
  const hasDetails = Boolean(
    hasReview
    || reason
    || requiredChange
    || preservedCount > 0
    || hasFields
    || shownError
    || secondaryOptions.length > 0
  );

  useEffect(() => {
    setValues(workflowSchemaDraft(schema, initialValues));
    setErrors({});
    setSubmissionError(undefined);
    setSubmittingOption("");
    setExpanded(false);
  }, [stableActionIdentity]);

  const resolve = async (option: string) => {
    const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
    const submitsFields = (
      action.kind === "workflow_starter_input" && normalized === "run"
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
        return;
      }
      payload = action.kind === "workflow_retry"
        ? { variables: parsed }
        : { inputs: parsed };
    }
    setSubmittingOption(option);
    setSubmissionError(undefined);
    try {
      await onResolve(option, undefined, payload);
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
      className="workflow-run-intervention"
      aria-label={t("component.workflow_run.intervention")}
      aria-busy={busy}
    >
      <div className="workflow-run-intervention-summary" data-status={isFailed ? "failed" : "paused"}>
        <span className="workflow-run-intervention-kicker">
          <span className="workflow-run-intervention-icon" aria-hidden="true">
            {isFailed ? <IconError size={13} /> : <IconPause size={13} />}
          </span>
          {isFailed
            ? t("component.workflow_run.failed_node")
            : t("component.workflow_run.attention_required")}
        </span>

        {reasonPreview && (
          <p className="workflow-run-intervention-preview">{reasonPreview}</p>
        )}

        <div className="workflow-run-intervention-summary-actions">
          {hasDetails && (
            <Button
              size="sm"
              variant="outline"
              aria-expanded={expanded}
              aria-controls={detailsId}
              onClick={() => setExpanded((current) => !current)}
            >
              {expanded
                ? t("component.workflow_run.hide_details")
                : t("component.workflow_run.details")}
              <IconChevronDown
                size={13}
                className="workflow-run-intervention-chevron"
                aria-hidden="true"
              />
            </Button>
          )}
          {primaryOption && (
            <Button
              size="sm"
              variant="primary"
              disabled={disabled || busy}
              loading={submittingOption === primaryOption}
              onClick={() => void resolve(primaryOption)}
            >
              {actionOptionLabel(run, primaryOption)}
            </Button>
          )}
        </div>
      </div>

      {expanded && (
        <div className="workflow-run-intervention-body" id={detailsId}>
          {hasReview && (
            <WorkflowApprovalReview
              review={action.review}
              reviewTitle={action.review_title as string | undefined}
            />
          )}

          {(reason || requiredChange || preservedCount > 0) && (
            <dl className="workflow-run-intervention-details">
              {reason && (
                <div>
                  <dt>{t("component.workflow_run.reason")}</dt>
                  <dd>{reason}</dd>
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
                {action.kind === "workflow_retry"
                  ? t(workflowRunInputFieldsLabelKey(action))
                  : t("component.workflow_run.input_fields")}
              </h3>
              <WorkflowSchemaFields
                rootKey={rootKey}
                schema={schema}
                value={values}
                errors={errors}
                disabled={disabled || busy}
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

          {secondaryOptions.length > 0 && (
            <div className="workflow-run-intervention-actions">
              {secondaryOptions.map((option) => (
                <Button
                  key={option}
                  size="sm"
                  variant="outline"
                  disabled={disabled || busy}
                  loading={submittingOption === option}
                  onClick={() => void resolve(option)}
                >
                  {actionOptionLabel(run, option)}
                </Button>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
