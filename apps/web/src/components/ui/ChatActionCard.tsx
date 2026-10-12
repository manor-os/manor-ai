/**
 * ChatActionCard — global UI for interactive chat actions.
 *
 * Variants:
 *   <ProposalCard>   — Approve + Feedback input (strategist proposals)
 *   <HitlInputCard>  — Text input + Submit (agent needs human info)
 *   <ApprovalCard>   — Simple approve-once / reject (plan approval, generic)
 *   <ResolvedBadge>  — Shows resolution state after action taken
 *
 * Used by: EmbeddedChat, FloatingChat, WorkspaceChat
 */
import { useEffect, useState, useRef, useId } from "react";
import {
  friendlyApprovalActionLabel,
  friendlyApprovalDescription,
  friendlyApprovalToolLabel,
  isErrorHitlCard,
  structuredApprovalCopy,
} from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import Chip from "./Chip";
import AnchoredPopover from "./AnchoredPopover";
import HitlKnowledgePicker from "./HitlKnowledgePicker";
import Input from "./Input";
import Modal from "./Modal";
import Select from "./Select";
import Textarea from "./Textarea";
import {
  APPROVAL_CHOICE_ALWAYS_APPROVE,
  APPROVAL_CHOICE_APPROVE,
  APPROVAL_CHOICE_REJECT,
  DEFAULT_APPROVAL_OPTIONS,
  currentApprovalOptions,
  oneTimeApprovalOptions,
  reviewApprovalOptions,
} from "../../lib/approvalOptions";
import { PendingActionKind } from "../../lib/pendingActionKinds";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";
import {
  WorkflowSchemaFields,
  parseWorkflowSchemaDraft,
  setWorkflowValueAtPath,
  visibleWorkflowSchemaFieldCount,
  workflowInputErrorPath,
  workflowSchemaDraft,
  workflowSchemaType,
  type WorkflowInputSchema,
} from "../workflows/WorkflowSchemaFields";
import WorkflowApprovalReview from "../workflows/WorkflowApprovalReview";
import ApprovalSaveRuleAction from "../approvals/ApprovalSaveRuleAction";
import {
  proposalApprovedRowIds,
  proposalImpactExplainer,
  proposalImpactLabel,
  proposalPriorityLabel,
  proposalTaskEntries,
} from "../../lib/proposalDisplay";

/* ── Types ── */

export interface PendingAction {
  kind: string;
  options?: string[];
  content?: unknown;
  args_preview?: unknown;
  operation?: unknown;
  /** Unified HitlRequest id backing this card (governance_approval /
   * runtime approval kinds). Resolving the card grants/denies that request.
   * The KEY name stays `approval_request_id`: it is a wire value living in
   * `pending_action` JSONB rows that are already persisted. */
  approval_request_id?: string | null;
  /** What kind of human involvement this is — "input" | "review" |
   * "authorize" | "choice" | "error". Straight off the HitlRequest row.
   * Optional: cards posted before the type system carry neither this nor
   * `payload`, and must keep rendering from `prompt` exactly as they did. */
  hitl_type?: string | null;
  /** Typed copy for `hitl_type`: what_happened / why / action_to_take /
   * action_link for `error`; question | action_description + why elsewhere. */
  payload?: Record<string, any> | null;
  /** Originating task — what the card deep-links to. */
  task_id?: string | null;
  [key: string]: any;
}

export interface Resolution {
  choice: string;
  note?: string;
  payload?: Record<string, any>;
}

type ApprovalTone = "approve" | "always" | "reject" | "secondary";

function normalizeChoice(choice: string): string {
  return String(choice || "").toLowerCase().replace(/[-\s]+/g, "_");
}

function approvalTone(choice: string): ApprovalTone {
  const normalized = normalizeChoice(choice);
  if (normalized === APPROVAL_CHOICE_ALWAYS_APPROVE) return "always";
  if (["approve", "approved", "approve_all", "approve_selected", "accept", "yes", "confirm", "continue_after_login"].includes(normalized)) return "approve";
  if (
    normalized === "revise"
    || normalized === "request_changes"
    || normalized === "cancel"
    || normalized === "confirm_effect_not_applied"
  ) return "secondary";
  if (
    ["reject", "rejected", "reject_all", "skip"].includes(normalized)
    || normalized === "no"
    || normalized === "deny"
    || normalized === "decline"
    || normalized === "stopped"
  ) return "reject";
  return "secondary";
}

function approvalLabel(choice: string): string {
  const normalized = normalizeChoice(choice);
  if (normalized === "approve_all") return t("component.chat_action_card.approve_all");
  if (normalized === "approve_selected") return t("component.chat_action_card.approve_selected");
  if (normalized === "reject_all") return t("component.chat_action_card.reject_all");
  if (normalized === "provide_answers" || normalized === "submit") return t("component.chat_action_card.submit");
  if (normalized === "confirm") return t("component.chat_action_card.confirm");
  if (normalized === "revise") return t("component.chat_action_card.revise");
  if (normalized === "request_changes") return t("page.task_detail.request_changes");
  if (normalized === "accept") return t("component.chat_action_card.accept");
  if (normalized === "cancel") return t("component.chat_action_card.cancel");
  if (normalized === "skip") return t("component.chat_action_card.skip");
  if (normalized === "sign_in") return t("component.chat_action_card.sign_in");
  if (normalized === "continue_after_login") return t("component.chat_action_card.continue");
  if (normalized === "retry" || normalized === "retry_now") return t("component.chat_action_card.retry");
  if (normalized === "confirm_effect_applied") return t("component.chat_action_card.effect_applied");
  if (normalized === "confirm_effect_not_applied") return t("component.chat_action_card.effect_not_applied");
  const tone = approvalTone(choice);
  if (tone === "always") return t("component.chat_action_card.always");
  if (tone === "reject") return t("component.approval_action_bar.reject");
  return t("component.approval_action_bar.approve");
}

function actionLabel(action?: string): string {
  return friendlyApprovalActionLabel(action);
}

function pathLabel(path: string): string {
  const normalized = String(path || "").trim();
  if ([".", "./", "/"].includes(normalized)) return t("component.chat_action_card.knowledge_root");
  return normalized;
}

function previewText(value: unknown): string | null {
  if (value == null) return null;
  let text = "";
  if (typeof value === "string") {
    text = value.trim();
  } else {
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value || "").trim();
    }
  }
  if (!text || text === "{}" || text === "[]") return null;
  const friendly = formatUserFacingText(text);
  return friendly.length > 1400 ? `${friendly.slice(0, 1400)}\n...` : friendly;
}

function asRecord(value: unknown): Record<string, any> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : null;
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  if (typeof value === "string" && value.trim()) return [value.trim()];
  return [];
}

function humanizeKey(value: string): string {
  return formatUserFacingLabel(value || "workspace changes");
}

function integrationEffectProviders(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value
    .filter((provider): provider is string => typeof provider === "string")
    .map((provider) => provider.trim())
    .filter(Boolean))];
}

function integrationProviderFromBinding(value: unknown): string | null {
  const binding = asRecord(value);
  if (binding?.capability_type === "mcp") {
    return String(binding.integration_key || "").trim() || null;
  }
  return String(binding?.tool_name || "").match(/^mcp__(.+?)__/)?.[1] || null;
}

/** Deep link back to the task a card came from.
 *
 *  Every governance card is about a step of some task, but only some of them
 *  used to say so. A plain anchor rather than a router <Link> so the card
 *  renders identically wherever it is mounted. */
export function CardOriginLink({ taskId }: { taskId?: string | null }) {
  const id = String(taskId || "").trim();
  if (!id) return null;
  return (
    <a className="chat-hitl-origin-link" href={`/tasks/${id}`}>
      {t("component.chat_action_card.view_task")}
    </a>
  );
}

/** Human label for an in-app route: "/integrations" → "Open integrations". */
function routeLinkLabel(link: string): string {
  const segment = String(link || "")
    .split(/[?#]/)[0]
    .split("/")
    .filter(Boolean)
    .pop();
  const name = segment ? humanizeKey(segment).toLowerCase() : "";
  return name
    ? t("component.chat_action_card.open_route").replace("{name}", name)
    : t("component.chat_action_card.open_link");
}

export function ApprovalSummary({ prompt, action, tool, hasWorkspace, paths, content, argsPreview, operation, hitlType, payload, taskId }: {
  prompt?: string;
  action?: string;
  tool?: string;
  hasWorkspace?: boolean;
  paths?: string[];
  content?: unknown;
  argsPreview?: unknown;
  operation?: unknown;
  /** `pending_action.hitl_type` / `.payload` — when present the card renders
   *  from them instead of rewriting the prompt. */
  hitlType?: string | null;
  payload?: Record<string, any> | null;
  taskId?: string | null;
}) {
  const shownPaths = (paths || [])
    .map((path) => String(path || "").trim())
    .filter(Boolean);
  const structured = structuredApprovalCopy(payload);
  const friendlyPrompt = friendlyApprovalDescription({
    prompt,
    action,
    tool,
    hasWorkspace,
    paths: shownPaths,
    content,
    argsPreview,
    operation,
    hitlType: hitlType || undefined,
    payload,
  });
  if (structured) {
    // Typed card: say what this is, why, and what to do — in the request's
    // own words, with shared display formatting applied to internal labels.
    return (
      <div className="chat-hitl-summary">
        <div className="chat-hitl-title">{structured.headline}</div>
        {structured.detail && (
          <div className="chat-hitl-description">{structured.detail}</div>
        )}
        {structured.actionToTake && (
          <div className="chat-hitl-action-to-take">
            <strong>{t("component.chat_action_card.what_to_do")}</strong>{" "}
            {structured.actionToTake}
          </div>
        )}
        <div className="chat-hitl-links">
          {structured.actionLink && (
            <a className="chat-hitl-action-link" href={structured.actionLink}>
              {routeLinkLabel(structured.actionLink)}
            </a>
          )}
          <CardOriginLink taskId={taskId} />
        </div>
      </div>
    );
  }
  const label = actionLabel(action);
  const isWorkspaceFileAction = String(action || "").toLowerCase().startsWith("workspace.file.");
  const pathCount = shownPaths.length;
  const title = isWorkspaceFileAction
    ? friendlyPrompt
    : pathCount === 1 && action
    ? `${label}?`
    : pathCount > 1 && action
      ? t("component.chat_action_card.files_question").replace("{action}", label).replace("{count}", String(pathCount))
      : friendlyPrompt || (action ? `${label}?` : t("component.chat_action_card.approval_needed"));
  const detailPrompt = friendlyPrompt && friendlyPrompt !== title ? friendlyPrompt : null;
  const previewPaths = shownPaths.slice(0, 4);
  const remainingPathCount = Math.max(0, shownPaths.length - previewPaths.length);
  // Only ever surface genuine human-readable text. Never dump the raw action
  // payload (tool keys, internal operation objects) into the chat.
  const rawContent = content ?? argsPreview ?? operation;
  const contentText = typeof rawContent === "string" ? previewText(rawContent) : null;
  return (
    <div className="chat-hitl-summary">
      <div className="chat-hitl-title">
        {title}
      </div>
      {detailPrompt && <div className="chat-hitl-description">{detailPrompt}</div>}
      {contentText && (
        <pre className="chat-hitl-content" aria-label={t("component.chat_action_card.approval_content_preview")}>
          {contentText}
        </pre>
      )}
      {previewPaths.length > 0 && (
        <div className="chat-hitl-paths" aria-label={t("component.chat_action_card.files_requiring_approval")}>
          {previewPaths.map((path) => (
            <code key={path}>{pathLabel(path)}</code>
          ))}
          {remainingPathCount > 0 && <span>{t("component.chat_action_card.more_count").replace("{count}", String(remainingPathCount))}</span>}
        </div>
      )}
      {taskId && (
        <div className="chat-hitl-links">
          <CardOriginLink taskId={taskId} />
        </div>
      )}
    </div>
  );
}

export function WorkspaceOperationReviewCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string) => void;
  disabled?: boolean;
}) {
  const operation = asRecord(action.operation) || asRecord(action) || {};
  const validation = asRecord(operation.validation);
  const changedKeys = stringList(operation.changed_keys);
  const patches = Array.isArray(operation.patches) ? operation.patches : [];
  const errors = Array.isArray(validation?.errors) ? validation.errors : [];
  const warnings = Array.isArray(validation?.warnings) ? validation.warnings : [];
  const missingSetup = stringList(validation?.missing_setup);
  const summary = String(operation.summary || "").trim();
  const invalid = errors.length > 0;
  // The typed `review` payload. `diff.removed_hard_blocks` lists the
  // never_allow patterns this draft would delete — the tier of governance
  // with no approval path at all, which `rules.replace` silently rebuilds.
  // Until this existed the card said only "Apply this workspace operation
  // draft?" while the draft dropped the hard block on billing.*.
  const reviewPayload = asRecord(action.payload);
  const reviewDiff = asRecord(reviewPayload?.diff);
  // Only the effective before/after diff proves a provider will change. A
  // patch or the integration_scope category alone may be an invalid/no-op draft.
  const integrationEffects = [
    { key: "disable", providers: integrationEffectProviders(reviewDiff?.disabled_integrations) },
    { key: "restore", providers: integrationEffectProviders(reviewDiff?.enabled_integrations) },
  ].filter(({ providers }) => providers.length > 0);
  const bindingDiff = asRecord(asRecord(asRecord(operation.diff)?.changes)?.capability_bindings);
  const beforeBindingKeys = new Set(
    (Array.isArray(bindingDiff?.before) ? bindingDiff.before : [])
      .map((row) => String(asRecord(row)?.binding_key || "")),
  );
  const afterBindingKeys = new Set(
    (Array.isArray(bindingDiff?.after) ? bindingDiff.after : [])
      .map((row) => String(asRecord(row)?.binding_key || "")),
  );
  const proposedBindings = bindingDiff
    ? (Array.isArray(bindingDiff.after) ? bindingDiff.after : [])
      .filter((row) => !beforeBindingKeys.has(String(asRecord(row)?.binding_key || "")))
    : patches.flatMap((patch) => {
      const entry = asRecord(patch);
      if (entry?.op !== "capability_binding.upsert") return [];
      const payload = asRecord(entry.payload);
      return Array.isArray(payload?.bindings) ? payload.bindings : [payload?.binding];
    });
  const addedIntegrations = changedKeys.includes("capability_bindings")
    ? integrationEffectProviders(proposedBindings.map(integrationProviderFromBinding))
    : [];
  const removedIntegrations = changedKeys.includes("capability_bindings") && bindingDiff
    ? integrationEffectProviders(
        (Array.isArray(bindingDiff.before) ? bindingDiff.before : [])
          .filter((row) => !afterBindingKeys.has(String(asRecord(row)?.binding_key || "")))
          .map(integrationProviderFromBinding),
      )
    : [];
  const integrationChanges = [
    { key: "add", providers: addedIntegrations },
    { key: "remove", providers: removedIntegrations },
    ...integrationEffects,
  ].filter(({ providers }) => providers.length > 0);
  const removedHardBlocks = stringList(
    reviewDiff?.removed_hard_blocks ?? operation.removed_hard_blocks,
  );
  const reviewWhy = String(reviewPayload?.why || "").trim();
  const disconnectedIntegrations = integrationEffectProviders(errors.flatMap((issue) => {
    const row = asRecord(issue);
    if (row?.code !== "integration_not_connected") return [];
    const provider = String(row.message || "").match(/Integration ['\"]([^'\"]+)['\"]/i)?.[1];
    return provider ? [provider] : addedIntegrations;
  }));
  const otherErrors = errors.filter((issue) => (
    asRecord(issue)?.code !== "integration_not_connected" || disconnectedIntegrations.length === 0
  ));

  const renderIssue = (issue: unknown, idx: number) => {
    const row = asRecord(issue);
    const path = row ? String(row.path || "").trim() : "";
    const message = formatUserFacingText(row ? String(row.message || issue || "").trim() : String(issue || "").trim());
    return (
      <li key={`${path || "issue"}-${idx}`}>
        {path && <code>{path}</code>} {message || "Review required"}
      </li>
    );
  };

  return (
    <div className="chat-hitl-summary chat-workspace-review">
      <div className="chat-hitl-title">
        {t("component.chat_action_card.workspace_changes_title")}
      </div>
      {integrationChanges.length > 0 ? (
        <dl className="chat-workspace-review__changes">
          {integrationChanges.map(({ key, providers }) => (
            <div className="chat-workspace-review__change" key={key}>
              <dt>{t(`component.chat_action_card.integrations_${key}_workspace`)}</dt>
              <dd>
                {providers.map((provider) => <Chip key={provider}>{humanizeKey(provider)}</Chip>)}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="chat-hitl-description">
          {formatUserFacingText(
            removedHardBlocks.length > 0 ? summary : reviewWhy || summary,
          ) || t("component.chat_action_card.workspace_changes_desc")}
        </div>
      )}

      {(removedIntegrations.length > 0 || integrationEffects.some(({ key }) => key === "disable")) && (
        <div className="chat-hitl-description">{t("component.chat_action_card.integrations_disable_scope_note")}</div>
      )}
      {integrationEffects.some(({ key }) => key === "restore") && (
        <div className="chat-hitl-description">{t("component.chat_action_card.integrations_restore_scope_note")}</div>
      )}

      {removedHardBlocks.length > 0 && (
        <div
          role="alert"
          className="chat-hitl-hard-block-warning"
          style={{
            marginTop: 10,
            padding: "10px 12px",
            borderRadius: 10,
            background: "#fef2f2",
            border: "2px solid #dc2626",
            color: "#7f1d1d",
            fontSize: 12,
          }}
        >
          <strong>{t("component.chat_action_card.removes_hard_blocks")}</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {removedHardBlocks.map((pattern) => (
              <li key={`hard-block-${pattern}`}><code>{pattern}</code></li>
            ))}
          </ul>
          <div style={{ marginTop: 6 }}>
            {t("component.chat_action_card.removes_hard_blocks_effect")}
          </div>
        </div>
      )}

      {disconnectedIntegrations.length > 0 && (
        <div className="chat-workspace-review__issue" role="alert">
          <strong>{t("component.chat_action_card.integration_connect_first", {
            names: disconnectedIntegrations.map(humanizeKey).join(", "),
          })}</strong>
          <div>{t("component.chat_action_card.integration_connect_hint")}</div>
          <a href="/integrations">{t("component.chat_action_card.open_integrations")}</a>
        </div>
      )}
      {otherErrors.length > 0 && (
        <div className="chat-workspace-review__issue" role="alert">
          <strong>{t("component.chat_action_card.fix_before_applying")}</strong>
          <ul>{otherErrors.slice(0, 4).map(renderIssue)}</ul>
        </div>
      )}
      {(warnings.length > 0 || missingSetup.length > 0) && (
        <details className="chat-workspace-review__notes">
          <summary>{t("component.chat_action_card.notes")}</summary>
          <ul>
            {missingSetup.slice(0, 3).map((item, idx) => (
              <li key={`missing-${item}-${idx}`}>{t("component.chat_action_card.missing_setup").replace("{item}", humanizeKey(item))}</li>
            ))}
            {warnings.slice(0, 3).map(renderIssue)}
          </ul>
        </details>
      )}

      <ApprovalCard
        // A review is a verdict on THIS diff. There is no coherent standing
        // version of "yes to whatever the next draft says", so the one-time
        // vocabulary is applied to whatever the blob carries.
        options={invalid ? ["reject"] : oneTimeApprovalOptions(action.options)}
        onResolve={onResolve}
        disabled={disabled}
        rejectLabel={invalid ? t("component.chat_action_card.discard_change") : undefined}
      />
    </div>
  );
}

/* ── Proposal Card (per-task selection + Approve/Reject/Feedback) ── */

/** M9.3 — the user-offerable rejection vocabulary. Mirrors
 * ``USER_REASON_CODES`` in packages/core/proposals/constants.py; the
 * system-only codes (POLICY_BLOCKED / STALE_REVISION / INSUFFICIENT_DATA)
 * are deliberately not offered here. */
const REJECT_REASON_CODES = [
  "WRONG_DIRECTION",
  "DUPLICATE",
  "TOO_EXPENSIVE",
  "BAD_TIMING",
  "NEEDS_CHANGES",
  "OTHER",
] as const;

function rejectReasonLabel(code: string): string {
  return t(`component.chat_action_card.reject_reason.${code.toLowerCase()}`);
}

/** Non-task cohort member (automation/workflow/goal change, experiment)
 *  carried by `pending_action.items`. Older cards omit the key entirely. */
interface ProposalItem {
  item_id: string;
  kind: string;
  action_key?: string;
  risk_level?: string;
  summary?: string;
}

function proposalItems(action?: PendingAction): ProposalItem[] {
  const raw = action?.items;
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((item) => item && typeof item === "object" && item.item_id)
    .map((item) => ({
      item_id: String(item.item_id),
      kind: String(item.kind || "item"),
      action_key: item.action_key ? String(item.action_key) : undefined,
      risk_level: item.risk_level ? String(item.risk_level) : undefined,
      summary: item.summary ? String(item.summary) : undefined,
    }));
}

function itemKindLabel(kind: string): string {
  const key = `component.chat_action_card.item_kind.${kind}`;
  const label = t(key);
  return label === key ? humanizeKey(kind) : label;
}

function itemRiskLabel(risk: string): string {
  const key = `component.chat_action_card.item_risk.${risk}`;
  const label = t(key);
  return label === key ? humanizeKey(risk) : label;
}

/** One checkable row of the unified cohort list. Tasks and non-task items
 *  share this shape so selection is a single set over row ids. */
interface ProposalRow {
  id: string;
  isTask: boolean;
  kind: string;
  riskLevel?: string;
  label: string;
  /** "High priority" — set only for the priorities worth calling out. */
  priorityLabel?: string | null;
  /** "Expected +1 toward “Daily video”" — the Strategist's own prediction. */
  impact?: string | null;
  isCritical?: boolean;
}

function proposalRows(action?: PendingAction): ProposalRow[] {
  const taskIds: string[] = action?.task_ids || [];
  const taskTitles: string[] = action?.task_titles || [];
  // Typed per-task payload. Cards posted before it shipped carry only
  // `task_titles`, so the extra fields degrade to absent — never to a guess.
  const entries = proposalTaskEntries(action?.tasks);
  const entriesById = new Map(
    entries.filter((entry) => entry.task_id).map((entry) => [entry.task_id!, entry]),
  );
  const taskRows: ProposalRow[] = taskIds.map((tid, i) => {
    const entry = entriesById.get(String(tid)) || entries[i];
    return {
      id: String(tid),
      isTask: true,
      kind: "task",
      label: formatUserFacingText(entry?.title || taskTitles[i]) || `Task ${i + 1}`,
      priorityLabel: entry ? proposalPriorityLabel(entry.priority) : null,
      impact: entry ? proposalImpactLabel(entry) : null,
      isCritical: entry?.priority === 5,
    };
  });
  const itemRows: ProposalRow[] = proposalItems(action).map((item) => ({
    id: item.item_id,
    isTask: false,
    kind: item.kind,
    riskLevel: item.risk_level,
    label: item.summary || itemKindLabel(item.kind),
  }));
  return [...taskRows, ...itemRows];
}

export function ProposalCard({
  action,
  onResolve,
  disabled,
  selectedRowIds,
  onSelectedRowIdsChange,
  rowsRenderedElsewhere,
}: {
  action?: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  selectedRowIds?: Set<string>;
  onSelectedRowIdsChange?: (selected: Set<string>) => void;
  rowsRenderedElsewhere?: boolean;
}) {
  const rows = proposalRows(action);
  const rowIds = rows.map((row) => row.id);
  const [internalSelected, setInternalSelected] = useState<Set<string>>(new Set(rowIds));
  const selected = selectedRowIds || internalSelected;
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [showRejectDialog, setShowRejectDialog] = useState(false);
  const [rejectReason, setRejectReason] = useState<string>("");
  const [rejectComment, setRejectComment] = useState("");

  const updateSelected = (next: Set<string>) => {
    if (onSelectedRowIdsChange) {
      onSelectedRowIdsChange(next);
    } else {
      setInternalSelected(next);
    }
  };

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    updateSelected(next);
  };
  const selectAll = () => updateSelected(new Set(rowIds));
  const selectNone = () => updateSelected(new Set());

  const handleApprove = () => {
    setSubmitting(true);
    const picked = rows.filter((row) => selected.has(row.id));
    if (picked.length === rows.length) {
      onResolve(APPROVAL_CHOICE_APPROVE);
    } else {
      onResolve("approve_selected", undefined, {
        selected_task_ids: picked.filter((row) => row.isTask).map((row) => row.id),
        selected_item_ids: picked.filter((row) => !row.isTask).map((row) => row.id),
      });
    }
  };





  const handleReject = () => {
    setShowRejectDialog(true);
  };

  const submitReject = () => {
    if (!rejectReason) return;
    setShowRejectDialog(false);
    setSubmitting(true);
    onResolve(
      APPROVAL_CHOICE_REJECT,
      rejectComment.trim() || undefined,
      { reason_code: rejectReason },
    );
  };

  const handleSendFeedback = () => {
    if (!feedback.trim()) return;
    setSubmitting(true);
    onResolve("feedback", feedback.trim());
  };

  const selectedCount = rows.filter((row) => selected.has(row.id)).length;
  const nothingSelected = selectedCount === 0;

  return (
    <div className="chat-proposal-actions-card">
      {/* One unified list — tasks and changes/experiments, each checkable */}
      {rows.length > 0 && !rowsRenderedElsewhere && (
        <div className="chat-proposal-task-list">
          {rows.length > 1 && (
            <div className="chat-proposal-select-controls">
              <button
                type="button"
                onClick={selectAll}
                disabled={submitting}
                className="chat-proposal-select-btn chat-proposal-select-btn--primary"
              >
                {t("component.chat_action_card.select_all")}
              </button>
              <button
                type="button"
                onClick={selectNone}
                disabled={submitting}
                className="chat-proposal-select-btn chat-proposal-select-btn--secondary"
              >
                {t("component.chat_action_card.clear")}
              </button>
            </div>
          )}
          {rows.map((row) => (
            <label
              key={row.id}
              className={`chat-proposal-task-option ${selected.has(row.id) ? "chat-proposal-task-option--selected" : ""}`}
            >
              <input
                type="checkbox"
                checked={selected.has(row.id)}
                onChange={() => toggle(row.id)}
                disabled={submitting}
                style={{ accentColor: "#436b65", flexShrink: 0 }}
              />
              <span className="chat-proposal-item-chips">
                <Chip size="sm" variant="slate">{itemKindLabel(row.kind)}</Chip>
                {row.riskLevel === "high" && (
                  <Chip size="sm" variant="red">{itemRiskLabel(row.riskLevel)}</Chip>
                )}
                {row.priorityLabel && (
                  <Chip size="sm" variant={row.isCritical ? "red" : "slate"}>
                    {row.priorityLabel}
                  </Chip>
                )}
              </span>
              <span>
                {row.label}
                {row.impact && (
                  <span
                    className="chat-proposal-impact"
                    title={proposalImpactExplainer()}
                  >
                    {row.impact}
                  </span>
                )}
              </span>
            </label>
          ))}
        </div>
      )}

      {/* Action buttons */}
      <div className="chat-hitl-actions">
        <button
          type="button"
          className="chat-hitl-btn-primary"
          onClick={handleApprove}
          disabled={disabled || submitting || nothingSelected}
        >
          {submitting
            ? "..."
            : selectedCount === rows.length
              ? t("component.chat_action_card.approve_all")
              : t("component.chat_action_card.approve_count").replace("{selected}", String(selectedCount)).replace("{total}", String(rows.length))}
        </button>
        <button
          type="button"
          className="chat-hitl-btn-secondary chat-hitl-btn-danger"
          onClick={handleReject}
          disabled={disabled || submitting}
        >
          {t("component.chat_action_card.reject_all")}
        </button>
        <button
          type="button"
          className="chat-hitl-btn-secondary"
          onClick={() => setShowFeedback(!showFeedback)}
          disabled={disabled || submitting}
          aria-expanded={showFeedback}
        >
          {showFeedback ? t("component.chat_action_card.cancel") : t("component.chat_action_card.feedback")}
        </button>
      </div>

      {/* Feedback textarea */}
      {showFeedback && (
        <div className="chat-hitl-input-card">
          <div style={{ marginBottom: 8 }}>
            <div className="chat-hitl-title" style={{ fontSize: 12 }}>{t("component.chat_action_card.feedback_title")}</div>
            <div className="chat-hitl-description">{t("component.chat_action_card.feedback_hint")}</div>
          </div>
          <div className="chat-hitl-input-row">
            <textarea
              aria-label={t("component.chat_action_card.feedback_title")}
              value={feedback}
              onChange={(e) => setFeedback(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSendFeedback(); } }}
              placeholder={t("component.chat_action_card.feedback_placeholder")}
              rows={2}
              disabled={submitting}
              className="chat-hitl-textarea"
            />
            <button
              type="button"
              onClick={handleSendFeedback}
              disabled={!feedback.trim() || submitting}
              className="chat-hitl-btn-primary"
            >
              {submitting ? t("component.chat_action_card.sending") : t("component.chat_action_card.send_feedback")}
            </button>
          </div>
        </div>
      )}

      {/* Reject dialog — required reason code + optional comment (M9.3) */}
      <Modal
        open={showRejectDialog}
        onClose={() => setShowRejectDialog(false)}
        title={t("component.chat_action_card.reject_dialog_title")}
        maxWidth="440px"
        footer={
          <>
            <button
              type="button"
              className="chat-hitl-btn-secondary"
              onClick={() => setShowRejectDialog(false)}
            >
              {t("component.chat_action_card.cancel")}
            </button>
            <button
              type="button"
              className="chat-hitl-btn-secondary chat-hitl-btn-danger"
              onClick={submitReject}
              disabled={!rejectReason}
            >
              {t("component.chat_action_card.reject_all")}
            </button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <div className="chat-hitl-description">
            {t("component.chat_action_card.reject_dialog_hint")}
          </div>
          <div role="radiogroup" aria-label={t("component.chat_action_card.reject_reason_label")} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            {REJECT_REASON_CODES.map((code) => (
              <label
                key={code}
                className={`chat-proposal-task-option ${rejectReason === code ? "chat-proposal-task-option--selected" : ""}`}
              >
                <input
                  type="radio"
                  name="proposal-reject-reason"
                  value={code}
                  checked={rejectReason === code}
                  onChange={() => setRejectReason(code)}
                  style={{ accentColor: "#436b65", flexShrink: 0 }}
                />
                <span>{rejectReasonLabel(code)}</span>
              </label>
            ))}
          </div>
          <label className="chat-hitl-field-label">
            <span>{t("component.chat_action_card.reject_comment_label")}</span>
            <textarea
              value={rejectComment}
              onChange={(e) => setRejectComment(e.target.value)}
              placeholder={t("component.chat_action_card.reject_comment_placeholder")}
              rows={2}
              className="chat-hitl-textarea"
            />
          </label>
        </div>
      </Modal>


    </div>
  );
}

/* ── Simple Approval Card (approve once / reject) ── */

export function ApprovalCard({ options, onResolve, disabled, blockApprove, rejectLabel }: {
  options?: string[];
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  /** Disable only the affirmative (approve) button — e.g. when a draft has
   *  validation errors — while keeping reject/dismiss clickable. */
  blockApprove?: boolean;
  rejectLabel?: string;
}) {
  // Historical cards cannot restore the retired, unscoped Always action.
  const opts = currentApprovalOptions(options);
  const [revisionRequest, setRevisionRequest] = useState("");
  const [revisionChoice, setRevisionChoice] = useState<string | null>(null);
  const showRevisionRequest = revisionChoice !== null;
  const submitRevision = () => {
    const value = revisionRequest.trim();
    if (!value || !revisionChoice || disabled) return;
    onResolve(revisionChoice, value, { review: { revision_request: value } });
  };
  return (
    <>
      <div className="chat-hitl-actions">
        {opts.map((opt) => {
          const tone = approvalTone(opt);
          const isApprove = tone === "approve" || tone === "always";
          const className = tone === "reject"
            ? "chat-hitl-btn-secondary chat-hitl-btn-danger"
            : tone === "always"
              ? "chat-hitl-btn-secondary chat-hitl-btn-quiet"
              : tone === "secondary"
                ? "chat-hitl-btn-secondary"
              : "chat-hitl-btn-primary";
          return (
            <button
              type="button"
              key={opt}
              className={className}
              onClick={() => {
                const normalized = normalizeChoice(opt);
                if (normalized === "revise" || normalized === "request_changes") {
                  setRevisionChoice((current) => (
                    current === normalized ? null : normalized
                  ));
                  return;
                }
                onResolve(opt);
              }}
              disabled={disabled || (blockApprove && isApprove)}
              title={
                blockApprove && isApprove
                  ? t("component.chat_action_card.approve_blocked_by_validation")
                  : undefined
              }
              aria-expanded={
                ["revise", "request_changes"].includes(normalizeChoice(opt))
                  ? showRevisionRequest
                  : undefined
              }
            >
              {tone === "reject" && rejectLabel ? rejectLabel : approvalLabel(opt)}
            </button>
          );
        })}
      </div>
      {showRevisionRequest && (
        <div className="chat-hitl-retry-guidance">
          <Textarea
            label={t("component.approval_action_bar.revision_instructions")}
            value={revisionRequest}
            onChange={(event) => setRevisionRequest(event.target.value)}
            placeholder={t("component.approval_action_bar.revision_placeholder")}
            rows={3}
            disabled={disabled}
          />
          <div className="chat-hitl-actions">
            <button
              type="button"
              className="chat-hitl-btn-primary"
              onClick={submitRevision}
              disabled={disabled || !revisionRequest.trim()}
            >
              {t("component.approval_action_bar.submit_revision")}
            </button>
          </div>
        </div>
      )}
    </>
  );
}

/* ── Error card (hitl_type === "error") ── */

/**
 * A step that already ran and FAILED. The user is not being asked for
 * permission — there is nothing left to authorize — so this card never
 * renders Approve / Always / Reject. It says what broke, why, what to do
 * about it, and links to the place where that is done.
 *
 * The two choices it does offer are the only honest ones: retry (I fixed it,
 * run it again) and cancel (give up on this step). "Approve" here is what let
 * one operator approve the same steps fifteen times without ever being told
 * their paired local worker daemon was offline.
 */
export const ERROR_CARD_OPTIONS = ["retry", "cancel"];

/** An allowlist, not a denylist. Every card in flight was posted with the
 *  approval vocabulary, and a denylist that forgets one of approve / always /
 *  reject puts that exact button back on a failure card. */
const ERROR_CARD_CHOICES = new Set([
  "retry",
  "retry_now",
  "confirm_effect_applied",
  "confirm_effect_not_applied",
  "cancel",
  "skip",
]);

type ReconciliationEffect = {
  effect_digest: string;
  action_group_id?: string;
  applied_result_required?: boolean;
  step_id?: string;
  tool_call_id?: string;
  arguments_hash?: string;
  effect?: string;
  tool?: string;
  action_key?: string;
  provider?: string;
};

function reconciliationEffects(action: PendingAction): ReconciliationEffect[] {
  const raw = action.payload?.reconciliation?.effects;
  if (!Array.isArray(raw)) return [];
  return raw.filter((effect): effect is ReconciliationEffect => (
    effect && typeof effect === "object"
    && typeof effect.effect_digest === "string"
    && Boolean(effect.effect_digest.trim())
  ));
}

function reconciliationGroups(effects: ReconciliationEffect[]) {
  const groups = new Map<string, ReconciliationEffect[]>();
  for (const effect of effects) {
    // New cards carry the server's exact grouping identity. For cards already
    // posted, group only receipts with the same Step and concrete tool call;
    // the resolver still validates every original digest before changing state.
    const legacyIdentity = effect.step_id && effect.tool && effect.tool_call_id
      && effect.arguments_hash
      ? JSON.stringify([
        effect.step_id, effect.tool, effect.tool_call_id,
        effect.arguments_hash, effect.effect, effect.action_key, effect.provider,
      ])
      : "";
    const key = effect.action_group_id || legacyIdentity || effect.effect_digest;
    groups.set(key, [...(groups.get(key) || []), effect]);
  }
  return [...groups].map(([key, members]) => ({ key, members }));
}

function EffectReconciliationCard({ action, onResolve, disabled, hasTaskReference }: {
  action: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  hasTaskReference?: boolean;
}) {
  const formId = useId();
  const groups = reconciliationGroups(reconciliationEffects(action));
  const [decisions, setDecisions] = useState<Record<string, string>>({});
  const [results, setResults] = useState<Record<string, string>>({});
  const [guidance, setGuidance] = useState("");
  const [error, setError] = useState("");
  const ready = groups.every(({ key, members }) => (
    Boolean(decisions[key])
    && (
      decisions[key] !== "applied"
      || members.every((effect) => effect.applied_result_required === false)
      || Boolean(String(results[key] || "").trim())
    )
  ));
  const retrySelected = groups.some(({ key }) => decisions[key] === "not_applied");

  const submit = () => {
    if (!ready || disabled) return;
    const byDigest: Record<string, string> = {};
    const resultsByDigest: Record<string, unknown> = {};
    for (const { key, members } of groups) {
      const raw = String(results[key] || "").trim();
      let parsed: unknown;
      if (decisions[key] === "applied" && raw) {
        try {
          parsed = JSON.parse(raw);
        } catch {
          setError(t("component.chat_action_card.effect_result_invalid"));
          return;
        }
      }
      for (const effect of members) {
        byDigest[effect.effect_digest] = decisions[key];
        if (decisions[key] === "applied" && raw) {
          resultsByDigest[effect.effect_digest] = parsed;
        }
      }
    }
    setError("");
    onResolve("reconcile_effects", guidance.trim() || undefined, {
      decisions: byDigest,
      results: resultsByDigest,
    });
  };

  return (
    <div className="chat-hitl-effect-card">
      <div className="chat-hitl-effect-header">
        <div className="chat-hitl-effect-title">{t("component.chat_action_card.effect_title")}</div>
        <p className="chat-hitl-effect-explanation">
          {t("component.chat_action_card.effect_explainer")}
        </p>
      </div>
      {groups.map(({ key, members }, index) => {
        const effect = members[0];
        const resultRequired = members.some((member) => member.applied_result_required !== false);
        const label = effect.action_key === "workspace.task.create"
          ? t("component.chat_action_card.effect_create_task")
          : effect.action_key
            ? friendlyApprovalActionLabel(effect.action_key)
            : effect.tool
              ? friendlyApprovalToolLabel(effect.tool)
              : t("component.chat_action_card.effect_unknown");
        const fieldId = `${formId}-result-${index}`;
        const resultLabel = t(resultRequired
          ? "component.chat_action_card.effect_result_required"
          : "component.chat_action_card.effect_result_optional");
        const resultField = (
          <Textarea
            id={fieldId}
            label={resultRequired ? resultLabel : undefined}
            ariaLabel={resultLabel}
            ariaDescribedBy={resultRequired ? `${fieldId}-help` : undefined}
            required={resultRequired}
            value={results[key] || ""}
            onChange={(event) => {
              setResults((current) => ({ ...current, [key]: event.target.value }));
              setError("");
            }}
            rows={2}
            disabled={disabled}
            className="chat-hitl-effect-field"
          />
        );
        return (
          <fieldset className="chat-hitl-effect-item" key={key} disabled={disabled}>
            <legend className="chat-hitl-effect-item-title">{label}</legend>
            <div className="chat-hitl-effect-choices">
              {(["applied", "not_applied"] as const).map((choice) => (
                <label className={`chat-hitl-effect-choice${decisions[key] === choice ? " is-selected" : ""}`} key={choice}>
                  <input
                    type="radio"
                    name={`effect-${key}`}
                    value={choice}
                    checked={decisions[key] === choice}
                    onChange={() => {
                      setDecisions((current) => ({ ...current, [key]: choice }));
                      setError("");
                    }}
                  />
                  <span>{t(`component.chat_action_card.${choice === "applied" ? "effect_happened" : "effect_not_happened"}`)}</span>
                </label>
              ))}
            </div>
            {decisions[key] === "applied" && (resultRequired ? (
              <div className="chat-hitl-effect-field">
                {resultField}
                <p id={`${fieldId}-help`} className="chat-hitl-effect-explanation">
                  {t("component.chat_action_card.effect_result_help")}
                </p>
              </div>
            ) : (
              <details className="chat-hitl-effect-details">
                <summary>{resultLabel}</summary>
                {resultField}
              </details>
            ))}
          </fieldset>
        );
      })}
      {retrySelected && (
        <details className="chat-hitl-effect-details">
          <summary>{t("component.chat_action_card.effect_guidance")}</summary>
          <Textarea
            ariaLabel={t("component.chat_action_card.effect_guidance")}
            value={guidance}
            onChange={(event) => setGuidance(event.target.value)}
            rows={2}
            disabled={disabled}
            className="chat-hitl-effect-field"
          />
        </details>
      )}
      {error && <p className="chat-hitl-effect-error" role="alert">{error}</p>}
      <div className="chat-hitl-effect-actions">
        <button type="button" className="chat-hitl-btn-primary" onClick={submit} disabled={disabled || !ready}>
          {t("component.chat_action_card.effect_verify")}
        </button>
        <button type="button" className="chat-hitl-btn-secondary" onClick={() => onResolve("cancel")} disabled={disabled}>
          {t("component.chat_action_card.effect_stop")}
        </button>
        {!hasTaskReference && <CardOriginLink taskId={action.task_id} />}
      </div>
    </div>
  );
}

function taskRecoveryCopy(action: PendingAction) {
  const structured = structuredApprovalCopy(action.payload);
  if (action.kind !== PendingActionKind.TASK_RECOVERY) return structured;
  const issue = typeof action.payload?.why === "string" ? action.payload.why.trim() : "";
  const recordedAction = issue.match(
    /^Step ['"](.+?)['"] recorded an external action\. A new plan cannot safely reuse its prior idempotency identity; reconcile that action before replanning\.?$/i,
  );
  if (!recordedAction) return structured;
  return {
    headline: t("component.chat_action_card.recorded_action_title"),
    detail: t("component.chat_action_card.recorded_action_detail", {
      step: formatUserFacingText(recordedAction[1]),
    }),
    actionToTake: null,
    actionLink: structured?.actionLink || null,
  };
}

export function HitlErrorCard({ action, onResolve, disabled, hasTaskReference }: {
  action: PendingAction;
  /** Omitted for read-only recovery history. */
  onResolve?: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  /** Workspace Chat already renders this task as a card below the action. */
  hasTaskReference?: boolean;
}) {
  // Optional guidance can change the next attempt's input. The server still
  // decides whether recovery is safe; rendering Retry does not grant replay.
  const [guidance, setGuidance] = useState("");
  if (onResolve && reconciliationEffects(action).length > 0) {
    return <EffectReconciliationCard action={action} onResolve={onResolve} disabled={disabled} hasTaskReference={hasTaskReference} />;
  }
  const structured = taskRecoveryCopy(action);
  const headline =
    structured?.headline
    || (typeof action.prompt === "string" && action.prompt.trim())
    || t("component.chat_action_card.action_needed");
  const options = (action.options || []).filter((opt) =>
    ERROR_CARD_CHOICES.has(normalizeChoice(opt)),
  );
  const taskLink = action.task_id ? `/tasks/${String(action.task_id).trim()}` : null;
  const actionLink = structured?.actionLink;
  const hasSeparateActionLink = actionLink && actionLink.replace(/\/+$/, "") !== taskLink;
  return (
    <div className="chat-hitl-error-card">
      <p className="chat-hitl-error-title">{headline}</p>
      {(structured?.detail || structured?.actionToTake) && (
        <details className="chat-hitl-error-details">
          <summary>{t("component.chat_action_card.error_details")}</summary>
          {structured.detail && <p>{structured.detail}</p>}
          {structured.actionToTake && <p>{structured.actionToTake}</p>}
        </details>
      )}
      {onResolve && (
        <details className="chat-hitl-error-details">
          <summary>{t("component.chat_action_card.effect_guidance")}</summary>
          <Textarea
            ariaLabel={t("component.chat_action_card.effect_guidance")}
            value={guidance}
            onChange={(e) => setGuidance(e.target.value)}
            placeholder={t("component.chat_action_card.retry_guidance_placeholder")}
            rows={2}
            disabled={disabled}
            className="chat-hitl-retry-guidance"
          />
        </details>
      )}
      <div className="chat-hitl-error-actions">
        {onResolve && (
          <ApprovalCard
            options={options.length ? options : ERROR_CARD_OPTIONS}
            onResolve={(choice) => onResolve(choice, guidance.trim() || undefined)}
            disabled={disabled}
          />
        )}
        {hasSeparateActionLink && (
          <a className="chat-hitl-action-link" href={actionLink}>
            {routeLinkLabel(actionLink)}
          </a>
        )}
        {!hasTaskReference && <CardOriginLink taskId={action.task_id} />}
      </div>
    </div>
  );
}

/* ── External message approval (show exact outbound content) ── */

export function ExternalMessageApprovalCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string) => void;
  disabled?: boolean;
}) {
  const channel = String(action.channel_type || action.channel || action.provider || "").trim();
  // Only show a human recipient name — never a raw chat_id / sender_id.
  const recipient = String(
    action.recipient ||
    action.recipient_name ||
    action.to ||
    "",
  ).trim();
  const draft = previewText(
    action.reply_text ??
    action.text ??
    action.message ??
    action.content ??
    action.args_preview,
  );

  return (
    <>
      <div className="chat-hitl-summary">
        <div className="chat-hitl-title">
          {t("component.chat_action_card.external_message_approval_title")}
        </div>
        <div className="chat-hitl-description">
          {t("component.chat_action_card.external_message_approval_desc")}
        </div>
        {(channel || recipient) && (
          <div className="chat-hitl-paths">
            {channel && (
              <code>
                {t("component.chat_action_card.external_message_channel")} {channel}
              </code>
            )}
            {recipient && (
              <code>
                {t("component.chat_action_card.external_message_recipient")} {recipient}
              </code>
            )}
          </div>
        )}
        {draft && (
          <pre className="chat-hitl-content" aria-label={t("component.chat_action_card.external_message_draft")}>
            {draft}
          </pre>
        )}
      </div>
      <ApprovalCard
        options={action.options || DEFAULT_APPROVAL_OPTIONS}
        onResolve={onResolve}
        disabled={disabled}
      />
    </>
  );
}

/* ── Retry Card (failed background automation) ── */

export function RetryActionCard({ onResolve, disabled }: {
  onResolve: () => void;
  disabled?: boolean;
}) {
  return (
    <div className="chat-hitl-actions">
      <button
        type="button"
        className="chat-hitl-btn-primary"
        onClick={onResolve}
        disabled={disabled}
      >
        {t("component.chat_action_card.retry_strategist")}
      </button>
    </div>
  );
}

/* ── HITL Input Card (text input + submit) ── */

export interface Attachment {
  name: string;
  id?: string;
  type: "file" | "knowledge";
  file?: File;
}

type WorkflowStarterInput = {
  key: string;
  label?: string;
  type?: "string" | "number" | "boolean" | "json";
  required?: boolean;
  hidden?: boolean;
  placeholder?: string;
  default?: unknown;
  description?: string;
  schema?: WorkflowInputSchema;
};

function formatWorkflowInputValue(value: unknown, type: string): string {
  if (value === undefined || value === null) return type === "boolean" ? "false" : "";
  if (type === "json" && typeof value !== "string") {
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  }
  return String(value);
}

function WorkflowStarterInputCard({ action, onResolve, disabled, workspaceId, submitChoice = "run" }: {
  action: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  workspaceId?: string;
  submitChoice?: "run" | "respond";
}) {
  const inputs = (Array.isArray(action.inputs) ? action.inputs : [])
    .filter((item: unknown): item is WorkflowStarterInput => Boolean(
      item && typeof item === "object" && String((item as WorkflowStarterInput).key || "").trim(),
    ));
  const visibleInputs = inputs.filter((input) => !input.hidden);
  const structuredInputs = visibleInputs.filter((input) => Boolean(input.schema));
  const useUnifiedSchemaGrid = structuredInputs.length > 0
    && structuredInputs.length === visibleInputs.length;
  const structuredSchema: WorkflowInputSchema = {
    type: "object",
    properties: Object.fromEntries(structuredInputs.map((input) => [
      input.key,
      {
        ...(input.schema || {}),
        title: input.label || input.schema?.title || humanizeKey(input.key),
        description: input.description || input.schema?.description,
      },
    ])),
    required: structuredInputs.filter((input) => input.required).map((input) => input.key),
    "x-ui": { order: structuredInputs.map((input) => input.key) },
  };
  const initialValues = asRecord(action.values) || {};
  const [expanded, setExpanded] = useState(true);
  const [values, setValues] = useState<Record<string, unknown>>(() => Object.fromEntries(
    inputs.map((input) => [
      input.key,
      input.schema
        ? workflowSchemaDraft(input.schema, initialValues[input.key] ?? input.default)
        : formatWorkflowInputValue(initialValues[input.key] ?? input.default, input.type || "string"),
    ]),
  ));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [fileBusy, setFileBusy] = useState(false);

  const submit = () => {
    if (fileBusy) return;
    const parsed: Record<string, unknown> = {};
    const nextErrors: Record<string, string> = {};
    for (const input of inputs) {
      const type = input.type || "string";
      const currentValue = values[input.key];
      if (input.schema) {
        parsed[input.key] = parseWorkflowSchemaDraft(
          input.schema,
          currentValue,
          input.key,
          Boolean(input.required),
          nextErrors,
        );
        continue;
      }
      const raw = String(currentValue ?? "");
      if (!raw.trim()) {
        if (input.required) nextErrors[input.key] = t("component.workspace_chat.workflow_input_required");
        else if (type === "string") parsed[input.key] = "";
        continue;
      }
      if (type === "number") {
        const number = Number(raw);
        if (!Number.isFinite(number)) nextErrors[input.key] = t("component.workspace_chat.workflow_input_invalid_number");
        else parsed[input.key] = number;
      } else if (type === "boolean") {
        parsed[input.key] = raw === "true";
      } else if (type === "json") {
        try { parsed[input.key] = JSON.parse(raw); }
        catch { nextErrors[input.key] = t("component.workspace_chat.workflow_input_invalid_json"); }
      } else {
        parsed[input.key] = raw.trim();
      }
    }
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    onResolve(submitChoice, undefined, { inputs: parsed });
  };

  const visibleFieldCount = visibleInputs.reduce(
    (total, input) => total + (
      input.schema ? visibleWorkflowSchemaFieldCount(input.schema) : 1
    ),
    0,
  );

  return (
    <div className="workflow-starter-input-card">
      <button
        type="button"
        className="workflow-starter-input-toggle"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
      >
        <span className="workflow-starter-input-toggle-copy">
          <strong>{action.title || t("component.workspace_chat.workflow_inputs")}</strong>
          {action.description && <small>{String(action.description)}</small>}
        </span>
        <span className="mono">{visibleFieldCount}</span>
      </button>
      {expanded && (
        <div className="workflow-starter-input-body">
          {useUnifiedSchemaGrid && (
            <WorkflowSchemaFields
              rootKey=""
              schema={structuredSchema}
              value={values}
              errors={errors}
              disabled={disabled}
              workspaceId={workspaceId}
              onFileBusyChange={setFileBusy}
              onChange={(path, nextValue) => {
                setValues((current) => (
                  setWorkflowValueAtPath(current, path, nextValue) as Record<string, unknown>
                ));
                setErrors((current) => ({
                  ...current,
                  [workflowInputErrorPath("", path)]: "",
                }));
              }}
            />
          )}
          {!useUnifiedSchemaGrid && visibleInputs.map((input) => {
            const type = input.type || "string";
            const id = `workflow-starter-input-${input.key}`;
            if (input.schema?.["x-ui"]?.control === "knowledge_file") {
              return <WorkflowSchemaFields
                key={input.key}
                rootKey=""
                schema={{
                  type: "object",
                  properties: { [input.key]: {
                    ...input.schema,
                    title: input.label || input.schema.title || humanizeKey(input.key),
                    description: input.description || input.schema.description,
                  } },
                  required: input.required ? [input.key] : [],
                }}
                value={values}
                errors={errors}
                disabled={disabled}
                workspaceId={workspaceId}
                onFileBusyChange={setFileBusy}
                onChange={(path, nextValue) => {
                  setValues((current) => setWorkflowValueAtPath(current, path, nextValue) as Record<string, unknown>);
                  setErrors((current) => ({ ...current, [workflowInputErrorPath("", path)]: "" }));
                }}
              />;
            }
            if (input.schema && workflowSchemaType(input.schema) === "object") {
              return (
                <div key={input.key} className="workflow-starter-input-object">
                  <div className="workflow-starter-input-object-heading">
                    <span>{input.label || humanizeKey(input.key)}{input.required ? " *" : ""}</span>
                    {input.description && <small>{input.description}</small>}
                  </div>
                  <WorkflowSchemaFields
                    rootKey={input.key}
                    schema={input.schema}
                    value={values[input.key]}
                    errors={errors}
                    disabled={disabled}
                    workspaceId={workspaceId}
                    onChange={(path, nextValue) => {
                      setValues((current) => ({
                        ...current,
                        [input.key]: setWorkflowValueAtPath(current[input.key], path, nextValue),
                      }));
                      setErrors((current) => ({
                        ...current,
                        [workflowInputErrorPath(input.key, path)]: "",
                      }));
                    }}
                  />
                </div>
              );
            }
            return (
              <label key={input.key} className="workflow-starter-input-field" htmlFor={id}>
                <span>
                  {input.label || humanizeKey(input.key)}
                  {input.required ? " *" : ""}
                </span>
                {type === "boolean" ? (
                  <input
                    type="checkbox"
                    id={id}
                    checked={String(values[input.key] || "false") === "true"}
                    onChange={(event) => setValues((current) => ({
                      ...current,
                      [input.key]: event.target.checked ? "true" : "false",
                    }))}
                    disabled={disabled}
                    className="workflow-starter-input-checkbox"
                  />
                ) : (
                  <textarea
                    id={id}
                    value={String(values[input.key] || "")}
                    onChange={(event) => {
                      setValues((current) => ({ ...current, [input.key]: event.target.value }));
                      setErrors((current) => ({ ...current, [input.key]: "" }));
                    }}
                    rows={type === "json" ? 3 : 2}
                    inputMode={type === "number" ? "decimal" : undefined}
                    placeholder={input.placeholder}
                    disabled={disabled}
                  />
                )}
                {errors[input.key] && <small>{errors[input.key]}</small>}
              </label>
            );
          })}
          <div className="workflow-starter-input-actions">
            <button type="button" className="chat-hitl-btn-secondary" onClick={() => onResolve("cancel")} disabled={disabled}>
              {t("component.chat_action_card.cancel")}
            </button>
            <button type="button" className="chat-hitl-btn-primary" onClick={submit} disabled={disabled || fileBusy}>
              {submitChoice === "respond" ? t("component.chat_action_card.submit") : t("component.workspace_chat.run_workflow")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function WorkflowRetryCard({ action, onResolve, disabled, workspaceId }: {
  action: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  workspaceId?: string;
}) {
  const schema = asRecord(action.editable_input_schema) as WorkflowInputSchema | null;
  const suppliedValues = {
    ...(asRecord(action.values) || {}),
    retry_segment_ids: action.retry_segment_ids || (asRecord(action.values) || {}).retry_segment_ids,
  };
  const [values, setValues] = useState<unknown>(() => (
    schema ? workflowSchemaDraft(schema, suppliedValues) : {}
  ));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const retryFrom = String(action.retry_from_step_id || action.step_id || "").trim();
  const observedProblems = Array.isArray(action.observed_problem)
    ? stringList(action.observed_problem)
    : [];
  const preservedCount = Array.isArray(action.preserved_receipts)
    ? action.preserved_receipts.length
    : 0;

  const submit = () => {
    const nextErrors: Record<string, string> = {};
    const parsed = schema
      ? parseWorkflowSchemaDraft(schema, values, "variables", false, nextErrors)
      : {};
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    onResolve("retry", undefined, { variables: parsed });
  };

  return (
    <div className="workflow-starter-input-card workflow-retry-card">
      <div className="workflow-retry-card-heading">
        <strong>{t("component.workspace_chat.workflow_retry_title")}</strong>
        <span>{formatUserFacingText(String(action.phase || action.business_outcome || "execution"))}</span>
      </div>
      {observedProblems.length > 0 ? (
        <ul className="workflow-retry-card-problems">
          {observedProblems.map((problem) => (
            <li key={problem}>{formatUserFacingText(problem)}</li>
          ))}
        </ul>
      ) : action.observed_problem && (
        <p>{formatUserFacingText(String(action.observed_problem))}</p>
      )}
      {action.required_change && (
        <small>{formatUserFacingText(String(action.required_change))}</small>
      )}
      {preservedCount > 0 && (
        <span className="workflow-retry-card-receipts mono">
          {t("component.workspace_chat.workflow_preserved_receipts", { count: preservedCount })}
        </span>
      )}
      {schema && Object.keys(schema.properties || {}).length > 0 && (
        <WorkflowSchemaFields
          rootKey="variables"
          schema={schema}
          value={values}
          errors={errors}
          disabled={disabled}
          workspaceId={workspaceId}
          onChange={(path, nextValue) => {
            setValues((current: unknown) => setWorkflowValueAtPath(current, path, nextValue));
            setErrors((current) => ({
              ...current,
              [workflowInputErrorPath("variables", path)]: "",
            }));
          }}
        />
      )}
      <div className="workflow-starter-input-actions">
        <button type="button" className="chat-hitl-btn-secondary" onClick={() => onResolve("cancel")} disabled={disabled}>
          {t("component.chat_action_card.cancel")}
        </button>
        <button type="button" className="chat-hitl-btn-primary" onClick={submit} disabled={disabled}>
          {t("component.workspace_chat.workflow_retry_from", { step: retryFrom })}
        </button>
      </div>
    </div>
  );
}

export function HitlInputCard({ action, onResolve, placeholder, disabled, resetToken, showSkip = true }: {
  action?: PendingAction;
  onResolve: (
    choice: string,
    note?: string,
    payload?: Record<string, any>,
    files?: File[],
  ) => void;
  placeholder?: string;
  disabled?: boolean;
  resetToken?: number;
  showSkip?: boolean;
}) {
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const attachButtonRef = useRef<HTMLButtonElement>(null);
  // Keep the note and File objects while a failed upload/resolve becomes retryable.
  useEffect(() => { setSubmitting(false); }, [resetToken]);
  const inputPayload = asRecord(action?.payload);
  const question = typeof inputPayload?.question === "string"
    ? inputPayload.question.trim()
    : typeof action?.prompt === "string" ? action.prompt.trim() : "";
  const context = typeof inputPayload?.context === "string"
    ? inputPayload.context.trim()
    : typeof inputPayload?.why === "string" ? inputPayload.why.trim() : "";
  // Inline metadata HITL synthesizes approval options without a Task/Step
  // origin. Only persisted input cards may use their top-level options.
  const rawOptions = inputPayload?.options
    || ((action?.task_id || action?.step_id) ? action.options : []);
  const options = Array.isArray(rawOptions)
    ? rawOptions.filter((option): option is string => typeof option === "string" && Boolean(option.trim()))
    : [];

  const handleSubmit = () => {
    if ((!value.trim() && attachments.length === 0) || submitting || disabled) return;
    setSubmitting(true);
    const localFiles = attachments.flatMap((attachment) =>
      attachment.file ? [attachment.file] : [],
    );
    const serializableAttachments = attachments
      .filter((attachment) => !attachment.file)
      .map(({ name, id, type }) => ({ name, id, type }));
    onResolve(
      "respond",
      value.trim(),
      serializableAttachments.length > 0
        ? { response: value.trim(), attachments: serializableAttachments }
        : undefined,
      localFiles.length > 0 ? localFiles : undefined,
    );
  };

  const handleFiles = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) {
      const newFiles: Attachment[] = Array.from(e.target.files).map((f) => ({
        name: f.name, type: "file" as const, file: f,
      }));
      setAttachments((prev) => [...prev, ...newFiles.filter((next) => !prev.some((existing) => existing.file === next.file))]);
    }
    e.target.value = "";
  };

  const removeAttachment = (idx: number) => setAttachments((a) => a.filter((_, i) => i !== idx));

  return (
    <div className="chat-hitl-input-card">
      {(question || context) && (
        <div className="chat-hitl-summary">
          {question && <div className="chat-hitl-title">{question}</div>}
          {context && <div className="chat-hitl-description">{context}</div>}
        </div>
      )}
      {options.length > 0 && (
        <div className="chat-hitl-input-options" role="group" aria-label={question || t("component.chat_action_card.type_your_response")}>
          {options.map((option) => (
            <button
              key={option}
              type="button"
              className="chat-hitl-input-option"
              aria-pressed={value.trim() === option}
              onClick={() => setValue(option)}
              disabled={disabled || submitting}
            >
              {option}
            </button>
          ))}
        </div>
      )}
      <input ref={fileRef} type="file" multiple accept="*/*" hidden disabled={disabled || submitting} onChange={handleFiles} />
      {/* Attachment pills */}
      {attachments.length > 0 && (
        <div className="chat-hitl-attachments">
          {attachments.map((a, i) => (
            <span key={a.id || `${a.name}-${i}`} className="chat-hitl-attachment">
              {a.type === "knowledge" ? (
                <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" /></svg>
              ) : (
                <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32" /></svg>
              )}
              <span className="chat-hitl-attachment-name" title={a.name}>{a.name}</span>
              <button type="button" disabled={disabled || submitting} aria-label={`${t("page.public_chat.remove_attachment")}: ${a.name}`} onClick={() => removeAttachment(i)}>&times;</button>
            </span>
          ))}
        </div>
      )}
      <div className="chat-hitl-input-row">
        <AnchoredPopover
          ariaLabel={t("component.chat_action_card.attach")}
          align="left"
          width={210}
          panelClassName="chat-hitl-attach-menu"
          trigger={
          <button
            ref={attachButtonRef}
            type="button"
            disabled={disabled || submitting}
            title={t("component.chat_action_card.attach")}
            aria-label={t("component.chat_action_card.attach")}
            className="chat-hitl-attach-trigger"
          >
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          }
        >
          {({ close }) => (
            <>
              <button
                type="button"
                disabled={disabled || submitting}
                onClick={() => { close(); fileRef.current?.click(); }}
              >
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" /></svg>
                {t("component.chat_action_card.local_file")}
              </button>
              <button
                type="button"
                disabled={disabled || submitting}
                onClick={() => {
                  close();
                  setKnowledgeOpen(true);
                }}
              >
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" /></svg>
                {t("component.chat_action_card.knowledge_base")}
              </button>
            </>
          )}
        </AnchoredPopover>
        <textarea
          value={value}
          aria-label={question || t("component.chat_action_card.type_your_response")}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); handleSubmit(); } }}
          placeholder={placeholder || t("component.chat_action_card.type_your_response")}
          rows={1}
          disabled={disabled || submitting}
          className="chat-hitl-textarea"
        />
        <button
          onClick={handleSubmit}
          disabled={(!value.trim() && attachments.length === 0) || submitting || disabled}
          className="chat-hitl-btn-primary"
        >
          {submitting ? t("component.chat_action_card.sending") : t("component.chat_action_card.submit")}
        </button>
        {showSkip && (
          <button
            onClick={() => onResolve("skip")}
            disabled={submitting || disabled}
            className="chat-hitl-btn-secondary"
          >
            {t("component.chat_action_card.skip")}
          </button>
        )}
      </div>
      {knowledgeOpen && (
        <HitlKnowledgePicker
          selectedIds={new Set(attachments.flatMap((attachment) => attachment.id ? [attachment.id] : []))}
          onToggle={(document) => setAttachments((current) => current.some((attachment) => attachment.id === document.id)
            ? current.filter((attachment) => attachment.id !== document.id)
            : [...current, { id: document.id, name: document.name, type: "knowledge" }])}
          onClose={() => setKnowledgeOpen(false)}
          restoreFocus={() => attachButtonRef.current?.focus()}
        />
      )}
    </div>
  );
}

/* ── Tool-level pending actions ── */

function questionKey(question: any, index: number): string {
  if (typeof question === "string") return question || `question_${index}`;
  return String(question?.key || question?.name || question?.id || question?.label || `question_${index}`);
}

function questionLabel(question: any, index: number): string {
  if (typeof question === "string") return question;
  if (question?.label || question?.title) {
    return String(question.label || question.title);
  }
  // Fall back to the field name, but humanize it so a raw key
  // (e.g. "target_account") never shows as the label.
  if (question?.name) return formatUserFacingLabel(String(question.name));
  return t("component.chat_action_card.question").replace("{index}", String(index + 1));
}

function optionLabel(option: any): string {
  if (option == null) return "";
  if (typeof option === "object") return String(option.label || option.name || option.value || "");
  return String(option);
}

function optionValue(option: any): string {
  if (option == null) return "";
  if (typeof option === "object") return String(option.value ?? option.id ?? option.label ?? option.name ?? "");
  return String(option);
}

export function NeedsInputCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
}) {
  const questions: any[] = Array.isArray(action.questions) ? action.questions : [];
  const [answers, setAnswers] = useState<Record<string, any>>({});
  const [submitting, setSubmitting] = useState(false);

  const setAnswer = (key: string, value: any) => {
    setAnswers((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = () => {
    if (submitting) return;
    setSubmitting(true);
    onResolve("provide_answers", undefined, { answers });
  };

  return (
    <div className="chat-hitl-input-card">
      {(action.title || action.context_summary) && (
        <div className="chat-hitl-summary">
          {action.title && <div className="chat-hitl-title">{action.title}</div>}
          {action.context_summary && <div className="chat-hitl-description">{action.context_summary}</div>}
        </div>
      )}
      <div className="chat-hitl-question-list">
        {questions.map((question, index) => {
          const key = questionKey(question, index);
          const label = questionLabel(question, index);
          const type = String(typeof question === "object" ? question.type || "" : "").toLowerCase();
          const options: any[] = Array.isArray(question?.options) ? question.options : [];
          const value = answers[key] ?? "";
          return (
            <div key={key} className="chat-hitl-question-field">
              <div className="chat-hitl-question-label">
                {label}
                {question?.required && (
                  <span className="chat-hitl-question-required" aria-hidden="true">*</span>
                )}
              </div>
              <div className="chat-hitl-question-control">
                {options.length > 0 ? (
                  <Select
                    value={String(value)}
                    onChange={(nextValue) => setAnswer(key, nextValue)}
                    disabled={disabled || submitting}
                    ariaLabel={label}
                    placeholder={question?.required ? t("component.chat_action_card.select") : t("component.chat_action_card.optional")}
                    options={options.map((option) => ({
                      value: optionValue(option),
                      label: optionLabel(option),
                    }))}
                    style={{ width: "100%" }}
                    dropdownMinWidth={220}
                  />
                ) : type === "checkbox" || type === "boolean" ? (
                  <input
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(e) => setAnswer(key, e.target.checked)}
                    disabled={disabled || submitting}
                    style={{ width: 16, height: 16, accentColor: "#436b65" }}
                  />
                ) : type === "textarea" || type === "multiline" ? (
                  <Textarea
                    value={String(value)}
                    onChange={(e) => setAnswer(key, e.target.value)}
                    disabled={disabled || submitting}
                    ariaLabel={label}
                    placeholder={String(question?.placeholder || "")}
                    rows={3}
                  />
                ) : (
                  <Input
                    type={type === "number" ? "number" : "text"}
                    value={String(value)}
                    onChange={(e) => setAnswer(key, e.target.value)}
                    disabled={disabled || submitting}
                    ariaLabel={label}
                    placeholder={String(question?.placeholder || "")}
                  />
                )}
              </div>
            </div>
          );
        })}
      </div>
      <div className="chat-hitl-actions" style={{ marginTop: 10 }}>
        <button
          onClick={handleSubmit}
          disabled={disabled || submitting}
          className="chat-hitl-btn-primary"
        >
          {submitting ? t("component.chat_action_card.submitting") : t("component.chat_action_card.submit")}
        </button>
        <button
          onClick={() => onResolve("skip")}
          disabled={disabled || submitting}
          className="chat-hitl-btn-secondary chat-hitl-btn-danger"
        >
          {t("component.chat_action_card.skip")}
        </button>
      </div>
    </div>
  );
}

export function NeedsLoginCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string) => void;
  disabled?: boolean;
}) {
  const [opened, setOpened] = useState(false);
  const loginUrl = String(action.login_url || "");
  const title = String(action.title || t("component.chat_action_card.sign_in_required"));

  const openLogin = () => {
    if (!loginUrl) return;
    window.open(loginUrl, "_blank", "noopener,noreferrer");
    setOpened(true);
  };

  return (
    <div className="chat-hitl-input-card">
      <div className="chat-hitl-summary">
        <div className="chat-hitl-title">{title}</div>
        {action.integration_hint && <div className="chat-hitl-description">{String(action.integration_hint)}</div>}
      </div>
      <div className="chat-hitl-actions">
        <button
          onClick={openLogin}
          disabled={disabled || !loginUrl}
          className="chat-hitl-btn-primary"
        >
          {t("component.chat_action_card.sign_in")}
        </button>
        <button
          onClick={() => onResolve("continue_after_login")}
          disabled={disabled}
          className={opened ? "chat-hitl-btn-primary" : "chat-hitl-btn-secondary"}
        >
          {t("component.chat_action_card.continue")}
        </button>
        <button
          onClick={() => onResolve("skip")}
          disabled={disabled}
          className="chat-hitl-btn-secondary chat-hitl-btn-danger"
        >
          {t("component.chat_action_card.skip")}
        </button>
      </div>
    </div>
  );
}

export function NeedsConfirmationCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string) => void;
  disabled?: boolean;
}) {
  const main =
    (typeof action.title === "string" && action.title.trim()) ||
    (typeof action.action_summary === "string" && action.action_summary.trim()) ||
    "";
  const impact = typeof action.impact === "string" ? action.impact.trim() : "";
  const prompt =
    [main, impact].filter(Boolean).join(" — ") ||
    t("component.chat_action_card.please_confirm");
  return (
    <>
      <ApprovalSummary prompt={prompt} action="confirm" />
      <ApprovalCard options={action.options || ["confirm", "cancel"]} onResolve={onResolve} disabled={disabled} />
    </>
  );
}

/* ── Resolved Badge (shows after action taken) ── */

function ResolvedActionSummary({ action, hasTaskReference }: { action: PendingAction; hasTaskReference?: boolean }) {
  if (!action?.kind || action.kind === "unknown" || action.kind === "approve_proposals") {
    return null;
  }

  if (isErrorHitlCard(action.hitl_type) && reconciliationEffects(action).length === 0) {
    return <HitlErrorCard action={action} hasTaskReference={hasTaskReference} />;
  }

  if (
    action.review != null
    && (action.kind === "workflow_approval" || action.hitl_type === "review")
  ) {
    return (
      <WorkflowApprovalReview
        prompt={action.prompt}
        review={action.review}
        reviewTitle={action.review_title}
      />
    );
  }

  const title = String(action.title || action.review_title || action.action_summary || "").trim();
  const description = String(action.context_summary || action.description || "").trim();
  if (title || description) {
    return (
      <div className="chat-hitl-summary">
        {title && <div className="chat-hitl-title">{title}</div>}
        {description && <div className="chat-hitl-description">{description}</div>}
      </div>
    );
  }

  if (
    action.prompt ||
    action.action ||
    action.tool ||
    action.content ||
    action.args_preview ||
    action.operation ||
    action.paths ||
    action.payload
  ) {
    return (
      <ApprovalSummary
        prompt={action.prompt}
        action={action.action || action.kind}
        tool={action.tool}
        hasWorkspace={Boolean(action.workspace?.id || action.workspace?.name)}
        paths={action.paths}
        content={action.content}
        argsPreview={action.args_preview}
        operation={action.operation}
        hitlType={action.hitl_type}
        payload={action.payload}
        taskId={action.task_id}
      />
    );
  }

  return null;
}

export function ResolvedBadge({ resolution, by }: { resolution: Resolution; by?: string }) {
  const choice = resolution.choice || "";
  const tone = approvalTone(choice);
  const normalized = normalizeChoice(choice);
  const isRetry = normalized === "retry" || normalized === "retry_now";
  const isSkipped = normalized === "skip";
  const isCancelled = normalized === "cancel" || normalized === "stopped";
  const isApprove = tone === "approve" || tone === "always";
  const isFeedback = normalized === "feedback";
  const isRespond = ["respond", "provide_answers", "submit"].includes(normalized);
  const isReject = tone === "reject";
  const isRevise = normalized === "revise" || normalized === "request_changes";
  const isNeutral = !isApprove && !isReject && !isFeedback && !isRespond && !isCancelled;

  const label = normalized === "superseded" ? t("component.chat_action_card.superseded")
    : normalized === "expired" ? t("component.chat_action_card.expired")
    : isRetry ? t("component.chat_action_card.retry_requested")
    : isCancelled ? t("component.status.cancelled")
    : isSkipped ? t("component.chat_action_card.skipped")
    : isRevise ? t("component.chat_action_card.revision_requested")
    : isFeedback ? t("component.chat_action_card.feedback_sent")
    : isRespond ? t("component.chat_action_card.responded")
    : tone === "always" ? t("component.chat_action_card.always_approved")
    : isApprove ? (normalized === "approve_selected" ? t("component.chat_action_card.partially_approved") : t("component.chat_action_card.approved"))
    : isReject ? t("component.chat_action_card.rejected")
    : t("component.chat_action_card.status_unknown");
  const variant = isNeutral
    ? "chat-hitl-resolved--revised"
    : (isApprove || isFeedback || isRespond)
      ? "chat-hitl-resolved--approved"
      : "chat-hitl-resolved--rejected";
  const icon = isRevise ? "↻ " : isNeutral ? "— " : (isApprove || isFeedback || isRespond) ? "✓ " : "✗ ";

  return (
    <div className={`chat-hitl-resolved ${variant}`}>
      {icon}{label}
      {by && (
        <span className="chat-hitl-resolved-by">
          {" "}{t("component.chat_action_card.resolved_by").replace("{name}", by)}
        </span>
      )}
      {resolution.note && <span className="chat-hitl-resolved-note"> — {resolution.note}</span>}
    </div>
  );
}

/** A proposal can resolve several independent rows with one click. The generic
 * status badge says who decided; this compact ledger says exactly what that
 * decision covered, so "partially approved" is never an opaque state. */
export function ProposalResolution({ action, resolution, by }: {
  action: PendingAction;
  resolution: Resolution;
  by?: string;
}) {
  const rows = proposalRows(action);
  const normalized = normalizeChoice(resolution.choice);
  const isDecision =
    normalized.includes("approve")
    || normalized.includes("reject")
    || normalized === "decline"
    || normalized === "no";
  if (!isDecision || rows.length === 0) {
    return <ResolvedBadge resolution={resolution} by={by} />;
  }

  const approvedIds = proposalApprovedRowIds(
    resolution,
    rows.map((row) => row.id),
  ) || new Set<string>();
  const isApproval = normalized.includes("approve");

  return (
    <div className="chat-proposal-resolution">
      <div className={`chat-hitl-resolved ${isApproval ? "chat-hitl-resolved--approved" : "chat-hitl-resolved--rejected"}`}>
        {isApproval ? "✓ " : "✗ "}
        {t("component.chat_action_card.proposal_decision")}
        {by && (
          <span className="chat-hitl-resolved-by">
            {" "}{t("component.chat_action_card.resolved_by").replace("{name}", by)}
          </span>
        )}
      </div>
      <ul className="chat-proposal-resolution-details">
        {rows.map((row) => {
          const approved = approvedIds.has(row.id);
          return (
            <li key={row.id} className="chat-proposal-resolution-row">
              <span className="chat-proposal-resolution-icon" aria-hidden="true">
                {approved ? "✓" : "—"}
              </span>
              <span>
                <span className="chat-proposal-resolution-label">
                  {t(
                    approved
                      ? "component.chat_action_card.approved_items"
                      : "component.chat_action_card.not_approved_items",
                  )}
                </span>
                <span className="chat-proposal-resolution-title">{row.label}</span>
              </span>
            </li>
          );
        })}
      </ul>
      {resolution.note && (
        <div className="chat-proposal-resolution-note">{resolution.note}</div>
      )}
    </div>
  );
}

/* ── Composite: renders the right card based on pending_action ── */

export default function ChatActionCard({ action, resolved, resolution, onResolve, disabled, resolvedByName, currentUserName, resetToken, proposalSelectedRowIds, onProposalSelectedRowIdsChange, proposalRowsRenderedElsewhere, workspaceId, hasTaskReference }: {
  action: PendingAction;
  resolved?: boolean;
  resolution?: Resolution | null;
  disabled?: boolean;
  /** Display name of whoever resolved this action (from the server). */
  resolvedByName?: string;
  /** Fallback name shown for an optimistic local resolution (the viewer). */
  currentUserName?: string;
  /** Increment after a failed request so an optimistic action becomes interactive again. */
  resetToken?: number;
  /** Workspace proposal cards render their selectable rows inside Proposed
   * Work. Other chat surfaces keep the ProposalCard's own unified row list. */
  proposalSelectedRowIds?: Set<string>;
  onProposalSelectedRowIdsChange?: (selected: Set<string>) => void;
  proposalRowsRenderedElsewhere?: boolean;
  workspaceId?: string;
  hasTaskReference?: boolean;
  onResolve: (
    choice: string,
    note?: string,
    payload?: Record<string, any>,
    files?: File[],
  ) => void;
}) {
  const [localResolution, setLocalResolution] = useState<Resolution | null>(null);
  const submittedRef = useRef(false);
  const previousResetTokenRef = useRef(resetToken);
  // Uploading is not resolved. Keep input cards mounted until server confirmation
  // so an error can unlock the same draft, including its original File objects.
  const isInputCard = action.kind === PendingActionKind.HUMAN_INPUT || action.kind === PendingActionKind.WORKFLOW_INPUT;
  const effectiveResolution = resolution || ((resolved || !isInputCard) ? localResolution : null) || (resolved ? { choice: "resolved" } : null);
  const locked = Boolean(disabled || resolved || localResolution || submittedRef.current);

  useEffect(() => {
    if (previousResetTokenRef.current === resetToken) return;
    previousResetTokenRef.current = resetToken;
    submittedRef.current = false;
    setLocalResolution(null);
  }, [resetToken]);

  const resolveOnce = (
    choice: string,
    note?: string,
    payload?: Record<string, any>,
    files?: File[],
  ) => {
    if (locked) return;
    submittedRef.current = true;
    setLocalResolution({ choice, note, payload });
    onResolve(choice, note, payload, files);
  };

  if (effectiveResolution) {
    // Server-provided resolver wins; an optimistic local resolution is always
    // the current viewer.
    const by = resolvedByName || (localResolution ? currentUserName : undefined);
    if (action.kind === PendingActionKind.APPROVE_PROPOSALS) {
      return <ProposalResolution action={action} resolution={effectiveResolution} by={by} />;
    }
    return (
      <div className="chat-hitl-resolved-card">
        <ResolvedActionSummary action={action} hasTaskReference={hasTaskReference} />
        <ResolvedBadge resolution={effectiveResolution} by={by} />
      </div>
    );
  }

  if (!action?.kind) {
    return null;
  }

  // Type first, kind second: an `error` is a failure report whatever plane
  // posted it, and must never be rendered as a request for permission.
  if (isErrorHitlCard(action.hitl_type)) {
    return (
      <HitlErrorCard
        action={action}
        onResolve={resolveOnce}
        disabled={locked}
        hasTaskReference={hasTaskReference}
      />
    );
  }

  if (action.kind === PendingActionKind.HUMAN_INPUT) {
    return <HitlInputCard action={action} onResolve={resolveOnce} disabled={locked} resetToken={resetToken} showSkip={false} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_STARTER_INPUT) {
    return <WorkflowStarterInputCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_RETRY) {
    return <WorkflowRetryCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_INPUT) {
    if (Array.isArray(action.inputs) && action.inputs.length > 0) {
      return <WorkflowStarterInputCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} submitChoice="respond" />;
    }
    return (
      <HitlInputCard
        onResolve={resolveOnce}
        placeholder={typeof action.prompt === "string" ? action.prompt : undefined}
        disabled={locked}
        resetToken={resetToken}
      />
    );
  }

  if (action.kind === "needs_input") {
    return <NeedsInputCard action={action} onResolve={resolveOnce} disabled={locked} />;
  }

  if (action.kind === "needs_login") {
    return <NeedsLoginCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === "needs_confirmation") {
    return <NeedsConfirmationCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === "approve_proposals") {
    return (
      <ProposalCard
        action={action}
        onResolve={resolveOnce}
        disabled={locked}
        selectedRowIds={proposalSelectedRowIds}
        onSelectedRowIdsChange={onProposalSelectedRowIdsChange}
        rowsRenderedElsewhere={proposalRowsRenderedElsewhere}
      />
    );
  }

  if (action.kind === "retry_strategist_review") {
    return <RetryActionCard onResolve={() => resolveOnce("retry")} disabled={locked} />;
  }

  if (action.kind === "workspace_operation_review") {
    return <WorkspaceOperationReviewCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === "external_message_approval") {
    return <ExternalMessageApprovalCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (
    action.review != null
    && (
      action.kind === PendingActionKind.WORKFLOW_APPROVAL
      || action.kind === PendingActionKind.TASK_APPROVAL
      || action.hitl_type === "review"
    )
  ) {
    return (
      <>
        <WorkflowApprovalReview
          prompt={action.prompt}
          review={action.review}
          reviewTitle={action.review_title}
        />
        {action.kind === PendingActionKind.TASK_APPROVAL && action.task_id && (
          <div className="chat-hitl-links">
            <CardOriginLink taskId={action.task_id} />
          </div>
        )}
        <ApprovalCard
          options={reviewApprovalOptions(action.options)}
          onResolve={resolveOnce}
          disabled={locked}
        />
      </>
    );
  }

  return (
    <>
      {(action.prompt || action.action || action.tool || action.content || action.args_preview || action.operation || action.paths || action.payload) && (
        <ApprovalSummary
          prompt={action.prompt}
          action={action.action || action.kind}
          tool={action.tool}
          hasWorkspace={Boolean(action.workspace?.id || action.workspace?.name)}
          paths={action.paths}
          content={action.content}
          argsPreview={action.args_preview}
          operation={action.operation}
          hitlType={action.hitl_type}
          payload={action.payload}
          taskId={action.task_id}
        />
      )}
      <ApprovalCard
        options={
          action.hitl_type === "review"
            ? reviewApprovalOptions(action.options)
            : action.options
        }
        onResolve={resolveOnce}
        disabled={locked}
      />
      {action.approval_request_id && action.hitl_type !== "review" && <ApprovalSaveRuleAction key={action.approval_request_id} requestId={action.approval_request_id} scopeLabel={action.workspace?.name} disabled={locked} onCompleted={() => setLocalResolution({ choice: "approve", note: t("approval_settings.approve_save_rule") })} />}
    </>
  );
}
