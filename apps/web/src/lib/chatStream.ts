/**
 * Shared SSE stream processing for chat components.
 *
 * Used by: EmbeddedChat, FloatingChat
 */
import { recommendationOf, type Recommendation, type RecommendedQuestion } from "./recommendedOptions";
import { requestInputFieldFrom, type RequestInputField } from "./requestInputForm";
import { useState, useEffect } from "react";
import { useUpgradeStore } from "../stores/upgrade";
import {
  api,
  type PlanLimitDetail,
  type PlanLimitKind,
  type WorkspaceLedgerOverview,
  type WorkspaceLedgerQueryVisualization,
} from "./api";
import { t } from "./i18n";
import { planLimitOffersUpgrade } from "./planLimit";
import { normalizeFileRef } from "./fileReferences";
import { isSiteAttachment } from "./publishedSites";
import type { ManualSkillReference } from "./manualSkillRefs";
import { TYPEWRITER_TICK_MS, nextTypewriterSlice } from "./chatTypewriter";
import { isWebsiteAccessDenied, PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE } from "./websiteAccessDenied";

/* ── Types ── */

export interface ToolCall {
  name: string;
  /** Already written for people: shown as is instead of a label made from `name`. */
  displayLabel?: string;
  args?: unknown;
  arguments?: string;
  result?: string;
  /** Full persisted Workspace Draft output. Live SSE keeps using the compact
   *  `result`; this is retained after history hydration only when the draft
   *  artifact JSON would otherwise be parsed from a truncated preview. */
  rawResult?: string;
  status?: "pending" | "success" | "error" | "unverified";
  reason?: string;
  status_code?: number;
  startedAt?: number;
  duration?: string;
  /** When a wrapper tool (invoke_skill, manor, code) is pending and a
   *  child sub-tool is running inside it, ``activeChild`` mirrors the
   *  child's name so the parent's spinner can render "invoke_skill →
   *  bash" instead of just "invoke_skill (loading)". Cleared when the
   *  child finishes. */
  activeChild?: string;
  lastChild?: string;
  childCount?: number;
  completedChildCount?: number;
}

export interface AssistantTextBlock {
  id: string;
  type: "text";
  phase?: "opening" | "progress" | "final" | string;
  after_step_seq?: number;
  text: string;
}

export interface AssistantProcessStep {
  id: string;
  seq?: number;
  kind?: "tool" | string;
  name: string;
  display_name?: string;
  display_key?: string;
  display_params?: Record<string, string | number>;
  status?: "running" | "pending" | "success" | "completed" | "error" | string;
  reason?: string;
  status_code?: number;
  summary?: string;
  assistant_text?: string;
  arguments_preview?: string;
  result_preview?: string;
  /** The model is still writing this call; it has not run yet. */
  drafting?: boolean;
  /** The line the model is writing now, while ``drafting``. */
  draft_preview?: string;
  recovered_by_step_id?: string;
  verified_by_step_id?: string;
  action_id?: string;
  approval_request_id?: string;
  presentation?: {
    kind: "chrome_wait_observation_timeout" | "chrome_stale_snapshot_rejection";
    tone: "neutral";
  };
  duration_ms?: number;
  started_at_ms?: number;
  ended_at_ms?: number;
}

export interface AssistantProcessBlock {
  id: string;
  type: "process";
  title?: string;
  note?: string;
  status?: "running" | "pending" | "completed" | "success" | "error" | string;
  default_collapsed?: boolean;
  duration_ms?: number;
  // Turn lifecycle is independent of individual tool-attempt outcomes.
  lifecycle_status?: "running" | "completed" | "failed" | "cancelled" | "waiting";
  outcome_status?: "answered" | "completed" | "partial" | "blocked" | "not_attempted";
  started_at_ms?: number;
  ended_at_ms?: number;
  steps: AssistantProcessStep[];
}

export interface AssistantLedgerOverviewVisualizationBlock {
  id: string;
  type: "visualization";
  kind: "workspace_ledger_overview";
  data: WorkspaceLedgerOverview;
}

export interface AssistantLedgerQueryVisualizationBlock {
  id: string;
  type: "visualization";
  kind: "ledger_query_result";
  data: WorkspaceLedgerQueryVisualization;
}

export type AssistantVisualizationBlock =
  | AssistantLedgerOverviewVisualizationBlock
  | AssistantLedgerQueryVisualizationBlock;

export interface AssistantResponseSurfaceAction {
  id: string;
  label: string;
  intent: "submit";
}

export interface AssistantTemplateSurfaceRender {
  kind: "template";
  template_id:
    | "learning.code_lab"
    | "response.choice"
    | "workspace.ledger.overview"
    | "workspace.ledger.query"
    | "card.chart"
    | "card.delivery"
    | "card.email"
    | "card.post"
    | "card.comment"
    | "card.person"
    | "card.event"
    | "card.stock"
    | "card.product"
    | "card.map"
    | "card.search_results";
  template_version: 1;
  props: Record<string, unknown>;
}

export interface AssistantGeneratedSurfaceRender {
  kind: "sandboxed_html";
  code: {
    version: 1;
    runtime: "sandboxed_html";
    html: string;
    css: string;
    javascript: string;
  };
  data: Record<string, unknown>;
  validation: {
    policy: "response_surface.v1" | "response_surface.v2";
    code_hash: string;
  };
}

export interface AssistantResponseSurfaceBlock {
  id: string;
  type: "surface";
  version: 1;
  title: string;
  description?: string;
  render: AssistantTemplateSurfaceRender | AssistantGeneratedSurfaceRender;
  display: {
    preferred: "inline" | "focus";
    inline_height: number;
    focusable: boolean;
  };
  actions: AssistantResponseSurfaceAction[];
  fallback_markdown: string;
}

export interface ResponseSurfaceSubmission {
  sourceMessageId: string;
  surfaceId: string;
  title: string;
  action: string;
  actionLabel: string;
  payload: Record<string, unknown>;
  context?: {
    templateId?: AssistantTemplateSurfaceRender["template_id"];
    instructions?: string;
    checks?: string[];
  };
}

export interface ResponseSurfaceSubmissionReceipt extends ResponseSurfaceSubmission {
  version: 1;
  eventId: string;
  recordedAt: string;
  /** UI projection only; never required by the submission API. */
  outcome?: "failed" | "interrupted";
  /** UI projection only; true after the receipt is read from durable history. */
  durable?: boolean;
  /** UI projection only; derived from the assistant message linked to this receipt. */
  status?: "pending" | "succeeded" | "failed" | "interrupted";
}

export interface ResponseSurfaceSubmissionResult {
  status: "succeeded" | "failed" | "cancelled";
  /** Whether the server durably accepted this event. */
  serverAccepted: boolean;
  /** Whether a determinate business outcome, not only a persistence failure, was observed. */
  terminalObserved: boolean;
}

export type AssistantBlock =
  | AssistantTextBlock
  | AssistantProcessBlock
  | AssistantVisualizationBlock
  | AssistantResponseSurfaceBlock;

// Tools that internally spawn nested agentic loops. While one of these
// is pending, sub-tool events that arrive should update the parent's
// ``activeChild`` so the spinner shows real progress instead of staying
// on a generic "loading" forever.
export const WRAPPER_TOOLS = new Set(["invoke_skill", "manor", "code"]);

export interface SubAgentEvent {
  run_id?: string;
  agent_id?: string;
  agent_subscription_id?: string;
  agent_name: string;
  agent_avatar?: string;
  service_key?: string;
  objective?: string;
  content: string;
  event_type?: string;
  status?: "running" | "completed" | "blocked" | "failed" | string;
  updated_at?: string;
  rounds?: number;
  tool_calls_made?: string[];
  tools?: {
    seq?: number;
    name: string;
    status?: "running" | "completed" | "error" | string;
    duration_ms?: number;
    arguments?: unknown;
  }[];
  tool?: {
    seq?: number;
    name: string;
    status?: "running" | "completed" | "error" | string;
    duration_ms?: number;
    arguments?: unknown;
  };
  stop_reason?: string;
  error?: string;
  timestamp?: string;
}

export interface HITLRequest {
  id: string;
  prompt: string;
  type?: "approval" | "input";
  action?: string;
  tool?: string;
  workspace?: { id?: string; name?: string };
  paths?: string[];
  content?: unknown;
  args_preview?: unknown;
  operation?: unknown;
  review?: unknown;
  review_title?: string;
  review_read_only?: boolean;
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
  /** Set when a durable run is parked on this approval: it is decided
   * against that run (`/chat/runs/{id}/approval`), not by a chat message. */
  runtime_run_id?: string;
  resolved?: boolean;
  resolution?: string;
}

export interface ChatMessageRef {
  type: string;
  id: string;
  title?: string;
  name?: string;
  [key: string]: unknown;
}

export interface ChatMessagePendingAction {
  kind: string;
  [key: string]: unknown;
}

export interface WorkflowResultReference {
  workflow_id: string;
  workflow_name: string;
  run_id: string;
  url?: string;
  status?: string;
  kind?: string;
  title?: string | null;
  summary?: string | null;
  publication_summary?: {
    count?: number;
    verified?: number;
    platforms?: string[];
  } | null;
}

export type WorkspaceRecommendationAction =
  | "create_new"
  | "open_existing"
  | "add_to_existing";

export interface WorkspaceRecommendation {
  action: WorkspaceRecommendationAction;
  reason: string;
  request: string;
  workspace_id?: string;
  workspace_name?: string;
}

export function normalizeWorkspaceRecommendation(
  value: unknown,
): WorkspaceRecommendation | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  const action = String(item.action || "") as WorkspaceRecommendationAction;
  const reason = String(item.reason || "").trim();
  const request = String(item.request || "").trim();
  const workspaceId = String(item.workspace_id || "").trim();
  const workspaceName = String(item.workspace_name || "").trim();
  if (!(["create_new", "open_existing", "add_to_existing"] as string[]).includes(action)) {
    return null;
  }
  if (!reason || !request) return null;
  if (action === "create_new" && workspaceId) return null;
  if (action !== "create_new" && !workspaceId) return null;
  return {
    action,
    reason,
    request,
    ...(workspaceId ? { workspace_id: workspaceId } : {}),
    ...(workspaceName ? { workspace_name: workspaceName } : {}),
  };
}

export interface ChatMessage {
  id?: string;
  conversation_id?: string;
  role: "user" | "assistant";
  content: string;
  tool_calls?: ToolCall[];
  assistant_blocks?: AssistantBlock[];
  sub_agent_events?: SubAgentEvent[];
  hitl_requests?: HITLRequest[];
  workflow_result?: WorkflowResultReference;
  timestamp?: string;
  attachments?: {
    name: string;
    /** Local upload bytes for preview/retry, held only in the live session. */
    file?: File;
    document_id?: string;
    type?: string;
    fileType?: string;
    mimeType?: string;
    previewUrl?: string;
    openUrl?: string;
    fsPath?: string;
    /** type "site": a published website (lib/publishedSites), not a file. */
    url?: string;
    site_id?: string;
    revision?: number;
  }[];
  mentions?: {
    id: string;
    type: "agent" | "user";
    name: string;
    subtitle?: string;
    avatarUrl?: string | null;
    avatarSeed?: string;
  }[];
  manualSkills?: { id: string; name: string; slug?: string }[];
  chatMode?: string;
  chatModePayload?: Record<string, unknown> | string;
  retryRequest?: {
    message: string;
    conversationId?: string;
    documentIds?: string[];
    agentId?: string;
    workspaceId?: string;
    manualSkillIds?: string[];
    manualSkillRefs?: ManualSkillReference[];
    chatMode?: string;
    chatModePayload?: Record<string, unknown>;
    responseSurfaceSubmission?: ResponseSurfaceSubmissionReceipt;
  };
  stream_error?: boolean;
  stop_reason?: string;
  limit_detail?: PlanLimitDetail;
  message_kind?: string | null;
  refs?: ChatMessageRef[] | null;
  meta?: Record<string, unknown> | null;
  pending_action?: ChatMessagePendingAction | null;
  resolved_at?: string | null;
  resolution?: Record<string, unknown> | null;
  updated_at?: string | null;
}

export function hasActivePersistedChatStream(
  messages: Pick<ChatMessage, "role" | "meta" | "stream_error" | "stop_reason">[],
): boolean {
  // Background artifact receipts may follow the running assistant row. Only
  // a newer user turn can fence off an older turn's stale stream marker.
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "user") return false;
    if (message.role !== "assistant") continue;
    // Terminal evidence wins over a checkpoint's stale running marker, also
    // after history/reopen. Ordinary error prose is not lifecycle evidence.
    if (message.stream_error || message.meta?.stream_error === true
      || message.meta?.stream_interrupted === true
      || ["error", "credit_exhausted"].includes(String(message.stop_reason || message.meta?.stop_reason || ""))) {
      return false;
    }
    const status = message.meta?.stream_status;
    if (status === "running" || status === "streaming") return true;
  }
  return false;
}

export function isChatRunActive(
  messages: ChatMessage[],
  transportStreaming: boolean,
): boolean {
  return transportStreaming || hasActivePersistedChatStream(messages);
}

export interface ChatStreamSnapshot {
  conversation_id?: string;
  message_id?: string;
  seq?: number;
  status?: string;
  content?: string;
  tool_calls?: unknown;
  assistant_blocks?: unknown;
}

export function isTerminalStreamSnapshot(snapshot: ChatStreamSnapshot): boolean {
  return snapshot.status !== "streaming";
}

function streamSnapshotTargetIndex(
  messages: ChatMessage[],
  snapshot: ChatStreamSnapshot,
): number {
  if (snapshot.message_id) {
    const byId = messages.findIndex((message) => message.id === snapshot.message_id);
    if (byId >= 0) return byId;
  }
  const tailIndex = messages.length - 1;
  const tail = messages[tailIndex];
  return tail && tail.role === "assistant" && !tail.id ? tailIndex : -1;
}

export function streamSnapshotNeedsHistory(
  messages: ChatMessage[],
  snapshot: ChatStreamSnapshot,
): boolean {
  return streamSnapshotTargetIndex(messages, snapshot) < 0;
}

export function mergeChatStreamSnapshot(
  messages: ChatMessage[],
  snapshot: ChatStreamSnapshot,
): ChatMessage[] {
  const content = typeof snapshot.content === "string" ? snapshot.content : "";
  const toolCalls = parseToolCalls(snapshot.tool_calls);
  const blocks = Array.isArray(snapshot.assistant_blocks)
    ? (snapshot.assistant_blocks as AssistantBlock[])
    : undefined;
  const targetIndex = streamSnapshotTargetIndex(messages, snapshot);
  const previous = targetIndex >= 0 ? messages[targetIndex] : undefined;
  const merged: ChatMessage = {
    ...(previous || { role: "assistant" as const, content: "" }),
    role: "assistant",
    id: snapshot.message_id || previous?.id,
    content,
    tool_calls: toolCalls || previous?.tool_calls,
    assistant_blocks: blocks || previous?.assistant_blocks,
    meta: {
      ...(previous?.meta || {}),
      stream_status: isTerminalStreamSnapshot(snapshot) ? undefined : "streaming",
    },
  };

  if (targetIndex < 0) return [...messages, merged];
  const updated = [...messages];
  updated[targetIndex] = merged;
  return updated;
}

export interface RuntimeQueueState {
  ticket: string;
  position?: number;
  etaSeconds?: number;
  pollAfterSeconds: number;
  deadlineAt?: string;
}

export interface RuntimeHumanInputField extends RequestInputField {
  label: string;
  type: string;
  required: boolean;
}

/** The `request_input` question a durable run is parked on. */
export interface RuntimeHumanInputState {
  requestId: string;
  status: string;
  question: string;
  options?: string[];
  /** The asker's recommended answer (one of `options`) and why. */
  recommendation?: Recommendation;
  fields?: RuntimeHumanInputField[];
  context?: string;
  /** Files the agent asks the person to judge (`request_input` file_review), resolved to Documents. */
  reviewFiles?: Array<{ document_id: string; name?: string; fs_path?: string }>;
  /** Where in Manor the person acts before answering (an in-app path the runtime set, e.g. a screen hand-over). */
  actionLink?: string;
  actionToTake?: string;
  deadlineAt?: string;
  answered?: boolean;
}

export interface RuntimeStreamState {
  runId: string;
  status: string;
  /** Why the run waits, e.g. `capability_approval` (parked on an approval). */
  statusReason?: string;
  queue?: RuntimeQueueState;
  humanInput?: RuntimeHumanInputState;
  pollAfterSeconds: number;
  lastEventId?: string;
}

export function runtimeHumanInputFromPayload(raw: unknown): RuntimeHumanInputState | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  const requestId = String(value.request_id || "");
  const question = String(value.question || "").trim();
  if (!requestId || !question) return undefined;
  const options = Array.isArray(value.options)
    ? value.options.map((item) => String(item)).filter(Boolean)
    : undefined;
  // Each field carries the schema its control is drawn from (the same set as a Workflow form).
  const fields = Array.isArray(value.fields)
    ? value.fields
        .map(requestInputFieldFrom)
        .filter((item): item is RuntimeHumanInputField => item !== null)
    : undefined;
  return {
    requestId,
    status: String(value.status || "pending"),
    question,
    options: options && options.length > 0 ? options : undefined,
    recommendation: recommendationOf(value as RecommendedQuestion) || undefined,
    fields: fields && fields.length > 0 ? fields : undefined,
    context: value.context ? String(value.context) : undefined,
    reviewFiles: Array.isArray(value.review_files)
      ? value.review_files.filter((item): item is { document_id: string; name?: string; fs_path?: string } =>
        Boolean(item) && typeof item === "object" && typeof (item as { document_id?: unknown }).document_id === "string")
      : undefined,
    actionLink: inAppLink(value.action_link),
    actionToTake: value.action_to_take ? String(value.action_to_take) : undefined,
    deadlineAt: value.deadline_at ? String(value.deadline_at) : undefined,
    answered: Boolean(value.answer) || String(value.status || "") !== "pending",
  };
}

/** Only a path inside Manor is followed from a question card. */
function inAppLink(raw: unknown): string | undefined {
  const link = typeof raw === "string" ? raw.trim() : "";
  return link.startsWith("/") && !link.startsWith("//") && !/[\\\s]/.test(link) ? link : undefined;
}

export function formatRuntimeQueueStatus(queue: RuntimeQueueState): string {
  const parts = [t("component.chat_runtime_queue.waiting")];
  if (Number.isFinite(queue.position) && Number(queue.position) > 0) {
    parts.push(
      t("component.chat_runtime_queue.position", {
        position: Math.floor(Number(queue.position)),
      }),
    );
  }
  if (Number.isFinite(queue.etaSeconds) && Number(queue.etaSeconds) > 0) {
    parts.push(
      t("component.chat_runtime_queue.eta", {
        minutes: Math.max(1, Math.ceil(Number(queue.etaSeconds) / 60)),
      }),
    );
  }
  return parts.join(" · ");
}

export function mergeSubAgentEvents(
  existing: SubAgentEvent[],
  rawEvent: Record<string, any>,
): SubAgentEvent[] {
  const incoming: SubAgentEvent = {
    ...rawEvent,
    agent_name: String(rawEvent.agent_name || rawEvent.name || "Sub-Agent"),
    content: String(rawEvent.content || ""),
  };
  const runId = String(incoming.run_id || "").trim();
  if (!runId) return [...existing, incoming];

  const index = existing.findIndex((event) => event.run_id === runId);
  const current: SubAgentEvent =
    index >= 0
      ? { ...existing[index] }
      : {
          run_id: runId,
          agent_name: incoming.agent_name,
          content: "",
          tools: [],
        };
  const tools = [...(current.tools || [])];
  if (incoming.tool?.name) {
    const toolIndex = tools.findIndex(
      (tool) =>
        incoming.tool?.seq != null &&
        tool.seq === incoming.tool.seq,
    );
    if (toolIndex >= 0) {
      tools[toolIndex] = { ...tools[toolIndex], ...incoming.tool };
    } else {
      tools.push(incoming.tool);
    }
  }

  const merged: SubAgentEvent = {
    ...current,
    ...incoming,
    tools: tools.slice(-20),
  };
  delete merged.tool;
  const next = [...existing];
  if (index >= 0) next[index] = merged;
  else next.push(merged);
  return next;
}

/* ── Helpers ── */

export type SetMessages = React.Dispatch<React.SetStateAction<ChatMessage[]>>;
export type SetConvId = React.Dispatch<React.SetStateAction<string | undefined>>;

export interface SSEHandlers {
  setMessages: SetMessages;
  setCurrentConvId: SetConvId;
  onRuntimeState?: (state: RuntimeStreamState) => void;
}

export interface StreamProcessResult {
  error?: {
    message: string;
    persisted: boolean;
    messageId?: string;
  };
  messageId?: string;
  persisted?: boolean;
  streamEnded?: boolean;
  stopReason?: string;
  limitDetail?: PlanLimitDetail;
  runtimeRunId?: string;
  runtimeStatus?: string;
  runtimeQueue?: RuntimeQueueState;
  runtimeHumanInput?: RuntimeHumanInputState;
  pollAfterSeconds?: number;
  lastEventId?: string;
}

const INTERNAL_FILE_PERMISSION_RE = /^\[File permission(?:\s+[^\]]*)?\]$/i;
const APPROVAL_RESOLUTION_RECEIPT_RE =
  /^[✓✗]\s*(?:Approved|Rejected|Cancelled|Resolved)(?:\s+—[\s\S]*)?$/i;

export function isInternalFilePermissionMessage(content: unknown): boolean {
  return typeof content === "string" && INTERNAL_FILE_PERMISSION_RE.test(content.trim());
}

/**
 * Historical personal-Chat approvals may contain both a resolved HITL card
 * and the Workspace Chat service's linked system receipt. The card is the
 * durable audit surface; rendering the receipt as another assistant bubble
 * repeats the same status without adding information.
 */
export function isRedundantApprovalResolutionReceipt(message: {
  role?: unknown;
  content?: unknown;
  message_kind?: unknown;
  refs?: unknown;
}): boolean {
  if (message.role !== "system" || message.message_kind !== "system") return false;
  if (
    typeof message.content !== "string" ||
    !APPROVAL_RESOLUTION_RECEIPT_RE.test(message.content.trim())
  ) {
    return false;
  }
  return (
    Array.isArray(message.refs) &&
    message.refs.some(
      (ref) =>
        ref != null &&
        typeof ref === "object" &&
        (ref as Record<string, unknown>).type === "message" &&
        Boolean((ref as Record<string, unknown>).id),
    )
  );
}

export function pendingHITLIds(messages: ChatMessage[]): string[] {
  return messages.flatMap((msg) =>
    (msg.hitl_requests || [])
      .filter((hitl) => !hitl.resolved)
      .map((hitl) => hitl.id)
      .filter(Boolean),
  );
}

export function mergeResolvedWorkflowMessage(
  messages: ChatMessage[],
  messageId: string,
  resolvedValue: unknown,
): ChatMessage[] {
  const resolved = resolvedValue && typeof resolvedValue === "object"
    ? resolvedValue as Partial<ChatMessage>
    : {};
  return messages.map((message) => (
    message.id === messageId
      ? {
          ...message,
          message_kind: resolved.message_kind ?? message.message_kind,
          refs: resolved.refs ?? message.refs,
          meta: resolved.meta ?? message.meta,
          pending_action: Object.prototype.hasOwnProperty.call(
            resolved,
            "pending_action",
          )
            ? resolved.pending_action
            : message.pending_action,
          resolved_at: resolved.resolved_at || new Date().toISOString(),
          resolution: resolved.resolution ?? message.resolution,
          updated_at: resolved.updated_at ?? message.updated_at,
        }
      : message
  ));
}

export async function resolveGlobalWorkflowMessageAction(
  messageId: string,
  choice: string,
  note?: string,
  payload?: Record<string, unknown>,
  files?: File[],
) {
  const uploadedAttachments = files?.length
    ? await Promise.all(files.map(async (file) => {
        const document = await api.documents.upload(file);
        if (!document?.id) throw new Error(`Failed to upload ${file.name}`);
        return { name: file.name, document_id: document.id, type: "knowledge" };
      }))
    : [];
  const existingAttachments = Array.isArray(payload?.attachments)
    ? payload.attachments
    : [];
  const resolvedPayload = uploadedAttachments.length > 0
    ? {
        ...(payload || {}),
        attachments: [...existingAttachments, ...uploadedAttachments],
      }
    : payload;
  return api.chat.resolveAction(messageId, choice, note, resolvedPayload);
}

export function hitlActionTranscriptText(action: string): string {
  const normalized = String(action || "").trim().toLowerCase();
  if (normalized === "approve" || normalized === "always_approve") {
    return "Approved the requested action.";
  }
  if (normalized === "reject") {
    return "Rejected the requested action.";
  }
  if (normalized === "revise") {
    return "Requested revisions to the Workflow output.";
  }
  if (normalized === "cancel") {
    return "Cancelled the Workflow action.";
  }
  return "Responded to the approval request.";
}

export function normalizeToolResult(result: unknown): string | undefined {
  if (result == null) return undefined;
  if (typeof result === "string") return result;
  if (typeof result === "number" || typeof result === "boolean") return String(result);
  try {
    const serialized = JSON.stringify(result);
    return serialized === undefined ? String(result) : serialized;
  } catch {
    return String(result);
  }
}

const INTERNAL_TOOL_CONTRACT_ERROR_RE =
  /(?:\b(?:unknown|unrecognized|unsupported|unregistered)\s+(?:runtime\s+)?(?:tool|function)(?:\s+(?:name|key))?\b|\b(?:tool|function)(?:\s+(?:name|key|id))?\b[\s\S]{0,100}?(?:not\s+(?:found|registered)|unregistered|mismatch|misalign(?:ed|ment)?|does\s+not\s+match|missing\s+(?:handler|executor))\b|\b(?:no|missing)\s+(?:registered\s+)?(?:handler|executor)\s+for\s+(?:tool|function)\b|\b(?:no|missing)\s+(?:(?:matching|registered)\s+)?(?:handler|executor)\s+for\b|\b(?:tool|function)\s+(?:name|key|id)\b[\s\S]{0,100}?(?:failed?|failure|error)\b|\bmcp__[a-z0-9_.-]+(?:__[a-z0-9_.-]+)+\b[\s\S]{0,100}?(?:failed?|failure|unavailable|error)\b)/i;
const TOOL_ERROR_PREFIX_RE =
  /\btool\s+error\s*\([^\)\r\n]{1,160}\)\s*:\s*/i;
const INTERNAL_ERROR_DETAIL_RE =
  /(?:\bsqlalchemy\b|\basyncpg\b|\bpsycopg\b|InFailedSQLTransactionError|current\s+transaction\s+is\s+aborted|Traceback\s+\(most\s+recent\s+call\s+last\)|\[SQL:|\[parameters?:)/i;
const INTERNAL_ERROR_FAILURE_SIGNAL_RE =
  /\b(?:error|failed|failure|exception|aborted)\b|Traceback\s+\(most\s+recent\s+call\s+last\)|\[SQL:|\[parameters?:/i;
const PUBLIC_ACTIONABLE_EXECUTOR_ERROR_RE =
  /^(?:(?:permission denied|authentication required|authorization required)\.?(?:\s+(?:reconnect|connect|sign in to|log in to) your [a-z0-9][a-z0-9 ._-]{0,79} account\.?)?|(?:rate limit|quota|usage limit) (?:exceeded|reached)\.?(?:\s+(?:try again later|upgrade your plan(?: or try again later)?)\.?)?|credits? exhausted\.?(?:\s+(?:add credits|upgrade your plan)(?: or try again later)?\.?)?)$/i;
const PUBLIC_TOOL_FAILURE_MESSAGE =
  "This operation is temporarily unavailable. Please try again.";
const PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE =
  "The Chrome browser task already finished. Start a new Chrome task before doing more browser work.";
const PUBLIC_TOOL_BLOCKED_MESSAGE = "This operation was blocked.";
const INTERNAL_TOOL_IDENTIFIER_RE =
  /\bmcp__[a-z0-9_.-]+(?:__[a-z0-9_.-]+)+\b/i;
const PUBLIC_REDACTED_TOOL_NAME = "operation";
const REDACTED_SENSITIVE_VALUE = "<redacted>";
export const LLM_CONFIGURATION_ERROR_MESSAGE =
  "AI is not configured for this chat. Ask your administrator to configure an AI provider, then try again.";
// A failure on the user's own (BYOK) model route. The backend emits exactly
// this sentence (packages/core/ai/runtime/output_policy.py) followed at most
// by a structured "HTTP <status> from <host> (model <id>)" detail. Any other
// text after the sentence is dropped here too, whichever layer sees it first.
export const BYOK_PROVIDER_ERROR_MESSAGE =
  "Your BYOK model provider returned an error. Check the model, API key, and base URL in your AI settings and the provider's status, then try again.";
// The same route failed transiently (no response, dropped stream, 429/5xx)
// through the backend's automatic retries: an outage, not a settings problem.
// Its detail adds "after <n> attempts over <s>s".
export const BYOK_PROVIDER_UNAVAILABLE_MESSAGE =
  "Your BYOK model provider stopped responding. This looks like a temporary outage at the provider, not a problem with your AI settings. Try again in a few minutes.";
const BYOK_PROVIDER_ERROR_CODE = "byok_provider_error";
const FILE_PERMISSION_ERROR_MESSAGES = {
  "You don't have permission to edit this file. Ask the file owner for edit access.": "lib.chat_stream.file_edit_denied",
  "You don't have permission to access this file. Ask the file owner for access.": "lib.chat_stream.file_access_denied",
} as const;

export function formatFilePermissionErrorMessage(message: unknown): string | null {
  const text = String(message ?? "").trim();
  for (const [english, key] of Object.entries(FILE_PERMISSION_ERROR_MESSAGES)) {
    const localized = t(key);
    if (text === english || text === localized) return localized;
  }
  return null;
}

const PUBLIC_BYOK_DETAIL_RE =
  /^(?:HTTP [1-5]\d{2}|no response) from [A-Za-z0-9.-]{1,253}(?::\d{1,5})? \(model [A-Za-z0-9._:/-]{1,120}\)(?: after \d{1,3} attempts? over \d{1,6}s)?$/;

function localizedByokProviderError(detail?: string, transient = false): string {
  const template = t(transient
    ? "lib.chat_stream.byok_provider_unavailable_with_detail"
    : "lib.chat_stream.byok_provider_error_with_detail");
  return detail ? template.replace("{detail}", detail) : template.split("\n\n", 1)[0];
}

export function formatByokProviderErrorMessage(message: unknown): string | null {
  const text = String(message ?? "").trim();
  if (text === BYOK_PROVIDER_ERROR_CODE || text === BYOK_PROVIDER_ERROR_MESSAGE) {
    return localizedByokProviderError();
  }
  if (text === BYOK_PROVIDER_UNAVAILABLE_MESSAGE) {
    return localizedByokProviderError(undefined, true);
  }
  for (const [sentence, transient] of [
    [BYOK_PROVIDER_ERROR_MESSAGE, false],
    [BYOK_PROVIDER_UNAVAILABLE_MESSAGE, true],
  ] as const) {
    const prefix = `${sentence}\n\nError detail: `;
    if (!text.startsWith(prefix)) continue;
    const detail = text.slice(prefix.length).trim();
    return localizedByokProviderError(PUBLIC_BYOK_DETAIL_RE.test(detail) ? detail : undefined, transient);
  }
  return null;
}

function redactSensitiveText(value: string): string {
  return value
    .replace(
      /(proxy[-_]authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?([^\s,;]+)/gi,
      `$1${REDACTED_SENSITIVE_VALUE}`,
    )
    .replace(
      /(authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,;]+)/gi,
      `$1${REDACTED_SENSITIVE_VALUE}`,
    )
    .replace(
      /(x[-_]?api[-_]?key\s*[:=]\s*)([^\s,;]+)/gi,
      `$1${REDACTED_SENSITIVE_VALUE}`,
    )
    .replace(
      /([?&](?:api_key|access_token|refresh_token|token|secret)=)([^&#\s]+)/gi,
      `$1${REDACTED_SENSITIVE_VALUE}`,
    )
    .replace(
      /\b(?:sk-(?:or|ant|proj|live|test)?-?[A-Za-z0-9._-]{8,}|ark-[A-Za-z0-9._-]{8,})\b/g,
      REDACTED_SENSITIVE_VALUE,
    );
}

function isPublicActionableToolExecutorFailure(detail: string): boolean {
  return PUBLIC_ACTIONABLE_EXECUTOR_ERROR_RE.test(detail.trim())
    || detail.trim() === LLM_CONFIGURATION_ERROR_MESSAGE;
}

function localizedRequestFailure() {
  return t("lib.chat_stream.request_failed_with_detail").split("\n\n", 1)[0];
}

/** Localized text for a run the server ended outside its agent loop. */
export function runtimeTerminalFailureText(reason: unknown): string | undefined {
  const code = typeof reason === "string" ? reason : "";
  if (code === "sandbox_queue_timeout") return t("component.chat_runtime_queue.timed_out");
  if (code.startsWith("sandbox_")) return t("component.chat_runtime_queue.start_failed");
  return undefined;
}

function publicBlockedToolResult(text: string): string | undefined {
  const trimmed = text.trim();
  if (
    trimmed === PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE
    || trimmed === PUBLIC_TOOL_BLOCKED_MESSAGE
  ) {
    return trimmed;
  }
  if (!trimmed.startsWith("{")) return undefined;
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
    if (String(parsed.status || "").trim().toLowerCase() !== "blocked") return undefined;
    const reason = String(parsed.reason || parsed.error || "").trim();
    if (reason === "chrome_tabs_already_finalized") {
      return PUBLIC_CHROME_TABS_ALREADY_FINALIZED_MESSAGE;
    }
    const message = String(parsed.message || "").trim();
    if (
      message
      && !INTERNAL_TOOL_IDENTIFIER_RE.test(message)
      && !INTERNAL_TOOL_CONTRACT_ERROR_RE.test(message)
    ) {
      return message;
    }
    return PUBLIC_TOOL_BLOCKED_MESSAGE;
  } catch {
    return undefined;
  }
}

export function formatPublicToolResult(result: unknown): string | undefined {
  const normalized = normalizeToolResult(result);
  const text = normalized ? redactSensitiveText(normalized) : normalized;
  if (!text) return text;
  if (text.trim() === PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE) return PUBLIC_WEBSITE_ACCESS_DENIED_MESSAGE;
  const blockedPublic = publicBlockedToolResult(text);
  if (blockedPublic) return blockedPublic;
  const hadExecutorPrefix = TOOL_ERROR_PREFIX_RE.test(text);
  const publicText = text.replace(TOOL_ERROR_PREFIX_RE, "").trim();
  if (hadExecutorPrefix) {
    return isPublicActionableToolExecutorFailure(publicText)
      ? publicText
      : localizedRequestFailure();
  }
  if (
    text !== PUBLIC_TOOL_FAILURE_MESSAGE
    && !INTERNAL_TOOL_CONTRACT_ERROR_RE.test(publicText)
    && !(
      INTERNAL_ERROR_DETAIL_RE.test(publicText)
      && INTERNAL_ERROR_FAILURE_SIGNAL_RE.test(publicText)
    )
  ) return text;
  return localizedRequestFailure();
}

function normalizeMessageAttachments(value: unknown): ChatMessage["attachments"] | undefined {
  if (!Array.isArray(value)) return undefined;
  const attachments = value
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object" && !Array.isArray(item))
    .map((item) => {
      if (isSiteAttachment(item)) {
        return {
          name: String(item.name || ""),
          type: "site",
          url: typeof item.url === "string" ? item.url : undefined,
          site_id: typeof item.site_id === "string" ? item.site_id : undefined,
          revision: typeof item.revision === "number" ? item.revision : undefined,
        };
      }
      // Persisted rows carry the snake_case FileRef; older rows camelCase.
      const ref = normalizeFileRef(item);
      return {
        name: ref.name,
        document_id: ref.document_id,
        type: item.type == null ? undefined : String(item.type),
        fileType: ref.file_type,
        mimeType: ref.mime,
        previewUrl: ref.preview_url,
        openUrl: ref.open_url,
        fsPath: ref.fs_path,
      };
    })
    .filter((item) => item.name);
  return attachments.length ? attachments : undefined;
}

function normalizeToolArguments(args: unknown): string | undefined {
  if (args == null) return undefined;
  if (typeof args === "string") return args;
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

export function inferToolStatus(result: unknown): ToolCall["status"] {
  const text = normalizeToolResult(result);
  if (!text) return "success";
  if (text.startsWith("Tool error")) return "error";
  try {
    const parsed = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      const status = String((parsed as any).status || "").toLowerCase();
      if (
        status === "error" ||
        status === "failed" ||
        status === "timeout" ||
        (parsed as any).error
      ) {
        return "error";
      }
    }
  } catch {
    // Non-JSON results are ordinary successful tool output unless they
    // use the explicit Tool error prefix handled above.
  }
  return "success";
}

export function settlePendingAssistantToolCalls(
  messages: ChatMessage[],
  status: ToolCall["status"],
): ChatMessage[] {
  const updated = [...messages];
  const last = updated[updated.length - 1];
  if (!last || last.role !== "assistant" || !last.tool_calls?.length) {
    return messages;
  }
  let changed = false;
  const tool_calls = last.tool_calls.map((tool) => {
    const toolStatus = tool.status || (tool.result ? "success" : "pending");
    if (toolStatus !== "pending") return tool;
    changed = true;
    return {
      ...tool,
      status,
      activeChild: undefined,
    };
  });
  if (!changed) return messages;
  updated[updated.length - 1] = { ...last, tool_calls };
  return updated;
}

export function markAssistantProcessBlocksSummarizing(blocks: AssistantBlock[] | undefined): AssistantBlock[] | undefined {
  if (!Array.isArray(blocks) || blocks.length === 0) return blocks;
  let changed = false;
  const next = blocks.map((block) => {
    if (block.type !== "process") return block;
    const steps = (block.steps || []).map((step) => {
      const status = step.status === "running" || step.status === "pending" ? "success" : step.status;
      if (status !== step.status) changed = true;
      return status === step.status ? step : { ...step, status };
    });
    if (
      block.status === "completed" &&
      block.default_collapsed === true &&
      steps === block.steps
    ) {
      return block;
    }
    changed = true;
    return {
      ...block,
      status: "completed",
      default_collapsed: true,
      steps,
    };
  });
  return changed ? next : blocks;
}

export function markAssistantProcessBlocksStopped(blocks: AssistantBlock[] | undefined): AssistantBlock[] | undefined {
  if (!Array.isArray(blocks) || blocks.length === 0) return blocks;
  let changed = false;
  const next = blocks.map((block) => {
    if (block.type !== "process") return block;
    if (block.lifecycle_status && !["running", "waiting"].includes(block.lifecycle_status)) return block;
    const steps = (block.steps || []).map((step) => {
      const status = step.status === "running" || step.status === "pending" ? "error" : step.status;
      if (status !== step.status) changed = true;
      return status === step.status ? step : { ...step, status };
    });
    if (
      block.status === "error" &&
      block.default_collapsed === true &&
      steps === block.steps
    ) {
      return block;
    }
    changed = true;
    return {
      ...block,
      status: "error",
      lifecycle_status: "cancelled" as const,
      ended_at_ms: block.ended_at_ms ?? Date.now(),
      default_collapsed: true,
      steps,
    };
  });
  return changed ? next : blocks;
}

export function markAssistantProcessBlocksFailed(
  blocks: AssistantBlock[] | undefined, content: string,
): AssistantBlock[] | undefined {
  const processes = blocks?.filter((block): block is AssistantProcessBlock => block.type === "process");
  if (!processes?.length) return undefined;
  const now = Date.now();
  return [
    ...processes.map((block) => ({
      ...block,
      lifecycle_status: "failed" as const,
      status: "error",
      ended_at_ms: block.ended_at_ms ?? now,
      duration_ms: block.duration_ms ?? (block.started_at_ms == null ? undefined : Math.max(0, now - block.started_at_ms)),
      steps: (block.steps || []).map((step) => ["running", "pending"].includes(step.status || "")
        ? { ...step, status: "error" } : step),
    })),
    { id: "blk_failure_summary", type: "text", phase: "final", text: content },
  ];
}

export function settlePendingAssistantProcess(messages: ChatMessage[]): ChatMessage[] {
  const withSettledTools = settlePendingAssistantToolCalls(messages, "error");
  const updated = [...withSettledTools];
  const lastIndex = updated.length - 1;
  const last = updated[lastIndex];
  if (!last || last.role !== "assistant") {
    return withSettledTools;
  }
  let nextLast = last;
  const assistant_blocks = markAssistantProcessBlocksStopped(last.assistant_blocks);
  if (assistant_blocks !== last.assistant_blocks) {
    nextLast = { ...nextLast, assistant_blocks };
  }
  const streamStatus = String(last.meta?.stream_status || "").toLowerCase();
  if (streamStatus === "running" || streamStatus === "streaming") {
    nextLast = {
      ...nextLast,
      meta: {
        ...(nextLast.meta || {}),
        stream_status: "interrupted",
        stream_interrupted: true,
      },
    };
  }
  if (nextLast === last) return withSettledTools;
  updated[lastIndex] = nextLast;
  return updated;
}

export function formatPersistedStreamErrorMessage(message: unknown): string {
  const detail = redactSensitiveText(
    normalizeToolResult(message)?.trim() || t("lib.chat_stream.unknown_error"),
  );
  const byokMessage = formatByokProviderErrorMessage(detail);
  if (byokMessage) return byokMessage;
  const permissionMessage = formatFilePermissionErrorMessage(detail);
  if (permissionMessage) return permissionMessage;
  const publicDetail = formatPublicToolResult(detail) || detail;
  if (
    /(?:sqlalchemy|asyncpg|psycopg|InFailedSQLTransactionError|current\s+transaction\s+is\s+aborted|Traceback\s+\(most\s+recent\s+call\s+last\)|\[SQL:|\[parameters?:)/i
      .test(detail)
    || publicDetail === localizedRequestFailure()
    || /^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$/.test(publicDetail)
  ) {
    return localizedRequestFailure();
  }
  if (publicDetail === "Sorry, the request failed. Please try again.") {
    return localizedRequestFailure();
  }
  const failurePrefixes = [
    `${localizedRequestFailure()}\n\nError detail: `,
    "Sorry, the request failed. Please try again.\n\nError detail: ",
  ];
  const matchedPrefix = failurePrefixes.find((prefix) =>
    publicDetail.startsWith(prefix),
  );
  const candidateDetail = matchedPrefix
    ? publicDetail.slice(matchedPrefix.length).trim()
    : publicDetail;
  if (!isPublicActionableToolExecutorFailure(candidateDetail)) {
    return localizedRequestFailure();
  }
  return t("lib.chat_stream.request_failed_with_detail").replace("{detail}", candidateDetail);
}

function normalizePlanLimitDetail(detail: unknown, fallback: string): PlanLimitDetail {
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    return {
      message: String(d.message || fallback),
      limit: typeof d.limit === "number" ? d.limit : null,
      current: typeof d.current === "number" ? d.current : null,
      plan: String(d.plan || "current"),
      // "trial_credit" (a provider-paid trial ran dry) must reach the notice.
      ...(typeof d.kind === "string" && d.kind ? { kind: d.kind as PlanLimitKind } : {}),
    };
  }
  return {
    message: typeof detail === "string" && detail ? detail : fallback,
    limit: null,
    current: null,
    plan: "current",
  };
}

function formatCreditLimitMessage(detail: PlanLimitDetail): string {
  return detail.message || t("component.upgrade_prompt.default_message");
}

export { TYPEWRITER_TICK_MS, nextTypewriterSlice } from "./chatTypewriter";
const TOOL_START_DISPLAY_DELAY_MS = 180;

/**
 * Parse persisted tool_calls from DB message into ToolCall[].
 * Handles both array [{name, result}] and object {name: result} shapes.
 */
export function parseToolCalls(raw: any): ToolCall[] | undefined {
  if (!raw) return undefined;
  const persistedDuration = (tc: any): string | undefined => {
    if (typeof tc?.duration === "string") return tc.duration;
    if (typeof tc?.duration_ms === "number" && Number.isFinite(tc.duration_ms)) {
      return (tc.duration_ms / 1000).toFixed(2) + "s";
    }
    return undefined;
  };
  const WORKSPACE_DRAFT_PUBLIC_KEYS = [
    "artifact_kind",
    "draft_id",
    "status",
    "ready",
    "missing",
    "fields",
    "assistant_reply",
    "title",
    "next_step",
    // The architect's question: answer buttons under the chat reply.
    "question",
  ] as const;
  const workspaceDraftPublicResult = (tc: any): string | undefined => {
    const toolName = String(tc?.name || "").toLowerCase();
    const canCreateDraftArtifact = [
      "manor",
      "start_workspace_draft",
      "continue_workspace_draft",
    ].includes(toolName);
    const status = String(tc?.status || "").toLowerCase();
    if (
      !canCreateDraftArtifact
      || ["blocked", "error", "failed", "timeout"].includes(status)
    ) {
      return undefined;
    }
    const candidates = [tc?.raw_result, tc?.rawResult, tc?.result];
    for (const candidate of candidates) {
      let parsed = candidate;
      if (typeof candidate === "string") {
        try {
          parsed = JSON.parse(candidate);
        } catch {
          continue;
        }
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) continue;
      if (parsed.artifact_kind !== "workspace_draft") continue;
      if (typeof parsed.draft_id !== "string" || !parsed.draft_id.trim()) continue;
      const projected: Record<string, unknown> = {};
      for (const key of WORKSPACE_DRAFT_PUBLIC_KEYS) {
        if (Object.prototype.hasOwnProperty.call(parsed, key)) {
          projected[key] = parsed[key];
        }
      }
      return JSON.stringify(projected);
    }
    return undefined;
  };
  if (Array.isArray(raw)) {
    return raw.map((tc: any) => ({
      name: tc.name || "tool",
      arguments: normalizeToolArguments(tc.arguments ?? tc.args),
      result: formatPublicToolResult(tc.result),
      rawResult: workspaceDraftPublicResult(tc),
      status: tc.status || inferToolStatus(tc.result),
      ...(isWebsiteAccessDenied({ ...tc, status: tc.status || inferToolStatus(tc.result) })
        ? { reason: "website_access_denied", status_code: 403 } : {}),
      duration: persistedDuration(tc),
    }));
  }
  return Object.entries(raw).map(([name, result]) => ({
    name,
    result: formatPublicToolResult(result),
    status: inferToolStatus(result),
  }));
}

export function reconcileUserMessageReceipt(
  messages: ChatMessage[],
  messageId: unknown,
  conversationId?: string,
  clientMessageId?: unknown,
): ChatMessage[] {
  const id = typeof messageId === "string" ? messageId.trim() : "";
  if (!id) return messages;
  let index = messages.length - 1;
  while (index >= 0 && messages[index].role !== "user") index -= 1;
  if (index < 0) return messages;
  const message = messages[index];
  const pendingClientMessageId = message.meta?.client_message_id;
  if (pendingClientMessageId && pendingClientMessageId !== clientMessageId) return messages;
  if (message.conversation_id && conversationId
    && message.conversation_id !== conversationId) return messages;
  // Replayed frames cannot replace a different, already durable user turn.
  if (message.id && !message.id.startsWith("local-user-") && message.id !== id) return messages;
  return messages.flatMap((item, itemIndex) => {
    if (itemIndex === index) return [{ ...item, id, conversation_id: conversationId || item.conversation_id }];
    return item.role === "user" && item.id === id ? [] : [item];
  });
}

/**
 * Process an SSE stream from the chat endpoint, updating messages state.
 * Handles: text tokens, tool_call start/end, sub_agent events, HITL requests.
 * Pass an AbortSignal to allow cancellation mid-stream.
 */
export async function processSSEStream(
  response: Response,
  { setMessages, setCurrentConvId, onRuntimeState }: SSEHandlers,
  currentConvId: string | undefined,
  signal?: AbortSignal,
): Promise<StreamProcessResult> {
  // Response headers acknowledge the durable user row even when the stream
  // disconnects before its first model event. They never imply model success.
  const receiptConversationId = response.headers.get("X-Conversation-ID")?.trim() || currentConvId;
  const receiptMessageId = response.headers.get("X-User-Message-ID");
  const receiptClientMessageId = response.headers.get("X-Client-Message-ID");
  if (!currentConvId || !receiptConversationId || currentConvId === receiptConversationId) {
    setMessages((messages) => reconcileUserMessageReceipt(messages, receiptMessageId, receiptConversationId, receiptClientMessageId));
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No reader available");

  // Cancelling the reader tears down a pending read even when no new SSE frame arrives.
  const cancelReader = () => {
    void reader.cancel().catch(() => undefined);
  };
  signal?.addEventListener("abort", cancelReader, { once: true });

  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent = "";
  let currentEventId: string | undefined;
  let resetBeforeNextText = false;
  let summaryStarted = false;
  let streamCancelled = false;
  let streamPaused = false;
  let runtimeStatusReason: string | undefined;
  const result: StreamProcessResult = {};
  let streamConversationId = currentConvId;
  let streamMessageId: string | undefined;
  let queuedText = "";
  let typewriterTimer: ReturnType<typeof setTimeout> | undefined;
  let typewriterIdleResolve: (() => void) | undefined;
  let deferredFinalBlocks: AssistantBlock[] | undefined;
  type PendingToolStart = {
    tc: any;
    timer: ReturnType<typeof setTimeout>;
    startedAt: number;
    visible: boolean;
  };
  const pendingToolStarts: PendingToolStart[] = [];

  const appendVisibleText = (chunk: string, shouldReset = false) => {
    setMessages((prev) => {
      const updated = [...prev];
      let last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content: "",
          timestamp: new Date().toISOString(),
        });
        last = updated[updated.length - 1]!;
      }
      if (last.role === "assistant") {
        updated[updated.length - 1] = {
          ...last,
          content: shouldReset
            ? chunk
            : `${normalizeToolResult(last.content) || ""}${chunk}`,
        };
      }
      return updated;
    });
  };

  const notifyTypewriterIdle = () => {
    if (!queuedText && !typewriterTimer && deferredFinalBlocks) {
      const blocks = deferredFinalBlocks;
      deferredFinalBlocks = undefined;
      applyAssistantBlocks(blocks);
    }
    if (!queuedText && !typewriterTimer && typewriterIdleResolve) {
      const resolve = typewriterIdleResolve;
      typewriterIdleResolve = undefined;
      resolve();
    }
  };

  const scheduleTypewriter = () => {
    if (typewriterTimer || !queuedText) return;
    typewriterTimer = setTimeout(() => {
      typewriterTimer = undefined;
      const [chunk, rest] = nextTypewriterSlice(queuedText);
      queuedText = rest;
      if (chunk) appendVisibleText(chunk);
      if (queuedText) scheduleTypewriter();
      notifyTypewriterIdle();
    }, TYPEWRITER_TICK_MS);
  };

  const enqueueText = (token: string, shouldReset: boolean) => {
    if (shouldReset) {
      queuedText = "";
      if (typewriterTimer) {
        clearTimeout(typewriterTimer);
        typewriterTimer = undefined;
      }
      appendVisibleText("", true);
    }
    queuedText += token;
    scheduleTypewriter();
  };

  const waitForTypewriterIdle = async () => {
    if (!queuedText && !typewriterTimer) return;
    if (signal?.aborted) {
      flushQueuedTextNow();
      return;
    }
    // The response reader has closed, but stopping presentation must still
    // release the queue and retain all accepted text.
    signal?.addEventListener("abort", flushQueuedTextNow, { once: true });
    try {
      await new Promise<void>((resolve) => {
        typewriterIdleResolve = resolve;
        scheduleTypewriter();
      });
    } finally {
      signal?.removeEventListener("abort", flushQueuedTextNow);
    }
  };

  const isRedactedToolFailure = (tc: any) =>
    tc?.status === "error" && tc?.name === PUBLIC_REDACTED_TOOL_NAME;

  const findPendingToolIndex = <T,>(
    items: T[],
    tc: any,
    toolCallFor: (item: T) => any,
    isPending: (item: T) => boolean,
  ): number => {
    const incomingArguments = normalizeToolArguments(tc?.arguments);
    const lastMatchingIndex = (matches: (toolCall: any) => boolean) => {
      for (let index = items.length - 1; index >= 0; index--) {
        if (isPending(items[index]) && matches(toolCallFor(items[index]))) return index;
      }
      return -1;
    };

    let index = incomingArguments
      ? lastMatchingIndex((candidate) => (
          candidate?.name === tc?.name
          && normalizeToolArguments(candidate?.arguments) === incomingArguments
        ))
      : -1;
    if (index < 0) {
      index = lastMatchingIndex((candidate) => candidate?.name === tc?.name);
    }
    if (index >= 0 || !isRedactedToolFailure(tc)) return index;
    if (incomingArguments) {
      index = lastMatchingIndex((candidate) => (
        normalizeToolArguments(candidate?.arguments) === incomingArguments
      ));
    }
    return index >= 0 ? index : lastMatchingIndex(() => true);
  };

  const findPendingToolStart = (tc: any) => {
    const index = findPendingToolIndex(
      pendingToolStarts,
      tc,
      (entry) => entry.tc,
      () => true,
    );
    return index >= 0 ? pendingToolStarts[index] : undefined;
  };

  const removePendingToolStart = (entry: PendingToolStart) => {
    const idx = pendingToolStarts.indexOf(entry);
    if (idx >= 0) pendingToolStarts.splice(idx, 1);
  };

  const applyToolCall = (
    tc: any,
    statusOverride?: ToolCall["status"],
    startedAtOverride?: number,
  ) => {
    const rawResultText = normalizeToolResult(tc.result);
    const status =
      statusOverride || tc.status || (rawResultText ? "success" : "pending");
    const resultText = formatPublicToolResult(tc.result);
    const websiteDenial = isWebsiteAccessDenied({ ...tc, status })
      ? { reason: "website_access_denied", status_code: 403 } : { reason: undefined, status_code: undefined };
    const incomingArguments = normalizeToolArguments(tc.arguments);
    setMessages((prev) => {
      const updated = [...prev];
      let last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content: "",
          tool_calls: [],
          timestamp: new Date().toISOString(),
        });
        last = updated[updated.length - 1]!;
      }
      if (last.role === "assistant") {
        const existing = [...(last.tool_calls || [])];

        // Find a still-pending wrapper (invoke_skill / manor / code)
        // anywhere in the list — if present, sub-tool events that
        // arrive should update its ``activeChild`` so the user sees
        // "invoke_skill → bash" instead of a stuck spinner.
        let pendingWrapperIdx = -1;
        for (let k = existing.length - 1; k >= 0; k--) {
          const t = existing[k];
          if (t.status === "pending" && WRAPPER_TOOLS.has(t.name)) {
            pendingWrapperIdx = k;
            break;
          }
        }
        const isChildOfWrapper =
          pendingWrapperIdx >= 0 && !WRAPPER_TOOLS.has(tc.name);

        if (status === "success" || status === "error" || status === "unverified") {
          let idx = findPendingToolIndex(
            existing,
            tc,
            (item) => item,
            (item) => item.status === "pending",
          );
          if (idx < 0 && WRAPPER_TOOLS.has(tc.name)) {
            for (let k = existing.length - 1; k >= 0; k--) {
              const candidate = existing[k];
              if (
                candidate.name === tc.name &&
                candidate.status === "success" &&
                (!incomingArguments || candidate.arguments === incomingArguments) &&
                !!normalizeToolResult(candidate.result)?.includes('"status": "delegated"')
              ) {
                idx = k;
                break;
              }
            }
          }
          const startedAt = existing[idx]?.startedAt || startedAtOverride;
          const elapsed = startedAt
            ? ((Date.now() - startedAt) / 1000).toFixed(1) + "s"
            : undefined;
          // Server may now include duration_ms — prefer that
          // (real wall-clock from agentic_loop) over the
          // client-side timer which loses time spent in queues.
          const dur = (tc as any).duration_ms
            ? ((tc as any).duration_ms / 1000).toFixed(2) + "s"
            : elapsed;
          if (idx >= 0) {
            existing[idx] = {
              ...existing[idx],
              name: tc.name || existing[idx].name,
              arguments: incomingArguments || existing[idx].arguments,
              result: resultText,
              status,
              ...websiteDenial,
              duration: dur,
            };
          } else {
            existing.push({
              name: tc.name || "tool",
              arguments: incomingArguments,
              result: resultText,
              status,
              ...websiteDenial,
              duration: dur,
            });
          }
          // Child finished — clear the wrapper's activeChild
          // marker if it was pointing at this tool.
          if (
            isChildOfWrapper &&
            existing[pendingWrapperIdx]?.activeChild === tc.name
          ) {
            existing[pendingWrapperIdx] = {
              ...existing[pendingWrapperIdx],
              activeChild: undefined,
              lastChild: tc.name,
              completedChildCount:
                (existing[pendingWrapperIdx].completedChildCount || 0) + 1,
            };
          }
        } else {
          // Pending event — push as own card AND mirror onto the
          // wrapper's activeChild if this is a sub-tool.
          existing.push({
            name: tc.name || "tool",
            arguments: incomingArguments,
            status: "pending",
            startedAt: startedAtOverride || Date.now(),
          });
          if (isChildOfWrapper) {
            existing[pendingWrapperIdx] = {
              ...existing[pendingWrapperIdx],
              activeChild: tc.name,
              lastChild: tc.name,
              childCount: (existing[pendingWrapperIdx].childCount || 0) + 1,
            };
          }
        }
        updated[updated.length - 1] = { ...last, tool_calls: existing };
      }
      return updated;
    });
  };

  const applyAssistantBlocks = (blocks: unknown, deferFinalText = false) => {
    if (!Array.isArray(blocks) || blocks.length === 0) return;
    const nextBlocks = summaryStarted
      ? markAssistantProcessBlocksSummarizing(blocks as AssistantBlock[]) || blocks
      : blocks;
    // A terminal snapshot includes the complete reply. Keep its process
    // lifecycle/timestamps immediately, but let text_delta paint the body
    // before exposing the same text through the structured final block.
    const deferFinal = (deferFinalText || Boolean(queuedText || typewriterTimer))
      && nextBlocks.some((block: AssistantBlock) => block.type === "text" && block.phase === "final" && block.text);
    deferredFinalBlocks = deferFinal ? nextBlocks as AssistantBlock[] : undefined;
    const visibleBlocks = deferFinal
      ? nextBlocks.map((block: AssistantBlock) => block.type === "text" && block.phase === "final" ? { ...block, text: "" } : block)
      : nextBlocks;
    setMessages((prev) => {
      const updated = [...prev];
      let last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content: "",
          assistant_blocks: visibleBlocks as AssistantBlock[],
          timestamp: new Date().toISOString(),
        });
        return updated;
      }
      if (last.role === "assistant") {
        updated[updated.length - 1] = {
          ...last,
          assistant_blocks: visibleBlocks as AssistantBlock[],
        };
      }
      return updated;
    });
  };

  const markSummaryStarted = () => {
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (!last || last.role !== "assistant" || !Array.isArray(last.assistant_blocks)) {
        return prev;
      }
      const assistantBlocks = markAssistantProcessBlocksSummarizing(last.assistant_blocks);
      if (assistantBlocks === last.assistant_blocks) return prev;
      updated[updated.length - 1] = {
        ...last,
        assistant_blocks: assistantBlocks,
      };
      return updated;
    });
  };

  const queueToolStart = (tc: any) => {
    const entry: PendingToolStart = {
      tc,
      startedAt: Date.now(),
      visible: false,
      timer: setTimeout(() => {
        entry.visible = true;
        applyToolCall(entry.tc, "pending", entry.startedAt);
      }, TOOL_START_DISPLAY_DELAY_MS),
    };
    pendingToolStarts.push(entry);
  };

  const settleToolCall = (tc: any, status: ToolCall["status"]) => {
    const pending = findPendingToolStart(tc);
    if (pending) {
      clearTimeout(pending.timer);
      removePendingToolStart(pending);
      applyToolCall(tc, status, pending.startedAt);
      return;
    }
    applyToolCall(tc, status);
  };

  const flushPendingToolStarts = () => {
    for (const entry of [...pendingToolStarts]) {
      clearTimeout(entry.timer);
      removePendingToolStart(entry);
      if (!entry.visible) applyToolCall(entry.tc, "pending", entry.startedAt);
    }
  };

  const clearPendingToolStarts = () => {
    for (const entry of [...pendingToolStarts]) {
      clearTimeout(entry.timer);
      removePendingToolStart(entry);
    }
  };

  const clearQueuedText = () => {
    deferredFinalBlocks = undefined;
    queuedText = "";
    if (typewriterTimer) {
      clearTimeout(typewriterTimer);
      typewriterTimer = undefined;
    }
    notifyTypewriterIdle();
  };

  const flushQueuedTextNow = () => {
    if (typewriterTimer) {
      clearTimeout(typewriterTimer);
      typewriterTimer = undefined;
    }
    if (queuedText) {
      const remaining = queuedText;
      queuedText = "";
      appendVisibleText(remaining);
    }
    notifyTypewriterIdle();
  };

  const tagLastAssistantMessage = (messageId: unknown) => {
    const id = typeof messageId === "string" ? messageId.trim() : "";
    if (!id) return;
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") return prev;
      updated[updated.length - 1] = { ...last, id };
      return updated;
    });
  };

  const normalizeEventId = (value: unknown): string | undefined => {
    if (typeof value !== "string") return undefined;
    const trimmed = value.trim();
    return trimmed || undefined;
  };

  const isForeignStreamEvent = (parsed: Record<string, unknown>) => {
    const eventConversationId = normalizeEventId(parsed.conversation_id);
    const eventMessageId = normalizeEventId(parsed.message_id);
    if (eventConversationId && streamConversationId && eventConversationId !== streamConversationId) {
      return true;
    }
    if (eventMessageId && streamMessageId && eventMessageId !== streamMessageId) {
      return true;
    }
    return false;
  };

  const applyStreamErrorMessage = (message: unknown, persisted: boolean, errorCode?: unknown) => {
    clearQueuedText();
    clearPendingToolStarts();
    // This body is the host's facts-only failure projection, not a rejected
    // model candidate or a provider diagnostic. History uses the same body.
    const validationFailure = errorCode === "final_response_validation_failed";
    const resourceFailure = typeof errorCode === "string" && ["sandbox_allocation_failed", "sandbox_queue_timeout", "sandbox_capacity_timeout", "sandbox_runner_unavailable", "sandbox_allocation_rejected"].includes(errorCode);
    const content = validationFailure || resourceFailure ? redactSensitiveText(String(message)) : persisted || INTERNAL_TOOL_CONTRACT_ERROR_RE.test(String(message))
      ? formatPersistedStreamErrorMessage(message)
      : (formatByokProviderErrorMessage(message) ?? String(message));
    setMessages((prev) => {
      const updated = [...prev];
      const last = updated[updated.length - 1];
      if (!last || last.role !== "assistant") {
        updated.push({
          role: "assistant",
          content,
          stream_error: true,
          timestamp: new Date().toISOString(),
          ...(validationFailure ? { meta: { error: "final_response_validation_failed" } } : {}),
        });
      } else {
        updated[updated.length - 1] = {
          ...last,
          content,
          stream_error: true,
          assistant_blocks: markAssistantProcessBlocksFailed(last.assistant_blocks, content),
          ...(validationFailure ? { meta: { ...last.meta, error: "final_response_validation_failed" } } : {}),
        };
      }
      return updated;
    });
  };

  try {
    while (true) {
    if (signal?.aborted) break;
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("id: ")) {
        currentEventId = line.slice(4).trim() || undefined;
        continue;
      }
      if (line.startsWith("event: ")) {
        currentEvent = line.slice(7).trim();
        continue;
      }
      if (!line.startsWith("data: ")) continue;
      const data = line.slice(6).trim();
      if (data === "[DONE]") continue;
      try {
        const parsed = JSON.parse(data);
        if (currentEventId) result.lastEventId = currentEventId;

        if (isForeignStreamEvent(parsed)) {
          continue;
        }

        // No new text/tool events may reopen a terminal turn. Already accepted
        // text keeps painting while terminal metadata in this chunk is read.
        if ((result.error || result.streamEnded) && !["error", "stream_end", "runtime_status", "runtime_cancelled"].includes(currentEvent)) {
          continue;
        }

        if (currentEvent === "error") {
          if (Array.isArray(parsed.assistant_blocks)) applyAssistantBlocks(parsed.assistant_blocks);
          const message = parsed.message || parsed.error || "Chat stream failed";
          const persisted = typeof parsed.persisted === "boolean"
            ? parsed.persisted
            : Boolean(parsed.message_id);
          result.error = {
            message: String(message),
            persisted,
            messageId: parsed.message_id,
          };
          tagLastAssistantMessage(parsed.message_id);
          // A model-authored explanation is authoritative. Localized reason
          // text is only a fallback when the server has no reply to show.
          const runtimeFailure = !parsed.message && !parsed.error
            ? runtimeTerminalFailureText(parsed.error_code) : undefined;
          applyStreamErrorMessage(runtimeFailure ?? message, runtimeFailure ? false : persisted, parsed.error_code);
          continue;
        }

        if (parsed.conversation_id && !currentConvId) {
          setCurrentConvId(parsed.conversation_id);
          currentConvId = parsed.conversation_id;
          streamConversationId = parsed.conversation_id;
        } else if (parsed.conversation_id && !streamConversationId) {
          streamConversationId = parsed.conversation_id;
        }

        if (Array.isArray(parsed.assistant_blocks)) {
          applyAssistantBlocks(parsed.assistant_blocks, currentEvent === "text_delta"
            && Boolean(normalizeToolResult(parsed.text_delta ?? parsed.token ?? parsed.content)));
        }

        if (
          currentEvent === "runtime_run" ||
          currentEvent === "runtime_waiting" ||
          currentEvent === "runtime_status" ||
          currentEvent === "runtime_cancelled"
        ) {
          const runId = normalizeEventId(parsed.run_id) || result.runtimeRunId;
          if (!runId) continue;
          const rawQueue =
            parsed.queue && typeof parsed.queue === "object"
              ? (parsed.queue as Record<string, unknown>)
              : undefined;
          const pollAfterSeconds = Math.max(
            1,
            Number(rawQueue?.poll_after_seconds || parsed.poll_after_seconds || result.pollAfterSeconds || 5),
          );
          const queue = rawQueue?.ticket
            ? {
                ticket: String(rawQueue.ticket),
                position:
                  rawQueue.position == null ? undefined : Number(rawQueue.position),
                etaSeconds:
                  rawQueue.eta_seconds == null ? undefined : Number(rawQueue.eta_seconds),
                pollAfterSeconds,
                deadlineAt:
                  rawQueue.deadline_at == null ? undefined : String(rawQueue.deadline_at),
              }
            : undefined;
          const status = String(
            parsed.status ||
              (currentEvent === "runtime_waiting"
                ? "waiting_resource"
                : currentEvent === "runtime_cancelled"
                  ? "cancelled"
                  : result.runtimeStatus || "queued"),
          );
          const humanInput =
            status === "waiting_resource"
              ? runtimeHumanInputFromPayload(parsed.human_input)
              : undefined;
          result.runtimeRunId = runId;
          result.runtimeStatus = status;
          runtimeStatusReason = typeof parsed.status_reason === "string" ? parsed.status_reason : undefined;
          result.runtimeQueue = status === "waiting_resource" ? queue : undefined;
          result.runtimeHumanInput = humanInput;
          result.pollAfterSeconds = pollAfterSeconds;
          if (currentEventId) result.lastEventId = currentEventId;
          onRuntimeState?.({
            runId,
            status,
            statusReason: runtimeStatusReason,
            queue: result.runtimeQueue,
            humanInput,
            pollAfterSeconds,
            lastEventId: currentEventId || result.lastEventId,
          });
          continue;
        }

        if (
          result.runtimeRunId
          && ["queued", "waiting_resource"].includes(result.runtimeStatus || "")
          && !(result.runtimeHumanInput && !result.runtimeHumanInput.answered)
          && ["stream_start", "tool_start", "tool_end", "text_delta", "summary_start"].includes(currentEvent)
        ) {
          // Execution events supersede an earlier resource wait. Replays of a
          // paused question must retain its authoritative human-input state.
          result.runtimeStatus = "running";
          result.runtimeQueue = undefined;
          result.runtimeHumanInput = undefined;
          onRuntimeState?.({
            runId: result.runtimeRunId,
            status: "running",
            pollAfterSeconds: result.pollAfterSeconds || 5,
            lastEventId: currentEventId || result.lastEventId,
          });
        }

        if (currentEvent === "stream_start") {
          streamMessageId = normalizeEventId(parsed.message_id) || streamMessageId;
          setMessages((messages) => reconcileUserMessageReceipt(messages, parsed.user_message_id, currentConvId, parsed.client_message_id));
          tagLastAssistantMessage(parsed.message_id);
          continue;
        }

        if (currentEvent === "text_reset") {
          resetBeforeNextText = parsed.clear !== true;
          clearQueuedText();
          if (parsed.clear === true) {
            setMessages((prev) => {
              const last = prev[prev.length - 1];
              if (!last || last.role !== "assistant") return prev;
              return [...prev.slice(0, -1), {
                ...last,
                content: "",
                assistant_blocks: last.assistant_blocks?.filter((block) => block.type !== "text"),
              }];
            });
          }
          continue;
        }

        if (currentEvent === "summary_start") {
          summaryStarted = true;
          resetBeforeNextText = true;
          clearQueuedText();
          markSummaryStarted();
          continue;
        }

        if (currentEvent === "process_note") {
          continue;
        }

        if (currentEvent === "stream_end") {
          result.streamEnded = true;
          streamMessageId = normalizeEventId(parsed.message_id) || streamMessageId;
          if (parsed.message_id) result.messageId = String(parsed.message_id);
          if (typeof parsed.persisted === "boolean") result.persisted = parsed.persisted;
          tagLastAssistantMessage(parsed.message_id);
          const workspaceRecommendation = normalizeWorkspaceRecommendation(
            parsed.workspace_recommendation,
          );
          if (workspaceRecommendation) {
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (!last || last.role !== "assistant") return prev;
              updated[updated.length - 1] = {
                ...last,
                meta: {
                  ...(last.meta || {}),
                  workspace_recommendation: workspaceRecommendation,
                },
              };
              return updated;
            });
          }
          const attachments = normalizeMessageAttachments(parsed.attachments);
          if (attachments) {
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (!last || last.role !== "assistant") {
                updated.push({
                  role: "assistant",
                  content: "",
                  attachments,
                  timestamp: new Date().toISOString(),
                });
              } else {
                updated[updated.length - 1] = { ...last, attachments };
              }
              return updated;
            });
          }
          const stopReason = String(parsed.stop_reason || "");
          if (stopReason === "cancelled") streamCancelled = true;
          if (stopReason === "hitl_required" || stopReason === "needs_hitl") streamPaused = true;
          if (stopReason === "credit_exhausted") {
            const detail = normalizePlanLimitDetail(
              parsed.limit_detail || parsed.detail,
              parsed.error || t("component.upgrade_prompt.default_message"),
            );
            result.stopReason = stopReason;
            result.limitDetail = detail;
            // The inline notice already explains a provider-paid trial stop.
            if (planLimitOffersUpgrade(detail.kind)) useUpgradeStore.getState().show(detail);
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (!last || last.role !== "assistant") {
                updated.push({
                  role: "assistant",
                  content: formatCreditLimitMessage(detail),
                  stop_reason: stopReason,
                  limit_detail: detail,
                  timestamp: new Date().toISOString(),
                });
              } else {
                updated[updated.length - 1] = {
                  ...last,
                  content: formatCreditLimitMessage(detail),
                  stop_reason: stopReason,
                  limit_detail: detail,
                };
              }
              return updated;
            });
          } else if (parsed.error) {
            const persisted = typeof parsed.persisted === "boolean"
              ? parsed.persisted
              : Boolean(parsed.message_id);
            result.error = {
              message: String(parsed.error),
              persisted,
              messageId: parsed.message_id ? String(parsed.message_id) : undefined,
            };
            applyStreamErrorMessage(parsed.error, persisted, parsed.error_code);
          } else if (stopReason === "error") {
            result.stopReason = stopReason;
          }
          continue;
        }

        const token = normalizeToolResult(parsed.text_delta ?? parsed.token ?? parsed.content) || "";
        if (token) {
          const shouldReset = resetBeforeNextText;
          resetBeforeNextText = false;
          enqueueText(token, shouldReset);
        }

        if (parsed.tool_call) {
          const tc = parsed.tool_call;
          const result = normalizeToolResult(tc.result);
          const status = tc.status || (result ? "success" : "pending");
          if (status === "pending") {
            queueToolStart(tc);
          } else {
            settleToolCall(tc, status);
          }
        }

        if (parsed.sub_agent) {
          setMessages((prev) => {
            const updated = [...prev];
            let last = updated[updated.length - 1];
            if (!last || last.role !== "assistant") {
              updated.push({
                role: "assistant",
                content: "",
                sub_agent_events: [],
                timestamp: new Date().toISOString(),
              });
              last = updated[updated.length - 1]!;
            }
            if (last.role === "assistant") {
              const existing = last.sub_agent_events || [];
              updated[updated.length - 1] = {
                ...last,
                sub_agent_events: mergeSubAgentEvents(existing, {
                  ...parsed.sub_agent,
                  agent_name:
                    parsed.sub_agent.agent_name ||
                    parsed.sub_agent.name ||
                    "Sub-Agent",
                  timestamp:
                    parsed.sub_agent.updated_at ||
                    parsed.sub_agent.timestamp ||
                    new Date().toISOString(),
                }),
              };
            }
            return updated;
          });
        }

        if (parsed.hitl) {
          setMessages((prev) => {
            const updated = [...prev];
            let last = updated[updated.length - 1];
            if (!last || last.role !== "assistant") {
              updated.push({
                role: "assistant",
                content: "",
                hitl_requests: [],
                timestamp: new Date().toISOString(),
              });
              last = updated[updated.length - 1]!;
            }
            if (last.role === "assistant") {
              const existing = last.hitl_requests || [];
              updated[updated.length - 1] = {
                ...last,
                hitl_requests: [
                  ...existing,
                  {
                    id: parsed.hitl.id || String(Date.now()),
                    prompt: parsed.hitl.prompt || "",
                    type: parsed.hitl.type || "approval",
                    action: parsed.hitl.action,
                    tool: parsed.hitl.tool,
                    workspace: parsed.hitl.workspace,
                    paths: Array.isArray(parsed.hitl.paths) ? parsed.hitl.paths : undefined,
                    content: parsed.hitl.content,
                    args_preview: parsed.hitl.args_preview,
                    operation: parsed.operation,
                    options: Array.isArray(parsed.hitl.options) ? parsed.hitl.options : undefined,
                  },
                ],
              };
            }
            return updated;
          });
        }
      } catch {
        /* skip unparseable */
      }
    }
    if (result.error?.persisted || result.streamEnded) {
      // Execution ends now, independently of HTTP EOF. Successful replies
      // retain their display cadence; cancellation and failures settle now.
      if (result.error || result.stopReason || streamCancelled || streamPaused || result.persisted === false) flushQueuedTextNow();
      cancelReader();
      break;
    }
    }
  } catch (err) {
    flushQueuedTextNow();
    throw err;
  } finally {
    signal?.removeEventListener("abort", cancelReader);
    if (signal?.aborted) {
      clearPendingToolStarts();
      flushQueuedTextNow();
    } else {
      flushPendingToolStarts();
    }
  }
  // A run that ended outside its loop (its Sandbox never started, its ticket
  // expired) may only report its terminal status. Never leave the bubble on
  // its last working state with no word that the request failed.
  if (
    result.runtimeRunId && result.runtimeStatus === "failed"
    && !result.error && !result.streamEnded && !signal?.aborted
  ) {
    const message = runtimeTerminalFailureText(runtimeStatusReason) ?? localizedRequestFailure();
    result.error = { message, persisted: false };
    applyStreamErrorMessage(message, false);
  }
  // A scoped terminal event is authoritative even if the replay's latest
  // runtime_status still describes the earlier input suspension. Never leave
  // a finished turn parked or route the next message back into its question.
  if (result.runtimeRunId && (result.streamEnded || result.error?.persisted)) {
    result.runtimeStatus = streamCancelled || result.runtimeStatus === "cancelled" ? "cancelled"
      : result.error || result.stopReason || result.persisted === false || result.runtimeStatus === "failed"
        ? "failed" : "completed";
    result.runtimeQueue = undefined;
    result.runtimeHumanInput = undefined;
    onRuntimeState?.({
      runId: result.runtimeRunId,
      status: result.runtimeStatus,
      pollAfterSeconds: result.pollAfterSeconds || 5,
      lastEventId: result.lastEventId,
    });
  }
  await waitForTypewriterIdle();
  return result;
}

/**
 * Debounce hook — shared between chat components.
 */
export function useDebounced<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return debounced;
}
