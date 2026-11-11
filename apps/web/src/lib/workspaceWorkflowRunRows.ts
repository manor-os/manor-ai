/**
 * Workspace chat shows each Workflow run as one event row, anchored where the
 * run first appears and updated in place. Every attempt of a run family, its
 * status updates and the system notice that started it ("Started X from
 * Workspace Flows.", a Proposal item) fold into that row. What a person must
 * decide stays a bubble on its own message; this module only says which run a
 * message belongs to and what the row reads.
 */

import { workflowStagePosition } from "../components/workflows/workflowRunDisplay";

export interface WorkflowRunRowMessage {
  id: string;
  created_at?: string | null;
  updated_at?: string | null;
  message_kind?: string | null;
  author_kind?: string | null;
  author_subscription_id?: string | null;
  refs?: Array<{ type?: string; id?: string; title?: string; name?: string } | null> | null;
  meta?: Record<string, any> | null;
  pending_action?: Record<string, any> | null;
  hitl_requests?: Array<Record<string, any> | null> | null;
  attachments?: unknown[] | null;
  resolved_at?: string | null;
}

/** What the row's glyph says: working, waiting on someone, done, stopped. */
export type WorkflowRunRowState =
  | "running"
  | "waiting"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export interface WorkflowRunRow {
  /** The family's first run id; stable across retries. */
  key: string;
  /** The newest attempt, which the "View" link opens. */
  runId: string;
  workflowId: string;
  title: string;
  state: WorkflowRunRowState;
  /** 1-based step in progress and the step count, when the run lists steps. */
  currentStep: number;
  totalSteps: number;
  /** Attempts after the first; a finished run reads "recovered after N retries". */
  retries: number;
  /** The newest status update. */
  latest: WorkflowRunRowMessage;
  /** Time of the latest status update, for the row's clock. */
  updatedAt: string;
}

export interface WorkflowRunRowFold {
  rows: Map<string, WorkflowRunRow>;
  /** Status updates and start notices shown through their run's row. A status
   *  update that also asks something ("change the input and retry") keeps its
   *  bubble too; `hasOwnBubble` says which. */
  rowKeyByMessageId: Map<string, string>;
  /** A run's waits come from the agent on that step, not from "Manor". */
  speakerByMessageId: Map<string, string>;
}

export const WORKFLOW_WAIT_ACTION_KINDS = new Set([
  "workflow_starter_input",
  "workflow_retry",
  "workflow_effect_review",
  "workflow_approval",
  "workflow_input",
]);

const WAITING_OUTCOMES = new Set(["needs_input", "revision_required"]);

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function messageRunId(message: WorkflowRunRowMessage): string {
  return text(message.pending_action?.workflow_chat_host_run_id)
    || text(message.pending_action?.workflow_run_id)
    || text(message.meta?.workflow_run_id)
    || text((message.refs || []).find((ref) => ref?.type === "workflow_run")?.id);
}

function isStatusUpdate(message: WorkflowRunRowMessage): boolean {
  return message.message_kind === "workflow_activity"
    && Boolean(text(message.meta?.workflow_run_id));
}

/** A run's status message that also carries a question is shown as the row and as a bubble. */
export function hasOwnBubble(message: WorkflowRunRowMessage): boolean {
  return Boolean(text(message.pending_action?.kind));
}

/** A short system line that only announces a start; never a person's message. */
function isStartNotice(message: WorkflowRunRowMessage): boolean {
  return message.author_kind === "system"
    && (message.message_kind === "system" || !message.message_kind)
    && !message.pending_action
    && !message.resolved_at
    && !(message.hitl_requests || []).length
    && !(message.attachments || []).length
    && !text(message.meta?.workflow_run_id);
}

function isOpenWait(message: WorkflowRunRowMessage): boolean {
  if (message.resolved_at) return false;
  if (WORKFLOW_WAIT_ACTION_KINDS.has(text(message.pending_action?.kind))) return true;
  return message.meta?.workflow_runtime_hitl === true
    && (message.hitl_requests || []).some((request) => request && !request.resolved);
}

function stamp(message: WorkflowRunRowMessage): string {
  return text(message.updated_at) || text(message.created_at);
}

function rowState(
  latest: WorkflowRunRowMessage,
  hasOpenWait: boolean,
): WorkflowRunRowState {
  const status = text(latest.meta?.workflow_status).toLowerCase() || "queued";
  const outcome = text(latest.meta?.workflow_business_outcome).toLowerCase();
  if (status === "failed") return "failed";
  if (status === "cancelled" || status === "skipped") return "cancelled";
  if (status === "completed") return WAITING_OUTCOMES.has(outcome) ? "waiting" : "completed";
  if (hasOpenWait || WAITING_OUTCOMES.has(outcome)) return "waiting";
  if (status === "paused") {
    // Paused for background work (an automatic retry, a render) is still running.
    const current = text(latest.meta?.workflow_current_step_id);
    const steps = Array.isArray(latest.meta?.workflow_steps) ? latest.meta!.workflow_steps : [];
    const step = steps.find((item: any) => text(item?.id) === current);
    return text(step?.status).toLowerCase() === "running" ? "running" : "paused";
  }
  return "running";
}

function stepProgress(latest: WorkflowRunRowMessage): { currentStep: number; totalSteps: number } {
  const steps = (Array.isArray(latest.meta?.workflow_steps) ? latest.meta!.workflow_steps : [])
    .filter((step: unknown) => step && typeof step === "object");
  const { current, total } = workflowStagePosition(steps);
  return { currentStep: current, totalSteps: total };
}

/** Where a run stands, as each step's change announces it. */
export interface WorkflowProgressEvent {
  workflow_run_id?: string | null;
  workflow_status?: string | null;
  workflow_current_step_id?: string | null;
  workflow_steps?: Array<{ id?: string | null; status?: string | null }> | null;
}

const IN_PROGRESS_STATUSES = new Set(["pending", "queued", "running", "paused"]);

/** The run's loaded row message moved to the announced step, or null when the
 *  announcement names another run, steps the row does not hold, or an ending
 *  (then the row is read again). */
export function applyWorkflowProgress<T extends WorkflowRunRowMessage>(
  message: T,
  progress: WorkflowProgressEvent | null | undefined,
): T | null {
  const steps = Array.isArray(message.meta?.workflow_steps) ? message.meta!.workflow_steps : [];
  const announced = Array.isArray(progress?.workflow_steps) ? progress!.workflow_steps! : [];
  if (!progress || text(progress.workflow_run_id) !== text(message.meta?.workflow_run_id)) return null;
  if (!IN_PROGRESS_STATUSES.has(text(progress.workflow_status).toLowerCase())) return null;
  const statusById = new Map(announced.map((step) => [text(step?.id), text(step?.status)]));
  if (statusById.size !== steps.length || steps.some((step: any) => !statusById.has(text(step?.id)))) return null;
  return {
    ...message,
    meta: {
      ...message.meta,
      workflow_status: text(progress.workflow_status) || message.meta?.workflow_status,
      workflow_current_step_id: text(progress.workflow_current_step_id) || message.meta?.workflow_current_step_id,
      workflow_steps: steps.map((step: any) => ({ ...step, status: statusById.get(text(step?.id)) || step.status })),
    },
  };
}

/** Fold one chronological message list into one row per Workflow run family. */
export function foldWorkflowRunRows(messages: WorkflowRunRowMessage[]): WorkflowRunRowFold {
  // Retries name the attempt they replace; walk back to the family's first run.
  const parentByRun = new Map<string, string>();
  const rootByRun = new Map<string, string>();
  for (const message of messages) {
    const runId = text(message.meta?.workflow_run_id) || text(message.pending_action?.workflow_run_id);
    if (!runId) continue;
    const root = text(message.meta?.workflow_root_run_id);
    if (root) rootByRun.set(runId, root);
    const parent = text(message.meta?.workflow_retry_of_run_id)
      || text(message.pending_action?.retry_of_run_id);
    if (parent && parent !== runId) parentByRun.set(runId, parent);
  }
  const familyKey = (runId: string): string => {
    let current = runId;
    const seen = new Set<string>();
    while (!seen.has(current)) {
      seen.add(current);
      const declared = rootByRun.get(current);
      if (declared) return declared;
      const parent = parentByRun.get(current);
      if (!parent) return current;
      current = parent;
    }
    return current;
  };

  const updatesByKey = new Map<string, WorkflowRunRowMessage[]>();
  const rowKeyByMessageId = new Map<string, string>();
  const messageById = new Map(messages.map((message) => [message.id, message]));
  for (const message of messages) {
    if (!isStatusUpdate(message)) continue;
    const key = familyKey(text(message.meta?.workflow_run_id));
    updatesByKey.set(key, [...(updatesByKey.get(key) || []), message]);
    rowKeyByMessageId.set(message.id, key);
    // The notice that started this run is part of its row.
    const origin = messageById.get(text(message.meta?.workflow_origin_message_id));
    if (origin && isStartNotice(origin)) rowKeyByMessageId.set(origin.id, key);
  }

  // Older starts carry no origin link: a "Started X from Workspace Flows."
  // notice joins the first run of that binding started with or after it, a
  // Proposal item notice the first run a Proposal started. Both are written in
  // one transaction, so they can share a timestamp and either can sort first.
  for (const message of messages) {
    if (rowKeyByMessageId.has(message.id) || !isStartNotice(message)) continue;
    const bindingId = text(message.meta?.workflow_route_source) ? text(message.meta?.workflow_binding_id) : "";
    const fromProposal = Boolean(text(message.meta?.proposal_workflow_run_item_id));
    if (!bindingId && !fromProposal) continue;
    const startedAt = text(message.created_at);
    const next = messages
      .filter((candidate) => (
        isStatusUpdate(candidate)
        && text(candidate.created_at) >= startedAt
        && (bindingId
          ? text(candidate.meta?.workflow_binding_id) === bindingId
          : text(candidate.meta?.workflow_route_source) === "proposal")
      ))
      .sort((left, right) => text(left.created_at).localeCompare(text(right.created_at)))[0];
    if (next) rowKeyByMessageId.set(message.id, rowKeyByMessageId.get(next.id)!);
  }

  const openWaitKeys = new Set<string>();
  const speakerByMessageId = new Map<string, string>();
  const waits: Array<[WorkflowRunRowMessage, string]> = [];
  for (const message of messages) {
    if (isStatusUpdate(message) && !hasOwnBubble(message)) continue;
    const runId = messageRunId(message);
    if (!runId) continue;
    const key = isStatusUpdate(message) ? rowKeyByMessageId.get(message.id)! : familyKey(runId);
    if (!updatesByKey.has(key)) continue;
    if (isOpenWait(message)) openWaitKeys.add(key);
    if (WORKFLOW_WAIT_ACTION_KINDS.has(text(message.pending_action?.kind)) && !text(message.author_subscription_id)) {
      waits.push([message, key]);
    }
  }

  const rows = new Map<string, WorkflowRunRow>();
  for (const [key, updates] of updatesByKey) {
    const ordered = [...updates].sort((left, right) => (
      Number(left.meta?.workflow_attempt_number || 1) - Number(right.meta?.workflow_attempt_number || 1)
      || stamp(left).localeCompare(stamp(right))
    ));
    const latest = ordered[ordered.length - 1];
    const workflowRef = (latest.refs || []).find((ref) => ref?.type === "workflow");
    rows.set(key, {
      key,
      runId: text(latest.meta?.workflow_run_id) || key,
      workflowId: text(workflowRef?.id) || text(latest.meta?.workflow_id),
      title: text(latest.meta?.workflow_title) || text(workflowRef?.title) || text(workflowRef?.name),
      state: rowState(latest, openWaitKeys.has(key)),
      ...stepProgress(latest),
      retries: Math.max(0, Number(latest.meta?.workflow_attempt_number || 1) - 1),
      latest,
      updatedAt: stamp(latest),
    });
  }

  for (const [message, key] of waits) {
    const latest = rows.get(key)?.latest;
    const steps = Array.isArray(latest?.meta?.workflow_steps) ? latest!.meta!.workflow_steps : [];
    const stepId = text(message.pending_action?.step_id);
    const speaker = text(steps.find((step: any) => text(step?.id) === stepId)?.subscription_id)
      || text(latest?.meta?.workflow_current_subscription_id);
    if (speaker) speakerByMessageId.set(message.id, speaker);
  }

  return { rows, rowKeyByMessageId, speakerByMessageId };
}
