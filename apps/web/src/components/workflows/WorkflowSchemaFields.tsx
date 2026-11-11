import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Select from "../ui/Select";
import DateTimePicker from "../ui/DateTimePicker";
import WorkflowWaitDocumentField from "./WorkflowWaitDocumentField";
import IntegrationLogo from "../IntegrationLogo";
import WorkflowAudioClipField, { audioClipSource } from "./WorkflowAudioClipField";
import { formatUserFacingLabel } from "../../lib/taskDisplay";
import {
  nextPresetPlaceholder,
  presetPlaceholderAt,
  presetPlaceholderTokens,
  parseWorkflowSchemaDraft as parseWorkflowSchemaDraftPure,
  setWorkflowValueAtPath,
  toggleWorkflowSchemaChoice,
  visibleWorkflowSchemaFieldCount,
  workflowAspectRatio,
  workflowInputErrorPath,
  workflowSchemaPreset,
  workflowSchemaChoices,
  workflowSchemaControl,
  workflowSchemaPictureChoices,
  workflowSchemaSuggestions,
  workflowSchemaChosen,
  workflowSchemaDescribedBy,
  workflowSchemaDraft as workflowSchemaDraftPure,
  workflowSchemaDateTimeValue,
  workflowSchemaDateTimeIso,
  workflowSchemaTimeZones,
  workflowSchemaCompactDocuments,
  workflowSchemaEntries,
  workflowSchemaFieldIds,
  workflowSchemaFieldShown,
  workflowSchemaHiddenResets,
  workflowSchemaOptionLabel,
  workflowSchemaProjectSelect,
  workflowSchemaRecordSelect,
  workflowSchemaSelectValue,
  workflowSchemaType,
  workflowValueAtPath,
  type WorkflowInputSchema,
} from "./workflowSchema";
import WorkflowKnowledgeFileField from "./WorkflowKnowledgeFileField";
import WorkflowRecordSelect, { WorkflowRecordChoices } from "./WorkflowRecordSelect";
import { getPreferredTimeZone } from "../../lib/format";
import WorkflowInputSuggestions from "./WorkflowInputSuggestions";

export {
  setWorkflowValueAtPath,
  visibleWorkflowSchemaFieldCount,
  workflowInputErrorPath,
  workflowSchemaDescribedBy,
  workflowSchemaEntries,
  workflowSchemaFieldIds,
  workflowSchemaFieldShown,
  workflowSchemaType,
  type WorkflowInputSchema,
} from "./workflowSchema";
import SuggestionGallery from "./SuggestionGallery";
import ConfiguredImage from "../ui/ConfiguredImage";
import {
  AddressField,
  ColorField,
  DateRangeField,
  LineItemsField,
  RatingField,
  RichTextField,
  SignatureField,
  TagsField,
} from "./WorkflowFormControlFields";

export function workflowSchemaDraft(schema: WorkflowInputSchema, supplied: unknown): unknown {
  return workflowSchemaDraftPure(schema, supplied, getPreferredTimeZone());
}

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
    anyOfRequired: (fields) => t("component.workspace_chat.workflow_input_any_of_required", {
      fields: fields.join(t("component.workspace_chat.workflow_input_list_separator")),
    }),
    invalidEmail: t("component.workspace_chat.workflow_input_invalid_email"),
    invalidPhone: t("component.workspace_chat.workflow_input_invalid_phone"),
    invalidColor: t("component.workspace_chat.workflow_input_invalid_color"),
    dateRangeOrder: t("component.workspace_chat.workflow_input_range_order"),
  });
}

interface WorkflowSchemaFieldsProps {
  rootKey: string;
  schema: WorkflowInputSchema;
  value: unknown;
  errors: Record<string, string>;
  disabled?: boolean;
  workspaceId?: string;
  /** Saved Workflow whose run inputs these are; record fields read their choices through it. */
  workflowId?: string;
  path?: string[];
  onChange: (path: string[], value: unknown) => void;
  onFileBusyChange?: (busy: boolean) => void;
}

interface WorkflowSchemaFieldTreeProps extends Omit<WorkflowSchemaFieldsProps, "onFileBusyChange"> {
  idNamespace: string;
  onFileBusyChange: (fieldId: string, busy: boolean) => void;
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

/** How a project's stage reads under its name in a picker. */
function projectStageLabel(stage: string): string {
  // Other project types name their own stage, e.g. "Awaiting acceptance".
  return stage === "youtube_saved"
    ? t("component.workspace_chat.workflow_project_published")
    : stage === "video_ready"
      ? t("component.workspace_chat.workflow_project_ready")
      : humanizeKey(stage);
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
  multiple = false,
  label,
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
  /** Several records of the type, kept as a line list of project ids. */
  multiple?: boolean;
  label?: string;
  onChange: (value: string) => void;
}) {
  const ui = schema["x-ui"] || {};
  const projectType = String(ui.project_type || "").trim();
  // No allowed_stages offers the type's records in every stage.
  const stages = ui.allowed_stages || [];
  const [candidates, setCandidates] = useState<WorkflowProjectCandidate[]>([]);
  const [loading, setLoading] = useState(Boolean(workspaceId));
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    if (!workspaceId || !projectType) {
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
      // Several picks come from the whole list, not only the latest records.
      ...(multiple ? { limit: 500 } : {}),
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
  }, [workspaceId, projectType, stages.join("\u0000"), multiple]);

  if (!workspaceId) {
    // Without a Workspace there is nothing to list; ids are typed, one per line for several.
    return multiple ? (
      <textarea
        id={id}
        value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)}
        rows={4}
        disabled={disabled}
        required={required}
        aria-required={required}
        aria-invalid={invalid}
        aria-describedby={describedBy}
      />
    ) : (
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

  // Listed stage by stage in the declared order; without stages, newest first.
  const ordered = stages.length
    ? stages.flatMap((stage) => candidates.filter((candidate) => candidate.current_stage === stage))
    : candidates;
  const options = ordered.map((candidate) => ({
    value: candidate.id,
    label: `${candidate.title}${candidate.youtube_video_id ? ` · ${candidate.youtube_video_id}` : ""} · ${new Date(candidate.created_at).toLocaleDateString(undefined, { timeZone: getPreferredTimeZone() })}`,
    description: projectStageLabel(candidate.current_stage),
  }));
  const ariaLabel = label || schema.title || humanizeKey(projectType);
  // A prefilled id stays visible even after its record left the list.
  const missing = [...workflowSchemaChosen(multiple ? value : String(value ?? ""))]
    .filter((option) => !candidates.some((item) => item.id === option))
    .map((option) => ({ value: option, label: option, description: undefined }));
  return (
    <>
      {multiple ? (
        loading
          ? <small id={id}>{t("component.workspace_chat.workflow_project_loading")}</small>
          : <WorkflowRecordChoices
            id={id}
            options={[...options, ...missing]}
            value={value}
            disabled={disabled || failed}
            required={required}
            describedBy={describedBy}
            invalid={invalid || failed}
            label={ariaLabel}
            onChange={onChange}
          />
      ) : (
        <Select
          id={id}
          role="combobox"
          value={String(value ?? "")}
          onChange={onChange}
          disabled={disabled || loading || failed}
          ariaRequired={required}
          ariaInvalid={invalid || failed}
          ariaDescribedBy={describedBy}
          ariaLabel={ariaLabel}
          placeholder={loading
            ? t("component.workspace_chat.workflow_project_loading")
            : t("component.chat_action_card.select")}
          filterable
          options={[...missing, ...options]}
        />
      )}
      {failed && (
        <small role="alert">{t("component.workspace_chat.workflow_project_load_failed")}</small>
      )}
      {!loading && !failed && candidates.length === 0 && (
        <small className="workflow-starter-input-help">{t("component.workspace_chat.workflow_project_empty")}</small>
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
  workflowId,
  path = [],
  idNamespace,
  onChange: onFieldChange,
  onFileBusyChange,
}: WorkflowSchemaFieldTreeProps) {
  const required = new Set(schema.required || []);
  const compactDocuments = workflowSchemaCompactDocuments(schema);
  const level = path.length ? workflowValueAtPath(value, path) : value;
  // An answer on this level that hides other inputs (x-ui.visible_when) puts them back to their defaults.
  const onChange = (changedPath: string[], nextValue: unknown) => {
    onFieldChange(changedPath, nextValue);
    if (changedPath.length !== path.length + 1) return;
    const resets = workflowSchemaHiddenResets(schema, changedPath[path.length], nextValue, getPreferredTimeZone());
    for (const [field, reset] of Object.entries(resets)) onFieldChange([...path, field], reset);
  };
  return (
    <div className="workflow-starter-input-schema-fields">
      {workflowSchemaEntries(schema).map(([key, child]) => {
        if (child["x-ui"]?.hidden || !workflowSchemaFieldShown(child, level)) return null;
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
        const formControl = workflowSchemaControl(child);
        // An object with its own control (a part of a track, a date range) is one field, not a group of fields.
        if (formControl === "group") {
          const nested = (
            <WorkflowSchemaFieldTree
              rootKey={rootKey}
              schema={child}
              value={value}
              errors={errors}
              disabled={disabled}
              workspaceId={workspaceId}
              workflowId={workflowId}
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
            <section key={key} className="workflow-starter-input-group" data-layout={child["x-ui"]?.layout}>
              <h4>{label}</h4>
              {nested}
            </section>
          );
        }
        const ui = child["x-ui"] || {};
        // Picking a template option also sets the inputs it brings along.
        const pick = (nextValue: unknown) => {
          onChange(childPath, nextValue);
          for (const [field, preset] of Object.entries(workflowSchemaPreset(child, nextValue))) {
            onChange([...path, field], preset);
          }
        };
        const timeZone = String(ui.timezone_field
          ? workflowValueAtPath(value, [...path, ui.timezone_field]) || getPreferredTimeZone()
          : getPreferredTimeZone());
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
                onBusyChange={(busy) => onFileBusyChange(fieldIds.fieldId, busy)}
                workspaceId={workspaceId}
              />
              {error && <small role="alert">{error}</small>}
            </div>
          );
        }
        if (ui.control === "audio_clip" && ui.audio_field) {
          const clip = (childValue && typeof childValue === "object" ? childValue : {}) as Record<string, unknown>;
          return (
            <div key={key} className="workflow-starter-input-field workflow-starter-input-field--files" role="group" aria-labelledby={`${fieldIds.fieldId}-label`}>
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <WorkflowAudioClipField
                id={fieldIds.fieldId}
                source={audioClipSource(workflowValueAtPath(value, [...path, ui.audio_field]))}
                start={clip.start_seconds}
                end={clip.end_seconds}
                clipSeconds={ui.clip_seconds || 30}
                minSeconds={ui.min_seconds}
                maxSeconds={ui.max_seconds}
                disabled={disabled}
                describedBy={describedBy}
                onChange={(start, end) => onChange(childPath, { ...clip, auto: false, start_seconds: start, end_seconds: end })}
                allowAuto={Boolean(ui.allow_auto)}
                // Unset means the AI picks when the schema offers that; a part dragged earlier keeps the hand-picked choice.
                auto={Boolean(ui.allow_auto) && (clip.auto === true || (clip.auto === undefined && clip.start_seconds === undefined))}
                onAutoChange={(auto) => onChange(childPath, auto ? { auto: true } : { auto: false })}
              />
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
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
                workflowRunInput
                disabled={disabled}
                onBusyChange={(busy) => onFileBusyChange(fieldIds.fieldId, busy)}
                compact={compactDocuments}
                accept={ui.accept}
                capture={ui.capture}
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
        // Controls that are one answer drawn their own way: each is a labelled group.
        const ownControl = formControl === "signature" || formControl === "date_range" || formControl === "tags"
          || formControl === "line_items" || formControl === "rating" || formControl === "color"
          || formControl === "rich_text" || formControl === "address";
        if (ownControl) {
          const fieldLabel = `${label}${isRequired ? " *" : ""}`;
          const childErrors = Object.entries(errors).filter(([path, message]) => message
            && path.startsWith(`${workflowInputErrorPath(rootKey, childPath)}.`));
          // A range's own dates report under it: show the first.
          const shownError = formControl === "signature" && error && isRequired && (!Array.isArray(childValue) || childValue.length === 0)
            ? t("component.workspace_chat.workflow_input_required")
            : error || (formControl === "date_range" ? childErrors[0]?.[1] : "");
          const controlProps = {
            id: fieldIds.fieldId, label: fieldLabel, disabled, required: isRequired,
            invalid: Boolean(shownError), describedBy,
          };
          return (
            <div
              key={key}
              className={`workflow-starter-input-field${formControl === "signature" || formControl === "line_items" || formControl === "rich_text"
                ? " workflow-starter-input-field--files" : ""}`}
              role="group"
              aria-labelledby={`${fieldIds.fieldId}-label`}
            >
              <span id={`${fieldIds.fieldId}-label`}>{fieldLabel}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              {formControl === "signature" ? (
                <SignatureField {...controlProps} value={childValue} workspaceId={workspaceId} workflowRunInput
                  onBusyChange={(busy) => onFileBusyChange(fieldIds.fieldId, busy)}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : formControl === "date_range" ? (
                <DateRangeField {...controlProps} value={childValue}
                  mode={child.properties?.start?.format === "date-time" ? "datetime" : "date"}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : formControl === "tags" ? (
                <TagsField {...controlProps} value={childValue} placeholder={ui.placeholder}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : formControl === "line_items" ? (
                <LineItemsField {...controlProps} schema={child} value={childValue} errors={errors}
                  errorPath={workflowInputErrorPath(rootKey, childPath)}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : formControl === "rating" ? (
                <RatingField {...controlProps} schema={child} value={childValue}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : formControl === "color" ? (
                <ColorField {...controlProps} value={childValue} onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : formControl === "rich_text" ? (
                <RichTextField {...controlProps} value={childValue} rows={ui.rows} placeholder={ui.placeholder}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              ) : (
                <AddressField {...controlProps} value={childValue} placeholder={ui.placeholder}
                  onChange={(nextValue) => onChange(childPath, nextValue)} />
              )}
              {shownError && <small id={fieldIds.errorId} role="alert">{shownError}</small>}
            </div>
          );
        }
        const caseField = child["x-enum-cases"]?.field;
        const pictureChoices = workflowSchemaPictureChoices(
          child, humanizeKey, caseField ? workflowValueAtPath(value, [...path, caseField]) : undefined,
        );
        if (pictureChoices.length > 0) {
          // Options shown as pictures are picked by looking at them, one at a time; those
          // suiting this kind of work come first and the rest wait under "More".
          const chosen = workflowSchemaSelectValue(child, childValue);
          const suited = pictureChoices.filter((choice) => choice.recommended);
          const others = suited.length ? pictureChoices.filter((choice) => !choice.recommended) : pictureChoices;
          const pictureButton = (choice: (typeof pictureChoices)[number]) => (
            <button
              key={choice.value}
              type="button"
              role="radio"
              className="workflow-starter-input-suggestion"
              aria-checked={chosen === choice.value}
              disabled={disabled}
              onClick={() => pick(choice.value)}
            >
              <ConfiguredImage src={choice.image} alt="" loading="lazy" decoding="async" />
              <strong>{choice.label}</strong>
              {choice.detail && <span>{choice.detail}</span>}
            </button>
          );
          return (
            <div key={key} className="workflow-starter-input-field workflow-starter-input-field--suggested">
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <div
                id={fieldIds.fieldId}
                role="radiogroup"
                aria-labelledby={`${fieldIds.fieldId}-label`}
                aria-describedby={describedBy}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
              >
                {suited.length > 0 && <div className="workflow-starter-input-suggestions" data-pictured>
                  {suited.map(pictureButton)}
                </div>}
                {suited.length > 0 && others.length > 0 ? (
                  <details
                    className="workflow-starter-input-more"
                    open={others.some((choice) => choice.value === chosen) || undefined}
                  >
                    <summary>{t("component.workflow_schema.more_choices", { count: others.length })}</summary>
                    <div className="workflow-starter-input-suggestions" data-pictured>{others.map(pictureButton)}</div>
                  </details>
                ) : others.length > 0 && (
                  <div className="workflow-starter-input-suggestions" data-pictured>{others.map(pictureButton)}</div>
                )}
              </div>
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
            </div>
          );
        }
        if ((ui.control === "segmented" || ui.control === "aspect") && child.enum?.length) {
          // A short fixed list reads as one row of buttons; ratios also draw their frame.
          const chosen = workflowSchemaSelectValue(child, childValue);
          // What the picked option means (e.g. what the AI decides), read under the buttons.
          const chosenDetail = child["x-enum-descriptions"]?.[child.enum.findIndex((option) => String(option) === chosen)];
          return (
            <div key={key} className="workflow-starter-input-field">
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <div
                id={fieldIds.fieldId}
                className="workflow-starter-input-segments"
                data-control={ui.control}
                role="radiogroup"
                aria-labelledby={`${fieldIds.fieldId}-label`}
                aria-describedby={describedBy}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
              >
                {child.enum.map((option) => {
                  const value = String(option);
                  const ratio = ui.control === "aspect" ? workflowAspectRatio(value) : null;
                  return (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      className="workflow-starter-input-segment"
                      aria-checked={chosen === value}
                      disabled={disabled}
                      onClick={() => pick(value)}
                    >
                      {ratio && <span
                        className="workflow-starter-input-frame"
                        aria-hidden="true"
                        style={{ width: ratio[0] >= ratio[1] ? 22 : Math.round(22 * ratio[0] / ratio[1]),
                          height: ratio[1] >= ratio[0] ? 22 : Math.round(22 * ratio[1] / ratio[0]) }}
                      />}
                      {workflowSchemaOptionLabel(child, option, humanizeKey)}
                    </button>
                  );
                })}
              </div>
              {chosenDetail && <small className="workflow-starter-input-help">{chosenDetail}</small>}
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
            </div>
          );
        }
        const suggestions = schemaType === "string" && (!ui.control || ui.control === "textarea")
          ? workflowSchemaSuggestions(child)
          : [];
        if (suggestions.length > 0) {
          // Suggested values are picked into the box, which stays free to edit or type over.
          const text = String(childValue ?? "");
          const fieldProps = {
            id: fieldIds.fieldId,
            value: text,
            disabled,
            required: isRequired,
            "aria-labelledby": `${fieldIds.fieldId}-label`,
            "aria-required": isRequired,
            "aria-invalid": Boolean(error),
            "aria-describedby": describedBy,
          };
          return (
            <div key={key} className="workflow-starter-input-field workflow-starter-input-field--suggested">
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              {ui.control === "textarea"
                ? <textarea {...fieldProps} rows={ui.rows || 3} onChange={(event) => onChange(childPath, event.target.value)} />
                : <input {...fieldProps} type="text" onChange={(event) => onChange(childPath, event.target.value)} />}
              <SuggestionGallery suggestions={suggestions} value={text} label={label} disabled={disabled} onPick={pick} />
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
            </div>
          );
        }
        if (workflowSchemaProjectSelect(child)?.multiple) {
          // Several Workspace records: a group of chips, not a label around one control.
          return (
            <div
              key={key}
              className="workflow-starter-input-field workflow-starter-input-field--choices"
              role="group"
              aria-labelledby={`${fieldIds.fieldId}-label`}
              aria-describedby={describedBy}
              aria-invalid={Boolean(error)}
            >
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <WorkflowProjectSelect
                id={fieldIds.fieldId}
                schema={child}
                value={childValue}
                workspaceId={workspaceId}
                disabled={disabled}
                required={isRequired}
                describedBy={describedBy}
                invalid={Boolean(error)}
                multiple
                label={`${label}${isRequired ? " *" : ""}`}
                onChange={(nextValue) => onChange(childPath, nextValue)}
              />
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
            </div>
          );
        }
        const recordSelect = workflowSchemaRecordSelect(child);
        if (recordSelect) {
          return (
            <div
              key={key}
              className="workflow-starter-input-field workflow-starter-input-field--choices"
              role="group"
              aria-labelledby={`${fieldIds.fieldId}-label`}
              aria-describedby={describedBy}
              aria-invalid={Boolean(error)}
            >
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <WorkflowRecordSelect
                id={fieldIds.fieldId}
                multiple={recordSelect.multiple}
                // Run inputs are the top-level fields; record sources live only there.
                inputKey={path.length === 0 ? key : ""}
                value={childValue}
                workflowId={workflowId}
                workspaceId={workspaceId}
                disabled={disabled}
                required={isRequired}
                describedBy={describedBy}
                invalid={Boolean(error)}
                label={`${label}${isRequired ? " *" : ""}`}
                onChange={(nextValue) => onChange(childPath, nextValue)}
              />
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
            </div>
          );
        }
        const choices = workflowSchemaChoices(child);
        if (choices && child.items) {
          const chosen = workflowSchemaChosen(childValue);
          return (
            <div
              key={key}
              className="workflow-starter-input-field workflow-starter-input-field--choices"
              role="group"
              aria-labelledby={`${fieldIds.fieldId}-label`}
              aria-describedby={describedBy}
              aria-invalid={Boolean(error)}
            >
              <span id={`${fieldIds.fieldId}-label`}>{label}{isRequired ? " *" : ""}</span>
              {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
              <div className="workflow-starter-input-choices">
                {choices.map((option, optionIndex) => (
                  <label key={option} className="workflow-starter-input-choice">
                    <input
                      id={optionIndex === 0 ? fieldIds.fieldId : undefined}
                      type="checkbox"
                      checked={chosen.has(option)}
                      onChange={(event) => onChange(
                        childPath,
                        toggleWorkflowSchemaChoice(child, childValue, option, event.target.checked),
                      )}
                      disabled={disabled}
                      className="workflow-starter-input-checkbox"
                    />
                    <span>{workflowSchemaOptionLabel(child.items!, option, String)}</span>
                  </label>
                ))}
              </div>
              {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
            </div>
          );
        }
        if (schemaType === "boolean" && !child.enum) {
          return (
            <label key={key} className="workflow-starter-input-field workflow-starter-input-field--boolean" htmlFor={fieldIds.fieldId}>
              <input
                id={fieldIds.fieldId}
                type="checkbox"
                // Yes / no reads as a switch on every form.
                role="switch"
                checked={Boolean(childValue)}
                onChange={(event) => onChange(childPath, event.target.checked)}
                disabled={disabled}
                required={isRequired}
                aria-label={`${label}${isRequired ? " *" : ""}`}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
                className="workflow-starter-input-checkbox"
              />
              {/* A choice that stands for a service (YouTube, TikTok) carries its logo. */}
              {ui.provider && <IntegrationLogo provider={ui.provider} size={20} />}
              <span className="workflow-starter-input-boolean-copy">
                <span>{label}{isRequired ? " *" : ""}</span>
                {child.description && <small id={fieldIds.helpId} className="workflow-starter-input-help">{child.description}</small>}
                {error && <small id={fieldIds.errorId} role="alert">{error}</small>}
              </span>
            </label>
          );
        }
        const field = (
          <label key={key} className="workflow-starter-input-field" htmlFor={fieldIds.fieldId}>
            <span>{label}{isRequired ? " *" : ""}</span>
            {child.description && (
              <small id={fieldIds.helpId} className="workflow-starter-input-help">
                {child.description}
              </small>
            )}
            {ui.source && (
              <small className="workflow-starter-input-source">
                {t("component.workflow_schema.source", { source: ui.source })}
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
            ) : ui.control === "timezone" ? (
              <Select
                id={fieldIds.fieldId}
                role="combobox"
                value={String(childValue ?? "")}
                onChange={(nextValue) => onChange(childPath, nextValue)}
                disabled={disabled}
                ariaLabel={`${label}${isRequired ? " *" : ""}`}
                ariaRequired={isRequired}
                ariaInvalid={Boolean(error)}
                ariaDescribedBy={describedBy}
                filterable
                options={workflowSchemaTimeZones(String(childValue ?? "")).map((zone) => ({ value: zone, label: zone }))}
              />
            ) : child.format === "date-time" || child.format === "date" ? (
              <DateTimePicker
                id={fieldIds.fieldId}
                mode={child.format === "date-time" ? "datetime" : "date"}
                value={child.format === "date-time"
                  ? workflowSchemaDateTimeValue(String(childValue ?? ""), timeZone)
                  : String(childValue ?? "")}
                onChange={(nextValue) => onChange(childPath, child.format === "date-time"
                  ? workflowSchemaDateTimeIso(nextValue, timeZone) : nextValue)}
                placeholder={t("component.chat_action_card.select")}
                disabled={disabled}
                ariaLabel={`${label}${isRequired ? " *" : ""}`}
                ariaRequired={isRequired}
                ariaInvalid={Boolean(error)}
                ariaDescribedBy={describedBy}
              />
            ) : child.enum ? (
              <Select
                id={fieldIds.fieldId}
                role="combobox"
                value={workflowSchemaSelectValue(child, childValue)}
                onChange={pick}
                disabled={disabled}
                ariaLabel={`${label}${isRequired ? " *" : ""}`}
                ariaRequired={isRequired}
                ariaInvalid={Boolean(error)}
                ariaDescribedBy={describedBy}
                placeholder={t("component.chat_action_card.select")}
                options={child.enum.map((option) => ({ value: String(option), label: workflowSchemaOptionLabel(child, option, humanizeKey) }))}
              />
            ) : schemaType === "array" || ui.control === "textarea" || ui.control === "line_list" ? (
              <textarea
                id={fieldIds.fieldId}
                {...presetBlankHandlers(child.default)}
                value={String(childValue ?? "")}
                placeholder={ui.placeholder}
                onChange={(event) => onChange(childPath, event.target.value)}
                rows={ui.rows || (schemaType === "array" ? 4 : 3)}
                disabled={disabled}
                required={isRequired}
                aria-required={isRequired}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
              />
            ) : (schemaType === "number" || schemaType === "integer") && ui.control === "range"
              && typeof child.minimum === "number" && typeof child.maximum === "number" ? (
              <span className="workflow-starter-input-range">
                <input
                  id={fieldIds.fieldId}
                  type="range"
                  value={String(childValue ?? child.default ?? child.minimum)}
                  min={child.minimum}
                  max={child.maximum}
                  step={ui.step || (schemaType === "integer" ? 1 : "any")}
                  onChange={(event) => onChange(childPath, event.target.value)}
                  disabled={disabled}
                  aria-required={isRequired}
                  aria-invalid={Boolean(error)}
                  aria-describedby={describedBy}
                />
                <output htmlFor={fieldIds.fieldId} className="mono">
                  {String(childValue ?? child.default ?? child.minimum)}{ui.unit ? ` ${ui.unit}` : ""}
                </output>
              </span>
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
                // type="url" makes the browser reject www.example.com; we add https:// instead.
                type={child.format === "date" ? "date" : formControl === "email" ? "email" : formControl === "phone" ? "tel" : "text"}
                inputMode={child.format === "uri" ? "url" : formControl === "email" ? "email" : formControl === "phone" ? "tel" : undefined}
                autoComplete={formControl === "email" ? "email" : formControl === "phone" ? "tel" : undefined}
                {...(child.format === "date" ? {} : presetBlankHandlers(child.default))}
                value={String(childValue ?? "")}
                placeholder={ui.placeholder}
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
        // Chips sit beside the label: a button inside a <label> would be a second labelable control.
        if (disabled || schemaType !== "string" || !ui.suggestions?.length) return field;
        return (
          <div key={key} className="workflow-starter-input-field-group">
            {field}
            <WorkflowInputSuggestions
              suggestions={ui.suggestions}
              value={childValue}
              onPick={(suggestion) => onChange(childPath, suggestion)}
            />
          </div>
        );
      })}
    </div>
  );
}

type TextField = HTMLInputElement | HTMLTextAreaElement;

/**
 * A preset's blanks ("[audience]") are filled in place: clicking a blank selects
 * it so typing replaces it, focusing the field by keyboard selects the first,
 * and Tab / Shift+Tab move between them (past the last, Tab leaves the field).
 */
const pointerFocused = new WeakSet<TextField>();

function presetBlankHandlers(template: unknown) {
  const blanks = presetPlaceholderTokens(template);
  if (!blanks.length) return {};
  const select = (field: TextField, range: [number, number] | null) => {
    if (!range) return false;
    field.setSelectionRange(range[0], range[1]);
    return true;
  };
  return {
    onMouseDown: (event: React.MouseEvent<TextField>) => { pointerFocused.add(event.currentTarget); },
    onFocus: (event: React.FocusEvent<TextField>) => {
      const field = event.currentTarget;
      // A click places its own caret (the mouse-up below picks the blank under it).
      if (!pointerFocused.delete(field)) select(field, nextPresetPlaceholder(field.value, 0, blanks));
    },
    onMouseUp: (event: React.MouseEvent<TextField>) => {
      const field = event.currentTarget;
      if (field.selectionStart === field.selectionEnd) {
        select(field, presetPlaceholderAt(field.value, field.selectionStart ?? 0, blanks));
      }
    },
    onKeyDown: (event: React.KeyboardEvent<TextField>) => {
      if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return;
      const field = event.currentTarget;
      const from = event.shiftKey ? field.selectionStart ?? 0 : field.selectionEnd ?? 0;
      if (select(field, nextPresetPlaceholder(field.value, from, blanks, event.shiftKey))) event.preventDefault();
    },
  };
}

export function WorkflowSchemaFields(props: WorkflowSchemaFieldsProps) {
  const idNamespace = useId();
  const busyFields = useRef(new Set<string>());
  const onFileBusyChange = useCallback((fieldId: string, busy: boolean) => {
    if (busy) busyFields.current.add(fieldId);
    else busyFields.current.delete(fieldId);
    props.onFileBusyChange?.(busyFields.current.size > 0);
  }, [props.onFileBusyChange]);
  return <WorkflowSchemaFieldTree {...props} idNamespace={idNamespace} onFileBusyChange={onFileBusyChange} />;
}
