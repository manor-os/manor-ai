import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const actionSource = await readFile(
  new URL("../src/components/ui/ChatActionCard.tsx", import.meta.url),
  "utf8",
);
const reviewSource = await readFile(
  new URL("../src/components/workflows/WorkflowApprovalReview.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const taskDisplaySource = await readFile(
  new URL("../src/lib/taskDisplay.ts", import.meta.url),
  "utf8",
);

test("workflow approvals render their structured review payload", () => {
  assert.match(reviewSource, /export default function WorkflowApprovalReview/);
  assert.match(actionSource, /import WorkflowApprovalReview from "\.\.\/workflows\/WorkflowApprovalReview"/);
  assert.match(actionSource, /action\.kind === PendingActionKind\.WORKFLOW_APPROVAL/);
  assert.match(actionSource, /review=\{action\.review\}/);
  assert.match(actionSource, /reviewTitle=\{action\.review_title\}/);
});

test("workflow decision labels preserve revise and acceptance semantics", () => {
  assert.match(actionSource, /normalized === "revise"/);
  assert.match(actionSource, /component\.chat_action_card\.revise/);
  assert.match(actionSource, /normalized === "accept"/);
  assert.match(actionSource, /component\.chat_action_card\.accept/);
});

test("workflow retry actions render editable recovery inputs", () => {
  assert.match(actionSource, /function WorkflowRetryCard/);
  assert.match(actionSource, /action\.kind === PendingActionKind\.WORKFLOW_RETRY/);
  assert.match(actionSource, /WorkflowSchemaFields/);
  assert.match(actionSource, /onResolve\("retry", undefined, \{ variables: parsed \}\)/);
  assert.match(actionSource, /action\.retry_from_step_id/);
  assert.match(actionSource, /const observedProblems = Array\.isArray/);
  assert.match(actionSource, /workflow-retry-card-problems/);
});

test("user-facing copy preserves operator-facing as a compound product term", () => {
  assert.doesNotMatch(taskDisplaySource, /\.replace\(\/\\boperator\\b\/gi, "you"\)/);
  assert.match(taskDisplaySource, /\.replace\(\/\\boperator\\b\(\?!-\)\/gi, "you"\)/);
});
