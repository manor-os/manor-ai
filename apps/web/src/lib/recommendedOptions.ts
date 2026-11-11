/**
 * Every question Manor asks carries one recommended answer.
 *
 * Mirrors packages/core/services/decision_recommendation.py: a card's
 * `recommendation.option` is one of its `options` and leads them; its
 * `reason` is one sentence shown under the buttons. `reason_code` (and an
 * option's `label_code`) name shared sentences so they read in the viewer's
 * language; the server text is the fallback.
 */
import { t } from "./i18n";

export interface Recommendation {
  option: string;
  reason: string;
  reason_code?: string;
  reason_params?: Record<string, string | number>;
}

export interface OptionDetail {
  label?: string;
  label_code?: string;
  label_params?: Record<string, string | number>;
  /** Sent back with the choice (a proposal's "only the first" picks). */
  payload?: Record<string, unknown>;
  scope?: string;
}

export interface RecommendedQuestion {
  options?: string[];
  recommendation?: Recommendation | null;
  option_details?: Record<string, OptionDetail> | null;
  allow_other?: boolean;
  batch_key?: string;
}

/** Asks the chat surface to answer a card from its composer ("Something else…"). */
export const FOCUS_COMPOSER_EVENT = "manor:focus-composer";
export type FocusComposerDetail = { messageId?: string; title?: string };

/** "Always allow": approve and save the allow rule the card describes. */
export const ALWAYS_ALLOW_CHOICE = "approve_and_save_rule";

function translated(key: string, vars?: Record<string, string | number>): string | null {
  const text = t(key, vars);
  return text && text !== key ? text : null;
}

/**
 * A card's own words that the server names by code (a connect request's title
 * and description), in the viewer's language; ``fallback`` otherwise.
 */
export function codedCardText(
  code: unknown, params: unknown, fallback: unknown,
): string {
  if (typeof code === "string" && code) {
    const text = translated(`recommendation.text.${code}`, params && typeof params === "object" ? params as Record<string, string | number> : undefined);
    if (text) return text;
  }
  return typeof fallback === "string" ? fallback : "";
}

export function recommendationOf(question?: RecommendedQuestion | null): Recommendation | null {
  const rec = question?.recommendation;
  return rec && typeof rec.option === "string" && rec.option ? rec : null;
}

/** The options in display order: the recommended one first. */
export function orderedOptions(options: string[], recommendation?: Recommendation | null): string[] {
  const chosen = recommendation?.option;
  if (!chosen || !options.includes(chosen)) return options;
  return [chosen, ...options.filter((option) => option !== chosen)];
}

export function recommendationReason(recommendation?: Recommendation | null): string {
  if (!recommendation) return "";
  if (recommendation.reason_code) {
    const text = translated(`recommendation.reason.${recommendation.reason_code}`, recommendation.reason_params);
    if (text) return text;
  }
  return recommendation.reason || "";
}

/** An option's own words (a combination, an "Always" scope), or null. */
export function optionDetailLabel(question: RecommendedQuestion | null | undefined, option: string): string | null {
  const detail = question?.option_details?.[option];
  if (!detail) return null;
  if (detail.label_code) {
    const text = translated(`recommendation.label.${detail.label_code}`, detail.label_params);
    if (text) return text;
  }
  return detail.label || null;
}

export function optionPayload(question: RecommendedQuestion | null | undefined, option: string): Record<string, unknown> | undefined {
  const payload = question?.option_details?.[option]?.payload;
  return payload && typeof payload === "object" ? payload : undefined;
}

/**
 * The question a Workspace draft turn asked (Create Workspace in a chat), from
 * the turn's workspace artifact. Picking an option is the user's next message.
 */
export function workspaceDraftQuestion(
  artifacts: Array<{ kind?: string; data?: unknown }> | undefined,
): RecommendedQuestion | null {
  for (const artifact of [...(artifacts || [])].reverse()) {
    if (artifact?.kind !== "workspace") continue;
    const question = (artifact.data as { question?: unknown } | undefined)?.question;
    if (question && typeof question === "object" && Array.isArray((question as RecommendedQuestion).options)) {
      return question as RecommendedQuestion;
    }
  }
  return null;
}
