/**
 * ChatActionCard — global UI for interactive chat actions.
 *
 * Variants:
 *   <ProposalCard>   — Approve + Feedback input (strategist proposals)
 *   <HitlInputCard>  — Text input + Submit (agent needs human info)
 *   <ApprovalCard>   — Simple approve-once / always-approve / reject (plan approval, generic)
 *   <ResolvedBadge>  — Shows resolution state after action taken
 *
 * Used by: EmbeddedChat, FloatingChat, WorkspaceChat
 */
import { useEffect, useState, useRef } from "react";
import {
  friendlyApprovalActionLabel,
  friendlyApprovalDescription,
  isErrorHitlCard,
  structuredApprovalCopy,
} from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import Chip from "./Chip";
import Modal from "./Modal";
import {
  APPROVAL_CHOICE_ALWAYS_APPROVE,
  APPROVAL_CHOICE_APPROVE,
  APPROVAL_CHOICE_REJECT,
  DEFAULT_APPROVAL_OPTIONS,
  oneTimeApprovalOptions,
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
import {
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
}

type ApprovalTone = "approve" | "always" | "reject" | "secondary";

function normalizeChoice(choice: string): string {
  return String(choice || "").toLowerCase().replace(/[-\s]+/g, "_");
}

function approvalTone(choice: string): ApprovalTone {
  const normalized = normalizeChoice(choice);
  if (normalized.includes("always")) return "always";
  if (normalized === "revise") return "secondary";
  if (
    normalized.includes("reject")
    || normalized.includes("cancel")
    || normalized.includes("skip")
    || normalized === "no"
    || normalized === "deny"
    || normalized === "decline"
    || normalized === "stopped"
  ) return "reject";
  return "approve";
}

function approvalLabel(choice: string): string {
  const normalized = normalizeChoice(choice);
  if (normalized === "approve_all") return t("component.chat_action_card.approve_all");
  if (normalized === "approve_selected") return t("component.chat_action_card.approve_selected");
  if (normalized === "reject_all") return t("component.chat_action_card.reject_all");
  if (normalized === "provide_answers" || normalized === "submit") return t("component.chat_action_card.submit");
  if (normalized === "confirm") return t("component.chat_action_card.confirm");
  if (normalized === "revise") return t("component.chat_action_card.revise");
  if (normalized === "accept") return t("component.chat_action_card.accept");
  if (normalized === "cancel") return t("component.chat_action_card.cancel");
  if (normalized === "skip") return t("component.chat_action_card.skip");
  if (normalized === "sign_in") return t("component.chat_action_card.sign_in");
  if (normalized === "continue_after_login") return t("component.chat_action_card.continue");
  if (normalized === "retry" || normalized === "retry_now") return t("component.chat_action_card.retry");
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
    // own words. No prompt rewriting runs here, so nothing can be swallowed.
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
  const removedHardBlocks = stringList(
    reviewDiff?.removed_hard_blocks ?? operation.removed_hard_blocks,
  );
  const reviewWhy = String(reviewPayload?.why || "").trim();

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
    <div className="chat-hitl-summary">
      <div className="chat-hitl-title">
        {t("component.chat_action_card.workspace_changes_title")}
      </div>
      <div className="chat-hitl-description">
        {formatUserFacingText(summary) || t("component.chat_action_card.workspace_changes_desc")}
      </div>

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

      {reviewWhy && removedHardBlocks.length === 0 && (
        <div className="chat-hitl-description" style={{ marginTop: 6 }}>
          {reviewWhy}
        </div>
      )}

      <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 10 }}>
        {(changedKeys.length ? changedKeys : ["workspace changes"]).map((key) => (
          <code key={key} style={{
            padding: "4px 8px",
            borderRadius: 999,
            background: "#f5f5f4",
            color: "#78716c",
            fontSize: 11,
            fontWeight: 600,
          }}>
            {humanizeKey(key)}
          </code>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8, marginTop: 10 }}>
        <div style={{ padding: "8px 10px", borderRadius: 10, background: "#fafaf9", border: "1px solid #e7e5e4" }}>
          <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "#a8a29e", fontWeight: 800 }}>{t("component.chat_action_card.changes")}</div>
          <div style={{ fontSize: 16, color: "#0f172a", fontWeight: 900 }}>{patches.length || "1+"}</div>
        </div>
        <div style={{ padding: "8px 10px", borderRadius: 10, background: invalid ? "#fef2f2" : "#f0fdf4", border: `1px solid ${invalid ? "#fecaca" : "#bbf7d0"}` }}>
          <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: invalid ? "#b91c1c" : "#15803d", fontWeight: 800 }}>{t("component.chat_action_card.review")}</div>
          <div style={{ fontSize: 13, color: invalid ? "#991b1b" : "#166534", fontWeight: 900 }}>{invalid ? t("component.chat_action_card.issue_count").replace("{count}", String(errors.length)) : t("component.chat_action_card.passed")}</div>
        </div>
        <div style={{ padding: "8px 10px", borderRadius: 10, background: "#fff7ed", border: "1px solid #fed7aa" }}>
          <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: "0.08em", color: "#c2410c", fontWeight: 800 }}>{t("component.chat_action_card.decision")}</div>
          <div style={{ fontSize: 13, color: "#9a3412", fontWeight: 900 }}>{t("component.chat_action_card.required")}</div>
        </div>
      </div>


      {errors.length > 0 && (
        <div style={{ marginTop: 10, padding: "10px 12px", borderRadius: 10, background: "#fef2f2", border: "1px solid #fecaca", color: "#991b1b", fontSize: 12 }}>
          <strong>{t("component.chat_action_card.fix_before_applying")}</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {errors.slice(0, 4).map(renderIssue)}
          </ul>
        </div>
      )}

      {(warnings.length > 0 || missingSetup.length > 0) && (
        <div style={{ marginTop: 10, padding: "10px 12px", borderRadius: 10, background: "#fffbeb", border: "1px solid #fde68a", color: "#92400e", fontSize: 12 }}>
          <strong>{t("component.chat_action_card.notes")}</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
            {missingSetup.slice(0, 3).map((item, idx) => (
              <li key={`missing-${item}-${idx}`}>{t("component.chat_action_card.missing_setup").replace("{item}", humanizeKey(item))}</li>
            ))}
            {warnings.slice(0, 3).map(renderIssue)}
          </ul>
        </div>
      )}

      <ApprovalCard
        // A review is a verdict on THIS diff. There is no coherent standing
        // version of "yes to whatever the next draft says", so the one-time
        // vocabulary is applied to whatever the blob carries.
        options={oneTimeApprovalOptions(action.options)}
        onResolve={onResolve}
        disabled={disabled}
        blockApprove={invalid}
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

export function ProposalCard({ action, onResolve, disabled }: {
  action?: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
}) {
  const rows = proposalRows(action);
  const rowIds = rows.map((row) => row.id);
  const [selected, setSelected] = useState<Set<string>>(new Set(rowIds));
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [showRejectDialog, setShowRejectDialog] = useState(false);
  const [rejectReason, setRejectReason] = useState<string>("");
  const [rejectComment, setRejectComment] = useState("");
  const [showAlwaysConfirm, setShowAlwaysConfirm] = useState(false);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const selectAll = () => setSelected(new Set(rowIds));
  const selectNone = () => setSelected(new Set());

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

  const handleAlwaysApprove = () => {
    setShowAlwaysConfirm(true);
  };

  const confirmAlwaysApprove = () => {
    setShowAlwaysConfirm(false);
    setSubmitting(true);
    onResolve(APPROVAL_CHOICE_ALWAYS_APPROVE);
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

  const selectedCount = selected.size;
  const nothingSelected = selectedCount === 0;

  return (
    <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
      {/* One unified list — tasks and changes/experiments, each checkable */}
      {rows.length > 0 && (
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
          className="chat-hitl-btn-secondary chat-hitl-btn-quiet"
          onClick={handleAlwaysApprove}
          disabled={disabled || submitting || rows.length === 0}
        >
          {t("component.chat_action_card.always_approve")}
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

      {/* Always-approve confirmation — states the standing scope (M8) */}
      <Modal
        open={showAlwaysConfirm}
        onClose={() => setShowAlwaysConfirm(false)}
        title={t("component.chat_action_card.always_approve")}
        maxWidth="440px"
        footer={
          <>
            <button
              type="button"
              className="chat-hitl-btn-secondary"
              onClick={() => setShowAlwaysConfirm(false)}
            >
              {t("component.chat_action_card.cancel")}
            </button>
            <button
              type="button"
              className="chat-hitl-btn-primary"
              onClick={confirmAlwaysApprove}
            >
              {t("component.chat_action_card.always_approve")}
            </button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <p style={{ color: "#57534e", fontSize: 14, lineHeight: 1.6, margin: 0 }}>
            {t("component.chat_action_card.always_approve_scope")}
          </p>
          <p style={{ color: "#78716c", fontSize: 13, lineHeight: 1.6, margin: 0 }}>
            {t("component.chat_action_card.always_approve_scope_manage")}
          </p>
        </div>
      </Modal>
    </div>
  );
}

/* ── Simple Approval Card (approve once / always approve / reject) ── */

export function ApprovalCard({ options, onResolve, disabled, blockApprove }: {
  options?: string[];
  onResolve: (choice: string) => void;
  disabled?: boolean;
  /** Disable only the affirmative (approve) button — e.g. when a draft has
   *  validation errors — while keeping reject/dismiss clickable. */
  blockApprove?: boolean;
}) {
  // The card renders exactly the vocabulary the producer posted. "Always" is
  // the user's to give for any capability they are shown a card for; only
  // `never_allow` is a hard block, and it never produces a card at all.
  const opts = options && options.length ? options : DEFAULT_APPROVAL_OPTIONS;
  return (
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
            onClick={() => onResolve(opt)}
            disabled={disabled || (blockApprove && isApprove)}
          >
            {approvalLabel(opt)}
          </button>
        );
      })}
    </div>
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
const ERROR_CARD_CHOICES = new Set(["retry", "retry_now", "cancel", "skip"]);

export function HitlErrorCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string) => void;
  disabled?: boolean;
}) {
  const structured = structuredApprovalCopy(action.payload);
  const headline =
    structured?.headline
    || (typeof action.prompt === "string" && action.prompt.trim())
    || t("component.chat_action_card.action_needed");
  const options = (action.options || []).filter((opt) =>
    ERROR_CARD_CHOICES.has(normalizeChoice(opt)),
  );
  return (
    <>
      <div className="chat-hitl-summary chat-hitl-summary--error">
        <div className="chat-hitl-title">{headline}</div>
        {structured?.detail && (
          <div className="chat-hitl-description">{structured.detail}</div>
        )}
        {structured?.actionToTake && (
          <div className="chat-hitl-action-to-take">
            <strong>{t("component.chat_action_card.what_to_do")}</strong>{" "}
            {structured.actionToTake}
          </div>
        )}
        <div className="chat-hitl-links">
          {structured?.actionLink && (
            <a className="chat-hitl-action-link" href={structured.actionLink}>
              {routeLinkLabel(structured.actionLink)}
            </a>
          )}
          <CardOriginLink taskId={action.task_id} />
        </div>
      </div>
      <ApprovalCard
        options={options.length ? options : ERROR_CARD_OPTIONS}
        onResolve={onResolve}
        disabled={disabled}
      />
    </>
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

function WorkflowStarterInputCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
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

  const submit = () => {
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
    onResolve("run", undefined, { inputs: parsed });
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
            <button type="button" className="chat-hitl-btn-primary" onClick={submit} disabled={disabled}>
              {t("component.workspace_chat.run_workflow")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function WorkflowRetryCard({ action, onResolve, disabled }: {
  action: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
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

export function HitlInputCard({ onResolve, placeholder, disabled }: {
  onResolve: (
    choice: string,
    note?: string,
    payload?: Record<string, any>,
    files?: File[],
  ) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const handleSubmit = () => {
    if ((!value.trim() && attachments.length === 0) || submitting) return;
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
      setAttachments((prev) => [...prev, ...newFiles]);
    }
    e.target.value = "";
  };

  const removeAttachment = (idx: number) => setAttachments((a) => a.filter((_, i) => i !== idx));

  return (
    <div className="chat-hitl-input-card">
      <input ref={fileRef} type="file" multiple accept="*/*" style={{ display: "none" }} onChange={handleFiles} />
      {/* Attachment pills */}
      {attachments.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
          {attachments.map((a, i) => (
            <span key={i} style={{
              display: "inline-flex", alignItems: "center", gap: 4,
              padding: "2px 8px", borderRadius: 6, fontSize: 11, fontWeight: 600,
              background: a.type === "knowledge" ? "rgba(95,132,189,0.08)" : "rgba(28,25,23,0.08)",
              color: a.type === "knowledge" ? "#4869ac" : "#436b65",
            }}>
              {a.type === "knowledge" ? (
                <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" /></svg>
              ) : (
                <svg width="10" height="10" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32" /></svg>
              )}
              {a.name}
              <span style={{ cursor: "pointer", opacity: 0.5 }} onClick={() => removeAttachment(i)}>&times;</span>
            </span>
          ))}
        </div>
      )}
      <div className="chat-hitl-input-row">
        {/* Attach dropdown — same as EmbeddedChat */}
        <div style={{ position: "relative" }} ref={menuRef}>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            disabled={disabled || submitting}
            title={t("component.chat_action_card.attach")}
            style={{
              width: 32, height: 32, borderRadius: 8, border: "none",
              background: "transparent", cursor: "pointer",
              display: "flex", alignItems: "center", justifyContent: "center",
              color: menuOpen ? "#436b65" : "#a8a29e", flexShrink: 0, transition: "color 0.15s",
            }}
            onMouseEnter={(e) => { if (!menuOpen) e.currentTarget.style.color = "#436b65"; }}
            onMouseLeave={(e) => { if (!menuOpen) e.currentTarget.style.color = "#a8a29e"; }}
          >
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          {menuOpen && (
            <div style={{
              position: "absolute", bottom: 40, left: 0, width: 190,
              background: "#fff", borderRadius: 12,
              boxShadow: "0 8px 24px rgba(0,0,0,0.12)", border: "1px solid rgba(28,25,23,0.06)",
              overflow: "hidden", zIndex: 10,
            }}>
              <button
                onClick={() => { setMenuOpen(false); fileRef.current?.click(); }}
                style={{
                  width: "100%", display: "flex", alignItems: "center", gap: 8,
                  padding: "10px 14px", border: "none", background: "transparent",
                  cursor: "pointer", fontSize: 13, fontWeight: 500, color: "#44403c", fontFamily: "inherit",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "#fafaf9"; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
              >
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="#436b65" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" /></svg>
                {t("component.chat_action_card.local_file")}
              </button>
              <div style={{ height: 1, background: "rgba(231,229,228,0.5)" }} />
              <button
                onClick={() => {
                  setMenuOpen(false);
                  // Emit a custom event that the parent can listen to for KB picker
                  window.dispatchEvent(new CustomEvent("hitl-kb-picker-open"));
                }}
                style={{
                  width: "100%", display: "flex", alignItems: "center", gap: 8,
                  padding: "10px 14px", border: "none", background: "transparent",
                  cursor: "pointer", fontSize: 13, fontWeight: 500, color: "#44403c", fontFamily: "inherit",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "#fafaf9"; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
              >
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="#4869ac" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" /></svg>
                {t("component.chat_action_card.knowledge_base")}
              </button>
            </div>
          )}
        </div>
        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSubmit(); } }}
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
        <button
          onClick={() => onResolve("skip")}
          disabled={submitting || disabled}
          className="chat-hitl-btn-secondary"
        >
          {t("component.chat_action_card.skip")}
        </button>
      </div>
    </div>
  );
}

/** Helper to add a knowledge base document to a HitlInputCard from outside */
export function addKnowledgeAttachment(name: string, docId: string) {
  window.dispatchEvent(new CustomEvent("hitl-kb-attachment", {
    detail: { name, id: docId, type: "knowledge" },
  }));
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
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {questions.map((question, index) => {
          const key = questionKey(question, index);
          const label = questionLabel(question, index);
          const type = String(typeof question === "object" ? question.type || "" : "").toLowerCase();
          const options: any[] = Array.isArray(question?.options) ? question.options : [];
          const value = answers[key] ?? "";
          return (
            <label key={key} className="chat-hitl-field-label">
              <span>{label}</span>
              {options.length > 0 ? (
                <select
                  value={String(value)}
                  onChange={(e) => setAnswer(key, e.target.value)}
                  disabled={disabled || submitting}
                  className="chat-hitl-textarea"
                  style={{ minHeight: 34 }}
                >
                  <option value="" disabled>{question?.required ? t("component.chat_action_card.select") : t("component.chat_action_card.optional")}</option>
                  {options.map((option) => (
                    <option key={optionValue(option)} value={optionValue(option)}>{optionLabel(option)}</option>
                  ))}
                </select>
              ) : type === "checkbox" || type === "boolean" ? (
                <input
                  type="checkbox"
                  checked={Boolean(value)}
                  onChange={(e) => setAnswer(key, e.target.checked)}
                  disabled={disabled || submitting}
                  style={{ width: 16, height: 16, accentColor: "#436b65" }}
                />
              ) : type === "textarea" || type === "multiline" ? (
                <textarea
                  value={String(value)}
                  onChange={(e) => setAnswer(key, e.target.value)}
                  disabled={disabled || submitting}
                  className="chat-hitl-textarea"
                  rows={2}
                />
              ) : (
                <input
                  type={type === "number" ? "number" : "text"}
                  value={String(value)}
                  onChange={(e) => setAnswer(key, e.target.value)}
                  disabled={disabled || submitting}
                  className="chat-hitl-textarea"
                  style={{ minHeight: 34 }}
                />
              )}
            </label>
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

export function ResolvedBadge({ resolution, by }: { resolution: Resolution; by?: string }) {
  const choice = resolution.choice || "";
  const tone = approvalTone(choice);
  const normalized = normalizeChoice(choice);
  const isRetry = choice === "retry" || choice === "retry_now";
  const isSkipped = normalized.includes("skip");
  const isCancelled = normalized.includes("cancel") || normalized === "stopped";
  const isApprove = tone === "approve" || tone === "always";
  const isFeedback = choice === "feedback";
  const isRespond = choice === "respond" || normalized === "provide_answers";
  const isReject = tone === "reject";

  const label = isRetry ? t("component.chat_action_card.retry_requested")
    : isCancelled ? t("component.status.cancelled")
    : isSkipped ? t("component.chat_action_card.skipped")
    : tone === "always" ? t("component.chat_action_card.always_approved")
    : isApprove ? (choice === "approve_selected" ? t("component.chat_action_card.partially_approved") : t("component.chat_action_card.approved"))
    : isFeedback ? t("component.chat_action_card.feedback_sent")
    : isRespond ? t("component.chat_action_card.responded")
    : isReject ? t("component.chat_action_card.rejected")
    : choice;
  const variant = (isApprove || isFeedback || isRespond) ? "chat-hitl-resolved--approved" : "chat-hitl-resolved--rejected";
  const icon = (isApprove || isFeedback || isRespond) ? "✓ " : "✗ ";

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

/* ── Composite: renders the right card based on pending_action ── */

export default function ChatActionCard({ action, resolved, resolution, onResolve, disabled, resolvedByName, currentUserName, resetToken }: {
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
  const effectiveResolution = resolution || localResolution || (resolved ? { choice: "resolved" } : null);
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
    setLocalResolution({ choice, note });
    onResolve(choice, note, payload, files);
  };

  if (effectiveResolution) {
    // Server-provided resolver wins; an optimistic local resolution is always
    // the current viewer.
    const by = resolvedByName || (localResolution ? currentUserName : undefined);
    return <ResolvedBadge resolution={effectiveResolution} by={by} />;
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
        onResolve={(choice) => resolveOnce(choice)}
        disabled={locked}
      />
    );
  }

  if (action.kind === PendingActionKind.HUMAN_INPUT) {
    return <HitlInputCard onResolve={resolveOnce} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_STARTER_INPUT) {
    return <WorkflowStarterInputCard action={action} onResolve={resolveOnce} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_RETRY) {
    return <WorkflowRetryCard action={action} onResolve={resolveOnce} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_INPUT) {
    return (
      <HitlInputCard
        onResolve={resolveOnce}
        placeholder={typeof action.prompt === "string" ? action.prompt : undefined}
        disabled={locked}
      />
    );
  }

  if (action.kind === PendingActionKind.NEEDS_INPUT) {
    return <NeedsInputCard action={action} onResolve={resolveOnce} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.NEEDS_LOGIN) {
    return <NeedsLoginCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.NEEDS_CONFIRMATION) {
    return <NeedsConfirmationCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.APPROVE_PROPOSALS) {
    return <ProposalCard action={action} onResolve={resolveOnce} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.RETRY_STRATEGIST_REVIEW) {
    return <RetryActionCard onResolve={() => resolveOnce("retry")} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.WORKSPACE_OPERATION_REVIEW) {
    return <WorkspaceOperationReviewCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.EXTERNAL_MESSAGE_APPROVAL) {
    return <ExternalMessageApprovalCard action={action} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />;
  }

  if (action.kind === PendingActionKind.WORKFLOW_APPROVAL && action.review != null) {
    return (
      <>
        <WorkflowApprovalReview
          prompt={action.prompt}
          review={action.review}
          reviewTitle={action.review_title}
        />
        <ApprovalCard options={action.options} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />
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
        options={action.options}
        onResolve={(choice) => resolveOnce(choice)}
        disabled={locked}
      />
    </>
  );
}
