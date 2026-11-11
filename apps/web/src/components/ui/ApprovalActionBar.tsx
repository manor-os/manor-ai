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
 *   _describe_action) and approve-once / reject actions.
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
  workflowReviewIsRevisionChoice,
} from "../workflows/WorkflowApprovalReview";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import TikTokPublishReview, { isTikTokPublishApproval } from "../workflows/TikTokPublishReview";
import IconTile from "./IconTile";
import { closeDetail, openDetail } from "../../stores/detail";
import {
  friendlyApprovalActionLabel,
  friendlyApprovalDescription,
  friendlyApprovalToolLabel,
} from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import { workflowReferencePath } from "../../lib/workflowRoutes";
import { currentApprovalOptions } from "../../lib/approvalOptions";
import { chatMessageAnchorId } from "../../lib/chatMessageAnchor";
import { preserveReturnToInHistory } from "../../lib/chatRouteReferences";
import { entityStoragePath, looksLikeFileReference } from "../../lib/fileReferences";
import Button from "./Button";
import CompactCard from "./CompactCard";
import Textarea from "./Textarea";
import ApprovalSaveRuleAction from "../approvals/ApprovalSaveRuleAction";
import {
  ALWAYS_ALLOW_CHOICE,
  optionDetailLabel,
  orderedOptions,
  recommendationOf,
  recommendationReason,
  type RecommendedQuestion,
} from "../../lib/recommendedOptions";
import { alwaysAllow } from "../../lib/alwaysAllow";
import { useToastStore } from "../../stores/toast";

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
  review_read_only?: boolean;
  review_editable_fields?: string[];
  review_revision_field?: string;
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
  recommendation?: RecommendedQuestion["recommendation"];
  option_details?: RecommendedQuestion["option_details"];
  batch_key?: string;
  resolved?: boolean;
  resolution?: string;
}

export interface ChatMessageWithHITL {
  id?: string;
  hitl_requests?: ApprovalHITLRequest[];
}

type PendingApproval = {
  hitl: ApprovalHITLRequest;
  sourceAnchorId: string;
};

interface Props {
  messages: ChatMessageWithHITL[];
  disabled?: boolean;
  /** ``receipt`` is the choice the card records when it differs from the
   * decision sent: Always allow approves, but the card says "Always". */
  onResolve: (hitlId: string, choice: string, review?: unknown, receipt?: string) => void | Promise<void>;
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
  const [completedIds, setCompletedIds] = useState<Set<string>>(() => new Set());
  const pending = useMemo(() => collectPendingApprovals(messages).filter(item => !completedIds.has(item.hitl.id)), [messages, completedIds]);
  const [selectedApprovalId, setSelectedApprovalId] = useState<string | null>(null);
  const [alwaysError, setAlwaysError] = useState("");

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
  const hasReview = current.review != null;
  const materialFiles = approvalMaterialFiles(current);
  const editablePublishReview = isEditablePublishApproval(current);
  const recommendation = recommendationOf(current);
  const options = orderedOptions(currentApprovalOptions(current.options), recommendation);
  const primaryOption = recommendation && options.includes(recommendation.option)
    ? recommendation.option
    : options.find((option) => approvalOptionTone(option) === "approve");
  const reason = recommendationReason(recommendation);
  // Several open approvals of the same kind are one question: approve them
  // all at once, or step through them with the queue arrows.
  const batch = current.batch_key && pending.length > 1
    && pending.every((item) => item.hitl.batch_key === current.batch_key)
    ? pending
    : null;
  const choose = (option: string) => {
    if (workflowReviewIsRevisionChoice(option)) {
      openRevisionRequest(current, onResolve, disabled, option);
      return;
    }
    if (option === ALWAYS_ALLOW_CHOICE) {
      setAlwaysError("");
      alwaysAllow(current.id, () => onResolve(current.id, "approve", undefined, ALWAYS_ALLOW_CHOICE))
        .then(() => setCompletedIds(ids => new Set([...ids, current.id])))
        .catch(() => setAlwaysError(t("recommendation.always_error")));
      return;
    }
    onResolve(current.id, option);
  };
  const workflowHref = workflowApprovalHref(current);
  const chromePublication = chromePublicationSummary(current);
  const hasCompactSummary = !workflowHref && (chromePublication !== null || materialFiles.length === 0);
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
          <span className="approval-action-bar__status" aria-hidden />
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
            {hasReview && !hasCompactSummary && (
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
                creationTarget={file.creationTarget}
                returnTo={sourceReturnTo}
                trustedReference={file.trusted}
                display="card"
                className="approval-action-bar__material-card approval-action-bar__material-card--document"
              />
            ))}
            {hasCompactSummary && (
              <div className="approval-action-bar__summary">
                <div className="approval-action-bar__action">
                  {chromePublication
                    ? chromePublication.destination
                      ? t("component.approval_action_bar.publish_content_to")
                        .replace("{destination}", chromePublication.destination)
                      : t("component.approval_action_bar.publish_content")
                    : friendlyApprovalActionLabel(current.action || current.capability_id || current.tool)}
                </div>
                {chromePublication?.account && (
                  <div className="approval-action-bar__context">
                    {t("component.approval_action_bar.publish_as")
                      .replace("{account}", chromePublication.account)}
                  </div>
                )}
                <p className="approval-action-bar__preview">
                  {chromePublication?.text
                    || approvalReviewSummary(current.review)
                    || approvalContentSummary(current)
                    || description}
                </p>
                <button
                  type="button"
                  className="approval-action-bar__review-link approval-action-bar__view-details"
                  onClick={() => hasReview
                    ? openApprovalReview(current, onResolve, disabled)
                    : openApprovalContent(current, description)}
                >
                  {t("component.approval_action_bar.view_details")}
                </button>
              </div>
            )}
          </div>
        </div>
        <div className="approval-action-bar__buttons">
          {isTikTokPublishApproval(current) || editablePublishReview ? (
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
              const variant = option === primaryOption ? "primary" : tone === "reject" ? "ghost" : "outline";
              const button = (
                <Button
                  variant={variant}
                  size="sm"
                  className={`approval-action-bar__btn approval-action-bar__btn--${tone}`}
                  disabled={disabled}
                  onClick={() => choose(option)}
                >
                  {optionDetailLabel(current, option) || approvalOptionLabel(option)}
                </Button>
              );
              return (
                <span
                  key={option}
                  className={`approval-action-bar__button-slot approval-action-bar__button-slot--${tone}`}
                >
                  {button}
                </span>
              );
            })
          )}
          {batch && !hasReview && (
            <Button
              variant="outline"
              size="sm"
              className="approval-action-bar__btn"
              disabled={disabled}
              onClick={() => batch.forEach((item) => onResolve(item.hitl.id, "approve"))}
            >
              {t("recommendation.batch.approve_all")} · {batch.length}
            </Button>
          )}
          {!hasReview && !options.includes(ALWAYS_ALLOW_CHOICE) && <ApprovalSaveRuleAction key={current.id} requestId={current.id} scopeLabel={current.workspace?.name} disabled={disabled} onCompleted={() => setCompletedIds(ids => new Set([...ids, current.id]))} />}
        </div>
        {(reason || alwaysError) && (
          <p className="chat-hitl-recommendation-reason approval-action-bar__reason" role={alwaysError ? "alert" : undefined}>
            {alwaysError || t("recommendation.reason_prefix", { reason })}
          </p>
        )}
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
  const files: Array<{ key: string; reference: string; label?: string; trusted?: boolean; creationTarget?: boolean }> = [];
  const add = (reference: unknown, label?: string, trusted = false, creationTarget = false) => {
    const normalized = String(reference || "").trim();
    if (!normalized || seen.has(normalized)) return;
    const isDocumentId = /^[0-9A-HJKMNP-TV-Z]{26}$/i.test(normalized);
    const isDocumentRoute = /^\/(?:viewer|documents)\//.test(normalized);
    if (!looksLikeFileReference(normalized) && !isDocumentId && !isDocumentRoute) return;
    seen.add(normalized);
    files.push({ key: normalized, reference: normalized, label, trusted, creationTarget });
  };

  // Approval paths are storage paths, which free-text file detection refuses.
  hitl.paths?.forEach((path) => {
    const storagePath = entityStoragePath(path);
    add(storagePath || path, undefined, Boolean(storagePath), Boolean(storagePath) && hitl.action === "workspace.file.create");
  });
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

function chromePublicationSummary(hitl: ApprovalHITLRequest) {
  if (hitl.action !== "chrome.publication.publish") return null;
  const review = hitl.review && typeof hitl.review === "object" && !Array.isArray(hitl.review)
    ? hitl.review as Record<string, unknown>
    : {};
  let destination = "";
  if (typeof review.destination_url === "string") {
    try {
      const url = new URL(review.destination_url);
      if (url.protocol === "https:" || url.protocol === "http:") {
        destination = url.hostname.replace(/^www\./, "");
      }
    } catch {
      // An invalid URL should not become a misleading publication destination.
    }
  }
  return {
    destination,
    account: typeof review.account === "string" ? review.account.trim() : "",
    text: typeof review.text === "string" ? review.text.trim() : "",
  };
}

function workflowApprovalHref(hitl: ApprovalHITLRequest): string | null {
  const workflowId = hitl.workflow?.id;
  if (!workflowId) return null;
  const runId = hitl.workflow?.run_id || hitl.workflow_run_id;
  return workflowReferencePath({
    workflow_id: workflowId,
    run_id: runId,
    url: hitl.workflow?.url,
  });
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
  if (isTikTokPublishApproval(hitl)) {
    openDetail({
      key: `tiktok-review:${hitl.id}`,
      title: "Review TikTok post",
      body: <TikTokPublishReview hitlId={hitl.id} disabled={disabled}
        onApprove={(review) => { closeDetail(); onResolve(hitl.id, "approve", review); }}
        onReject={() => { closeDetail(); onResolve(hitl.id, "reject"); }} />,
      width: 680,
    });
    return;
  }
  const editable = isEditablePublishApproval(hitl);
  const revisionField = typeof hitl.review_revision_field === "string"
    ? hitl.review_revision_field
    : "";
  const options = currentApprovalOptions(hitl.options)
    .filter((choice) => !editable || revisionField || !workflowReviewIsRevisionChoice(choice));
  let currentDraft = cloneReviewRecord(hitl.review);
  let currentDraftIsValid = true;
  const resolve = async (choice: string) => {
    if (!editable && workflowReviewIsRevisionChoice(choice)) {
      openRevisionRequest(hitl, onResolve, disabled, choice);
      return;
    }
    const sendsReview = editable && (["approve", "always_approve"].includes(choice) || workflowReviewIsRevisionChoice(choice));
    if (sendsReview && !currentDraftIsValid) return;
    if (workflowReviewIsRevisionChoice(choice) && revisionField && !String(currentDraft[revisionField] ?? "").trim()) return;
    try {
      await onResolve(hitl.id, choice, sendsReview ? currentDraft : undefined);
      closeDetail();
    } catch (error) {
      useToastStore.getState().error(
        error instanceof Error ? error.message : t("lib.api.chat_failed"),
      );
    }
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
          editableFields={hitl.review_editable_fields}
          revisionField={revisionField || undefined}
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
              onClick={() => void resolve(choice)}
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
  return hitl.review_read_only !== true && workflowReviewIsEditable(hitl.review);
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
  choice = "revise",
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
        choice={choice}
        onResolve={onResolve}
        disabled={disabled}
      />
    ),
    width: 560,
  });
}

function RevisionRequestForm({
  hitl,
  choice,
  onResolve,
  disabled,
}: {
  hitl: ApprovalHITLRequest;
  choice: string;
  onResolve: Props["onResolve"];
  disabled?: boolean;
}) {
  const [revisionRequest, setRevisionRequest] = useState("");
  const submit = () => {
    const value = revisionRequest.trim();
    if (!value || disabled) return;
    closeDetail();
    onResolve(hitl.id, choice, { revision_request: value });
  };
  return (
    <div className="approval-revision-request">
      <p className="approval-revision-request__description">
        {t("component.approval_action_bar.request_changes_description")}
      </p>
      <Textarea
        label={t("component.approval_action_bar.revision_instructions")}
        ariaLabel={t("component.approval_action_bar.revision_instructions")}
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
  if (workflowReviewIsRevisionChoice(option)) return "revise";
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
