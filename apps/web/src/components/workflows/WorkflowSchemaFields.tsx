import { type ReactNode, useId, useState } from "react";
import { t } from "../../lib/i18n";
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
  workflowSchemaType,
  workflowValueAtPath,
  type WorkflowInputSchema,
} from "./workflowSchema";

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
  path?: string[];
  onChange: (path: string[], value: unknown) => void;
}

interface WorkflowSchemaFieldTreeProps extends WorkflowSchemaFieldsProps {
  idNamespace: string;
}

interface WorkflowSchemaCollapsibleGroupProps {
  label: string;
  initiallyOpen: boolean;
  children: ReactNode;
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
  path = [],
  idNamespace,
  onChange,
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
              path={childPath}
              idNamespace={idNamespace}
              onChange={onChange}
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
        return (
          <label key={key} className="workflow-starter-input-field" htmlFor={fieldIds.fieldId}>
            <span>{label}{isRequired ? " *" : ""}</span>
            {child.description && (
              <small id={fieldIds.helpId} className="workflow-starter-input-help">
                {child.description}
              </small>
            )}
            {child.enum ? (
              <select
                id={fieldIds.fieldId}
                value={String(childValue ?? "")}
                onChange={(event) => onChange(childPath, event.target.value)}
                disabled={disabled}
                required={isRequired}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
              >
                <option value="" disabled>{t("component.chat_action_card.select")}</option>
                {child.enum.map((option) => (
                  <option key={String(option)} value={String(option)}>{humanizeKey(String(option))}</option>
                ))}
              </select>
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
