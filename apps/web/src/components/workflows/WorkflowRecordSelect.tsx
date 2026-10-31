import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import Select from "../ui/Select";
import Chip from "../ui/Chip";
import { IconClose } from "../icons";
import { toggleWorkflowSchemaChoice, workflowSchemaChosen, type WorkflowInputSchema } from "./workflowSchema";

const RECORD_SCHEMA: WorkflowInputSchema = { type: "array", items: { type: "string" } };

/**
 * Picks Workspace Ledger records for a `record_select` run input of a saved
 * Workflow. The value is a record key (one) or a line list of keys (several).
 */
export default function WorkflowRecordSelect({
  id,
  multiple,
  inputKey,
  value,
  workflowId,
  workspaceId,
  disabled,
  required,
  describedBy,
  invalid,
  label,
  onChange,
}: {
  id: string;
  multiple: boolean;
  inputKey: string;
  value: unknown;
  workflowId?: string;
  workspaceId?: string;
  disabled?: boolean;
  required: boolean;
  describedBy?: string;
  invalid: boolean;
  label: string;
  onChange: (value: string) => void;
}) {
  const [options, setOptions] = useState<Array<{ value: string; label: string; description?: string | null }>>([]);
  const live = Boolean(workflowId && workspaceId && inputKey);
  const [loading, setLoading] = useState(live);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let active = true;
    if (!live) {
      setOptions([]);
      setLoading(false);
      return () => { active = false; };
    }
    setLoading(true);
    setFailed(false);
    void api.workflows.listRecordChoices({
      workflow_id: workflowId!,
      input_key: inputKey,
      workspace_id: workspaceId!,
    }).then((items) => {
      if (active) setOptions(items);
    }).catch(() => {
      if (active) {
        setOptions([]);
        setFailed(true);
      }
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [live, workflowId, workspaceId, inputKey]);

  if (!live) {
    // Without a saved Workflow there is no source to read; keys are typed.
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
  if (loading) {
    return <small id={id}>{t("component.workspace_chat.workflow_record_choices_loading")}</small>;
  }
  const chosen = workflowSchemaChosen(value);
  // Keep a prefilled key visible even after its record left the source.
  const listed = [
    ...options,
    ...[...chosen]
      .filter((option) => !options.some((item) => item.value === option))
      .map((option) => ({ value: option, label: option, description: null })),
  ];
  if (failed && listed.length === 0) {
    return <small id={id} role="alert">{t("component.workspace_chat.workflow_record_choices_failed")}</small>;
  }
  if (listed.length === 0) {
    return <small id={id}>{t("component.workspace_chat.workflow_record_choices_empty")}</small>;
  }
  if (!multiple) {
    return (
      <Select
        id={id}
        role="combobox"
        value={String(value ?? "")}
        onChange={onChange}
        disabled={disabled}
        ariaRequired={required}
        ariaInvalid={invalid}
        ariaDescribedBy={describedBy}
        ariaLabel={label}
        placeholder={t("component.chat_action_card.select")}
        filterable
        options={listed.map((item) => ({
          value: item.value,
          label: item.label,
          description: item.description || undefined,
        }))}
      />
    );
  }
  // Several records: picked ones as removable chips, the rest behind one
  // searchable dropdown, so a long Ledger never turns into a long form.
  const order = listed.map((item) => item.value);
  const picked = listed.filter((item) => chosen.has(item.value));
  const toggle = (option: string, checked: boolean) => onChange(
    toggleWorkflowSchemaChoice(RECORD_SCHEMA, value, option, checked, order),
  );
  return (
    <div className="workflow-record-select">
      {picked.length > 0 && (
        <div className="workflow-record-select__chips">
          {picked.map((item) => (
            <Chip key={item.value} variant="slate">
              <span title={item.description ? `${item.label} · ${item.description}` : item.label}>{item.label}</span>
              <button
                type="button"
                className="workflow-record-select__remove"
                onClick={() => toggle(item.value, false)}
                disabled={disabled}
                aria-label={t("component.workspace_chat.workflow_record_choices_remove", { name: item.label })}
              >
                <IconClose size={10} aria-hidden="true" />
              </button>
            </Chip>
          ))}
        </div>
      )}
      <Select
        id={id}
        role="combobox"
        value=""
        onChange={(option) => { if (option) toggle(option, true); }}
        disabled={disabled || picked.length === listed.length}
        ariaRequired={required}
        ariaInvalid={invalid}
        ariaDescribedBy={describedBy}
        ariaLabel={label}
        placeholder={t("component.workspace_chat.workflow_record_choices_add")}
        filterable
        options={listed
          .filter((item) => !chosen.has(item.value))
          .map((item) => ({ value: item.value, label: item.label, description: item.description || undefined }))}
      />
    </div>
  );
}
