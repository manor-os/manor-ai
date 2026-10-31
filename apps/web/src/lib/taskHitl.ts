import type { Task } from "./types";
import { isMasterAgent } from "./constants";


export function canResumeLegacyAgentInput(
  task: Task | null | undefined,
): boolean {
  if (
    !task
    || Boolean(task.owner_subscription_id || task.owner_service_key)
  ) return false;
  const hasLegacyHitlAgent = Boolean(
    task.agent_id || isMasterAgent(task.agent_id, task.agent_type),
  );
  return task.status === "waiting_on_customer" && hasLegacyHitlAgent;
}
