import { type ReactNode, useEffect, useId, useState } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Select from "../ui/Select";
import WorkflowWaitDocumentField from "./WorkflowWaitDocumentField";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import {
  parseWorkflowSchemaDraft as parseWorkflowSchemaDraftPure,
  setWorkflowValueAtPath,
  visibleWorkflowSchemaFieldCount,
  workflowInputErrorPath,
  workflowSchemaDescribedBy,
  workflowSchemaDraft,
  workflowSchemaEntries,
  workflowSchemaFieldIds,
  workflowSchemaOptionLabel,
  workflowSchemaSelectValue,
  workflowSchemaType,
  workflowValueAtPath,
  type WorkflowInputSchema,
} from "./workflowSchema";
import WorkflowKnowledgeFileField from "./WorkflowKnowledgeFileField";

export {
  setWorkflowValueAtPath,
  visibleWorkflowSchemaFieldCount,
  workflowInputErrorPath,
  workflowSchemaDescribedBy,
  workflowSchemaDraft,
  workflowSchemaEntries,
  workflowSchemaFieldIds,
  workflowSchemaType,
  type WorkflowInputSchema,
} from "./workflowSchema";

function humanizeKey(value: string): string {
  return formatUserFacingLabel(value || "workspace changes");
}

export function parseWorkflowSchemaDraft(
  schema: WorkflowInputSchema,
  draft: unknown,
  path: string,
  required: boolean,
  errors: Record<string, string>,
): unknown {
  return parseWorkflowSchemaDraftPure(schema, draft, path, required, errors, {
    required: t("component.workspace_chat.workflow_input_required"),
    invalidJson: t("component.workspace_chat.workflow_input_invalid_json"),
    invalidUrl: t("component.workspace_chat.workflow_input_invalid_url"),
    invalidFormat: t("component.workspace_chat.workflow_input_invalid_format"),
    invalidNumber: t("component.workspace_chat.workflow_input_invalid_number"),
    minLength: (minimum) => t("component.workspace_chat.workflow_input_min_length", { minimum }),
    maxLength: (maximum) => t("component.workspace_chat.workflow_input_max_length", { maximum }),
    minItems: (minimum) => t("component.workspace_chat.workflow_input_min_items", { minimum }),
    maxItems: (maximum) => t("component.workspace_chat.workflow_input_max_items", { maximum }),
    uniqueItems: t("component.workspace_chat.workflow_input_unique_items"),
    minimum: (minimum) => t("component.workspace_chat.workflow_input_minimum", { minimum }),
    maximum: (maximum) => t("component.workspace_chat.workflow_input_maximum", { maximum }),
    exclusiveMinimum: (minimum) => t(
      "component.workspace_chat.workflow_input_exclusive_minimum",
      { minimum },
    ),
    exclusiveMaximum: (maximum) => t(
      "component.workspace_chat.workflow_input_exclusive_maximum",
      { maximum },
    ),
  });
}

interface WorkflowSchemaFieldsProps {
  rootKey: string;
  schema: WorkflowInputSchema;
  value: unknown;
  errors: Record<string, string>;
  disabled?: boolean;
  workspaceId?: string;
  path?: string[];
  onChange: (path: string[], value: unknown) => void;
  onFileBusyChange?: (busy: boolean) => void;
}

interface WorkflowSchemaFieldTreeProps extends WorkflowSchemaFieldsProps {
  idNamespace: string;
}

interface WorkflowSchemaCollapsibleGroupProps {
  label: string;
  initiallyOpen: boolean;
  children: ReactNode;
}

interface WorkflowProjectCandidate {
  id: string;
  current_stage: string;
  title: string;
  youtube_video_id?: string | null;
  created_at: string;
}

function WorkflowProjectSelect({
  id,
  schema,
  value,
  workspaceId,
  disabled,
  required,
  describedBy,
  invalid,
  onChange,
}: {
  id: string;
  schema: WorkflowInputSchema;
  value: unknown;
  workspaceId?: string;
  disabled?: boolean;
  required: boolean;
  describedBy?: string;
  invalid: boolean;
  onChange: (value: string) => void;
}) {
  const ui = schema["x-ui"] || {};
  const projectType = String(ui.project_type || "").trim();
  const stages = ui.allowed_stages || [];
  const [candidates, setCandidates] = useState<WorkflowProjectCandidate[]>([]);
  const [loading, setLoading] = useState(Boolean(workspaceId));
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    if (!workspaceId || !projectType || stages.length === 0) {
      setCandidates([]);
      setLoading(false);
      return () => { active = false; };
    }
    setLoading(true);
    setFailed(false);
    setCandidates([]);
    void api.workflows.listProjectCandidates({
      workspace_id: workspaceId,
      project_type: projectType,
      stages,
    }).then((items) => {
      if (active) setCandidates(items);
    }).catch(() => {
      if (active) {
        setCandidates([]);
        setFailed(true);
      }
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [workspaceId, projectType, stages.join("\u0000")]);

  if (!workspaceId) {
    return (
      <input
        id={id}
        type="text"
        value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        required={required}
        aria-required={required}
        aria-invalid={invalid}
        aria-describedby={describedBy}
      />
    );
  }

  const selected = String(value ?? "");
  const selectedIsMissing = Boolean(selected && !candidates.some((item) => item.id === selected));
  const groups = stages.map((stage) => ({
    stage,
    candidates: candidates.filter((candidate) => candidate.current_stage === stage),
  })).filter((group) => group.candidates.length > 0);
  return (
    <>
      <Select
        id={id}
        role="combobox"
        value={selected}
        onChange={onChange}
        disabled={disabled || loading || failed}
        ariaRequired={required}
        ariaInvalid={invalid || failed}
        ariaDescribedBy={describedBy}
        ariaLabel={schema.title || humanizeKey(projectType)}
        placeholder={loading
          ? t("component.workspace_chat.workflow_project_loading")
          : t("component.chat_action_card.select")}
        filterable
        options={[
          ...(selectedIsMissing ? [{ value: selected, label: selected }] : []),
          ...groups.flatMap((group) => group.candidates.map((candidate) => ({
            value: candidate.id,
            label: `${candidate.title}${candidate.youtube_video_id ? ` · ${candidate.youtube_video_id}` : ""} · ${new Date(candidate.created_at).toLocaleDateString()}`,
            description: t(group.stage === "youtube_saved"
              ? "component.workspace_chat.workflow_project_published"
              : "component.workspace_chat.workflow_project_ready"),
          }))),
        ]}
      />
      {failed && (
        <small role="alert">{t("component.workspace_chat.workflow_project_load_failed")}</small>
      )}
      {!loading && !failed && candidates.length === 0 && (
        <small>{t("component.workspace_chat.workflow_project_empty")}</small>
      )}
    </>
  );
}

function WorkflowSchemaCollapsibleGroup({
  label,
  initiallyOpen,
  children,
}: WorkflowSchemaCollapsibleGroupProps) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <details
      className="workflow-starter-input-group"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>{label}</summary>
      {children}
    </details>
  );
}

function WorkflowSchemaFieldTree({
  rootKey,
  schema,
  value,
  errors,
  disabled,
  workspaceId,
  path = [],
  idNamespace,
  onChange,
  onFileBusyChange,
}: WorkflowSchemaFieldTreeProps) {
  const required = new Set(schema.required || []);
  return (
    <div className="workflow-starter-input-schema-fields">
      {workflowSchemaEntries(schema).map(([key, child]) => {
        if (child["x-ui"]?.hidden) return null;
        const childPath = [...path, key];
        const schemaType = workflowSchemaType(child);
        const fieldIds = workflowSchemaFieldIds(idNamespace, rootKey, childPath);
        const label = child.title || humanizeKey(key);
        const childValue = workflowValueAtPath(value, childPath);
        const error = errors[workflowInputErrorPath(rootKey, childPath)];
        const isRequired = required.has(key);
        const describedBy = workflowSchemaDescribedBy(
          fieldIds,
          Boolean(child.description),
          Boolean(error),
        );
        if (schemaType === "object") {
          const nested = (
            <WorkflowSchemaFieldTree
              rootKey={rootKey}
              schema={child}
              value={value}
              errors={errors}
              disabled={disabled}
              workspaceId={workspaceId}
              path={childPath}
              idNamespace={idNamespace}
              onChange={onChange}
              onFileBusyChange={onFileBusyChange}
            />
          );
          return child["x-ui"]?.collapsible ? (
            <WorkflowSchemaCollapsibleGroup
              key={key}
              label={label}
              initiallyOpen={!child["x-ui"]?.collapsed}
            >
              {nested}
            </WorkflowSchemaCollapsibleGroup>
          ) : (
            <section key={key} className="workflow-starter-input-group">
              <h4>{label}</h4>
              {nested}
            </section>
          );
        }
        const ui = child["x-ui"] || {};
        if (ui.control === "knowledge_file" && ui.file_kind) {
          return (
            <div key={key} className="workflow-starter-input-field workflow-starter-input-field--files" role="group" aria-labelledby={`${fieldIds.fieldId}-label`}>
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small className="workflow-starter-input-help">{child.description}</small>}
              <WorkflowKnowledgeFileField
                value={String(childValue || "")}
                kind={ui.file_kind}
                disabled={disabled}
                onChange={(nextValue) => onChange(childPath, nextValue)}
                onBusyChange={onFileBusyChange}
              />
              {error && <small role="alert">{error}</small>}
            </div>
          );
        }
        if (ui.control === "documents") {
          return (
            <div
              key={key}
              className="workflow-starter-input-field workflow-starter-input-field--files"
              role="group"
              aria-labelledby={`${fieldIds.fieldId}-label`}
              aria-describedby={describedBy}
              aria-invalid={Boolean(error)}
            >
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <WorkflowWaitDocumentField
                id={fieldIds.fieldId}
                value={childValue}
                workspaceId={workspaceId}
                disabled={disabled}
                onChange={(nextValue) => onChange(childPath, nextValue)}
              />
              {error && <small id={fieldIds.errorId} role="alert">
                {isRequired && (!Array.isArray(childValue) || childValue.length === 0)
                  ? t("component.workflow_wait_document.required")
                  : error}
              </small>}
            </div>
          );
        }
        return (
          <label key={key} className="workflow-starter-input-field" htmlFor={fieldIds.fieldId}>
            <span>{label}{isRequired ? " *" : ""}</span>
            {child.description && (
              <small id={fieldIds.helpId} className="workflow-starter-input-help">
                {child.description}
              </small>
            )}
            {ui.control === "workflow_project_select" ? (
              <WorkflowProjectSelect
                id={fieldIds.fieldId}
                schema={child}
                value={childValue}
                workspaceId={workspaceId}
                disabled={disabled}
                required={isRequired}
                describedBy={describedBy}
                invalid={Boolean(error)}
                onChange={(nextValue) => onChange(childPath, nextValue)}
              />
            ) : child.enum ? (
              <Select
                id={fieldIds.fieldId}
                role="combobox"
                value={workflowSchemaSelectValue(child, childValue)}
                onChange={(nextValue) => onChange(childPath, nextValue)}
                disabled={disabled}
                ariaLabel={`${label}${isRequired ? " *" : ""}`}
                ariaRequired={isRequired}
                ariaInvalid={Boolean(error)}
                ariaDescribedBy={describedBy}
                placeholder={t("component.chat_action_card.select")}
                options={child.enum.map((option) => ({ value: String(option), label: workflowSchemaOptionLabel(child, option, humanizeKey) }))}
              />
            ) : schemaType === "boolean" ? (
              <input
                id={fieldIds.fieldId}
                type="checkbox"
                checked={Boolean(childValue)}
                onChange={(event) => onChange(childPath, event.target.checked)}
                disabled={disabled}
                required={isRequired}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
                className="workflow-starter-input-checkbox"
              />
            ) : schemaType === "array" || ui.control === "textarea" || ui.control === "line_list" ? (
              <textarea
                id={fieldIds.fieldId}
                value={String(childValue ?? "")}
                onChange={(event) => onChange(childPath, event.target.value)}
                rows={ui.rows || (schemaType === "array" ? 4 : 3)}
                disabled={disabled}
                required={isRequired}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
              />
            ) : schemaType === "number" || schemaType === "integer" ? (
              <input
                id={fieldIds.fieldId}
                type="number"
                value={String(childValue ?? "")}
                min={child.minimum}
                max={child.maximum}
                step={schemaType === "integer" ? 1 : "any"}
                onChange={(event) => onChange(childPath, event.target.value)}
                disabled={disabled}
                required={isRequired}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
              />
            ) : (
              <input
                id={fieldIds.fieldId}
                type={child.format === "uri" ? "url" : "text"}
                value={String(childValue ?? "")}
                onChange={(event) => onChange(childPath, event.target.value)}
                disabled={disabled}
                required={isRequired}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
              />
            )}
            {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
          </label>
        );
      })}
    </div>
  );
}

export function WorkflowSchemaFields(props: WorkflowSchemaFieldsProps) {
  const idNamespace = useId();
  return <WorkflowSchemaFieldTree {...props} idNamespace={idNamespace} />;
}
