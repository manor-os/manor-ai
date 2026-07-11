/**
 * ApprovalActionBar — sticky bottom bar that surfaces all unresolved
 * approval-style HITL requests in the current conversation, separately
 * from the inline chat cards.
 *
 * Why this exists:
 *   The previous UI rendered Approve / Reject buttons INSIDE the
 *   message card that contained the request. Two problems:
 *     1. Each new approval round (e.g. when LinkedIn's runtime policy
 *        re-asks because args changed) added a new card; the user had
 *        to scroll to find the right one.
 *     2. The approval body was a JSON dump of {tool, action,
 *        risk_level, arguments} — no user could read it.
 *   This bar pulls all pending approvals to a single, persistent
 *   surface above the composer. Each approval shows a one-line,
 *   human-readable description (built backend-side by
 *   _describe_action) and approve-once / always-approve / reject actions.
 *
 * Used by EmbeddedChat + FloatingChat. Rendering rules:
 *   - Hidden when there are zero pending approvals.
 *   - Shows the OLDEST pending approval first (FIFO — that's the
 *     one the agent is currently waiting on).
 *   - When more than one is pending, surfaces the count and lets the
 *     user advance through them one at a time.
 *
 * The inline message cards still render an ApprovalSummary (the
 * description) for chat-history readability, but no longer include
 * action buttons. Resolution flows back through the same
 * onResolve(hitlId, choice) callback used before.
 */
import { useMemo } from "react";
import {
  friendlyApprovalActionLabel,
  friendlyApprovalDescription,
  friendlyApprovalToolLabel,
} from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import { DEFAULT_APPROVAL_OPTIONS } from "../../lib/approvalOptions";

export interface ApprovalHITLRequest {
  id: string;
  type?: string;
  prompt?: string;
  action?: string;
  tool?: string;
  workspace?: { id?: string; name?: string };
  paths?: string[];
  content?: unknown;
  args_preview?: unknown;
  operation?: unknown;
  options?: string[];
  resolved?: boolean;
  resolution?: string;
}

export interface ChatMessageWithHITL {
  hitl_requests?: ApprovalHITLRequest[];
}

type ApprovalDetail = { label: string; value: string };

interface Props {
  messages: ChatMessageWithHITL[];
  disabled?: boolean;
  onResolve: (hitlId: string, choice: string) => void;
  /**
   * Visual context for width alignment with the composer below.
   *
   * - `"embedded"` (default) — match `.embedded-chat-footer > .chat-composer`:
   *    24 px outer padding + `min(100%, 920px)` inner cap.
   * - `"embedded-output-open"` — same as `embedded` but narrows to 820 px
   *    when the OutputPanel is open (mirrors `.embedded-chat-footer--output-open`).
   * - `"floating"` — match `.floating-chat-footer`: 12 px outer padding + 100%
   *    inner width (the panel itself constrains the overall width).
   */
  variant?: "embedded" | "embedded-output-open" | "floating";
}

export default function ApprovalActionBar({
  messages,
  disabled,
  onResolve,
  variant = "embedded",
}: Props) {
  const pending = useMemo(() => collectPendingApprovals(messages), [messages]);
  if (pending.length === 0) return null;

  const current = pending[0];
  const remaining = pending.length - 1;
  const details = approvalDetails(current);

  const description = friendlyApprovalDescription({
    prompt: current.prompt,
    action: current.action,
    tool: current.tool,
    hasWorkspace: Boolean(current.workspace?.id || current.workspace?.name),
    paths: current.paths,
    content: current.content,
    argsPreview: current.args_preview,
    operation: current.operation,
  });

  const rootClasses = [
    "approval-action-bar",
    variant === "embedded-output-open" && "approval-action-bar--output-open",
    variant === "floating" && "approval-action-bar--floating",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      className={rootClasses}
      role="region"
      aria-label={t("component.approval_action_bar.pending_approval")}
    >
      <div className="approval-action-bar__inner">
        <div className="approval-action-bar__header">
          <div className="approval-action-bar__icon" aria-hidden>
            !
          </div>
          <div className="approval-action-bar__heading">
            <div className="approval-action-bar__title">
              {t("component.approval_action_bar.action_requires_approval")}
            </div>
            {remaining > 0 && (
              <div className="approval-action-bar__remaining">
                {t("component.approval_action_bar.more_pending").replace(
                  "{count}",
                  String(remaining),
                )}
              </div>
            )}
          </div>
          <div className="approval-action-bar__buttons">
            {(current.options?.length
              ? current.options
              : DEFAULT_APPROVAL_OPTIONS
            ).map((option) => {
              const tone = approvalOptionTone(option);
              return (
                <button
                  key={option}
                  type="button"
                  className={`approval-action-bar__btn approval-action-bar__btn--${tone}`}
                  disabled={disabled}
                  onClick={() => onResolve(current.id, option)}
                >
                  {approvalOptionLabel(option)}
                </button>
              );
            })}
          </div>
        </div>
        <div className="approval-action-bar__body">
          <div className="approval-action-bar__description">{description}</div>
          {details.length > 0 && (
            <div className="approval-action-bar__details">
              {details.map((detail) => (
                <div key={detail.label} className="approval-action-bar__detail">
                  <span>{detail.label}</span>
                  <code>{detail.value}</code>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function collectPendingApprovals(
  messages: ChatMessageWithHITL[],
): ApprovalHITLRequest[] {
  const out: ApprovalHITLRequest[] = [];
  const seen = new Set<string>();
  for (const msg of messages) {
    if (!msg.hitl_requests) continue;
    for (const hitl of msg.hitl_requests) {
      if (hitl.type !== "approval") continue;
      if (hitl.resolved) continue;
      if (seen.has(hitl.id)) continue;
      seen.add(hitl.id);
      out.push(hitl);
    }
  }
  return out;
}

function approvalDetails(hitl: ApprovalHITLRequest): ApprovalDetail[] {
  const details: ApprovalDetail[] = [];
  if (hitl.tool) details.push({ label: "Tool", value: friendlyApprovalToolLabel(hitl.tool) });
  if (hitl.action) details.push({ label: "Action", value: friendlyApprovalActionLabel(hitl.action) });
  const workspace = hitl.workspace?.name || hitl.workspace?.id;
  if (workspace) details.push({ label: "Workspace", value: workspace });
  if (hitl.paths?.length) {
    details.push({ label: "Paths", value: hitl.paths.join(", ") });
  }
  return details;
}

function approvalOptionTone(option: string): "approve" | "reject" {
  return option === "reject" ? "reject" : "approve";
}

function approvalOptionLabel(option: string): string {
  if (option === "always_approve") return "Always approve";
  if (option === "reject") return "Reject";
  return "Approve";
}
