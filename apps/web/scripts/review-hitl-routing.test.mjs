import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const reviewSource = await readFile(
  new URL("../src/components/workflows/WorkflowApprovalReview.tsx", import.meta.url),
  "utf8",
);
const actionCardSource = await readFile(
  new URL("../src/components/ui/ChatActionCard.tsx", import.meta.url),
  "utf8",
);
const plannerSource = await readFile(
  new URL("../../../packages/core/ai/runtime/planning.py", import.meta.url),
  "utf8",
);
const executorSource = await readFile(
  new URL("../../../packages/core/plans/executor.py", import.meta.url),
  "utf8",
);
const approvalOptionsSource = await readFile(
  new URL("../src/lib/approvalOptions.ts", import.meta.url),
  "utf8",
);
const taskTimelineSource = await readFile(
  new URL("../src/components/task/TaskExecutionTimeline.tsx", import.meta.url),
  "utf8",
);
const taskDetailSource = await readFile(
  new URL("../src/pages/TaskDetail.tsx", import.meta.url),
  "utf8",
);
const taskHitlSource = await readFile(
  new URL("../src/lib/taskHitl.ts", import.meta.url),
  "utf8",
);
const taskRecoverySource = await readFile(
  new URL("../src/components/task/TaskRecoveryPanel.tsx", import.meta.url),
  "utf8",
);
const tasksSource = await readFile(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const appLayoutSource = await readFile(
  new URL("../src/layouts/AppLayout.tsx", import.meta.url),
  "utf8",
);

test("Plan content review uses the typed review HITL route", () => {
  assert.match(executorSource, /PendingActionKind\.GOVERNANCE_APPROVAL\.value/);
  assert.match(executorSource, /"hitl_type": HitlType\.REVIEW\.value/);
  assert.match(executorSource, /"review": review_packet/);
  assert.match(executorSource, /"options": review_card_options\(values\.get\("options"\)\)/);
});

test("Task and Workflow review HITL share the same renderer", () => {
  assert.match(actionCardSource, /action\.kind === "workflow_approval" \|\| action\.hitl_type === "review"/);
  assert.match(actionCardSource, /<WorkflowApprovalReview/);
  assert.doesNotMatch(actionCardSource, /ArtifactReviewCard/);
  assert.match(reviewSource, /<InlineFileReferenceCard/);
  assert.match(reviewSource, /review\.artifacts/);
  assert.match(reviewSource, /review\.files/);
});

test("Review change requests collect guidance before resolving", () => {
  assert.match(actionCardSource, /normalized === "revise" \|\| normalized === "request_changes"/);
  assert.match(actionCardSource, /onResolve\(revisionChoice, value, \{ review: \{ revision_request: value \} \}\)/);
  assert.match(actionCardSource, /options=\{reviewApprovalOptions\(action\.options\)\}/);
  assert.match(actionCardSource, /revision_instructions/);
});

test("Review option filtering never falls back to generic standing approval", () => {
  assert.match(approvalOptionsSource, /export const REVIEW_APPROVAL_OPTIONS/);
  assert.match(approvalOptionsSource, /chosen\.some\(/);
  assert.match(approvalOptionsSource, /return \[\.\.\.REVIEW_APPROVAL_OPTIONS\]/);
  assert.match(approvalOptionsSource, /return filtered\.length \? filtered : \[\.\.\.REVIEW_APPROVAL_OPTIONS\]/);
  assert.match(actionCardSource, /action\.hitl_type === "review"[\s\S]*reviewApprovalOptions\(action\.options\)/);
});

test("review checklist is rendered as a non-interactive bullet list in every review variant", () => {
  assert.match(reviewSource, /function ReviewChecklist/);
  assert.match(reviewSource, /className="chat-workflow-review-checklist"/);
  assert.doesNotMatch(reviewSource, /chat-workflow-review-checklist-marker/);
  assert.match(reviewSource, /!\["review_artifacts", "artifacts", "files", "checklist"\]\.includes\(key\)/);
  assert.equal(
    (reviewSource.match(/<ReviewChecklist items=\{checklist\} \/>/g) || []).length,
    2,
  );
});

test("files opened from review preserve chat as the viewer return target", async () => {
  const fileCardSource = await readFile(
    new URL("../src/components/InlineFileReferenceCard.tsx", import.meta.url),
    "utf8",
  );
  const viewerSource = await readFile(
    new URL("../src/pages/FileViewer.tsx", import.meta.url),
    "utf8",
  );
  assert.match(fileCardSource, /chatReturnTo: currentReturnTo/);
  assert.match(fileCardSource, /returnTo: currentReturnTo/);
  assert.match(viewerSource, /const viewerReturnTo = getViewerReturnTo\(location\.state\)/);
  assert.match(viewerSource, /navigate\(-1\)/);
  assert.match(viewerSource, /navigate\(viewerReturnTo \|\| "\/knowledge", \{ replace: true \}\)/);
  assert.equal(
    (viewerSource.match(/aria-label=\{t\("page\.file_viewer\.go_back"\)\}/g) || []).length,
    2,
  );
});

test("Planner and executor preserve upstream artifact references", () => {
  assert.match(plannerSource, /params\.review_artifacts/);
  assert.match(plannerSource, /typed review HITL surface/);
  assert.match(executorSource, /step\.params = resolve_refs\(step\.params or \{\}, prior_results\)/);
  assert.match(executorSource, /"pending_action": _human_step_pending_action\(step\.params\)/);
  assert.match(executorSource, /pending_action=evt\.get\("pending_action"\)/);
});

test("waiting human steps are resolved through HITL cards, not generic retry", () => {
  const retryConstants = taskTimelineSource.slice(
    taskTimelineSource.indexOf("const FAILED_RETRYABLE_STEP_STATUSES"),
    taskTimelineSource.indexOf("const PLAN_STATUS_LABELS"),
  );
  assert.doesNotMatch(retryConstants, /waiting_human/);
  assert.doesNotMatch(taskTimelineSource, /WAITING_RETRYABLE_STEP_STATUSES/);
  assert.match(taskTimelineSource, /const canRetryStep = RETRYABLE_STEP_STATUSES\.has\(step\.step_status\)/);
});

test("Task free-form input does not masquerade as a typed review decision", () => {
  assert.match(taskHitlSource, /export function resumablePlanInputStep/);
  assert.match(taskHitlSource, /!step\.requires_approval/);
  assert.match(taskHitlSource, /stepHitlType\(step\) === "input"/);
  assert.match(taskHitlSource, /step\.kind === "human"/);
  assert.doesNotMatch(taskHitlSource, /Boolean\(step\.human_input_prompt\)/);
  assert.match(taskDetailSource, /step_id: stepId/);
  assert.match(taskDetailSource, /hasPendingPlanDecision/);
});

test("Task input prompt and fields stay bound to the exact resumed Step", () => {
  assert.match(taskRecoverySource, /String\(log\.meta\?\.step_id \|\| ""\) === inputStepId/);
  assert.match(taskRecoverySource, /inputStepId, lastEvent\?\.id/);
  assert.match(taskDetailSource, /inputStepId=\{pendingPlanInputStep\?\.id\}/);
  assert.match(tasksSource, /inputStepId=\{pendingPlanInputStep\?\.id\}/);
});

test("Task detail refreshes Plan and Step state after a HITL response", () => {
  const mutation = taskDetailSource.slice(
    taskDetailSource.indexOf("const hitlMutation = useMutation"),
    taskDetailSource.indexOf("const actionReplyCommentMutation"),
  );
  assert.match(mutation, /invalidateQueries\(\{ queryKey: \["task-plans", taskId\] \}\)/);
  assert.match(mutation, /result\.plan_id/);
  assert.match(mutation, /invalidateQueries\(\{ queryKey: \["plan-steps", result\.plan_id\] \}\)/);
});

test("Task retry refreshes Plan and Step state on every Task surface", () => {
  for (const [source, start, end, taskKey] of [
    [taskDetailSource, "const retryMutation = useMutation", "const hitlMutation", "taskId"],
    [tasksSource, "const retryMut = useMutation", "const hitlMut", "task.id"],
  ]) {
    const mutation = source.slice(source.indexOf(start), source.indexOf(end));
    assert.match(mutation, new RegExp(`invalidateQueries\\(\\{ queryKey: \\["task-plans", ${taskKey.replace(".", "\\.")}\\] \\}\\)`));
    assert.match(mutation, /result\.plan_id/);
    assert.match(mutation, /invalidateQueries\(\{ queryKey: \["plan-steps", result\.plan_id\] \}\)/);
  }
});

test("cross-tab Task runtime events refresh Task, Plan, and Step projections", () => {
  const handler = appLayoutSource.slice(
    appLayoutSource.indexOf("const onTaskUpdate = useCallback"),
    appLayoutSource.indexOf("const onJobUpdate = useCallback"),
  );
  assert.match(handler, /queryKey: \["task", taskId\]/);
  assert.match(handler, /queryKey: \["task-plans", taskId\]/);
  assert.match(handler, /queryKey: \["plan-steps", planId\]/);
  assert.match(executorSource, /await self\._emit_task_event\(task_event\)/);
  assert.match(executorSource, /broadcast_task_runtime_update/);
});

test("Task-bound dispatch recovery hides generic Plan and Step retry controls", () => {
  assert.match(taskTimelineSource, /TASK_RECOVERY_DISPATCH_FAILURE_TYPES/);
  assert.match(taskTimelineSource, /taskRecoveryOwnsRetry/);
  assert.match(taskTimelineSource, /const effectiveTaskStatus = taskStatus \?\? plan\.task_status/);
  assert.match(taskTimelineSource, /effectiveTaskStatus === "waiting_on_customer"/);
  assert.match(taskDetailSource, /taskStatus=\{task\?\.status\}/);
  assert.match(taskTimelineSource, /RETRYABLE_PLAN_STATUSES\.has\(plan\.status\) && !taskRecoveryOwnsRetry/);
  assert.match(taskTimelineSource, /const canRetryStep = RETRYABLE_STEP_STATUSES\.has\(step\.step_status\) && retrySurfaceAvailable/);
});

test("legacy Task HITL waits for the Plan query to resolve", () => {
  assert.match(taskHitlSource, /plansResolved: boolean/);
  assert.match(taskHitlSource, /!plansResolved/);
  assert.match(taskHitlSource, /Boolean\(plan\)/);
  assert.match(
    taskHitlSource,
    /task\.owner_subscription_id \|\| task\.owner_service_key/,
  );
  assert.match(
    taskDetailSource,
    /canResumeLegacyAgentInput\(task, latestPlan, plansResolved\)/,
  );
  assert.match(
    tasksSource,
    /canResumeLegacyAgentInput\(task, latestPlan, taskPlansResolved\)/,
  );
});

test("Task recovery waits for both Plan and Step decision state", () => {
  assert.match(taskDetailSource, /isSuccess: planStepsResolved/);
  assert.match(
    taskDetailSource,
    /planDecisionStateResolved = plansResolved[\s\S]*!latestPlan \|\| planStepsResolved/,
  );
  assert.match(
    taskDetailSource,
    /showTaskRecoveryPanel = planDecisionStateResolved/,
  );
  assert.match(tasksSource, /isSuccess: planStepsResolved/);
  assert.match(
    tasksSource,
    /planDecisionStateResolved = taskPlansResolved[\s\S]*!latestPlan \|\| planStepsResolved/,
  );
  assert.match(tasksSource, /\{planDecisionStateResolved && hasTaskRecoveryOrigin && \(/);
});
