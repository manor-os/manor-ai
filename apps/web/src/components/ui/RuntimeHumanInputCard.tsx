import { useState } from "react";

import type { RuntimeHumanInputState } from "../../lib/chatStream";
import { t } from "../../lib/i18n";
import type { WorkflowDocumentRef } from "../../lib/workflowRunInputs";
import ReviewDeliverables from "../workflows/ReviewDeliverables";
import { workflowReviewArtifacts } from "../workflows/WorkflowApprovalReview";
import WorkflowWaitDocumentField from "../workflows/WorkflowWaitDocumentField";
import InAppActionLink from "./InAppActionLink";
import Select from "./Select";

// A file answer is the files' standard references, one per line.
function fileAnswer(files: WorkflowDocumentRef[]): string {
  return files.map((file) => `[${file.name || file.document_id}](/viewer/${file.document_id})`).join("\n");
}

/**
 * The question a durable chat run is parked on (`request_input`). Submitting
 * resumes the run with the answer as the tool's result; the composer's next
 * message does the same, so this card is the explicit, structured path.
 */
export function RuntimeHumanInputCard({
  input,
  onSubmit,
  busy,
  workspaceId,
}: {
  input: RuntimeHumanInputState;
  onSubmit: (answer: string, fields?: Record<string, string>, choice?: string) => Promise<void>;
  busy: boolean;
  /** Where an uploaded file is added to Knowledge. */
  workspaceId?: string;
}) {
  const [answer, setAnswer] = useState("");
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [fieldFiles, setFieldFiles] = useState<Record<string, WorkflowDocumentRef[]>>({});
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
      // A choice keeps what the person wrote: typing a note, then pressing
      // "Request changes", must send the note with the choice.
      await onSubmit(
        answer.trim() || choice || "",
        fields.length > 0 ? fieldValues : undefined,
        choice,
      );
      setAnswer("");
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    }
  };

  // A question that sends the person somewhere first (a screen
  // hand-over) is answered with one "Done" once they are back.
  const options = input.options?.length
    ? input.options
    : input.actionLink && fields.length === 0
      ? [t("component.chat_runtime_input.done")]
      : [];
  const hasOptions = options.length > 0;
  const updateField = (name: string, value: string) =>
    setFieldValues((prev) => ({ ...prev, [name]: value }));

  // Minimal by design: the question, what it is about, a note, and the choices.
  // With choices, a choice is the answer (and carries the note); a separate
  // send button is only needed for a free-form answer.
  return (
    <div
      className="chat-runtime-input-card"
      role="form"
      aria-live="polite"
      aria-label={t("component.chat_runtime_input.title")}
    >
      <div className="chat-runtime-input-card__question">{input.question}</div>
      {input.context && (
        <div className="chat-runtime-input-card__context">{input.context}</div>
      )}
      {input.actionLink && (
        <div className="chat-runtime-input-card__link">
          <InAppActionLink link={input.actionLink} testId="runtime-input-action-link" />
          {input.actionToTake && <span>{input.actionToTake}</span>}
        </div>
      )}
      {/* A file review shows the files themselves, as a Workflow review does. */}
      {input.reviewFiles && input.reviewFiles.length > 0 && (
        <div className="chat-runtime-input-card__files">
          <ReviewDeliverables artifacts={workflowReviewArtifacts({ files: input.reviewFiles })} />
        </div>
      )}
      {fields.length > 0 && (
        <div className="chat-runtime-input-card__fields">
          {fields.map((field) => {
            const label = `${field.label || field.name}${field.required ? " *" : ""}`;
            return field.type === "select" && field.options ? (
              <div key={field.name} className="chat-runtime-input-card__field">
                <span>{label}</span>
                <Select
                  value={fieldValues[field.name] || ""}
                  disabled={busy}
                  ariaLabel={label}
                  ariaRequired={field.required}
                  placeholder={t("component.chat_action_card.select")}
                  options={field.options}
                  onChange={(value) => updateField(field.name, value)}
                />
              </div>
            ) : field.type === "file" ? (
              <div key={field.name} className="chat-runtime-input-card__field" role="group" aria-label={label}>
                <span>{label}</span>
                <WorkflowWaitDocumentField
                  id={`runtime-input-${field.name}`}
                  value={fieldFiles[field.name] || []}
                  workspaceId={workspaceId}
                  disabled={busy}
                  onChange={(files) => {
                    setFieldFiles((prev) => ({ ...prev, [field.name]: files }));
                    updateField(field.name, fileAnswer(files));
                  }}
                />
              </div>
            ) : field.type === "textarea" ? (
              <textarea
                key={field.name}
                rows={2}
                aria-label={label}
                placeholder={label}
                value={fieldValues[field.name] || ""}
                disabled={busy}
                onChange={(event) => updateField(field.name, event.target.value)}
              />
            ) : (
              <input
                key={field.name}
                aria-label={label}
                placeholder={label}
                type={field.type === "number" ? "number" : field.type === "date" ? "date" : "text"}
                value={fieldValues[field.name] || ""}
                disabled={busy}
                onChange={(event) => updateField(field.name, event.target.value)}
              />
            );
          })}
        </div>
      )}
      {/* The asker's own fields are where the answer goes; a second free box beside them only confuses. */}
      {fields.length === 0 && (
        <textarea
          className="chat-runtime-input-card__answer"
          rows={hasOptions ? 1 : 2}
          value={answer}
          disabled={busy}
          aria-label={t(hasOptions ? "component.chat_runtime_input.note" : "component.chat_runtime_input.placeholder")}
          placeholder={t(hasOptions ? "component.chat_runtime_input.note" : "component.chat_runtime_input.placeholder")}
          onChange={(event) => setAnswer(event.target.value)}
          onKeyDown={(event) => {
            if (!hasOptions && event.key === "Enter" && !event.shiftKey && canSubmit) {
              event.preventDefault();
              void submit();
            }
          }}
        />
      )}
      <div className="chat-runtime-input-card__actions">
        {hasOptions ? options.map((option, index) => (
          <button
            key={option}
            type="button"
            className={index === 0 ? "chat-runtime-input-card__submit" : "chat-runtime-input-card__option"}
            disabled={busy || missingRequired}
            onClick={() => void submit(option)}
          >
            {option}
          </button>
        )) : (
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
        )}
      </div>
      {error && <div className="chat-runtime-input-card__error">{error}</div>}
    </div>
  );
}

export default RuntimeHumanInputCard;
