import { t } from "../../lib/i18n";
import type { RecommendedQuestion } from "../../lib/recommendedOptions";
import RecommendedOptions from "./RecommendedOptions";

/**
 * A question a message asks that is answered by replying (`answer_as:
 * "message"`): each option sends itself as the person's reply, so it resolves
 * on the path a typed answer takes. Typing anything else in the composer is
 * the "something else" answer.
 */
function normalizeOption(option: string) {
  return option.trim().replace(/[.…。]+$/u, "").toLocaleLowerCase();
}

export default function MessageQuestionOptions({ question, onAnswer, onOther, disabled }: {
  question: RecommendedQuestion;
  onAnswer: (answer: string) => void;
  /** "Something else…": put the cursor in the composer. */
  onOther?: () => void;
  disabled?: boolean;
}) {
  // The menu always ends with its own "Something else…"; a model-written one would say it twice.
  const otherLabel = normalizeOption(t("recommendation.other"));
  const options = (question.options || []).filter((option) =>
    typeof option === "string" && option.trim() && (!onOther || normalizeOption(option) !== otherLabel));
  if (options.length === 0) return null;
  return (
    <div className="chat-message-question" role="group" aria-label={t("recommendation.recommended")}>
      <RecommendedOptions
        question={question}
        options={options}
        labelFor={(option) => option}
        onChoose={(option) => onAnswer(option)}
        onOther={onOther}
        disabled={disabled}
      />
    </div>
  );
}
