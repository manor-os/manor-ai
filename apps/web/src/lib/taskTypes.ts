/** Canonical task types that product behavior may branch on.
 *
 * Workspaces may retain custom task-type slugs for categorization. A product
 * behavior such as the approval gate must match an explicit canonical value,
 * never infer intent from a task title, description, or instruction string.
 */
export enum TaskType {
  GENERAL = "general",
  AI_GENERATED = "ai_generated",
  SCHEDULED = "scheduled",
  CUSTOMER_REQUEST = "customer_request",
  INCIDENT = "incident",
  INSPECTION = "inspection",
  FOLLOW_UP = "follow_up",
  APPROVAL = "approval",
  INTERACTIVE = "interactive",
}

export function isApprovalTaskType(value: unknown): boolean {
  return value === TaskType.APPROVAL;
}

export function isInteractiveTaskType(value: unknown): boolean {
  return value === TaskType.INTERACTIVE;
}
