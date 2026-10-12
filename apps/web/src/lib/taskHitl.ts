import type { ExecutionPlan, ExecutionStep, Task, TaskInputRequest } from "./types";
import { isMasterAgent } from "./constants";


const HITL_RESUMABLE_PLAN_STATUSES = new Set(["running", "paused", "needs_attention"]);
const HITL_TYPES = new Set(["authorize", "input", "review", "error"]);

/** A runtime subagent wait is actionable only through its server-projected
 * INPUT card. Step prose/kind cannot turn an approval into a text question. */
export function pendingTaskInputRequests(
  task: Task | null | undefined,
  plan: ExecutionPlan | null | undefined,
  steps: ExecutionStep[],
): TaskInputRequest[] {
  if (!task?.workspace_id || plan?.task_id !== task.id
    || !HITL_RESUMABLE_PLAN_STATUSES.has(String(plan?.status || ""))
    || ["completed", "cancelled", "failed"].includes(task.status)) return [];
  const waitingIds = new Set(steps.filter((step) => (
    step.plan_id === plan.id && step.step_status === "waiting_human"
  )).map((step) => step.id));
  const seen = new Set<string>();
  return (task.pending_input_requests || []).filter((request) => {
    const action = request.pending_action;
    if (!request.message_id || !request.request_id || request.plan_id !== plan.id
      || !waitingIds.has(request.step_id) || seen.has(request.step_id)
      || action?.hitl_type !== "input"
      || !["human_input", "needs_input"].includes(action.kind)
      || action.approval_request_id !== request.request_id
      || action.task_id !== task.id || action.plan_id !== plan.id
      || action.step_id !== request.step_id) return false;
    seen.add(request.step_id);
    return true;
  });
}

function stepHitlType(step: ExecutionStep): string {
  const params = step.params && typeof step.params === "object" ? step.params : {};
  const pendingAction = params.pending_action && typeof params.pending_action === "object"
    ? params.pending_action
    : {};
  const explicit = String(pendingAction.hitl_type || params.hitl_type || "").trim().toLowerCase();
  if (HITL_TYPES.has(explicit)) return explicit;
  if (
    params.review != null
    || params.review_artifacts != null
    || params.artifacts_for_review != null
  ) return "review";
  if (step.requires_approval) return "authorize";
  return "input";
}

export function resumablePlanInputStep(
  plan: ExecutionPlan | null | undefined,
  steps: ExecutionStep[],
): ExecutionStep | null {
  if (!HITL_RESUMABLE_PLAN_STATUSES.has(String(plan?.status || ""))) return null;
  return steps.find((step) => (
    step.step_status === "waiting_human"
    && !step.requires_approval
    && stepHitlType(step) === "input"
    && step.kind === "human"
  )) || null;
}

export function hasPendingPlanDecision(
  steps: ExecutionStep[],
  inputStepId?: string | null,
): boolean {
  return steps.some((step) => (
    step.step_status === "waiting_human" && step.id !== inputStepId
  ));
}

export function needsEffectReconciliation(
  task: Task | null | undefined,
  plan: ExecutionPlan | null | undefined,
): boolean {
  if (
    task?.status !== "waiting_on_customer"
    || !task.workspace_id
    || plan?.task_id !== task.id
  ) return false;
  const error = plan.last_error;
  return (
    (error?.type === "ActionOutcomeReconciliationRequired"
      || error?.type === "TaskEffectHistoryBlocked")
    && Array.isArray(error.reconciliation?.effects)
    && error.reconciliation.effects.length > 0
  );
}

export function canResumeLegacyAgentInput(
  task: Task | null | undefined,
  plan: ExecutionPlan | null | undefined,
  plansResolved: boolean,
): boolean {
  if (
    !plansResolved
    || !task
    || Boolean(plan)
    || Boolean(task.owner_subscription_id || task.owner_service_key)
  ) return false;
  const hasLegacyHitlAgent = Boolean(
    task.agent_id || isMasterAgent(task.agent_id, task.agent_type),
  );
  return task.status === "waiting_on_customer" && hasLegacyHitlAgent;
}
