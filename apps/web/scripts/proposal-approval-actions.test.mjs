#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const chatActionCard = readFileSync(
  new URL("../src/components/ui/ChatActionCard.tsx", import.meta.url),
  "utf8",
);
const approvalOptions = readFileSync(
  new URL("../src/lib/approvalOptions.ts", import.meta.url),
  "utf8",
);
const strategistService = readFileSync(
  new URL("../../../packages/core/strategist/service.py", import.meta.url),
  "utf8",
);

test("proposal actions submit the shared canonical approval choices", () => {
  // Unified cohort list: "everything checked" is measured against every row
  // (tasks AND non-task items), not just the tasks.
  assert.match(
    chatActionCard,
    /if \(picked\.length === rows\.length\) \{\s+onResolve\(APPROVAL_CHOICE_APPROVE\);/,
  );
  assert.match(
    chatActionCard,
    /onResolve\(APPROVAL_CHOICE_ALWAYS_APPROVE\);/,
  );
  assert.match(
    chatActionCard,
    /onResolve\(\s*APPROVAL_CHOICE_REJECT,\s*rejectComment\.trim\(\) \|\| undefined,\s*\{ reason_code: rejectReason \},\s*\)/,
  );
  assert.doesNotMatch(chatActionCard, /onResolve\("approve_all"\)/);
  assert.doesNotMatch(chatActionCard, /onResolve\("reject_all"\)/);
});

test("partial approval sends both halves of the unified selection", () => {
  assert.match(
    chatActionCard,
    /onResolve\("approve_selected", undefined, \{\s+selected_task_ids: picked\.filter\(\(row\) => row\.isTask\)\.map\(\(row\) => row\.id\),\s+selected_item_ids: picked\.filter\(\(row\) => !row\.isTask\)\.map\(\(row\) => row\.id\),/,
  );
  // Select-all / clear operate over every row, not just the tasks.
  assert.match(chatActionCard, /const selectAll = \(\) => updateSelected\(new Set\(rowIds\)\);/);
});

test("resolved proposal labels every row instead of summarizing only a count", () => {
  assert.match(chatActionCard, /export function ProposalResolution/);
  assert.match(chatActionCard, /rows\.map\(\(row\) =>/);
  assert.match(chatActionCard, /const approved = approvedIds\.has\(row\.id\)/);
  assert.match(chatActionCard, /chat-proposal-resolution-title">\{row\.label\}/);
  assert.doesNotMatch(chatActionCard, /proposal_approved_count/);
  assert.match(
    chatActionCard,
    /setLocalResolution\(\{ choice, note, payload \}\)/,
  );
});

test("proposal payload advertises approve, always approve, and reject", () => {
  assert.match(
    approvalOptions,
    /DEFAULT_APPROVAL_OPTIONS = \[\s+APPROVAL_CHOICE_APPROVE,\s+APPROVAL_CHOICE_ALWAYS_APPROVE,\s+APPROVAL_CHOICE_REJECT,/,
  );
  assert.match(
    strategistService,
    /"options": list\(DEFAULT_APPROVAL_OPTIONS\)/,
  );
});
