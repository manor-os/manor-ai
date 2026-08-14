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
import { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  IconChevronLeft,
  IconChevronRight,
  IconDocument,
  IconFlow,
  IconSettings,
} from "../icons";
import WorkflowApprovalReview, {
  EditableWorkflowApprovalReview,
  workflowReviewArtifacts,
  workflowReviewIsEditable,
} from "../workflows/WorkflowApprovalReview";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import IconTile from "./IconTile";
import { closeDetail, openDetail } from "../../stores/detail";
import {
  friendlyApprovalActionLabel,
  friendlyApprovalDescription,
  friendlyApprovalToolLabel,
} from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import { DEFAULT_APPROVAL_OPTIONS } from "../../lib/approvalOptions";
import { chatMessageAnchorId } from "../../lib/chatMessageAnchor";
import { preserveReturnToInHistory } from "../../lib/chatRouteReferences";
import { looksLikeFileReference } from "../../lib/fileReferences";
import Button from "./Button";
import CompactCard from "./CompactCard";
import Textarea from "./Textarea";
import Tooltip from "./Tooltip";

export interface ApprovalHITLRequest {
  id: string;
  type?: string;
  prompt?: string;
  action?: string;
  capability_id?: string;
  tool?: string;
  workspace?: { id?: string; name?: string };
  paths?: string[];
  content?: unknown;
  args_preview?: unknown;
  operation?: unknown;
  review?: unknown;
  review_title?: string;
  workflow?: {
    id?: string;
    name?: string;
    run_id?: string;
    url?: string;
  };
  node?: { id?: string; name?: string; type?: string };
  workflow_run_id?: string;
  workflow_step_id?: string;
  options?: string[];
  resolved?: boolean;
  resolution?: string;
}

export interface ChatMessageWithHITL {
  id?: string;
  hitl_requests?: ApprovalHITLRequest[];
}

type ApprovalDetail = { label: string; value: string };
type PendingApproval = {
  hitl: ApprovalHITLRequest;
  sourceAnchorId: string;
};

interface Props {
  messages: ChatMessageWithHITL[];
  disabled?: boolean;
  onResolve: (hitlId: string, choice: string, review?: unknown) => void;
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
  const location = useLocation();
  const navigate = useNavigate();
  const pending = useMemo(() => collectPendingApprovals(messages), [messages]);
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(null);

  const requestedIndex = selectedApprovalId
    ? pending.findIndex((item) => item.hitl.id === selectedApprovalId)
    : -1;
  const selectedIndex = requestedIndex >= 0 ? requestedIndex : 0;
  const currentItem = pending[selectedIndex];

  useEffect(() => {
    if (pending.length === 0) {
      if (selectedApprovalId !== null) setSelectedApprovalId(null);
      return;
    }
    if (requestedIndex < 0) setSelectedApprovalId(pending[0].hitl.id);
  }, [pending, requestedIndex, selectedApprovalId]);

  if (pending.length === 0) return null;

  const current = currentItem.hitl;
  const details = approvalDetails(current);
  const hasReview = current.review != null;
  const materialFiles = approvalMaterialFiles(current);
  const editablePublishReview = isEditablePublishApproval(current);
  const options = current.options?.length
    ? current.options
    : DEFAULT_APPROVAL_OPTIONS;
  const workflowHref = workflowApprovalHref(current);
  const sourceReturnTo = `${location.pathname}${location.search}#${currentItem.sourceAnchorId}`;

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
  const workflowSubtitle = [current.review_title, current.node?.name]
    .map((value) => String(value || "").trim())
    .filter((value, index, values) => value && values.indexOf(value) === index)
    .join(" · ") || description;

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
            {pending.length > 1 && (
              <div className="approval-action-bar__queue">
                <button
                  type="button"
                  className="approval-action-bar__queue-btn"
                  aria-label={t("component.approval_action_bar.previous_pending")}
                  disabled={selectedIndex === 0}
                  onClick={() => setSelectedApprovalId(pending[selectedIndex - 1]?.hitl.id || null)}
                >
                  <IconChevronLeft size={14} />
                </button>
                <span className="approval-action-bar__position">
                  {t("component.approval_action_bar.pending_position")
                    .replace("{current}", String(selectedIndex + 1))
                    .replace("{total}", String(pending.length))}
                </span>
                <button
                  type="button"
                  className="approval-action-bar__queue-btn"
                  aria-label={t("component.approval_action_bar.next_pending")}
                  disabled={selectedIndex === pending.length - 1}
                  onClick={() => setSelectedApprovalId(pending[selectedIndex + 1]?.hitl.id || null)}
                >
                  <IconChevronRight size={14} />
                </button>
              </div>
            )}
          </div>
        </div>
        <div className="approval-action-bar__body">
          <div className="approval-action-bar__materials">
            {workflowHref && (
              <CompactCard
                className="approval-action-bar__material-card approval-action-bar__material-card--workflow"
                icon={(
                  <IconTile size={34}>
                    <IconFlow size={17} />
                  </IconTile>
                )}
                title={current.workflow?.name || current.workflow?.id || t("component.workflow_result.workflow")}
                subtitle={workflowSubtitle}
                meta={t("component.workflow_result.workflow")}
                onClick={() => {
                  preserveReturnToInHistory(sourceReturnTo);
                  navigate(workflowHref, {
                    state: {
                      returnTo: sourceReturnTo,
                      chatReturnTo: sourceReturnTo,
                    },
                  });
                }}
              />
            )}
            {hasReview && (
              <CompactCard
                className="approval-action-bar__material-card approval-action-bar__material-card--review"
                icon={(
                  <IconTile size={34}>
                    <IconDocument size={17} />
                  </IconTile>
                )}
                title={current.review_title || t("component.approval_action_bar.review_material")}
                subtitle={approvalReviewSummary(current.review)
                  || t("component.approval_action_bar.open_exact_material")}
                meta={t("component.approval_action_bar.review")}
                onClick={() => openApprovalReview(current, onResolve, disabled)}
              />
            )}
            {materialFiles.map((file) => (
              <InlineFileReferenceCard
                key={file.key}
                reference={file.reference}
                label={file.label}
                returnTo={sourceReturnTo}
                display="card"
                className="approval-action-bar__material-card approval-action-bar__material-card--document"
              />
            ))}
            {!workflowHref && !hasReview && materialFiles.length === 0 && (
              <CompactCard
                className="approval-action-bar__material-card approval-action-bar__material-card--operation"
                icon={(
                  <IconTile size={34}>
                    <IconSettings size={17} />
                  </IconTile>
                )}
                title={friendlyApprovalActionLabel(current.action || current.capability_id || current.tool)}
                subtitle={approvalContentSummary(current) || description}
                meta={friendlyApprovalToolLabel(current.tool)}
                onClick={() => openApprovalContent(current, description)}
              />
            )}
          </div>
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
        <div className="approval-action-bar__buttons">
          {editablePublishReview ? (
            <Button
              variant="primary"
              size="sm"
              className="approval-action-bar__btn approval-action-bar__btn--approve"
              disabled={disabled}
              onClick={() => openApprovalReview(current, onResolve, disabled)}
            >
              {t("component.approval_action_bar.review_and_approve")}
            </Button>
          ) : (
            options.map((option) => {
              const tone = approvalOptionTone(option);
              const variant = tone === "approve" ? "primary" : "outline";
              const button = (
                <Button
                  variant={variant}
                  size="sm"
                  className={`approval-action-bar__btn approval-action-bar__btn--${tone}`}
                  disabled={disabled}
                  onClick={() => {
                    if (option === "revise") {
                      openRevisionRequest(current, onResolve, disabled);
                      return;
                    }
                    onResolve(current.id, option);
                  }}
                >
                  {approvalOptionLabel(option)}
                </Button>
              );
              if (option !== "always_approve") {
                return (
                  <span
                    key={option}
                    className={`approval-action-bar__button-slot approval-action-bar__button-slot--${tone}`}
                  >
                    {button}
                  </span>
                );
              }
              return (
                <Tooltip
                  key={option}
                  className="approval-action-bar__standing-tooltip"
                  content={standingApprovalDescription(current)}
                  position="top"
                >
                  {button}
                </Tooltip>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}

function collectPendingApprovals(
  messages: ChatMessageWithHITL[],
): PendingApproval[] {
  const out: PendingApproval[] = [];
  const seen = new Set<string>();
  for (const [messageIndex, msg] of messages.entries()) {
    if (!msg.hitl_requests) continue;
    for (const hitl of msg.hitl_requests) {
      if (hitl.type !== "approval") continue;
      if (hitl.resolved) continue;
      if (seen.has(hitl.id)) continue;
      seen.add(hitl.id);
      out.push({
        hitl,
        sourceAnchorId: chatMessageAnchorId(msg.id, messageIndex),
      });
    }
  }
  return out;
}

function approvalMaterialFiles(hitl: ApprovalHITLRequest) {
  const seen = new Set<string>();
  const files: Array<{ key: string; reference: string; label?: string }> = [];
  const add = (reference: unknown, label?: string) => {
    const normalized = String(reference || "").trim();
    if (!normalized || seen.has(normalized)) return;
    const isDocumentId = /^[0-9A-HJKMNP-TV-Z]{26}$/i.test(normalized);
    const isDocumentRoute = /^\/(?:viewer|documents)\//.test(normalized);
    if (!looksLikeFileReference(normalized) && !isDocumentId && !isDocumentRoute) return;
    seen.add(normalized);
    files.push({ key: normalized, reference: normalized, label });
  };

  hitl.paths?.forEach((path) => add(path));
  workflowReviewArtifacts(hitl.review).forEach((artifact) => {
    add(artifact.reference, artifact.label);
  });
  return files;
}

function truncateMaterialText(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > 180 ? `${normalized.slice(0, 177)}…` : normalized;
}

function approvalReviewSummary(value: unknown): string {
  if (typeof value === "string") return truncateMaterialText(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      const summary = approvalReviewSummary(item);
      if (summary) return summary;
    }
    return "";
  }
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  const priorityKeys = [
    "title",
    "headline",
    "subject",
    "thesis",
    "summary",
    "message",
    "caption",
    "body",
    "content",
    "text",
  ];
  for (const key of priorityKeys) {
    const summary = approvalReviewSummary(record[key]);
    if (summary) return summary;
  }
  for (const [key, item] of Object.entries(record)) {
    if (["review_artifacts", "artifacts", "files", "outputs"].includes(key)) continue;
    const summary = approvalReviewSummary(item);
    if (summary) return summary;
  }
  return "";
}

function approvalContentSummary(hitl: ApprovalHITLRequest): string {
  return approvalReviewSummary(hitl.content)
    || approvalReviewSummary(hitl.args_preview)
    || approvalReviewSummary(hitl.operation);
}

function approvalDetails(hitl: ApprovalHITLRequest): ApprovalDetail[] {
  const details: ApprovalDetail[] = [];
  if (hitl.action) details.push({ label: "Operation", value: friendlyApprovalActionLabel(hitl.action) });
  if (hitl.tool && hitl.tool !== "workflow") {
    details.push({ label: "Tool", value: friendlyApprovalToolLabel(hitl.tool) });
  }
  const workspace = hitl.workspace?.name || hitl.workspace?.id;
  if (workspace) details.push({ label: "Workspace", value: workspace });
  return details;
}

function workflowApprovalHref(hitl: ApprovalHITLRequest): string | null {
  const declared = String(hitl.workflow?.url || "").trim();
  if (/^\/flows(?:[?#]|$)/.test(declared)) return declared;
  const workflowId = hitl.workflow?.id;
  if (!workflowId) return null;
  const query = new URLSearchParams();
  query.set("workflow", workflowId);
  const runId = hitl.workflow?.run_id || hitl.workflow_run_id;
  if (runId) query.set("run", runId);
  return `/flows?${query.toString()}`;
}

function standingApprovalDescription(hitl: ApprovalHITLRequest): string {
  const operation = friendlyApprovalActionLabel(
    hitl.action || hitl.capability_id || hitl.tool,
  );
  const workspace = hitl.workspace?.name || hitl.workspace?.id;
  const key = workspace
    ? "component.approval_action_bar.always_approve_scope_workspace"
    : "component.approval_action_bar.always_approve_scope_chat";
  return t(key)
    .replace("{operation}", operation)
    .replace("{workspace}", workspace || "");
}

function openApprovalContent(hitl: ApprovalHITLRequest, description: string) {
  const detailValue = hitl.content ?? hitl.args_preview ?? hitl.operation;
  let detailText = description;
  if (detailValue != null) {
    if (typeof detailValue === "string") {
      detailText = detailValue;
    } else {
      try {
        detailText = JSON.stringify(detailValue, null, 2);
      } catch {
        detailText = String(detailValue);
      }
    }
  }
  openDetail({
    key: `approval-content:${hitl.id}`,
    icon: (
      <IconTile size={44}>
        <IconSettings size={20} />
      </IconTile>
    ),
    title: friendlyApprovalActionLabel(hitl.action || hitl.capability_id || hitl.tool),
    subtitle: friendlyApprovalToolLabel(hitl.tool),
    body: (
      <pre className="chat-hitl-content" aria-label={t("component.chat_action_card.approval_content_preview")}>
        {detailText}
      </pre>
    ),
    width: 620,
  });
}

function openApprovalReview(
  hitl: ApprovalHITLRequest,
  onResolve: Props["onResolve"],
  disabled?: boolean,
) {
  const editable = isEditablePublishApproval(hitl);
  const options = (hitl.options?.length ? hitl.options : DEFAULT_APPROVAL_OPTIONS)
    .filter((choice) => !editable || choice !== "revise");
  let currentDraft = cloneReviewRecord(hitl.review);
  let currentDraftIsValid = true;
  const resolve = (choice: string) => {
    const sendsReview = editable && ["approve", "always_approve"].includes(choice);
    if (sendsReview && !currentDraftIsValid) return;
    closeDetail();
    onResolve(hitl.id, choice, sendsReview ? currentDraft : undefined);
  };
  openDetail({
    key: `workflow-review:${hitl.id}`,
    icon: (
      <IconTile size={44}>
        <IconFlow size={20} />
      </IconTile>
    ),
    title: hitl.review_title || t("component.approval_action_bar.review_approval_material"),
    subtitle: [hitl.workflow?.name, hitl.node?.name].filter(Boolean).join(" · "),
    body: editable ? (
        <EditableWorkflowApprovalReview
          prompt={hitl.prompt}
          review={hitl.review}
          reviewTitle={hitl.review_title}
          disabled={disabled}
          onDraftState={(draft, valid) => {
            currentDraft = draft;
            currentDraftIsValid = valid;
          }}
        />
      ) : (
        <WorkflowApprovalReview
          prompt={hitl.prompt}
          review={hitl.review}
          reviewTitle={hitl.review_title}
          full
        />
      ),
    actions: (
      <div className="approval-review-actions">
        {options.map((choice) => {
          const className = [
            "approval-review-action",
            choice === "approve" && "approval-review-action--primary",
            approvalOptionTone(choice) === "reject" && "approval-review-action--danger",
          ].filter(Boolean).join(" ");
          return (
            <button
              key={choice}
              type="button"
              className={className}
              onClick={() => resolve(choice)}
              disabled={disabled}
            >
              {approvalOptionLabel(choice)}
            </button>
          );
        })}
      </div>
    ),
    width: 680,
  });
}

function isEditablePublishApproval(hitl: ApprovalHITLRequest): boolean {
  // Editorial packets need the same edit-in-place review as a final publish
  // packet. Requiring a separate "revise" branch makes the operator leave
  // the material they are judging and start another run for a typo.
  return workflowReviewIsEditable(hitl.review);
}

function cloneReviewRecord(review: unknown): Record<string, unknown> {
  try {
    return JSON.parse(JSON.stringify(review)) as Record<string, unknown>;
  } catch {
    return {};
  }
}

function openRevisionRequest(
  hitl: ApprovalHITLRequest,
  onResolve: Props["onResolve"],
  disabled?: boolean,
) {
  openDetail({
    key: `approval-revision:${hitl.id}`,
    icon: (
      <IconTile size={44}>
        <IconDocument size={20} />
      </IconTile>
    ),
    title: t("component.approval_action_bar.request_changes_title"),
    subtitle: friendlyApprovalActionLabel(hitl.action || hitl.capability_id || hitl.tool),
    body: (
      <RevisionRequestForm
        hitl={hitl}
        onResolve={onResolve}
        disabled={disabled}
      />
    ),
    width: 560,
  });
}

function RevisionRequestForm({
  hitl,
  onResolve,
  disabled,
}: {
  hitl: ApprovalHITLRequest;
  onResolve: Props["onResolve"];
  disabled?: boolean;
}) {
  const [revisionRequest, setRevisionRequest] = useState("");
  const submit = () => {
    const value = revisionRequest.trim();
    if (!value || disabled) return;
    closeDetail();
    onResolve(hitl.id, "revise", { revision_request: value });
  };
  return (
    <div className="approval-revision-request">
      <p className="approval-revision-request__description">
        {t("component.approval_action_bar.request_changes_description")}
      </p>
      <Textarea
        label={t("component.approval_action_bar.revision_instructions")}
        value={revisionRequest}
        onChange={(event) => setRevisionRequest(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder={t("component.approval_action_bar.revision_placeholder")}
        rows={5}
        disabled={disabled}
        className="approval-revision-request__field"
      />
      <div className="approval-review-actions">
        <button
          type="button"
          className="approval-review-action"
          onClick={closeDetail}
        >
          {t("component.approval_action_bar.cancel")}
        </button>
        <button
          type="button"
          className="approval-review-action approval-review-action--primary"
          onClick={submit}
          disabled={disabled || !revisionRequest.trim()}
        >
          {t("component.approval_action_bar.submit_revision")}
        </button>
      </div>
    </div>
  );
}

function approvalOptionTone(option: string): "approve" | "standing" | "revise" | "reject" {
  if (option === "always_approve") return "standing";
  if (option === "revise") return "revise";
  return ["reject", "cancel", "deny", "decline"].includes(option) ? "reject" : "approve";
}

function approvalOptionLabel(option: string): string {
  if (option === "always_approve") return t("component.approval_action_bar.always_approve");
  if (option === "reject") return t("component.approval_action_bar.reject");
  if (option === "cancel") return t("component.approval_action_bar.cancel");
  if (option === "revise") return t("component.approval_action_bar.revise");
  if (option === "approve") return t("component.approval_action_bar.approve");
  return option.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}
