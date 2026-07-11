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
import { useState, useRef } from "react";
import { friendlyApprovalActionLabel, friendlyApprovalDescription } from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import { DEFAULT_APPROVAL_OPTIONS } from "../../lib/approvalOptions";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";

/* ── Types ── */

export interface PendingAction {
  kind: string;
  options?: string[];
  content?: unknown;
  args_preview?: unknown;
  operation?: unknown;
  [key: string]: any;
}

export interface Resolution {
  choice: string;
  note?: string;
}

type ApprovalTone = "approve" | "always" | "reject";

function normalizeChoice(choice: string): string {
  return String(choice || "").toLowerCase().replace(/[-\s]+/g, "_");
}

function approvalTone(choice: string): ApprovalTone {
  const normalized = normalizeChoice(choice);
  if (normalized.includes("always")) return "always";
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

export function ApprovalSummary({ prompt, action, tool, hasWorkspace, paths, content, argsPreview, operation }: {
  prompt?: string;
  action?: string;
  tool?: string;
  hasWorkspace?: boolean;
  paths?: string[];
  content?: unknown;
  argsPreview?: unknown;
  operation?: unknown;
}) {
  const shownPaths = (paths || [])
    .map((path) => String(path || "").trim())
    .filter(Boolean);
  const friendlyPrompt = friendlyApprovalDescription({
    prompt,
    action,
    tool,
    hasWorkspace,
    paths: shownPaths,
    content,
    argsPreview,
    operation,
  });
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

      <ApprovalCard options={action.options || DEFAULT_APPROVAL_OPTIONS} onResolve={onResolve} disabled={disabled} blockApprove={invalid} />
    </div>
  );
}

/* ── Proposal Card (per-task selection + Approve/Reject/Feedback) ── */

export function ProposalCard({ action, onResolve, disabled }: {
  action?: PendingAction;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
}) {
  const taskIds: string[] = action?.task_ids || [];
  const taskTitles: string[] = action?.task_titles || [];
  const [selected, setSelected] = useState<Set<string>>(new Set(taskIds));
  const [feedback, setFeedback] = useState("");
  const [showFeedback, setShowFeedback] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };
  const selectAll = () => setSelected(new Set(taskIds));
  const selectNone = () => setSelected(new Set());

  const handleApprove = () => {
    setSubmitting(true);
    const ids = Array.from(selected);
    if (ids.length === taskIds.length) {
      onResolve("approve_all");
    } else {
      onResolve("approve_selected", undefined, { selected_task_ids: ids });
    }
  };

  const handleAlwaysApprove = () => {
    setSubmitting(true);
    onResolve("always_approve");
  };

  const handleReject = () => {
    setSubmitting(true);
    onResolve("reject_all");
  };

  const handleSendFeedback = () => {
    if (!feedback.trim()) return;
    setSubmitting(true);
    onResolve("feedback", feedback.trim());
  };

  const selectedCount = selected.size;

  return (
    <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
      {/* Per-task checkboxes */}
      {taskIds.length > 0 && (
        <div className="chat-proposal-task-list">
          {taskIds.length > 1 && (
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
          {taskIds.map((tid, i) => (
            <label
              key={tid}
              className={`chat-proposal-task-option ${selected.has(tid) ? "chat-proposal-task-option--selected" : ""}`}
            >
              <input
                type="checkbox"
                checked={selected.has(tid)}
                onChange={() => toggle(tid)}
                disabled={submitting}
                style={{ accentColor: "#436b65", flexShrink: 0 }}
              />
              <span>{formatUserFacingText(taskTitles[i]) || `Task ${i + 1}`}</span>
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
          disabled={disabled || submitting || selectedCount === 0}
        >
          {submitting
            ? "..."
            : selectedCount === taskIds.length
              ? t("component.chat_action_card.approve_all")
              : t("component.chat_action_card.approve_count").replace("{selected}", String(selectedCount)).replace("{total}", String(taskIds.length))}
        </button>
        <button
          type="button"
          className="chat-hitl-btn-secondary chat-hitl-btn-quiet"
          onClick={handleAlwaysApprove}
          disabled={disabled || submitting || taskIds.length === 0}
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
  const opts = options || DEFAULT_APPROVAL_OPTIONS;
  return (
    <div className="chat-hitl-actions">
      {opts.map((opt) => {
        const tone = approvalTone(opt);
        const isApprove = tone !== "reject" && tone !== "always";
        const className = tone === "reject"
          ? "chat-hitl-btn-secondary chat-hitl-btn-danger"
          : tone === "always"
            ? "chat-hitl-btn-secondary chat-hitl-btn-quiet"
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
      <ApprovalCard options={action.options || DEFAULT_APPROVAL_OPTIONS} onResolve={onResolve} disabled={disabled} />
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

export function HitlInputCard({ onResolve, placeholder, disabled }: {
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
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
    const serializableAttachments = attachments.map(({ name, id, type }) => ({ name, id, type }));
    onResolve(
      "respond",
      value.trim(),
      serializableAttachments.length > 0
        ? { response: value.trim(), attachments: serializableAttachments }
        : undefined,
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

export default function ChatActionCard({ action, resolved, resolution, onResolve, disabled, resolvedByName, currentUserName }: {
  action: PendingAction;
  resolved?: boolean;
  resolution?: Resolution | null;
  disabled?: boolean;
  /** Display name of whoever resolved this action (from the server). */
  resolvedByName?: string;
  /** Fallback name shown for an optimistic local resolution (the viewer). */
  currentUserName?: string;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
}) {
  const [localResolution, setLocalResolution] = useState<Resolution | null>(null);
  const submittedRef = useRef(false);
  const effectiveResolution = resolution || localResolution || (resolved ? { choice: "resolved" } : null);
  const locked = Boolean(disabled || resolved || localResolution || submittedRef.current);

  const resolveOnce = (choice: string, note?: string, payload?: Record<string, any>) => {
    if (locked) return;
    submittedRef.current = true;
    setLocalResolution({ choice, note });
    onResolve(choice, note, payload);
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

  if (action.kind === "human_input") {
    return <HitlInputCard onResolve={resolveOnce} disabled={locked} />;
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
    return <ProposalCard action={action} onResolve={resolveOnce} disabled={locked} />;
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

  return (
    <>
      {(action.prompt || action.action || action.tool || action.content || action.args_preview || action.operation || action.paths) && (
        <ApprovalSummary
          prompt={action.prompt}
          action={action.action || action.kind}
          tool={action.tool}
          hasWorkspace={Boolean(action.workspace?.id || action.workspace?.name)}
          paths={action.paths}
          content={action.content}
          argsPreview={action.args_preview}
          operation={action.operation}
        />
      )}
      <ApprovalCard options={action.options} onResolve={(choice) => resolveOnce(choice)} disabled={locked} />
    </>
  );
}
