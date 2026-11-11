import { useMemo, useState } from "react";

import type { RuntimeHumanInputState } from "../../lib/chatStream";
import { formDraftKey, useFormDraft } from "../../lib/formDraft";
import { t } from "../../lib/i18n";
import { orderedOptions, recommendationReason } from "../../lib/recommendedOptions";
import { requestInputFormSchema } from "../../lib/requestInputForm";
import ReviewDeliverables from "../workflows/ReviewDeliverables";
import { workflowReviewArtifacts } from "../workflows/WorkflowApprovalReview";
import {
  parseWorkflowSchemaDraft,
  setWorkflowValueAtPath,
  workflowInputErrorPath,
  WorkflowSchemaFields,
  workflowSchemaDraft,
} from "../workflows/WorkflowSchemaFields";
import FormDraftNotice from "./FormDraftNotice";
import InAppActionLink from "./InAppActionLink";

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
  onSubmit: (answer: string, fields?: Record<string, unknown>, choice?: string) => Promise<void>;
  busy: boolean;
  /** Where an uploaded file is added to Knowledge. */
  workspaceId?: string;
}) {
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const fields = useMemo(() => input.fields || [], [input.fields]);
  // The asker's fields are one form drawn with the Workflow form's controls.
  const schema = useMemo(() => requestInputFormSchema(fields), [fields]);
  const draft = useFormDraft<unknown>(
    fields.length ? formDraftKey("request-input", input.requestId) : null,
    () => workflowSchemaDraft(schema, {}),
  );
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [fileBusy, setFileBusy] = useState(false);
  const canSubmit = !busy && !fileBusy && (answer.trim().length > 0 || fields.length > 0);

  const submit = async (choice?: string) => {
    if (busy || fileBusy) return;
    setError(null);
    let parsed: Record<string, unknown> | undefined;
    if (fields.length > 0) {
      const errors: Record<string, string> = {};
      parsed = parseWorkflowSchemaDraft(schema, draft.values, "fields", false, errors) as Record<string, unknown>;
      setFieldErrors(errors);
      if (Object.values(errors).some(Boolean)) return;
    }
    try {
      // A choice keeps what the person wrote: typing a note, then pressing
      // "Request changes", must send the note with the choice.
      await onSubmit(answer.trim() || choice || "", parsed, choice);
      setAnswer("");
      draft.clear();
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : String(submitError));
    }
  };

  // A question that sends the person somewhere first (a screen
  // hand-over) is answered with one "Done" once they are back. Otherwise
  // the recommended answer leads and is the one primary button.
  const options = input.options?.length
    ? orderedOptions(input.options, input.recommendation)
    : input.actionLink && fields.length === 0
      ? [t("component.chat_runtime_input.done")]
      : [];
  const hasOptions = options.length > 0;
  const reason = recommendationReason(input.recommendation);

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
        <div className="chat-question-form">
          {draft.restored && <FormDraftNotice onDiscard={draft.reset} disabled={busy} />}
          <WorkflowSchemaFields
            rootKey="fields"
            schema={schema}
            value={draft.values}
            errors={fieldErrors}
            disabled={busy}
            workspaceId={workspaceId}
            onFileBusyChange={setFileBusy}
            onChange={(path, next) => {
              draft.setValues((current: unknown) => setWorkflowValueAtPath(current, path, next));
              setFieldErrors((current) => ({ ...current, [workflowInputErrorPath("fields", path)]: "" }));
            }}
          />
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
            if (event.key === "Enter" && !event.shiftKey && canSubmit) {
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
            disabled={busy || fileBusy}
            onClick={() => void submit(option)}
          >
            {option}
          </button>
        )).concat(fields.length === 0 ? [(
          // Answering in one's own words: whatever is typed in the note.
          <button
            key="__other"
            type="button"
            className="chat-runtime-input-card__option"
            disabled={!canSubmit}
            onClick={() => void submit()}
          >
            {t("recommendation.other")}
          </button>
        )] : []) : (
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
      {reason && hasOptions && (
        <p className="chat-hitl-recommendation-reason">{t("recommendation.reason_prefix", { reason })}</p>
      )}
      {error && <div className="chat-runtime-input-card__error">{error}</div>}
    </div>
  );
}

export default RuntimeHumanInputCard;
