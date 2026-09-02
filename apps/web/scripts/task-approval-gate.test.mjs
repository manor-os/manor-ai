#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const taskDetail = readFileSync(
  new URL("../src/pages/TaskDetail.tsx", import.meta.url),
  "utf8",
);
const taskTypes = readFileSync(
  new URL("../src/lib/taskTypes.ts", import.meta.url),
  "utf8",
);
const pendingKinds = readFileSync(
  new URL("../src/lib/pendingActionKinds.ts", import.meta.url),
  "utf8",
);
const chatActionCard = readFileSync(
  new URL("../src/components/ui/ChatActionCard.tsx", import.meta.url),
  "utf8",
);
const styles = readFileSync(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);

test("approval gate requires the explicit canonical task enum", () => {
  assert.match(taskTypes, /export enum TaskType \{/);
  assert.match(taskTypes, /APPROVAL = ["']approval["']/);
  assert.match(taskTypes, /return value === TaskType\.APPROVAL;/);
  assert.doesNotMatch(taskTypes, /includes\(/);
});

test("task detail does not infer approval behavior from user-authored prose", () => {
  assert.match(taskDetail, /const isApprovalTask = isApprovalTaskType\(task\.task_type\);/);
  assert.match(taskDetail, /const showTaskApproval = isApprovalTask;/);
  assert.doesNotMatch(taskDetail, /title[^;\n]*includes\(["']approval["']\)/);
  assert.doesNotMatch(taskDetail, /instructions[^;\n]*includes\(["']approval["']\)/);
});

test("only resolved Plan decision state can expose task recovery controls", () => {
  assert.match(
    taskDetail,
    /const actionableInputRequest = canResumePendingInput \? pendingInputRequest : null;/,
  );
  assert.match(
    taskDetail,
    /const showTaskRecoveryPanel = planDecisionStateResolved\s*&& hasTaskRecoveryOrigin\s*&& !actionableInputRequest\s*&& \(!hasPendingTypedDecision \|\| canResumePendingInput\);/,
  );
  assert.match(taskDetail, /hasPendingInput=\{!!actionableInputRequest\}/);
});

test("task approval and recovery are first-class Workspace Chat HITL cards", () => {
  assert.match(pendingKinds, /TASK_APPROVAL: ["']task_approval["']/);
  assert.match(pendingKinds, /TASK_RECOVERY: ["']task_recovery["']/);
  assert.match(chatActionCard, /PendingActionKind\.TASK_APPROVAL/);
  assert.match(chatActionCard, /normalized === ["']request_changes["']/);
  assert.match(chatActionCard, /isErrorHitlCard\(action\.hitl_type\)/);
});

test("task HITL cards use the shared neutral card treatment", () => {
  assert.doesNotMatch(chatActionCard, /chat-hitl-summary--error/);
  assert.doesNotMatch(styles, /\.chat-hitl-summary--error/);
  assert.match(
    chatActionCard,
    /normalized === ["']cancel["'][\s\S]*?return ["']secondary["']/,
  );
});

test("task recovery uses the shared textarea primitive", () => {
  assert.match(chatActionCard, /import Textarea from ["']\.\/Textarea["']/);
  assert.match(
    chatActionCard,
    /<Textarea[\s\S]*?ariaLabel=\{t\(["']component\.chat_action_card\.retry_guidance_placeholder["']\)\}[\s\S]*?className=["']chat-hitl-retry-guidance["']\s*\/>/,
  );
});
