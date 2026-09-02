import type { ExecutionPlan, ExecutionStep, Task } from "./types";
import { isMasterAgent } from "./constants";


const HITL_RESUMABLE_PLAN_STATUSES = new Set(["running", "paused", "needs_attention"]);
const HITL_TYPES = new Set(["authorize", "input", "review", "error"]);

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
