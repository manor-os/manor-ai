/**
 * Reading a Strategist proposal card.
 *
 * The backend (`packages/core/strategist/service.py::_post_proposal_chat`)
 * sends one typed entry per proposed task on `pending_action.tasks` and on
 * `meta.proposal.tasks`. Everything here turns those numbers into words —
 * nothing is recovered from the message body.
 */
import { t } from "./i18n";
import { formatUserFacingText } from "./taskDisplay";

/** One proposed task exactly as the backend sends it. */
export interface ProposalTaskEntry {
  task_id?: string;
  title: string;
  priority?: number;
  rationale?: string;
  /** Present only together with `metric_delta` — the goal the number moves. */
  goal_id?: string;
  goal_title?: string;
  metric_key?: string;
  /** The Strategist's predicted change to the linked goal's metric. */
  metric_delta?: number;
}

/** Strategist priority scale, mirrored from PROPOSAL_PRIORITY_WORDS. */
const PRIORITY_I18N_KEYS: Record<number, string> = {
  5: "component.proposal.priority_critical",
  4: "component.proposal.priority_high",
  3: "component.proposal.priority_medium",
  2: "component.proposal.priority_low",
  1: "component.proposal.priority_minimal",
};

/**
 * Priorities that earn a chip. 3 (medium) is the Strategist's default, so a
 * chip on every row would discriminate nothing; only above-default urgency
 * is worth the pixels.
 */
const PROMINENT_PRIORITIES = new Set([5, 4]);

/** Typed entries from a message, whichever surface carries them. */
export function proposalTaskEntries(source: unknown): ProposalTaskEntry[] {
  if (!Array.isArray(source)) return [];
  return source
    .filter((entry): entry is Record<string, any> =>
      Boolean(entry && typeof entry === "object" && entry.title))
    .map((entry) => ({
      task_id: entry.task_id ? String(entry.task_id) : undefined,
      title: String(entry.title),
      priority: typeof entry.priority === "number" ? entry.priority : undefined,
      rationale: entry.rationale ? String(entry.rationale) : undefined,
      goal_id: entry.goal_id ? String(entry.goal_id) : undefined,
      goal_title: entry.goal_title ? String(entry.goal_title) : undefined,
      metric_key: entry.metric_key ? String(entry.metric_key) : undefined,
      metric_delta:
        typeof entry.metric_delta === "number" ? entry.metric_delta : undefined,
    }));
}

/** "High priority" — or null when the priority isn't worth calling out. */
export function proposalPriorityLabel(priority?: number): string | null {
  if (typeof priority !== "number" || !PROMINENT_PRIORITIES.has(priority)) {
    return null;
  }
  return t(PRIORITY_I18N_KEYS[priority]);
}

/** Signed delta, e.g. "+1" / "-2.5" — never a bare unsigned number. */
function formatDelta(delta: number): string {
  const rounded = Math.round(delta * 100) / 100;
  // Matches the backend's `{:+g}` so card and body text never disagree.
  return `${rounded >= 0 ? "+" : ""}${rounded}`;
}

/** What the predicted number moves: the goal's name, else its metric key. */
function impactSubject(entry: ProposalTaskEntry): string {
  const title = (entry.goal_title || "").trim();
  if (title) return formatUserFacingText(title);
  const metricKey = (entry.metric_key || "").trim();
  return metricKey ? formatUserFacingText(metricKey.replace(/_/g, " ")) : "";
}

/**
 * "Expected +1 toward “Daily finished video”" — the Strategist's prediction,
 * phrased so the number says what it refers to. Falls back to a neutral
 * "Expected impact +1" when nothing readable names the metric.
 */
export function proposalImpactLabel(entry: ProposalTaskEntry): string | null {
  if (typeof entry.metric_delta !== "number") return null;
  const delta = formatDelta(entry.metric_delta);
  const subject = impactSubject(entry);
  if (!subject) {
    return t("component.proposal.expected_impact_plain").replace("{delta}", delta);
  }
  return t("component.proposal.expected_impact")
    .replace("{delta}", delta)
    .replace("{goal}", subject);
}

/** One-sentence explainer: this is a prediction, later checked against reality. */
export function proposalImpactExplainer(): string {
  return t("component.proposal.expected_impact_hint");
}
