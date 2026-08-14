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

test("Plan content review uses the typed review HITL route", () => {
  assert.match(executorSource, /PendingActionKind\.GOVERNANCE_APPROVAL\.value/);
  assert.match(executorSource, /"hitl_type": HitlType\.REVIEW\.value/);
  assert.match(executorSource, /"review": review_packet/);
  assert.match(executorSource, /"options": values\.get\("options"\) or \["approve", "reject"\]/);
});

test("Task and Workflow review HITL share the same renderer", () => {
  assert.match(actionCardSource, /action\.kind === "workflow_approval" \|\| action\.hitl_type === "review"/);
  assert.match(actionCardSource, /<WorkflowApprovalReview/);
  assert.doesNotMatch(actionCardSource, /ArtifactReviewCard/);
  assert.match(reviewSource, /<InlineFileReferenceCard/);
  assert.match(reviewSource, /review\.artifacts/);
  assert.match(reviewSource, /review\.files/);
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
  assert.match(viewerSource, /navigate\(knowledgeReturnTo \|\| "\/knowledge"\)/);
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
