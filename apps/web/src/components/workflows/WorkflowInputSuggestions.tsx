import { t } from "../../lib/i18n";

/** One-click recommended answers shown beside a text input (never inside its <label>). */
export default function WorkflowInputSuggestions({
  suggestions,
  value,
  disabled,
  onPick,
}: {
  suggestions?: string[];
  value: unknown;
  disabled?: boolean;
  onPick: (suggestion: string) => void;
}) {
  const current = String(value ?? "");
  const shown = !disabled && Array.isArray(suggestions)
    ? suggestions.filter((item) => typeof item === "string" && item.trim())
    : [];
  // Nothing to offer when every suggestion already is the field's value.
  if (!shown.some((item) => item !== current)) return null;
  return (
    <div className="workflow-input-picks" role="group" aria-label={t("component.workflow_run.suggested")}>
      <span aria-hidden="true">{t("component.workflow_run.suggested")}</span>
      {shown.map((suggestion) => (
        <button
          key={suggestion}
          type="button"
          className="workflow-input-pick"
          title={suggestion}
          aria-pressed={current === suggestion}
          onClick={() => onPick(suggestion)}
        >
          {suggestion}
        </button>
      ))}
    </div>
  );
}
