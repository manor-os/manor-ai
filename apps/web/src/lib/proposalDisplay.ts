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
  /** Reports and ledger evidence the Strategist relied on for this task. */
  basis?: {
    report_refs: string[];
    evidence_refs: string[];
  };
  /** Plain-language facts resolved from the raw refs for display. */
  basis_display?: {
    report_domains: string[];
    signals: Array<{ description: string; domain?: string }>;
  };
  /** Present only together with `metric_delta` — the goal the number moves. */
  goal_id?: string;
  goal_title?: string;
  metric_key?: string;
  /** The Strategist's predicted change to the linked goal's metric. */
  metric_delta?: number;
}

export interface ProposalDecisionResolution {
  choice?: string | null;
  payload?: Record<string, unknown> | null;
}

function decisionIds(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
}

/** Resolve the final approval state for every row on a proposal card.
 * Returns null for non-decision outcomes such as feedback, so those cards can
 * keep their ordinary resolved treatment. */
export function proposalApprovedRowIds(
  resolution: ProposalDecisionResolution | null | undefined,
  allRowIds: string[],
): Set<string> | null {
  const choice = String(resolution?.choice || "")
    .toLowerCase()
    .replace(/[-\s]+/g, "_");
  if (["approve", "approve_all", "always_approve"].includes(choice)) {
    return new Set(allRowIds);
  }
  if (choice === "approve_selected") {
    const payload = resolution?.payload || {};
    return new Set([
      ...decisionIds(payload.approved_task_ids || payload.selected_task_ids),
      ...decisionIds(payload.approved_item_ids || payload.selected_item_ids),
    ]);
  }
  if (
    [
      "reject",
      "rejected",
      "reject_all",
      "deny",
      "decline",
      "no",
      "cancel",
      "cancelled",
      "stopped",
    ].includes(choice)
  ) {
    return new Set();
  }
  return null;
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
    .map((entry) => {
      const basis = entry.basis && typeof entry.basis === "object"
        ? entry.basis as Record<string, unknown>
        : null;
      const basisRefs = (value: unknown) => Array.isArray(value)
        ? value.map((ref) => String(ref || "").trim()).filter(Boolean)
        : [];
      const reportRefs = basisRefs(basis?.report_refs);
      const evidenceRefs = basisRefs(basis?.evidence_refs);
      const basisDisplay = entry.basis_display && typeof entry.basis_display === "object"
        ? entry.basis_display as Record<string, unknown>
        : null;
      const reportDomains = basisRefs(basisDisplay?.report_domains);
      const signals = Array.isArray(basisDisplay?.signals)
        ? basisDisplay.signals
          .filter((signal): signal is Record<string, unknown> =>
            Boolean(signal && typeof signal === "object" && signal.description))
          .map((signal) => ({
            description: String(signal.description).trim(),
            domain: signal.domain ? String(signal.domain).trim() : undefined,
          }))
          .filter((signal) => signal.description)
        : [];
      return {
        task_id: entry.task_id ? String(entry.task_id) : undefined,
        title: String(entry.title),
        priority: typeof entry.priority === "number" ? entry.priority : undefined,
        rationale: entry.rationale ? String(entry.rationale) : undefined,
        basis: reportRefs.length || evidenceRefs.length
          ? { report_refs: reportRefs, evidence_refs: evidenceRefs }
          : undefined,
        basis_display: reportDomains.length || signals.length
          ? { report_domains: reportDomains, signals }
          : undefined,
        goal_id: entry.goal_id ? String(entry.goal_id) : undefined,
        goal_title: entry.goal_title ? String(entry.goal_title) : undefined,
        metric_key: entry.metric_key ? String(entry.metric_key) : undefined,
        metric_delta:
          typeof entry.metric_delta === "number" ? entry.metric_delta : undefined,
      };
    });
}

const BASIS_DOMAIN_I18N_KEYS: Record<string, string> = {
  goal: "component.proposal.basis_domain_goal",
  execution: "component.proposal.basis_domain_execution",
  automation_portfolio: "component.proposal.basis_domain_automation",
  artifact_knowledge: "component.proposal.basis_domain_artifacts",
  human_participation: "component.proposal.basis_domain_human_input",
  capacity_cost: "component.proposal.basis_domain_capacity",
  risk_governance: "component.proposal.basis_domain_risk",
  learning_evidence: "component.proposal.basis_domain_learning",
};

const BASIS_SOURCE_I18N_KEYS: Record<string, string> = {
  task: "component.proposal.basis_source_tasks",
  goal: "component.proposal.basis_domain_goal",
  scheduled_job: "component.proposal.basis_domain_automation",
  step: "component.proposal.basis_source_workflow",
  workflow: "component.proposal.basis_source_workflow",
  approval_request: "component.proposal.basis_domain_risk",
  human_commitment: "component.proposal.basis_domain_human_input",
  runtime_event: "component.proposal.basis_domain_execution",
  workspace_event: "component.proposal.basis_source_activity",
};

export interface ProposalBasisView {
  sources: string[];
  signals: string[];
}

function uniqueLabels(values: string[]): string[] {
  return values.filter((value, index) => value && values.indexOf(value) === index);
}

function proposalBasisDomainLabel(domain: string): string {
  const key = BASIS_DOMAIN_I18N_KEYS[domain.toLowerCase()];
  return t(key || "component.proposal.basis_source_review");
}

function proposalBasisSourceLabel(ref: string): string {
  const prefix = ref.toLowerCase().split(":", 1)[0];
  const key = BASIS_SOURCE_I18N_KEYS[prefix];
  return t(key || "component.proposal.basis_source_activity");
}

/**
 * User-facing explanation of a task's basis. Raw report ids and ledger ids
 * remain in the typed payload for auditability, but never reach the card.
 */
export function proposalBasisView(entry: ProposalTaskEntry): ProposalBasisView | null {
  const display = entry.basis_display;
  const domains = display?.report_domains?.length
    ? display.report_domains
    : (entry.basis?.report_refs || []);
  const signalDomains = (display?.signals || [])
    .map((signal) => signal.domain || "")
    .filter(Boolean);
  const sources = uniqueLabels([
    ...domains.map(proposalBasisDomainLabel),
    ...signalDomains.map(proposalBasisDomainLabel),
    ...(entry.basis?.evidence_refs || []).map(proposalBasisSourceLabel),
  ]);
  const signals = uniqueLabels(
    (display?.signals || []).map((signal) => signal.description.trim()),
  );
  return sources.length || signals.length ? { sources, signals } : null;
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
