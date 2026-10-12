import { useState } from "react";

import type { RuntimeHumanInputState } from "../../lib/chatStream";
import { t } from "../../lib/i18n";

/**
 * The question a durable chat run is parked on (`request_input`). Submitting
 * resumes the run with the answer as the tool's result; the composer's next
 * message does the same, so this card is the explicit, structured path.
 */
export function RuntimeHumanInputCard({
  input,
  onSubmit,
  busy,
}: {
  input: RuntimeHumanInputState;
  onSubmit: (answer: string, fields?: Record<string, string>, choice?: string) => Promise<void>;
  busy: boolean;
}) {
  const [answer, setAnswer] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const fields = input.fields || [];
  const missingRequired = fields.some(
    (field) => field.required && !String(fieldValues[field.name] || "").trim(),
  );
  const canSubmit = !busy && !missingRequired && (answer.trim().length > 0 || fields.length > 0);

  const submit = async (choice?: string) => {
    if (busy) return;
    setError(null);
    try {
      await onSubmit(
        choice || answer.trim(),
        fields.length > 0 ? fieldValues : undefined,
        choice,
      );
      setAnswer("");
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    }
  };

  return (
    <div className="chat-runtime-input-card" role="form" aria-live="polite">
      <div className="chat-runtime-input-card__title">
        {t("component.chat_runtime_input.title")}
      </div>
      <div className="chat-runtime-input-card__question">{input.question}</div>
      {input.context && (
        <div className="chat-runtime-input-card__context">{input.context}</div>
      )}
      {input.options && input.options.length > 0 && (
        <div className="chat-runtime-input-card__options">
          {input.options.map((option) => (
            <button
              key={option}
              type="button"
              className="chat-runtime-input-card__option"
              disabled={busy}
              onClick={() => void submit(option)}
            >
              {option}
            </button>
          ))}
        </div>
      )}
      {fields.length > 0 && (
        <div className="chat-runtime-input-card__fields">
          {fields.map((field) => (
            <label key={field.name} className="chat-runtime-input-card__field">
              <span>
                {field.label}
                {field.required ? " *" : ""}
              </span>
              {field.type === "select" && field.options ? (
                <select
                  value={fieldValues[field.name] || ""}
                  disabled={busy}
                  onChange={(event) =>
                    setFieldValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                  }
                >
                  <option value="">—</option>
                  {field.options.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type={field.type === "number" ? "number" : field.type === "date" ? "date" : "text"}
                  value={fieldValues[field.name] || ""}
                  disabled={busy}
                  onChange={(event) =>
                    setFieldValues((prev) => ({ ...prev, [field.name]: event.target.value }))
                  }
                />
              )}
            </label>
          ))}
        </div>
      )}
      <textarea
        className="chat-runtime-input-card__answer"
        rows={2}
        value={answer}
        disabled={busy}
        placeholder={t("component.chat_runtime_input.placeholder")}
        onChange={(event) => setAnswer(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && canSubmit) {
            event.preventDefault();
            void submit();
          }
        }}
      />
      <div className="chat-runtime-input-card__actions">
        <button
          type="button"
          className="chat-runtime-input-card__submit"
          disabled={!canSubmit}
          onClick={() => void submit()}
        >
          {busy
            ? t("component.chat_runtime_input.sending")
            : t("component.chat_runtime_input.submit")}
        </button>
        <span className="chat-runtime-input-card__hint">
          {t("component.chat_runtime_input.hint")}
        </span>
      </div>
      {error && <div className="chat-runtime-input-card__error">{error}</div>}
    </div>
  );
}

export default RuntimeHumanInputCard;
