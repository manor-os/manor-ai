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
import { isUnrelatedReviewWarning, workspaceChangeCopy } from "../../lib/workspaceChangeCopy";
import { CardReplyContext } from "./cardReplyContext";
import { Suspense, lazy, useCallback, useContext, useEffect, useMemo, useState, useRef, useId } from "react";
import { formDraftKey, useFormDraft } from "../../lib/formDraft";
import FormDraftNotice from "./FormDraftNotice";
import { requestInputAnswerText, requestInputFieldFrom, requestInputFormSchema, type RequestInputField } from "../../lib/requestInputForm";
import RecommendedOptions from "./RecommendedOptions";
import {
  ALWAYS_ALLOW_CHOICE,
  FOCUS_COMPOSER_EVENT,
  type FocusComposerDetail,
  orderedOptions,
  recommendationOf,
  codedCardText,
  type RecommendedQuestion,
} from "../../lib/recommendedOptions";
import { alwaysAllow } from "../../lib/alwaysAllow";
import {
  friendlyApprovalActionLabel,
  friendlyApprovalDescription,
  friendlyApprovalToolLabel,
  isErrorHitlCard,
  structuredApprovalCopy,
} from "../../lib/approvalCopy";
import { t } from "../../lib/i18n";
import { formatDate } from "../../lib/format";
import { stripInternalReferences } from "../../lib/assistant-visible-text.mjs";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useInRouterContext } from "react-router-dom";
import { api } from "../../lib/api";
import { appPath } from "../../embed/embedContext";
import { integrationCatalogQueryOptions } from "../../lib/integrationCatalog";
import IntegrationConnectRow from "../integrations/IntegrationConnectRow";
import { parseIntegrationSetupLink } from "../../lib/integrationSetupLinks";
import ChatMarkdown from "../ChatMarkdown";
import Chip from "./Chip";
import AnchoredPopover from "./AnchoredPopover";
import HitlKnowledgePicker from "./HitlKnowledgePicker";
import InAppActionLink from "./InAppActionLink";
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
import WorkflowApprovalReview, { ApprovalChangePreview, EditableWorkflowApprovalReview, workflowReviewIsEditable, workflowReviewIsRevisionChoice } from "../workflows/WorkflowApprovalReview";
import WorkflowInputSuggestions from "../workflows/WorkflowInputSuggestions";
import { workflowRetrySuppliedValues, type WorkflowRunAction } from "../workflows/workflowRunDisplay";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import { entityStoragePath } from "../../lib/fileReferences";
import ApprovalSaveRuleAction from "../approvals/ApprovalSaveRuleAction";
import {
  proposalApprovedRowIds,
  proposalImpactExplainer,
  proposalImpactLabel,
  proposalPriorityLabel,
  proposalTaskEntries,
} from "../../lib/proposalDisplay";

const BlueprintUpgradeDialog = lazy(() => import("../blueprints/BlueprintUpgradeDialog"));

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
  /** Set by approvals whose effect is a delivery (external replies). */
  delivery_outcome?: "sent" | "unknown" | "not_sent" | string;
}

type ApprovalTone = "approve" | "always" | "reject" | "secondary";

function normalizeChoice(choice: string): string {
  return String(choice || "").toLowerCase().replace(/[-\s]+/g, "_");
}

function approvalTone(choice: string): ApprovalTone {
  const normalized = normalizeChoice(choice);
  if (normalized === APPROVAL_CHOICE_ALWAYS_APPROVE) return "always";
  if (["approve", "approved", "approve_all", "approve_selected", "accept", "yes", "confirm", "continue_after_login", ALWAYS_ALLOW_CHOICE].includes(normalized)) return "approve";
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

/** An answer that means "change it" has to say what: it opens the note box
 * instead of answering on its own. */
function asksForChanges(option: string): boolean {
  return workflowReviewIsRevisionChoice(option)
    || ["request_change", "make_changes", "changes", "edit"].includes(normalizeChoice(option))
    || /^(?:需要|请|要)?修改/.test(option.trim());
}

/** The choice as a resolved card names it: both standing approvals read
 * "Always", whichever surface offered them. */
function resolvedChoiceLabel(choice: string): string {
  const normalized = normalizeChoice(choice);
  if (normalized === ALWAYS_ALLOW_CHOICE || approvalTone(choice) === "always") {
    return t("component.chat_action_card.always");
  }
  return approvalLabel(choice);
}

function approvalLabel(choice: string): string {
  const normalized = normalizeChoice(choice);
  if (normalized === ALWAYS_ALLOW_CHOICE) return t("recommendation.label.always_allow");
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
    <a className="chat-hitl-origin-link" href={appPath(`/tasks/${id}`)}>
      {t("component.chat_action_card.view_task")}
    </a>
  );
}

/** The task id of a "/tasks/<id>" route; "" for any other link. */
function taskIdFromRoute(link: string | null | undefined): string {
  const match = String(link || "").split(/[?#]/)[0].match(/^\/?tasks\/([^/]+)\/?$/);
  return match ? match[1] : "";
}

/** A record id (ULID / UUID) names nothing a person can read. */
const RECORD_ID_SEGMENT = /^(?:[0-9a-z]{26}|[0-9a-f]{8}-[0-9a-f-]{27})$/i;

/** Human label for an in-app route: "/integrations" → "Open integrations". */
export function routeLinkLabel(link: string): string {
  if (taskIdFromRoute(link)) return t("component.chat_action_card.view_task");
  const segments = String(link || "")
    .split(/[?#]/)[0]
    .split("/")
    .filter(Boolean);
  const last = segments.pop();
  // "/things/<id>" opens one thing: label it by its collection, not its id.
  const segment = last && RECORD_ID_SEGMENT.test(last) ? segments.pop() : last;
  const name = segment ? humanizeKey(segment).toLowerCase() : "";
  return name
    ? t("component.chat_action_card.open_route").replace("{name}", name)
    : t("component.chat_action_card.open_link");
}

export function ApprovalSummary({ prompt, action, tool, hasWorkspace, paths, pathsFromText = false, content, argsPreview, operation, hitlType, payload, taskId }: {
  prompt?: string;
  action?: string;
  tool?: string;
  hasWorkspace?: boolean;
  /** The request's own `paths`: entity storage paths, opened as such. */
  paths?: string[];
  /** `paths` were scraped from message text, so are never trusted. */
  pathsFromText?: boolean;
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
    // An action link to this card's own task already is its origin link.
    const linkedTaskId = taskIdFromRoute(structured.actionLink).toLowerCase();
    const originTaskId = linkedTaskId && linkedTaskId === String(taskId || "").trim().toLowerCase()
      ? null
      : taskId;
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
          {structured.actionLink && <InAppActionLink link={structured.actionLink} />}
          <CardOriginLink taskId={originTaskId} />
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
  // Some requests repeat the target path as their content; that is not content.
  const contentText = typeof rawContent === "string" && !shownPaths.includes(rawContent.trim())
    ? previewText(rawContent)
    : null;
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
          {previewPaths.map((path) => {
            if ([".", "./", "/"].includes(path)) return <code key={path}>{pathLabel(path)}</code>;
            // Approval paths are storage paths, which free-text file detection
            // refuses; without trust the chip did nothing when clicked.
            const storagePath = pathsFromText ? "" : entityStoragePath(path);
            return (
              <InlineFileReferenceCard
                key={path}
                reference={storagePath || path}
                label={path.split("/").pop() || path}
                trustedReference={Boolean(storagePath)}
                creationTarget={!pathsFromText && action === "workspace.file.create"}
                compact
              />
            );
          })}
          {remainingPathCount > 0 && <span>{t("component.chat_action_card.more_count").replace("{count}", String(remainingPathCount))}</span>}
        </div>
      )}
      {!contentText && argsPreview != null && <ApprovalChangePreview argsPreview={argsPreview} />}
      {taskId && (
        <div className="chat-hitl-links">
          <CardOriginLink taskId={taskId} />
        </div>
      )}
    </div>
  );
}

/** Opens the candidate page a page change stages, beside its chat, before it is approved. */
function AppPagePreviewLink({ workspaceId, draftId }: { workspaceId: string; draftId: string }) {
  const href = `/chat?workspace=${encodeURIComponent(workspaceId)}&app_preview=${encodeURIComponent(draftId)}`;
  const label = t("component.chat_action_card.preview_app_page");
  return useInRouterContext()
    ? <Link className="chat-workspace-review__preview" to={href}>{label}</Link>
    : <a className="chat-workspace-review__preview" href={appPath(href)}>{label}</a>;
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
  const invalid = errors.length > 0;
  const previewWorkspaceId = String(operation.workspace_id || "");
  const previewDraftId = String(operation.draft_id || action.draft_id || "");
  const changesAppPage = patches.some((patch) => asRecord(patch)?.op === "app_view.update");
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
  // Not connected yet is not a blocker: the change applies with that work
  // waiting, and the Workspace then asks in its chat to connect it.
  const isPendingConnection = (issue: unknown) => asRecord(issue)?.code === "integration_pending_connection";
  const pendingIntegrations = integrationEffectProviders(
    warnings.filter(isPendingConnection).map((issue) => asRecord(issue)?.provider),
  );
  const change = workspaceChangeCopy(operation);
  const otherWarnings = change.pageOnly ? [] : warnings.filter((issue) => !isPendingConnection(issue) && !isUnrelatedReviewWarning(issue));
  const listSeparator = t("component.chat_action_card.change_list_separator");

  const renderIssue = (issue: unknown, idx: number) => {
    const row = asRecord(issue);
    const path = row ? String(row.path || "").trim() : "";
    const message = formatUserFacingText(row ? String(row.message || issue || "").trim() : String(issue || "").trim());
    return (
      <li key={`${path || "issue"}-${idx}`}>
        {message || t("component.chat_action_card.workspace_changes_desc")}
      </li>
    );
  };

  return (
    <div className="chat-hitl-summary chat-workspace-review">
      <div className="chat-hitl-title">
        {t(change.pageOnly ? "component.chat_action_card.page_changes_title" : "component.chat_action_card.workspace_changes_title")}
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
          {change.pageOnly
            ? t("component.chat_action_card.page_changes_desc")
            : change.groups.length > 0
              ? t("component.chat_action_card.change_will_update", {
                  items: change.groups.map((group) => t(`component.chat_action_card.change_group_${group}`)).join(listSeparator),
                })
              : t("component.chat_action_card.workspace_changes_desc")}
        </div>
      )}
      {change.added.length > 0 && (
        <div className="chat-hitl-description">{t("component.chat_action_card.change_added", { names: change.added.join(listSeparator) })}</div>
      )}
      {change.removed.length > 0 && (
        <div className="chat-hitl-description">{t("component.chat_action_card.change_removed", { names: change.removed.join(listSeparator) })}</div>
      )}

      {changesAppPage && !invalid && !disabled && previewWorkspaceId && previewDraftId && (
        <AppPagePreviewLink workspaceId={previewWorkspaceId} draftId={previewDraftId} />
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

      {pendingIntegrations.length > 0 && (
        <div className="chat-workspace-review__pending">
          <strong>{t("component.chat_action_card.integration_pending_connection", {
            names: pendingIntegrations.map(humanizeKey).join(", "),
          })}</strong>
          <div>{t("component.chat_action_card.integration_pending_connection_hint")}</div>
        </div>
      )}
      {errors.length > 0 && (
        <div className="chat-workspace-review__issue" role="alert">
          <strong>{t("component.chat_action_card.fix_before_applying")}</strong>
          <ul>{errors.slice(0, 4).map(renderIssue)}</ul>
        </div>
      )}
      {(otherWarnings.length > 0 || missingSetup.length > 0) && (
        <details className="chat-workspace-review__notes">
          <summary>{t("component.chat_action_card.notes")}</summary>
          <ul>
            {missingSetup.slice(0, 3).map((item, idx) => (
              <li key={`missing-${item}-${idx}`}>{t("component.chat_action_card.missing_setup").replace("{item}", humanizeKey(item))}</li>
            ))}
            {otherWarnings.slice(0, 3).map(renderIssue)}
          </ul>
        </details>
      )}

      <ApprovalCard
        // A review is a verdict on THIS diff. There is no coherent standing
        // version of "yes to whatever the next draft says", so the one-time
        // vocabulary is applied to whatever the blob carries.
        options={invalid ? ["reject"] : oneTimeApprovalOptions(action.options)}
        question={invalid ? null : action}
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
  // Cards decided as whole combinations ("all / only the first / not now")
  // carry their choices; rows are only read, never ticked.
  const combinations = Boolean(action?.option_details && action?.options?.length);
  const chooseCombination = (choice: string, payload?: Record<string, unknown>) => {
    if (choice === APPROVAL_CHOICE_REJECT) {
      handleReject();
      return;
    }
    setSubmitting(true);
    onResolve(choice, undefined, payload as Record<string, any> | undefined);
  };

  return (
    <div className="chat-proposal-actions-card">
      {/* One unified list — tasks and changes/experiments, each checkable */}
      {rows.length > 0 && !rowsRenderedElsewhere && (
        <div className="chat-proposal-task-list">
          {rows.length > 1 && !combinations && (
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
              {!combinations && (
                <input
                  type="checkbox"
                  checked={selected.has(row.id)}
                  onChange={() => toggle(row.id)}
                  disabled={submitting}
                  style={{ accentColor: "#436b65", flexShrink: 0 }}
                />
              )}
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
      {combinations ? (
        <RecommendedOptions
          question={action}
          options={action?.options || []}
          labelFor={(choice) => (
            choice === APPROVAL_CHOICE_REJECT ? t("component.chat_action_card.reject_all") : approvalLabel(choice)
          )}
          onChoose={chooseCombination}
          onOther={() => setShowFeedback(!showFeedback)}
          otherOpen={showFeedback}
          disabled={disabled || submitting}
        />
      ) : (
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
      )}

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

/** "Revise" / "Request changes" on any card: say what should change, then
 * send it. One box, one wording, on approvals and questions alike. */
function RevisionRequestBox({ disabled, onSubmit }: { disabled?: boolean; onSubmit: (request: string) => void }) {
  const [request, setRequest] = useState("");
  const send = () => { if (request.trim() && !disabled) onSubmit(request.trim()); };
  return (
    <div className="chat-hitl-retry-guidance chat-hitl-revision">
      <Textarea
        label={t("component.approval_action_bar.revision_instructions")}
        value={request}
        onChange={(event) => setRequest(event.target.value)}
        placeholder={t("component.approval_action_bar.revision_placeholder")}
        rows={3}
        disabled={disabled}
      />
      <div className="chat-hitl-actions">
        <button type="button" className="chat-hitl-btn-primary" onClick={send} disabled={disabled || !request.trim()}>
          {t("component.approval_action_bar.submit_revision")}
        </button>
      </div>
    </div>
  );
}

export function ApprovalCard({ options, onResolve, disabled, blockApprove, rejectLabel, question }: {
  options?: string[];
  /** The card itself: its recommendation, option wording and payloads. */
  question?: RecommendedQuestion | null;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
  disabled?: boolean;
  /** Disable only the affirmative (approve) button — e.g. when a draft has
   *  validation errors — while keeping reject/dismiss clickable. */
  blockApprove?: boolean;
  rejectLabel?: string;
}) {
  // Historical cards cannot restore the retired, unscoped Always action.
  const opts = currentApprovalOptions(options);
  const [revisionChoice, setRevisionChoice] = useState<string | null>(null);
  const showRevisionRequest = revisionChoice !== null;
  const toggleRevision = (opt: string) => {
    const normalized = normalizeChoice(opt);
    setRevisionChoice((current) => (current === normalized ? null : normalized));
  };
  const chooseRecommended = (opt: string, payload?: Record<string, unknown>) => {
    if (asksForChanges(opt)) { toggleRevision(opt); return; }
    onResolve(opt, undefined, payload as Record<string, any> | undefined);
  };
  const cardReply = useContext(CardReplyContext);
  const isApproveChoice = (opt: string) => ["approve", "always"].includes(approvalTone(opt))
    || normalizeChoice(opt) === ALWAYS_ALLOW_CHOICE;
  const revisionBox = showRevisionRequest && (
    <RevisionRequestBox
      disabled={disabled}
      onSubmit={(request) => onResolve(revisionChoice!, request, { review: { revision_request: request } })}
    />
  );
  if (recommendationOf(question)) {
    return (
      <RecommendedOptions
        question={question}
        options={opts}
        labelFor={(opt) => (approvalTone(opt) === "reject" && rejectLabel ? rejectLabel : approvalLabel(opt))}
        onChoose={chooseRecommended}
        // An approval has no text answer of its own: "Something else…" is a
        // reply in the conversation, which the asking agent reads.
        onOther={() => window.dispatchEvent(
          new CustomEvent<FocusComposerDetail>(FOCUS_COMPOSER_EVENT, { detail: cardReply }),
        )}
        disabled={disabled}
        isDisabled={(opt) => Boolean(blockApprove && isApproveChoice(opt))}
        titleFor={(opt) => (
          blockApprove && isApproveChoice(opt)
            ? t("component.chat_action_card.approve_blocked_by_validation")
            : undefined
        )}
        ariaExpandedFor={(opt) => (asksForChanges(opt) ? showRevisionRequest : undefined)}
      >
        {revisionBox}
      </RecommendedOptions>
    );
  }
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
                if (asksForChanges(opt)) { toggleRevision(opt); return; }
                onResolve(opt);
              }}
              disabled={disabled || (blockApprove && isApprove)}
              title={
                blockApprove && isApprove
                  ? t("component.chat_action_card.approve_blocked_by_validation")
                  : undefined
              }
              aria-expanded={asksForChanges(opt) ? showRevisionRequest : undefined}
            >
              {tone === "reject" && rejectLabel ? rejectLabel : approvalLabel(opt)}
            </button>
          );
        })}
      </div>
      {revisionBox}
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
            question={action}
            options={options.length ? options : ERROR_CARD_OPTIONS}
            onResolve={(choice) => onResolve(choice, guidance.trim() || undefined)}
            disabled={disabled}
          />
        )}
        {hasSeparateActionLink && <InAppActionLink link={actionLink} />}
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
        question={action}
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
  suggestions?: string[];
};

/** A wait's `input_schema` as the fields the input card asks, in the schema's own order. */
function workflowWaitSchemaInputs(schema: unknown): WorkflowStarterInput[] {
  const root = asRecord(schema);
  const properties = asRecord(root?.properties);
  if (!root || !properties) return [];
  const required = new Set(Array.isArray(root.required) ? root.required.map(String) : []);
  const declared = Object.keys(properties).filter((key) => asRecord(properties[key]));
  const order = Array.isArray(root["x-ui"]?.order) ? root["x-ui"].order.map(String) : [];
  const keys = [...order.filter((key: string) => declared.includes(key)), ...declared.filter((key) => !order.includes(key))];
  return keys.map((key) => {
    const child = properties[key] as WorkflowInputSchema;
    return { key, schema: child, required: required.has(key), hidden: Boolean(child["x-ui"]?.hidden) };
  });
}

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
  const starterWorkflowId = typeof action.workflow_id === "string" ? action.workflow_id : undefined;
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
        ...(input.suggestions?.length
          ? { "x-ui": { ...(input.schema?.["x-ui"] || {}), suggestions: input.suggestions } }
          : {}),
      },
    ])),
    required: structuredInputs.filter((input) => input.required).map((input) => input.key),
    "x-ui": { order: structuredInputs.map((input) => input.key) },
  };
  const initialValues = asRecord(action.values) || {};
  const [expanded, setExpanded] = useState(true);
  // An unsent form survives a reload: one draft per Workflow (start) or per run step (a wait).
  const draft = useFormDraft<Record<string, unknown>>(
    formDraftKey("workflow-form", String(action.kind || ""), String(action.approval_request_id || action.run_id || action.workflow_id || ""), String(action.step_id || "start")),
    () => Object.fromEntries(
      inputs.map((input) => [
        input.key,
        input.schema
          ? workflowSchemaDraft(input.schema, initialValues[input.key] ?? input.default)
          : formatWorkflowInputValue(initialValues[input.key] ?? input.default, input.type || "string"),
      ]),
    ),
  );
  const { values, setValues } = draft;
  // Choices named in Chat that the input does not offer, shown where the person picks.
  const [errors, setErrors] = useState<Record<string, string>>(() => Object.fromEntries(
    Object.entries(asRecord(action.input_notes) || {})
      .filter(([, note]) => typeof note === "string")
      .map(([key, note]) => [key, String(note)]),
  ));
  const [busyFieldKeys, setBusyFieldKeys] = useState<Set<string>>(() => new Set());
  const setFileBusy = useCallback((rootKey: string, busy: boolean) => {
    setBusyFieldKeys((current) => {
      if (current.has(rootKey) === busy) return current;
      const next = new Set(current);
      if (busy) next.add(rootKey);
      else next.delete(rootKey);
      return next;
    });
  }, []);
  const fileBusy = busyFieldKeys.size > 0;

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
    draft.clear();
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
          {draft.restored && <FormDraftNotice onDiscard={draft.reset} disabled={disabled} />}
          {useUnifiedSchemaGrid && (
            <WorkflowSchemaFields
              rootKey=""
              schema={structuredSchema}
              value={values}
              errors={errors}
              disabled={disabled}
              workspaceId={workspaceId}
              workflowId={starterWorkflowId}
              onFileBusyChange={(busy) => setFileBusy("unified", busy)}
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
            const ownControl = input.schema?.["x-ui"]?.control;
            // Controls with their own picker (files, records, time zones, dates) render through the shared schema field.
            if (input.schema && (ownControl === "knowledge_file" || ownControl === "record_select" || ownControl === "timezone"
              || input.schema.format === "date" || input.schema.format === "date-time")) {
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
                workflowId={starterWorkflowId}
                onFileBusyChange={(busy) => setFileBusy(`input:${input.key}`, busy)}
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
                    onFileBusyChange={(busy) => setFileBusy(`input:${input.key}`, busy)}
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
            const field = (
              <label key={input.key} className="workflow-starter-input-field" htmlFor={id}>
                <span>
                  {input.label || humanizeKey(input.key)}
                  {input.required ? " *" : ""}
                </span>
                {input.description && <small className="workflow-starter-input-help">{input.description}</small>}
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
            if (disabled || type !== "string" || !input.suggestions?.length) return field;
            return (
              <div key={input.key} className="workflow-starter-input-field-group">
                {field}
                <WorkflowInputSuggestions
                  suggestions={input.suggestions}
                  value={values[input.key]}
                  onPick={(suggestion) => {
                    setValues((current) => ({ ...current, [input.key]: suggestion }));
                    setErrors((current) => ({ ...current, [input.key]: "" }));
                  }}
                />
              </div>
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
  const suppliedValues = workflowRetrySuppliedValues(action as WorkflowRunAction, schema);
  const [values, setValues] = useState<unknown>(() => (
    schema ? workflowSchemaDraft(schema, suppliedValues) : {}
  ));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const retryFrom = String(action.retry_from_step_name || action.retry_from_step_id || action.step_id || "").trim();
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
        {/* A question in a bubble carries no kind badge (phase / outcome are internal). */}
        <strong>{t("component.workspace_chat.workflow_retry_title")}</strong>
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

/** What an input card asks, in the asker's own words.
 *
 *  `payload.why` is not shown: the record layer requires one, so producers
 *  fill it from the asker's context or justification and otherwise with a
 *  stock line ("The agent needs this to finish the task.") that tells the
 *  person nothing the card does not already say. */
function inputCardCopy(action?: PendingAction): { question: string; context: string } {
  const payload = asRecord(action?.payload);
  const question = typeof payload?.question === "string"
    ? payload.question.trim()
    : typeof action?.prompt === "string" ? action.prompt.trim() : "";
  const justification = asRecord(payload?.justification);
  const context = [payload?.context, justification?.why_human_required]
    .find((value): value is string => typeof value === "string" && Boolean(value.trim()));
  // A request that has a deadline says so on the card, as a date a person reads.
  const due = typeof action?.expected_by === "string" ? formatDate(action.expected_by, "") : "";
  const neededBy = due ? t("component.chat_action_card.needed_by", { date: due }) : "";
  return { question, context: stripInternalReferences([context?.trim(), neededBy].filter(Boolean).join(" · ")) };
}

/** Past this a question is a paragraph: it reads at body weight, not bold. */
const LONG_QUESTION_CHARS = 140;

function InputQuestionSummary({ question, context }: { question: string; context: string }) {
  if (!question && !context) return null;
  const long = question.length > LONG_QUESTION_CHARS || question.includes("\n");
  return (
    <div className="chat-hitl-summary">
      {question && (
        <div className={`chat-hitl-title${long ? " chat-hitl-title--long" : ""}`}>
          <ChatMarkdown content={question} />
        </div>
      )}
      {context && <div className="chat-hitl-description">{context}</div>}
    </div>
  );
}

export function HitlInputCard({ action, onResolve, placeholder, disabled, resetToken, showSkip = true, workspaceId }: {
  action?: PendingAction;
  workspaceId?: string;
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
  // With options to pick from, picking one is the answer. A "change it" option
  // opens the shared revision box; "Something else…" opens this card's own box.
  const [revising, setRevising] = useState<string | null>(null);
  const [writing, setWriting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const attachButtonRef = useRef<HTMLButtonElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Keep the note and File objects while a failed upload/resolve becomes retryable.
  useEffect(() => { setSubmitting(false); }, [resetToken]);
  const inputPayload = asRecord(action?.payload);
  const { question, context } = inputCardCopy(action);
  // Inline metadata HITL synthesizes approval options without a Task/Step
  // origin. Only persisted input cards may use their top-level options.
  const rawOptions = inputPayload?.options
    || ((action?.task_id || action?.step_id || action?.commitment_id) ? action.options : []);
  const recommendation = recommendationOf(action) || recommendationOf(inputPayload as RecommendedQuestion | null);
  const options = orderedOptions(Array.isArray(rawOptions)
    ? rawOptions.filter((option): option is string => typeof option === "string" && Boolean(option.trim()))
    : [], recommendation);
  // The asker's fields (request_input) are one form drawn with the Workflow form's controls.
  const questionFields = useMemo(() => (Array.isArray(inputPayload?.fields) ? inputPayload.fields : [])
    .map(requestInputFieldFrom)
    .filter((field): field is RequestInputField => field !== null), [inputPayload?.fields]);
  const formSchema = useMemo(() => requestInputFormSchema(questionFields), [questionFields]);
  const form = useFormDraft<unknown>(
    questionFields.length ? formDraftKey("hitl-input", String(action?.approval_request_id || "")) : null,
    () => workflowSchemaDraft(formSchema, {}),
  );
  const [formErrors, setFormErrors] = useState<Record<string, string>>({});
  const [formFileBusy, setFormFileBusy] = useState(false);
  const parsedForm = (): Record<string, unknown> | null => {
    const errors: Record<string, string> = {};
    const parsed = parseWorkflowSchemaDraft(formSchema, form.values, "fields", false, errors) as Record<string, unknown>;
    setFormErrors(errors);
    return Object.values(errors).some(Boolean) ? null : parsed;
  };
  const submitForm = (choice?: string) => {
    if (submitting || disabled || formFileBusy) return;
    const fields = parsedForm();
    if (!fields) return;
    const said = choice || requestInputAnswerText(questionFields, fields, t("component.workflow_schema.yes"), t("component.workflow_schema.no"));
    setSubmitting(true);
    form.clear();
    onResolve("respond", said, { response: said, fields });
  };
  // One tap answers with a choice; the box below is for anything else.
  const answerWith = (option: string, payload?: Record<string, unknown>) => {
    if (submitting || disabled) return;
    if (asksForChanges(option)) {
      setRevising((current) => (current === option ? null : option));
      setWriting(false);
      return;
    }
    // With a form, a choice carries what was filled in.
    if (questionFields.length) {
      submitForm(option);
      return;
    }
    setSubmitting(true);
    onResolve("respond", option, { ...payload, response: option });
  };
  const writeSomethingElse = () => {
    setRevising(null);
    setWriting(true);
    setTimeout(() => textareaRef.current?.focus(), 0);
  };
  const sendRevision = (request: string) => {
    if (!revising || submitting || disabled) return;
    setSubmitting(true);
    // "Request changes: shorter subject line" — the choice and what it means.
    const said = `${revising}: ${request}`;
    onResolve("respond", said, { response: said });
  };
  // The asker's own fields are where the answer goes; the free box is only for "Something else…".
  const showComposer = (options.length === 0 && questionFields.length === 0) || writing || attachments.length > 0;

  const handleSubmit = () => {
    if ((!value.trim() && attachments.length === 0) || submitting || disabled) return;
    setSubmitting(true);
    const said = value.trim();
    const localFiles = attachments.flatMap((attachment) =>
      attachment.file ? [attachment.file] : [],
    );
    const serializableAttachments = attachments
      .filter((attachment) => !attachment.file)
      .map(({ name, id, type }) => ({ name, id, type }));
    onResolve(
      "respond",
      said,
      serializableAttachments.length > 0
        ? { response: said, attachments: serializableAttachments }
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
      <InputQuestionSummary question={question} context={context} />
      {options.length > 0 && (
        // The same answers as an approval: the recommendation first, "Something
        // else…" last, and a change option asks what should change.
        <RecommendedOptions
          question={{ recommendation, option_details: action?.option_details ?? inputPayload?.option_details, allow_other: true }}
          options={options}
          labelFor={(option) => option}
          onChoose={answerWith}
          onOther={writeSomethingElse}
          otherOpen={writing}
          disabled={disabled || submitting}
          ariaExpandedFor={(option) => (asksForChanges(option) ? revising === option : undefined)}
        >
          {revising && <RevisionRequestBox disabled={disabled || submitting} onSubmit={sendRevision} />}
        </RecommendedOptions>
      )}
      {questionFields.length > 0 && (
        <div className="chat-question-form">
          {form.restored && <FormDraftNotice onDiscard={form.reset} disabled={disabled || submitting} />}
          <WorkflowSchemaFields
            rootKey="fields"
            schema={formSchema}
            value={form.values}
            errors={formErrors}
            disabled={disabled || submitting}
            workspaceId={workspaceId}
            onFileBusyChange={setFormFileBusy}
            onChange={(path, next) => {
              form.setValues((current: unknown) => setWorkflowValueAtPath(current, path, next));
              setFormErrors((current) => ({ ...current, [workflowInputErrorPath("fields", path)]: "" }));
            }}
          />
          {options.length === 0 && (
            <div className="workflow-starter-input-actions">
              {showSkip && (
                <button type="button" className="chat-hitl-btn-secondary" onClick={() => onResolve("skip")} disabled={submitting || disabled}>
                  {t("component.chat_action_card.skip")}
                </button>
              )}
              <button type="button" className="chat-hitl-btn-primary" onClick={() => submitForm()} disabled={submitting || disabled || formFileBusy}>
                {submitting ? t("component.chat_action_card.sending") : t("component.chat_action_card.submit")}
              </button>
            </div>
          )}
        </div>
      )}
      <input ref={fileRef} type="file" multiple accept="*/*" hidden disabled={disabled || submitting} onChange={handleFiles} />
      {showComposer && <>
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
          ref={textareaRef}
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
      </>}
      {knowledgeOpen && (
        <HitlKnowledgePicker
          selectedIds={new Set(attachments.flatMap((attachment) => attachment.id ? [attachment.id] : []))}
          onToggle={(document) => setAttachments((current) => current.some((attachment) => attachment.id === document.id)
            ? current.filter((attachment) => attachment.id !== document.id)
            : [...current, { id: document.id, name: document.name, type: "knowledge" }])}
          onClose={() => setKnowledgeOpen(false)}
          restoreFocus={() => attachButtonRef.current?.focus()}
          workspaceId={workspaceId}
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
  // An integration's setup page opens over this chat instead of a new tab.
  const setupProvider = loginUrl ? parseIntegrationSetupLink(loginUrl, window.location.origin) : null;
  // Any sign-in that is an integration's setup is a request to connect it.
  const connectRequest = Boolean(setupProvider);
  const catalog = useQuery(integrationCatalogQueryOptions(connectRequest));
  const server = connectRequest ? catalog.data?.find((item) => item.server_key === setupProvider) : undefined;
  const title = codedCardText(action.title_code, action.title_params, action.title)
    || t("component.chat_action_card.sign_in_required");
  // A connect request says what waits for the account; a login wall names it.
  const description = codedCardText(action.description_code, action.description_params, action.description)
    || String(action.integration_hint || "");

  // A run waiting at a step for this account continues as soon as it is
  // connected from this card; there is nothing else to decide.
  const resumesRun = Boolean(action.waiting_workflow_run_id);

  const openLogin = () => {
    if (!loginUrl) return;
    window.open(loginUrl, "_blank", "noopener,noreferrer");
    setOpened(true);
  };

  if (connectRequest && server?.agent_can_use) {
    const provider = String((action.title_params as { provider?: string } | undefined)?.provider || server.name || setupProvider);
    // Connected: the work that waited for it can go on, recommended first.
    return (
      <div className="chat-hitl-input-card">
        <div className="chat-hitl-summary">
          <div className="chat-hitl-title">{t("component.integration_connect.card_connected", { provider })}</div>
          <div className="chat-hitl-description">{t("component.integration_connect.card_continue_hint")}</div>
        </div>
        <RecommendedOptions
          question={{
            options: ["continue_after_login"],
            option_details: { continue_after_login: { label: t("ui.continue_the_waiting_work"), label_code: "integration.continue" } },
            recommendation: { option: "continue_after_login", reason: "", reason_code: "connected_continue", reason_params: { provider } },
          }}
          options={["continue_after_login"]}
          labelFor={approvalLabel}
          onChoose={onResolve}
          disabled={disabled}
        />
      </div>
    );
  }

  if (connectRequest && setupProvider) {
    // An integration is connected with the same row as everywhere; a run waiting for it then goes on.
    const skippable = (action.options || []).includes("skip");
    return (
      <div className="chat-hitl-input-card">
        <div className="chat-hitl-summary">
          <div className="chat-hitl-title">{title}</div>
          {description && <div className="chat-hitl-description">{description}</div>}
        </div>
        <IntegrationConnectRow as="div" requirement={{ kind: "integration", provider: setupProvider, label: server?.name }}
          onConnected={() => { if (resumesRun) onResolve("continue_after_login"); }} />
        {skippable && <RecommendedOptions
          question={{ ...action, recommendation: undefined }}
          options={["skip"]}
          labelFor={approvalLabel}
          onChoose={onResolve}
          disabled={disabled}
        />}
      </div>
    );
  }

  return (
    <div className="chat-hitl-input-card">
      <div className="chat-hitl-summary">
        <div className="chat-hitl-title">{title}</div>
        {description && <div className="chat-hitl-description">{description}</div>}
      </div>
      {/* Sign in on the provider's own page; then continue. The next step
          is always the recommended one. */}
      <RecommendedOptions
        question={opened ? {
          ...action,
          option_details: { skip: action.option_details?.skip || {} },
          recommendation: { option: "continue_after_login", reason: "" },
        } : action}
        options={opened ? ["continue_after_login", "skip"] : (action.options?.length ? action.options : ["sign_in", "skip"])}
        labelFor={approvalLabel}
        onChoose={(choice) => (choice === "sign_in" ? openLogin() : onResolve(choice))}
        isDisabled={(choice) => choice === "sign_in" && !loginUrl}
        disabled={disabled}
      />
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
      <ApprovalCard question={action} options={action.options || ["confirm", "cancel"]} onResolve={onResolve} disabled={disabled} />
    </>
  );
}

/* ── Resolved Badge (shows after action taken) ── */

/** A connect card's answer in its own words: "Continue the waiting work" / "Remind me later". */
function connectChoiceLabel(action: PendingAction, choice?: string): string | undefined {
  if (!action?.connection_request) return undefined;
  if (choice === "continue_after_login") return t("recommendation.label.integration.continue");
  if (choice === "skip") return t("recommendation.label.integration.later");
  return undefined;
}

function ResolvedActionSummary({ action, hasTaskReference, resolution, promptShown }: {
  action: PendingAction;
  hasTaskReference?: boolean;
  promptShown?: boolean;
  resolution?: { choice?: string } | null;
}) {
  if (!action?.kind || action.kind === "unknown" || action.kind === "approve_proposals") {
    return null;
  }

  if (action.connection_request) {
    // A connect request reads in the viewer's language; continuing means it was connected.
    const provider = String((action.title_params as { provider?: string } | undefined)?.provider || action.provider_key || "");
    const connected = resolution?.choice === "continue_after_login";
    return (
      <div className="chat-hitl-summary">
        <div className="chat-hitl-title">
          {connected ? t("component.integration_connect.card_connected", { provider }) : codedCardText(action.title_code, action.title_params, action.title)}
        </div>
        {!connected && (
          <div className="chat-hitl-description">
            {codedCardText(action.description_code, action.description_params, action.description)}
          </div>
        )}
      </div>
    );
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
        promptShown={promptShown}
        resolved
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

  if (action.kind === PendingActionKind.HUMAN_INPUT) {
    // Answered: what was asked, not the call to answer it.
    const { question, context } = inputCardCopy(action);
    if (question || context) return <InputQuestionSummary question={question} context={context} />;
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

function deliveryOutcomeLabel(resolution: Resolution): string {
  const outcome = String(resolution.delivery_outcome || "");
  if (outcome === "sent") return t("component.chat_action_card.delivery_sent");
  if (outcome === "unknown") return t("component.chat_action_card.delivery_unknown");
  if (outcome === "not_sent") return t("component.chat_action_card.delivery_not_sent");
  return "";
}

function ResolvedCheckIcon() {
  return (
    <svg className="chat-hitl-resolved-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 12.5l4.5 4.5L19 7.5" />
    </svg>
  );
}

/** An answered card's options collapse into this one line inside the same
 * bubble: "You chose “Approve” · Sent". No separate receipt is posted. */
export function ResolvedBadge({ resolution, by, choiceLabel }: {
  resolution: Resolution;
  by?: string;
  /** The card's own words for the choice (e.g. a connect card's options). */
  choiceLabel?: string;
}) {
  const choice = resolution.choice || "";
  const tone = approvalTone(choice);
  const normalized = normalizeChoice(choice);
  const isApprove = tone === "approve" || tone === "always";
  const isReply = ["respond", "provide_answers", "submit", "feedback"].includes(normalized);
  const isReject = tone === "reject" && normalized !== "stopped";
  const isPersonChoice = Boolean(choiceLabel) || isApprove || isReject || [
    "retry", "retry_now", "cancel", "revise", "request_changes",
  ].includes(normalized);
  // Closed by the system, or a choice this card does not know: never claim
  // that someone approved it.
  const systemStatus = normalized === "superseded" ? t("component.chat_action_card.superseded")
    : normalized === "expired" ? t("component.chat_action_card.expired")
    : normalized === "stopped" ? t("component.status.cancelled")
    : !isReply && !isPersonChoice ? t("component.chat_action_card.status_unknown")
    : "";

  const label = systemStatus
    || (isReply
      ? by
        ? t("component.chat_action_card.someone_replied", { name: by })
        : t("component.chat_action_card.you_replied")
      : by
        ? t("component.chat_action_card.someone_chose", { name: by, choice: choiceLabel || resolvedChoiceLabel(choice) })
        : t("component.chat_action_card.you_chose", { choice: choiceLabel || resolvedChoiceLabel(choice) }));
  const variant = systemStatus
    ? "chat-hitl-resolved--revised"
    : (isApprove || isReply)
      ? "chat-hitl-resolved--approved"
      : isReject
        ? "chat-hitl-resolved--rejected"
        : "chat-hitl-resolved--revised";
  const outcome = deliveryOutcomeLabel(resolution);

  return (
    <div className={`chat-hitl-resolved ${variant}`}>
      {systemStatus ? <span className="chat-hitl-resolved-icon" aria-hidden="true">—</span> : <ResolvedCheckIcon />}
      <span className="chat-hitl-resolved-label">{label}</span>
      {outcome && <span className="chat-hitl-resolved-outcome"> · {outcome}</span>}
      {resolution.note && <span className="chat-hitl-resolved-note"> · {resolution.note}</span>}
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

  return (
    <div className="chat-proposal-resolution">
      <ResolvedBadge resolution={{ ...resolution, note: undefined }} by={by} />
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

/* ── Blueprint release offered by a delivered Workspace's provider ── */

function blueprintUpdateText(key: string, params?: Record<string, string | number>): string {
  return t(`component.chat_action_card.blueprint_update_${key}`, params);
}

/** One click applies the release with the default choices; the upgrade
 *  dialog is there for someone who wants to choose item by item. */
function BlueprintUpdateCard({ action, onResolve, disabled, workspaceId }: {
  action: PendingAction;
  onResolve: (choice: string) => void;
  disabled?: boolean;
  workspaceId?: string;
}) {
  const [reviewing, setReviewing] = useState(false);
  const version = String(action.version || "");
  const plan = useQuery({
    queryKey: ["blueprint-update-card", workspaceId, version],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId!, undefined, undefined, version || undefined),
    enabled: Boolean(workspaceId),
    staleTime: 30_000,
  });
  const runs = plan.data?.active_runs ?? [];
  const needsSetup = plan.data ? plan.data.variables_ready === false || plan.data.setup_preflight?.ready === false : false;
  const update = async () => {
    // Runs finish without telling this card; look again before applying.
    const fresh = runs.length > 0 || needsSetup ? (await plan.refetch()).data : plan.data;
    if (fresh && (fresh.active_runs.length > 0 || fresh.variables_ready === false || fresh.setup_preflight?.ready === false)) return;
    onResolve("update");
  };
  // Minimal: the version, the provider's notes, one action. Detail lives in the dialog.
  const blocked = runs.length > 0
    ? blueprintUpdateText("blocked_runs", { count: runs.length })
    : needsSetup ? blueprintUpdateText("blocked_setup") : "";
  return (
    <div className="space-y-2">
      <strong className="block text-sm text-stone-900">{blueprintUpdateText("title", { version })}</strong>
      {action.release_notes && <p className="whitespace-pre-line text-sm text-stone-600">{String(action.release_notes)}</p>}
      {blocked && <p className="text-xs text-amber-800">{blocked}</p>}
      <div className="flex items-center gap-3">
        <button type="button" className="chat-hitl-btn-primary" title={blueprintUpdateText("keeps_edits")}
          onClick={() => void update()} disabled={disabled || plan.isFetching || needsSetup}>
          {blueprintUpdateText("update")}
        </button>
        <button type="button" className="text-xs text-stone-500 hover:text-stone-800" onClick={() => setReviewing(true)} disabled={!workspaceId}>
          {blueprintUpdateText("review")}
        </button>
      </div>
      {workspaceId && reviewing && <Suspense fallback={null}>
        <BlueprintUpgradeDialog open onClose={() => setReviewing(false)} workspaceId={workspaceId}
          workspaceName={String(action.workspace_name || "")} />
      </Suspense>}
    </div>
  );
}

function BlueprintUpdateResolution({ action, resolution, workspaceId }: {
  action: PendingAction;
  resolution: Resolution;
  workspaceId?: string;
}) {
  const queryClient = useQueryClient();
  const [reverted, setReverted] = useState(false);
  const [busy, setBusy] = useState(false);
  const payload = resolution.payload ?? {};
  const version = String(payload.version || action.version || "");
  const applied = resolution.choice === "update" && payload.updated !== undefined;
  // Undo reverts the Workspace's latest upgrade, so offer it only on the card that made it.
  const latest = useQuery({
    queryKey: ["blueprint-update-restore-point", workspaceId],
    queryFn: () => api.workspaces.blueprintUpgradePlan(workspaceId!),
    enabled: Boolean(workspaceId && applied && payload.can_revert && !payload.undone),
    staleTime: 30_000,
  });
  const canUndo = applied && !reverted && !payload.undone && latest.data?.can_revert
    && latest.data.restore_point?.blueprint_version === version;
  const undo = async () => {
    if (!workspaceId) return;
    setBusy(true);
    try {
      await api.workspaces.revertBlueprintUpgrade(workspaceId);
      setReverted(true);
      await Promise.all(["workspace", "blueprint-update-restore-point", "blueprint-update-card", "blueprint-upgrade-plan"]
        .map((key) => queryClient.invalidateQueries({ queryKey: [key, workspaceId] })));
    } finally {
      setBusy(false);
    }
  };
  // One quiet line: what happened, and undo while it is still the latest upgrade.
  const line = resolution.choice === "superseded"
    ? blueprintUpdateText("superseded", { version: String(payload.version || "") })
    : !applied ? t("status.loading")
    : reverted || payload.undone ? blueprintUpdateText("reverted")
    : [blueprintUpdateText("updated", { version }),
       Number(payload.kept) > 0 ? blueprintUpdateText("updated_kept", { kept: Number(payload.kept) }) : ""]
      .filter(Boolean).join(" · ");
  return (
    <div className="flex items-center gap-3 text-sm text-stone-500">
      <span>{line}</span>
      {canUndo && <button type="button" className="text-xs text-stone-500 underline-offset-2 hover:text-stone-800 hover:underline"
        disabled={busy} onClick={() => void undo()}>
        {blueprintUpdateText("revert")}
      </button>}
    </div>
  );
}

/* ── Composite: renders the right card based on pending_action ── */

function EditableChatWorkflowReview({ action, disabled, onResolve }: {
  action: PendingAction;
  disabled: boolean;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
}) {
  const [draft, setDraft] = useState<Record<string, unknown>>(action.review);
  const [valid, setValid] = useState(true);
  const revisionField = typeof action.review_revision_field === "string" ? action.review_revision_field : "";
  const revision = revisionField ? String(draft[revisionField] ?? "").trim() : "";
  const onDraftState = useCallback((value: Record<string, unknown>, isValid: boolean) => {
    setDraft(value);
    setValid(isValid);
  }, []);
  return <>
    <EditableWorkflowApprovalReview
      prompt={action.prompt}
      review={action.review}
      reviewTitle={action.review_title}
      editableFields={action.review_editable_fields}
      revisionField={revisionField || undefined}
      disabled={disabled}
      onDraftState={onDraftState}
    />
    <div className="chat-hitl-actions">
      {reviewApprovalOptions(action.options).filter((choice) => !asksForChanges(choice) || revisionField).map((choice) => {
        const revise = asksForChanges(choice);
        const approve = approvalTone(choice) === "approve";
        return <button
          key={choice}
          type="button"
          className={approve ? "chat-hitl-btn-primary" : "chat-hitl-btn-secondary"}
          disabled={disabled || ((approve || revise) && !valid) || (revise && !revision) || (approve && Boolean(revision))}
          onClick={() => onResolve(choice, revise ? revision : undefined, (approve || revise) ? { review: draft } : undefined)}
        >{revise ? t("component.workflow_review.request_changes") : approvalLabel(choice)}</button>;
      })}
    </div>
  </>;
}

export default function ChatActionCard({ action, resolved, resolution, onResolve, disabled, resolvedByName, resetToken, error, proposalSelectedRowIds, onProposalSelectedRowIdsChange, proposalRowsRenderedElsewhere, workspaceId, hasTaskReference, promptShown }: {
  action: PendingAction;
  /** The action's prompt already reads as the message text above the card. */
  promptShown?: boolean;
  resolved?: boolean;
  resolution?: Resolution | null;
  disabled?: boolean;
  /** Display name of whoever resolved this action (from the server). */
  resolvedByName?: string;
  /** Kept for callers; the viewer's own choice always reads "You chose". */
  currentUserName?: string;
  /** Increment after a failed request so an optimistic action becomes interactive again. */
  resetToken?: number;
  /** Workspace proposal cards render their selectable rows inside Proposed
   * Work. Other chat surfaces keep the ProposalCard's own unified row list. */
  /** Why the last answer was not accepted; the card stays open with it. */
  error?: string;
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
  const [alwaysAllowError, setAlwaysAllowError] = useState("");
  const submittedRef = useRef(false);
  const previousResetTokenRef = useRef(resetToken);
  // Uploading is not resolved. Keep input cards mounted until server confirmation
  // so an error can unlock the same draft, including its original File objects.
  const editableWorkflowReview = action.kind === PendingActionKind.WORKFLOW_APPROVAL
    && action.review_read_only !== true
    && Array.isArray(action.review_editable_fields)
    && workflowReviewIsEditable(action.review);
  const isInputCard = action.kind === PendingActionKind.HUMAN_INPUT || action.kind === PendingActionKind.WORKFLOW_INPUT || editableWorkflowReview;
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
    if (choice === ALWAYS_ALLOW_CHOICE && action.approval_request_id) {
      // Save the allow rule through the versioned command, then approve on
      // this card's own path; a failure leaves the card open and undecided.
      setAlwaysAllowError("");
      setLocalResolution({ choice, note, payload });
      alwaysAllow(action.approval_request_id, () => onResolve("approve", note, payload, files)).catch(() => {
        submittedRef.current = false;
        setLocalResolution(null);
        setAlwaysAllowError(t("recommendation.always_error"));
      });
      return;
    }
    setLocalResolution({ choice, note, payload });
    onResolve(choice, note, payload, files);
  };

  const renderCard = () => {
    if (effectiveResolution) {
      // Another person's choice names them; the viewer's own reads "You chose".
      const by = resolvedByName;
      if (action.kind === PendingActionKind.APPROVE_PROPOSALS) {
        return <ProposalResolution action={action} resolution={effectiveResolution} by={by} />;
      }
      if (action.kind === PendingActionKind.BLUEPRINT_UPDATE) {
        return <BlueprintUpdateResolution action={action} resolution={effectiveResolution} workspaceId={workspaceId} />;
      }
      return (
        <div className="chat-hitl-resolved-card">
          <ResolvedActionSummary action={action} hasTaskReference={hasTaskReference} resolution={effectiveResolution as { choice?: string } | null} promptShown={promptShown} />
          <ResolvedBadge resolution={effectiveResolution} by={by} choiceLabel={connectChoiceLabel(action, effectiveResolution?.choice)} />
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
      return <HitlInputCard action={action} onResolve={resolveOnce} disabled={locked} resetToken={resetToken} showSkip={false} workspaceId={workspaceId} />;
    }

    if (action.kind === PendingActionKind.WORKFLOW_STARTER_INPUT) {
      return <WorkflowStarterInputCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} />;
    }

    if (action.kind === PendingActionKind.WORKFLOW_RETRY) {
      return <WorkflowRetryCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} />;
    }

    if (action.kind === PendingActionKind.BLUEPRINT_UPDATE) {
      return <BlueprintUpdateCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} />;
    }

    if (action.kind === PendingActionKind.WORKFLOW_INPUT) {
      if (Array.isArray(action.inputs) && action.inputs.length > 0) {
        return <WorkflowStarterInputCard action={action} onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} submitChoice="respond" />;
      }
      // A wait with its own form asks its fields, holding their defaults (an edit shows the
      // record's current values); the server reads `inputs` as this wait's answer.
      const schemaInputs = workflowWaitSchemaInputs(action.input_schema);
      if (schemaInputs.length > 0) {
        return <WorkflowStarterInputCard
          action={{ ...action, inputs: schemaInputs, title: action.title || action.prompt }}
          onResolve={resolveOnce} disabled={locked} workspaceId={workspaceId} submitChoice="respond" />;
      }
      return (
        <HitlInputCard
          onResolve={resolveOnce}
          placeholder={typeof action.prompt === "string" ? action.prompt : undefined}
          disabled={locked}
          resetToken={resetToken}
          workspaceId={workspaceId}
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
      if (editableWorkflowReview) {
        return <EditableChatWorkflowReview action={action} disabled={locked} onResolve={resolveOnce} />;
      }
      return (
        <>
          <WorkflowApprovalReview
            prompt={action.prompt}
            review={action.review}
            reviewTitle={action.review_title}
            promptShown={promptShown}
          />
          {action.kind === PendingActionKind.TASK_APPROVAL && action.task_id && (
            <div className="chat-hitl-links">
              <CardOriginLink taskId={action.task_id} />
            </div>
          )}
          <ApprovalCard
            question={action}
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
          question={action}
          options={
            action.hitl_type === "review"
              ? reviewApprovalOptions(action.options)
              : action.options
          }
          onResolve={resolveOnce}
          disabled={locked}
        />
        {alwaysAllowError && <p className="chat-hitl-recommendation-reason" role="alert">{alwaysAllowError}</p>}
        {/* Cards that already offer "Always allow" as an option need no second control. */}
        {action.approval_request_id && action.hitl_type !== "review" && !(action.options || []).includes(ALWAYS_ALLOW_CHOICE) && <ApprovalSaveRuleAction key={action.approval_request_id} requestId={action.approval_request_id} scopeLabel={action.workspace?.name} disabled={locked} onCompleted={() => setLocalResolution({ choice: "approve", note: t("approval_settings.approve_save_rule") })} />}
      </>
    );
  };
  const card = renderCard();
  // A refused answer leaves the card open, with why, quietly.
  const showError = Boolean(error && !effectiveResolution);
  return showError ? (
    <>
      {card}
      <p className="chat-hitl-recommendation-reason chat-hitl-answer-error" role="alert">{error}</p>
    </>
  ) : card;
}
