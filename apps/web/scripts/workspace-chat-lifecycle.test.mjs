#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const anchoredPopoverSource = await readFile(
  new URL("../src/components/ui/AnchoredPopover.tsx", import.meta.url),
  "utf8",
);

test("workspace chat exposes a permission-gated pause and resume control", () => {
  assert.match(workspaceChatSource, /canManageWorkspace\(currentUser, workspaceStaff \|\| \[\]\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.staff\.list\(workspaceId\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.pause\(workspaceId, transitionId\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.resume\(/);
  assert.match(workspaceChatSource, /className="btn-manor-ghost workspace-chat-lifecycle-trigger"/);
  assert.match(workspaceChatSource, /ariaLabel=\{workspaceLifecycleActionLabel\}/);
  assert.match(workspaceChatSource, /pause_workspace_runtime/);
  assert.match(workspaceChatSource, /start_workspace_runtime/);
  assert.match(workspaceChatSource, /workspace_runtime_starting/);
  assert.match(workspaceChatSource, /workspace_runtime_pausing/);
  assert.match(workspaceChatSource, /workspace_lifecycle: true/);
  assert.match(workspaceChatSource, /workspace_lifecycle_action/);
  assert.match(workspaceChatSource, /local-workspace-lifecycle-/);
  assert.match(workspaceChatSource, /workspaceLifecycleTransitionIdRef/);
  assert.match(workspaceChatSource, /lifecycle_message_id/);
  assert.match(workspaceChatSource, /timelineKey/);
  assert.match(workspaceChatSource, /workspace-chat", targetWorkspaceId/);
  assert.match(workspaceChatSource, /existingIndex/);
  assert.match(workspaceChatSource, /lifecycleGlyph/);
  assert.match(workspaceChatSource, /workspace\?\.status === "active" && workspace\.heartbeat_enabled/);
  assert.match(workspaceChatSource, /data-state=\{autonomousRunning \? "active" : "paused"\}/);
  assert.match(workspaceChatSource, /<IconPause size=\{17\} \/>/);
  assert.match(workspaceChatSource, /<IconPlay size=\{17\} \/>/);
});

test("workspace lifecycle hover panel lists and independently edits Goals in running and paused states", () => {
  assert.match(workspaceChatSource, /<AnchoredPopover[\s\S]*workspace-chat-autonomy-popover/);
  assert.match(workspaceChatSource, /openOnHover/);
  assert.match(workspaceChatSource, /onOpenChange=\{\(open\) => \{[\s\S]*workspaceAutonomyGoalsQuery\.refetch\(\)/);
  assert.match(workspaceChatSource, /className="workspace-chat-autonomy-panel"/);
  assert.match(workspaceChatSource, /className="workspace-chat-autonomy-header"/);
  assert.match(workspaceChatSource, /className="workspace-chat-autonomy-list" role="list"/);
  assert.match(workspaceChatSource, /className="workspace-chat-autonomy-editor"/);
  assert.match(workspaceChatSource, /workspaceAutonomyGoalsQuery/);
  assert.match(workspaceChatSource, /enabled: Boolean\(canToggleWorkspace\)/);
  assert.match(workspaceChatSource, /saveWorkspaceAutonomyGoal/);
  assert.match(workspaceChatSource, /api\.goals\.create\(\{ workspace_id: workspaceId, title, target_value: 1 \}\)/);
  assert.match(workspaceChatSource, /api\.goals\.update\(editor\.id, \{ title \}\)/);
  assert.match(workspaceChatSource, /api\.goals\.delete\(goal\.id\)/);
  assert.match(workspaceChatSource, /<ConfirmDialog[\s\S]*delete_goal_confirmation/);
  assert.match(workspaceChatSource, /<IconTrash size=\{15\} \/>/);
  assert.match(workspaceChatSource, /autonomy_goal_edit_label/);
  assert.match(workspaceChatSource, /edit_goal_named/);
  assert.match(workspaceChatSource, /closeAutonomyGoalEditor/);
  assert.match(workspaceChatSource, /autonomyGoalEditButtonRefs/);
  assert.match(workspaceChatSource, /autonomyGoalAddButtonRef/);
  assert.match(workspaceChatSource, /withGoal: workspaceAutonomyGoalsQuery\.isSuccess[\s\S]*workspaceAutonomyGoals\.length > 0[\s\S]*: undefined/);
  assert.match(workspaceChatSource, /className="workspace-chat-autonomy-start"[\s\S]*disabled=\{\s*Boolean\(autonomyGoalEditor\)/);
  assert.match(workspaceChatSource, /api\.workspaces\.resume\(workspaceId, transitionId\)/);
  assert.match(workspaceChatSource, /runWorkspaceLifecycleFromPanel/);
  assert.match(workspaceChatSource, /autonomousRunning \? <IconPause size=\{14\} \/> : <IconPlay size=\{14\} \/>/);
  assert.doesNotMatch(workspaceChatSource, /type AutonomyGoalMode/);
  assert.doesNotMatch(workspaceChatSource, /goalDraft/);
  assert.doesNotMatch(workspaceChatSource, /use_goals:/);
  assert.doesNotMatch(workspaceChatSource, /<RadioCard/);
  assert.match(workspaceChatSource, /<Input/);
  assert.match(apiSource, /goal\?: \{ title: string; target_value: number \}/);
  assert.match(apiSource, /request<void>\(`\/goals\/\$\{id\}`, \{ method: "DELETE" \}\)/);
  assert.doesNotMatch(apiSource, /use_goals\?: boolean/);
  assert.doesNotMatch(apiSource, /resume\$\{options \? "\/v2" : ""\}/);
  assert.match(stylesSource, /\.workspace-chat-autonomy-panel \{/);
  assert.match(stylesSource, /\.workspace-chat-autonomy-state \{[\s\S]*min-height: 120px/);
  assert.match(stylesSource, /\.workspace-chat-autonomy-row \{/);
  assert.match(stylesSource, /\.workspace-chat-autonomy-row-action:focus-visible/);
  assert.match(anchoredPopoverSource, /onMouseEnter=\{openOnHover \? openFromHover : undefined\}/);
  assert.match(anchoredPopoverSource, /onMouseLeave=\{openOnHover \? closeAfterHover : undefined\}/);
  assert.doesNotMatch(anchoredPopoverSource, /onFocus=\{openOnHover \? openFromHover : undefined\}/);
  assert.match(anchoredPopoverSource, /requestAnimationFrame[\s\S]*querySelector<HTMLElement>/);
  assert.match(anchoredPopoverSource, /interactionPinnedRef/);
  assert.match(anchoredPopoverSource, /onFocusCapture=\{openOnHover/);
  assert.match(anchoredPopoverSource, /onMouseDown=\{openOnHover/);
});

test("workspace chat lifecycle changes refresh shared workspace state", () => {
  assert.match(workspaceChatSource, /setQueryData<Workspace\[\]>\(\["workspaces"\]/);
  assert.match(workspaceChatSource, /invalidateQueries\(\{ queryKey: \["workspace-heartbeat", targetWorkspaceId\] \}\)/);
  assert.match(workspaceChatSource, /invalidateQueries\(\{ queryKey: \["workspace-activity", targetWorkspaceId\] \}\)/);
  assert.match(workspaceChatSource, /invalidateQueries\(\{ queryKey: \["workspace-chat", targetWorkspaceId\] \}\)/);
});

test("workspace chat lifecycle icon has hover and keyboard focus states", () => {
  assert.match(stylesSource, /\.workspace-chat-lifecycle-trigger\.btn-manor-ghost:hover:not\(:disabled\)/);
  assert.match(stylesSource, /\.workspace-chat-lifecycle-trigger\.btn-manor-ghost:focus-visible \{[\s\S]*var\(--accent-ring\)/);
  assert.match(stylesSource, /workspace-chat-lifecycle-pending/);
  assert.match(stylesSource, /workspace-chat-lifecycle-icon\[data-state="paused"\]/);
  assert.match(stylesSource, /ws-activity-line-row--workspace-lifecycle/);
  assert.match(stylesSource, /workspace-chat-lifecycle-start-icon/);
  assert.match(stylesSource, /workspace-chat-lifecycle-pause-icon/);
  assert.match(stylesSource, /workspace-chat-lifecycle-complete-icon/);
  assert.match(stylesSource, /data-lifecycle-action/);
  assert.match(stylesSource, /workspace-chat-lifecycle-icon::after/);
  assert.match(stylesSource, /width: min\(100%, 760px\)/);
  assert.match(stylesSource, /-webkit-line-clamp: 2/);
  assert.match(stylesSource, /prefers-reduced-motion/);
});
