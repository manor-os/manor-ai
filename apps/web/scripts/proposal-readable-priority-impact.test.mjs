#!/usr/bin/env node
/**
 * The proposal card renders priority and predicted impact from the typed
 * payload the backend sends — never by parsing the message body back.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");

const proposalDisplay = read("../src/lib/proposalDisplay.ts");
const chatActionCard = read("../src/components/ui/ChatActionCard.tsx");
const workspaceChat = read("../src/components/WorkspaceChat.tsx");
const styles = read("../src/index.css");
const strategistService = read("../../../packages/core/strategist/service.py");
const en = read("../src/lib/i18n/en.ts");
const zh = read("../src/lib/i18n/zh.ts");
const es = read("../src/lib/i18n/es.ts");

test("the backend sends typed per-task entries, not just titles", () => {
  // pending_action carries the structured list alongside the legacy titles…
  assert.match(strategistService, /"tasks": task_entries,/);
  assert.match(strategistService, /"task_titles": \[t\.title for t in proposal\.tasks\],/);
  // …and meta carries it for the cards that have no pending_action at all
  // (auto-approved / policy-denied).
  assert.match(
    strategistService,
    /"proposal": \{\s+"review_id": proposal\.review_id,\s+"summary": proposal\.summary,\s+"notes": proposal\.notes,\s+"tasks": task_entries,/,
  );
  // Impact keys ship together or not at all.
  assert.match(
    strategistService,
    /if goal and impact\.metric_delta is not None:\s+entry\["goal_id"\]/,
  );
  // Rationale evidence travels with the same typed task entry.
  assert.match(
    strategistService,
    /if pt\.basis:\s+entry\["basis"\] = pt\.basis\.model_dump\(mode="json"\)/,
  );
});

test("open proposal approvals explain their basis without exposing raw ids", () => {
  // Proposal approvals bypass only the generic raw-body suppression. Their
  // content still goes through ProposalMessageContent's typed payload.
  assert.match(
    workspaceChat,
    /msg\.message_kind === "proposal" &&\s+msg\.pending_action\.kind === PendingActionKind\.APPROVE_PROPOSALS/,
  );
  assert.match(workspaceChat, /<p>\{proposal\.summary\}<\/p>/);
  assert.match(workspaceChat, /formatUserFacingText\(task\.rationale\)/);
  assert.match(workspaceChat, /<details className="workspace-proposal-basis">/);
  assert.match(workspaceChat, /const basis = proposalBasisView\(task\);/);
  assert.match(workspaceChat, /basis\.sources\.join\(" · "\)/);
  assert.match(workspaceChat, /formatUserFacingText\(signal\)/);
  assert.doesNotMatch(workspaceChat, /evidenceRefs\.join|reportRefs\.join/);
  assert.match(workspaceChat, /proposal\.notes\.slice\(0, 5\)/);
  assert.match(
    workspaceChat,
    /footnotes\.map[\s\S]*<details className="workspace-proposal-notes">[\s\S]*proposal_context/,
  );
  assert.match(
    workspaceChat,
    /proposal\.notes\.length > 0 && !hasProposalTasks[\s\S]*blocking_reasons/,
  );
  assert.match(
    workspaceChat,
    /tasks: Array\.isArray\(proposal\.tasks\) && proposal\.tasks\.length > 0[\s\S]*\? proposal\.tasks[\s\S]*: actionTasks/,
  );

  // The typed parser preserves the evidence refs; empty bases stay hidden.
  assert.match(proposalDisplay, /basis: reportRefs\.length \|\| evidenceRefs\.length/);
  assert.match(proposalDisplay, /export function proposalBasisView/);
  assert.match(proposalDisplay, /basis_source_activity/);
  assert.match(strategistService, /def _proposal_basis_display/);
  assert.match(strategistService, /observation\.get\("description"\)/);
  assert.match(styles, /\.workspace-proposal-basis summary:focus-visible/);
  assert.match(styles, /\.workspace-proposal-notes summary:focus-visible/);
  assert.match(styles, /@media \(max-width: 520px\)[\s\S]*\.workspace-proposal-basis-row/);
  for (const locale of [en, zh, es]) {
    assert.match(locale, /"component\.workspace_chat\.proposal_basis":/);
    assert.match(locale, /"component\.workspace_chat\.proposal_basis_reports":/);
    assert.match(locale, /"component\.workspace_chat\.proposal_basis_evidence":/);
    assert.match(locale, /"component\.proposal\.basis_domain_execution":/);
    assert.match(locale, /"component\.proposal\.basis_source_activity":/);
  }
});

test("priority maps through an explicit lookup, not arithmetic", () => {
  assert.match(
    proposalDisplay,
    /const PRIORITY_I18N_KEYS: Record<number, string> = \{\s+5: "component\.proposal\.priority_critical",\s+4: "component\.proposal\.priority_high",\s+3: "component\.proposal\.priority_medium",\s+2: "component\.proposal\.priority_low",\s+1: "component\.proposal\.priority_minimal",/,
  );
  // Only above-default urgency earns a chip.
  assert.match(proposalDisplay, /const PROMINENT_PRIORITIES = new Set\(\[5, 4\]\);/);
  assert.match(
    proposalDisplay,
    /if \(typeof priority !== "number" \|\| !PROMINENT_PRIORITIES\.has\(priority\)\) \{\s+return null;/,
  );
});

test("expected impact names the goal, with a neutral fallback", () => {
  assert.match(proposalDisplay, /t\("component\.proposal\.expected_impact_plain"\)/);
  assert.match(
    proposalDisplay,
    /t\("component\.proposal\.expected_impact"\)\s+\.replace\("\{delta\}", delta\)\s+\.replace\("\{goal\}", subject\)/,
  );
  // No delta → no phrase; a bare number is never rendered.
  assert.match(
    proposalDisplay,
    /if \(typeof entry\.metric_delta !== "number"\) return null;/,
  );
  for (const locale of [en, zh]) {
    assert.match(locale, /"component\.proposal\.priority_high":/);
    assert.match(locale, /"component\.proposal\.expected_impact":/);
    assert.match(locale, /"component\.proposal\.expected_impact_plain":/);
    assert.match(locale, /"component\.proposal\.expected_impact_hint":/);
  }
});

test("both cards read the structured payload", () => {
  // Pending approval rows.
  assert.match(chatActionCard, /const entries = proposalTaskEntries\(action\?\.tasks\);/);
  assert.match(chatActionCard, /priorityLabel: entry \? proposalPriorityLabel\(entry\.priority\) : null,/);
  assert.match(chatActionCard, /impact: entry \? proposalImpactLabel\(entry\) : null,/);
  // Rendered message body card.
  assert.match(
    workspaceChat,
    /const structuredTasks = proposalTaskEntries\(structured\?\.tasks\);/,
  );
  assert.match(
    workspaceChat,
    /const priorityLabel = proposalPriorityLabel\(task\.priority\);\s+const impact = proposalImpactLabel\(task\);/,
  );
  // In Workspace Chat, the approval checkbox lives on the Proposed Work row;
  // ProposalCard keeps only the action buttons, so the task title is not
  // repeated below the proposal.
  assert.match(workspaceChat, /function ProposalSelectionToggle/);
  assert.match(workspaceChat, /proposalRowsRenderedElsewhere=\{proposalRowsRenderedInline\}/);
  assert.match(chatActionCard, /rows\.length > 0 && !rowsRenderedElsewhere/);
  assert.match(chatActionCard, /selectedRowIds=\{proposalSelectedRowIds\}/);
  assert.match(styles, /\.workspace-proposal-task\.is-selectable/);
  assert.match(styles, /\.chat-proposal-actions-card \.chat-hitl-btn-secondary/);
  assert.match(styles, /\.chat-proposal-actions-card \.chat-hitl-btn-danger \{\s+color: #9a6a64;/);
  const proposalStyles = styles.slice(
    styles.indexOf(".workspace-proposal-card"),
    styles.indexOf("/* User bubble */"),
  );
  assert.doesNotMatch(proposalStyles, /#292524|#1c1917|#6f6861|#79716b/);
  // The numeric priority never lands in the UI as a bare rank badge.
  assert.doesNotMatch(
    workspaceChat,
    /className="workspace-proposal-task-rank">\s*\{task\.rank \|\| index \+ 1\}\s*<\/div>\s+<div className="workspace-proposal-task-body">\s+<div className="workspace-proposal-task-title-row">\s+<span className="workspace-proposal-task-title">\s+\{formatUserFacingText/,
  );
});

test("the explainer says the number is a prediction that gets checked", () => {
  assert.match(chatActionCard, /title=\{proposalImpactExplainer\(\)\}/);
  assert.match(workspaceChat, /title=\{proposalImpactExplainer\(\)\}/);
  assert.match(en, /"component\.proposal\.expected_impact_hint":\s*\n?\s*"The Strategist's own prediction/);
});

test("the legacy prose parser is only the historical-card fallback", () => {
  // Exactly one call site, in the branch taken when no structured payload
  // is present.
  const calls = workspaceChat.match(/parseWorkspaceProposal\(content\)/g) || [];
  assert.equal(calls.length, 1);
  assert.match(
    workspaceChat,
    /:\s*\/\/ `parseWorkspaceProposal` survives for ONE reason: proposal cards\s+\/\/ posted before the structured payload shipped still have to render\./,
  );
  assert.match(workspaceChat, /const legacyTasks = structured \? \[\] : proposal\?\.tasks \|\| \[\];/);
});

test("resolved workspace proposals mark each proposed row inline", () => {
  assert.match(proposalDisplay, /export function proposalApprovedRowIds/);
  assert.match(workspaceChat, /function ProposalDecisionStatus/);
  assert.match(
    workspaceChat,
    /approved=\{approvedRowIds\.has\(structuredTaskRowIds\[index\]\)\}/,
  );
  assert.match(
    workspaceChat,
    /approved=\{approvedRowIds\.has\(itemRowIds\[index\]\)\}/,
  );
  assert.match(workspaceChat, /const inlineProposalDecision = Boolean\(/);
  assert.match(workspaceChat, /\{!inlineProposalDecision &&\s+\(\(msg\.pending_action/);
  assert.match(workspaceChat, /action=\{displayPendingAction\}/);
  assert.match(workspaceChat, /resolution=\{msg\.resolution\}/);
  assert.match(styles, /\.workspace-proposal-decision\.is-approved/);
  for (const locale of [en, zh, es]) {
    assert.match(locale, /"component\.chat_action_card\.approved_by":/);
  }
});
