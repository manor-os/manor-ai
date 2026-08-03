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
const strategistService = read("../../../packages/core/strategist/service.py");
const en = read("../src/lib/i18n/en.ts");
const zh = read("../src/lib/i18n/zh.ts");

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
