/**
 * Every `message_kind` the backend writes into a Workspace conversation.
 *
 * Each kind renders as a bubble or an event row. The kind itself is internal:
 * it is never shown as a label or badge. tests/test_workspace_message_kinds.py
 * fails when the backend writes a kind that is missing here.
 */
export const WORKSPACE_MESSAGE_KINDS = [
  "agent_error",
  "agent_update",
  "briefing",
  "external_message",
  "goal_alert",
  "hitl_prompt",
  "hitl_request",
  "proposal",
  "step_event",
  "strategist_activity",
  "system",
  "text",
  "workflow_activity",
] as const;

export type WorkspaceMessageKind = (typeof WORKSPACE_MESSAGE_KINDS)[number];

type LifecycleMeta = { meta?: Record<string, unknown> | null };

/** A second "started" right after a "started" says nothing new: the latest one stands for both. */
export function repeatsWorkspaceLifecycle(previous: LifecycleMeta, next: LifecycleMeta): boolean {
  return previous.meta?.workspace_lifecycle === true
    && next.meta?.workspace_lifecycle === true
    && previous.meta?.workspace_lifecycle_action === next.meta?.workspace_lifecycle_action
    && previous.meta?.workspace_lifecycle_phase === next.meta?.workspace_lifecycle_phase;
}
