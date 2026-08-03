/**
 * `pending_action.kind` — the card vocabulary.
 *
 * Mirrors `packages/core/constants/pending_actions.py` (`PendingActionKind`).
 * The backend picks the branch that resolves a card off this word; this file
 * picks the branch that renders it. They are the same closed set, and
 * `tests/test_enum_vocabularies.py::test_frontend_pending_action_kinds_are_real_kinds`
 * fails if this file grows a kind the backend never writes — which would be a
 * card that renders and then resolves to nothing.
 *
 * These values are persisted in the `messages.pending_action` JSONB column;
 * they are wire format, not display strings.
 */
export const PendingActionKind = {
  NEEDS_LOGIN: "needs_login",
  NEEDS_INPUT: "needs_input",
  NEEDS_CONFIRMATION: "needs_confirmation",
  HUMAN_INPUT: "human_input",
  GOVERNANCE_APPROVAL: "governance_approval",
  APPROVE_PROPOSALS: "approve_proposals",
  RETRY_STRATEGIST_REVIEW: "retry_strategist_review",
  WORKSPACE_OPERATION_REVIEW: "workspace_operation_review",
  EXTERNAL_MESSAGE_APPROVAL: "external_message_approval",
  WORKFLOW_STARTER_INPUT: "workflow_starter_input",
  WORKFLOW_INPUT: "workflow_input",
  WORKFLOW_APPROVAL: "workflow_approval",
  WORKFLOW_RETRY: "workflow_retry",
} as const;

export type PendingActionKindValue =
  (typeof PendingActionKind)[keyof typeof PendingActionKind];
