import type { ReactNode } from "react";
import { t } from "../../lib/i18n";
import {
  optionDetailLabel,
  optionPayload,
  orderedOptions,
  recommendationOf,
  recommendationReason,
  type RecommendedQuestion,
} from "../../lib/recommendedOptions";

/**
 * A question's answers: the recommended one first and the only brand-coloured
 * button, the others neutral, "Something else…" last when the person may
 * answer in their own words, and one line saying why the recommendation.
 */
export default function RecommendedOptions({
  question,
  options,
  labelFor,
  onChoose,
  onOther,
  otherOpen,
  disabled,
  isDisabled,
  titleFor,
  ariaExpandedFor,
  children,
}: {
  question?: RecommendedQuestion | null;
  options: string[];
  /** Label for an option the card itself knows how to name. */
  labelFor: (option: string) => string;
  onChoose: (option: string, payload?: Record<string, unknown>) => void;
  /** Present when the card can take a free answer; opens it. */
  onOther?: () => void;
  otherOpen?: boolean;
  disabled?: boolean;
  isDisabled?: (option: string) => boolean;
  titleFor?: (option: string) => string | undefined;
  ariaExpandedFor?: (option: string) => boolean | undefined;
  /** Rendered between the buttons and the reason (e.g. a free-text box). */
  children?: ReactNode;
}) {
  const recommendation = recommendationOf(question);
  const ordered = orderedOptions(options, recommendation);
  const primary = recommendation && ordered.includes(recommendation.option)
    ? recommendation.option
    : ordered[0];
  const reason = recommendationReason(recommendation);
  return (
    <>
      <div className="chat-hitl-actions">
        {ordered.map((option) => (
          <button
            type="button"
            key={option}
            className={option === primary ? "chat-hitl-btn-primary" : "chat-hitl-btn-secondary"}
            onClick={() => onChoose(option, optionPayload(question, option))}
            disabled={disabled || isDisabled?.(option)}
            title={titleFor?.(option)}
            aria-expanded={ariaExpandedFor?.(option)}
            data-recommended={option === primary ? "true" : undefined}
          >
            {optionDetailLabel(question, option) || labelFor(option)}
          </button>
        ))}
        {onOther && (question?.allow_other ?? true) && (
          <button
            type="button"
            className="chat-hitl-btn-secondary chat-hitl-btn-other"
            onClick={onOther}
            disabled={disabled}
            aria-expanded={otherOpen}
          >
            {t("recommendation.other")}
          </button>
        )}
      </div>
      {children}
      {reason && (
        <p className="chat-hitl-recommendation-reason">
          {t("recommendation.reason_prefix", { reason })}
        </p>
      )}
    </>
  );
}
