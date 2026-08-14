import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

test("workflow approvals expose review material and concrete workflow identity", () => {
  const source = read("../src/components/ui/ApprovalActionBar.tsx");
  const reviewSource = read("../src/components/workflows/WorkflowApprovalReview.tsx");
  const fileCardSource = read("../src/components/InlineFileReferenceCard.tsx");
  assert.match(source, /approval-action-bar__materials/);
  assert.match(source, /approval-action-bar__material-card--workflow/);
  assert.match(source, /approval-action-bar__material-card--review/);
  assert.match(source, /approval-action-bar__material-card--document/);
  assert.match(source, /workflowReviewArtifacts/);
  assert.match(source, /display="card"/);
  assert.match(fileCardSource, /display === "card"/);
  assert.match(fileCardSource, /<CompactCard/);
  assert.match(source, /label: "Operation"/);
  assert.match(source, /WorkflowApprovalReview/);
  assert.match(source, /approval-review-actions/);
  assert.match(source, /component\.approval_action_bar\.review_and_approve/);
  assert.match(reviewSource, /component\.approval_action_bar\.edit_then_approve/);
  assert.match(source, /choice !== "revise"/);
  assert.match(reviewSource, /component\.approval_action_bar\.open_current_link/);
  assert.match(source, /EditableWorkflowApprovalReview/);
  assert.match(reviewSource, /function reviewHref/);
  assert.match(reviewSource, /chat-workflow-review-link/);
});

test("the HITL bar is the unified queue for every pending chat approval", () => {
  const source = read("../src/components/ui/ApprovalActionBar.tsx");
  assert.match(source, /selectedApprovalId/);
  assert.match(source, /previous_pending/);
  assert.match(source, /next_pending/);
  assert.match(source, /pending_position/);
  assert.match(source, /chatMessageAnchorId/);
  assert.match(source, /sourceAnchorId/);
});

test("the HITL bar keeps review content before a shared, visually ranked action row", () => {
  const source = read("../src/components/ui/ApprovalActionBar.tsx");
  const css = read("../src/index.css");
  assert.match(source, /import Button from "\.\/Button"/);
  assert.ok(
    source.indexOf('className="approval-action-bar__body"') <
      source.indexOf('className="approval-action-bar__buttons"'),
  );
  assert.match(source, /option === "always_approve"\) return "standing"/);
  assert.match(
    source,
    /tone === "approve"[\s\S]*?"primary"[\s\S]*?: "outline"/,
  );
  assert.match(
    css,
    /\.approval-action-bar__title\s*\{[^}]*font-size:\s*13px[^}]*font-weight:\s*700/,
  );
  assert.doesNotMatch(
    css,
    /\.approval-action-bar__title\s*\{[^}]*text-transform:\s*uppercase/,
  );
  assert.match(
    css,
    /@media \(max-width:\s*520px\)[\s\S]*?\.approval-action-bar__buttons\s*\{[^}]*display:\s*grid[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/,
  );
});

test("opening a workflow returns through its page back control to the HITL message", () => {
  const source = read("../src/components/ui/ApprovalActionBar.tsx");
  const flowsSource = read("../src/pages/Flows.tsx");
  assert.match(source, /preserveReturnToInHistory\(sourceReturnTo\)/);
  assert.match(source, /chatReturnTo: sourceReturnTo/);
  assert.match(source, /returnTo: sourceReturnTo/);
  assert.match(source, /#\$\{currentItem\.sourceAnchorId\}/);
  assert.doesNotMatch(source, /view_in_conversation/);
  assert.match(flowsSource, /if \(workflowReturnTo\) \{[\s\S]*navigate\(workflowReturnTo\)/);
});

test("Always approve keeps its durable scope explanation in a hover and focus tooltip", () => {
  const source = read("../src/components/ui/ApprovalActionBar.tsx");
  const tooltipSource = read("../src/components/ui/Tooltip.tsx");
  assert.match(source, /always_approve_scope_workspace/);
  assert.match(source, /always_approve_scope_chat/);
  assert.match(source, /<Tooltip/);
  assert.match(source, /content=\{standingApprovalDescription\(current\)\}/);
  assert.doesNotMatch(source, /approval-action-bar__standing-scope/);
  assert.match(tooltipSource, /onFocusCapture=\{showImmediately\}/);
  assert.match(tooltipSource, /role="tooltip"/);
});

test("both chat surfaces expose stable message anchors for returning to a request", () => {
  const embeddedSource = read("../src/components/EmbeddedChat.tsx");
  const floatingSource = read("../src/components/FloatingChat.tsx");
  const messageRowSource = read("../src/components/chat/MessageRow.tsx");
  assert.match(embeddedSource, /id=\{messageAnchorId\}/);
  assert.match(floatingSource, /id=\{chatMessageAnchorId\(msg\.id, i\)\}/);
  assert.match(messageRowSource, /id=\{id\}/);
});

test("edited workflow review material is sent with the approval", () => {
  for (const path of [
    "../src/components/EmbeddedChat.tsx",
    "../src/components/FloatingChat.tsx",
  ]) {
    const source = read(path);
    assert.match(source, /payload: \{ review \}/);
  }
});

test("revisable external approvals collect instructions before resolving", () => {
  const source = read("../src/components/ui/ApprovalActionBar.tsx");
  const chatActionSource = read("../src/components/ui/ChatActionCard.tsx");
  assert.match(source, /option === "revise"/);
  assert.match(source, /openRevisionRequest\(current, onResolve, disabled\)/);
  assert.match(source, /onResolve\(hitl\.id, "revise", \{ revision_request: value \}\)/);
  assert.match(source, /component\.approval_action_bar\.revision_instructions/);
  assert.match(chatActionSource, /review: \{ revision_request: value \}/);
});

test("system workflow messages use the assistant avatar on both chat surfaces", () => {
  for (const path of [
    "../src/components/EmbeddedChat.tsx",
    "../src/components/FloatingChat.tsx",
  ]) {
    const source = read(path);
    assert.match(source, /role: \(m\.role === "user" \? "user" : "assistant"\)/);
  }
});

test("publication approval prompts do not render a duplicate message bubble", () => {
  for (const path of [
    "../src/components/EmbeddedChat.tsx",
    "../src/components/FloatingChat.tsx",
  ]) {
    const source = read(path);
    assert.match(source, /发布\|发表\|delete/);
    assert.match(source, /publish\|publicat\|post/);
    assert.match(
      source,
      /hasAssistantBlocks\s*&&\s*!canRetryFromContent\s*&&\s*!suppressApprovalBubble/,
    );
  }
});

test("resolved approval cards hide their redundant linked system receipt", () => {
  const streamSource = read("../src/lib/chatStream.ts");
  assert.match(streamSource, /isRedundantApprovalResolutionReceipt/);
  assert.match(streamSource, /message\.message_kind !== "system"/);
  assert.match(streamSource, /APPROVAL_RESOLUTION_RECEIPT_RE/);
  for (const path of [
    "../src/components/EmbeddedChat.tsx",
    "../src/components/FloatingChat.tsx",
  ]) {
    assert.match(read(path), /!isRedundantApprovalResolutionReceipt\(m\)/);
  }
  const workspaceSource = read("../src/components/WorkspaceChat.tsx");
  assert.match(workspaceSource, /!isRedundantApprovalResolutionReceipt\(\{/);
  assert.match(workspaceSource, /content: msg\.body/);
});
