#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const taskDetailSource = await readFile(
  new URL("../src/pages/TaskDetail.tsx", import.meta.url),
  "utf8",
);
const workspaceChatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const tasksSource = await readFile(
  new URL("../src/pages/Tasks.tsx", import.meta.url),
  "utf8",
);
const taskPropertiesSource = await readFile(
  new URL("../src/components/task/TaskPropertiesPanel.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const stylesheetSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);
const userAvatarSource = await readFile(
  new URL("../src/components/ui/UserAvatar.tsx", import.meta.url),
  "utf8",
);

test("interactive Tasks present their existing Task conversation as a Task Session", () => {
  assert.match(taskDetailSource, /taskSession=\{isInteractiveTask \? \{/);
  assert.match(taskDetailSource, /threadRef=\{\{ kind: "task", id: task\.id \}\}/);
  assert.match(taskDetailSource, /workspace=\{taskWorkspace\}/);
  assert.match(taskDetailSource, /objective: interactiveSessionObjective \|\| undefined/);
  assert.match(taskDetailSource, /phases: interactiveSessionPhases/);
});

test("Task Session presentation removes Workspace group-chat chrome", () => {
  assert.match(workspaceChatSource, /interface TaskSessionPresentation/);
  assert.match(workspaceChatSource, /className="task-session-chat-header"/);
  assert.match(workspaceChatSource, /\{!isTaskSession && \(\s*<div className="embedded-chat-header/);
  assert.match(workspaceChatSource, /\{!isTaskSession && \(workspaceAgentsLoading \?/);
  assert.match(workspaceChatSource, /\{!isTaskSession && \(\s*<WorkspaceWorkflowRunHost/);
});

test("interactive Tasks launch their conversation in a full-screen session", () => {
  assert.match(taskDetailSource, /createPortal/);
  assert.match(taskDetailSource, /taskSessionFullscreen/);
  assert.match(taskDetailSource, /className="btn-manor task-session-open-button"/);
  assert.match(taskDetailSource, /className="task-detail-task-session"/);
  assert.match(taskDetailSource, /className="task-session-fullscreen"/);
  assert.match(taskDetailSource, /className="task-session-fullscreen-chat"/);
  assert.match(taskDetailSource, /appRoot\?\.setAttribute\("inert", ""\)/);
  assert.match(taskDetailSource, /if \(event\.defaultPrevented\) return/);
  assert.match(taskDetailSource, /if \(event\.key !== "Escape"\) return/);
  assert.match(taskDetailSource, /const taskSessionCanRender = Boolean\(/);
  assert.match(
    taskDetailSource,
    /if \(!taskSessionCanRender\) \{[\s\S]{0,160}setTaskSessionFullscreen\(false\)/,
  );
  assert.ok(
    taskDetailSource.indexOf("const taskSessionCanRender = Boolean(")
      < taskDetailSource.indexOf("/* Loading / error */"),
    "Task Session readiness must be checked before Task Detail can return an error shell",
  );
  assert.doesNotMatch(taskDetailSource, /task-detail-task-session-frame/);
  assert.match(workspaceChatSource, /<details\s+className="task-session-chat-header"/);
  assert.match(workspaceChatSource, /className="task-session-chat-summary"/);
  assert.match(stylesheetSource, /\.task-detail-task-session\s*\{[\s\S]{0,120}order: -2/);
  assert.match(stylesheetSource, /\.task-session-fullscreen\s*\{[\s\S]{0,80}position: fixed;[\s\S]{0,40}inset: 0/);
  assert.doesNotMatch(stylesheetSource, /\.task-detail-task-session-frame/);
});

test("Task Session restores focus to the launcher that opened it", () => {
  assert.match(taskDetailSource, /taskSessionLauncherRef\.current = event\.currentTarget/);
  assert.equal([...taskDetailSource.matchAll(/onClick=\{openTaskSession\}/g)].length, 1);
  assert.doesNotMatch(taskDetailSource, /ref=\{taskSessionLauncherRef\}/);
});

test("Task Session exposes one primary launcher and a neutral summary card", () => {
  assert.doesNotMatch(taskDetailSource, /task-session-launch-action/);
  assert.doesNotMatch(stylesheetSource, /\.task-session-launch-action/);
  assert.match(taskDetailSource, /className="task-session-launch-card"/);
});

test("full-screen Task Session traps focus with the shared dialog behavior", () => {
  assert.match(taskDetailSource, /trapDialogTabKey/);
  assert.match(taskDetailSource, /const taskSessionDialogRef = useRef<HTMLDivElement>/);
  assert.match(taskDetailSource, /trapDialogTabKey\(event, taskSessionDialogRef\.current\)/);
  assert.match(taskDetailSource, /ref=\{taskSessionDialogRef\}/);
  assert.match(taskDetailSource, /tabIndex=\{-1\}/);
});

test("Task Session composer is scoped to replying to the host", () => {
  assert.match(workspaceChatSource, /replaceActionButtons=\{isTaskSession \|\| chatMode !== "auto"\}/);
  assert.match(workspaceChatSource, /mentions=\{isTaskSession \? \[\] : mentionOptions\}/);
  assert.match(workspaceChatSource, /workflows=\{isTaskSession \? \[\] : workflowInvokeOptions\}/);
  assert.match(workspaceChatSource, /page\.task_detail\.session_reply_placeholder/);
});

test("Task Session submissions continue the conversation restored from history", () => {
  assert.match(
    workspaceChatSource,
    /const activeConversationId = conversationId \|\| wsConversationId \|\| undefined/,
  );
  assert.match(
    workspaceChatSource,
    /api\.chat\.stream\([\s\S]{0,220}activeConversationId/,
  );
  assert.match(
    workspaceChatSource,
    /activeConversationId,[\s\S]{0,120}initialMessages/,
  );
});

test("Task Session suppresses only its own Task reference without removing message refs", () => {
  assert.match(
    workspaceChatSource,
    /suppressedTaskReferenceId=\{isTaskSession \? threadRef\?\.id : undefined\}/,
  );
  assert.match(workspaceChatSource, /suppressedTaskReferenceId\?: string/);
  assert.match(
    workspaceChatSource,
    /\.filter\(\(\{ ref \}\) => ref\.id !== suppressedTaskReferenceId\)/,
  );
  assert.match(
    workspaceChatSource,
    /taskId !== suppressedTaskReferenceId/,
  );
  assert.doesNotMatch(workspaceChatSource, /hideTaskReferences/);
});

test("Task Session uses the backend-resolved Host identity", () => {
  assert.match(taskDetailSource, /task\.session_host_available === true/);
  assert.doesNotMatch(taskDetailSource, /task\.session_host_name \|\| assigneeLabel/);
  assert.doesNotMatch(taskDetailSource, /task\.session_host_avatar \|\| assigneeAvatarUrl/);
  assert.match(taskDetailSource, /hostName: sessionHostLabel/);
  assert.match(taskDetailSource, /hostAvailable: sessionHostAvailable/);
  assert.match(taskDetailSource, /isMasterAgent\(task\.session_host_agent_id\)/);
  assert.match(
    workspaceChatSource,
    /disabled=\{composerDisabled \|\| taskSession\?\.hostAvailable === false\}/,
  );
  assert.match(
    workspaceChatSource,
    /const resolvedAgent = isTaskSession\s*\?\s*null\s*:\s*isResponseSurfaceSubmission\s*\?\s*null\s*:\s*resolveInlineMention\(rawText\)/,
  );
  assert.match(
    workspaceChatSource,
    /const targetName = isTaskSession\s*\?\s*taskSession\?\.hostName/,
  );
});

test("interactive Task creation selects an exact Agent subscription", () => {
  assert.match(apiSource, /subscriptions: Array<\{/);
  assert.match(tasksSource, /formSelectedAgentSubscriptionId/);
  assert.match(tasksSource, /payload\.owner_subscription_id = formSelectedAgentSubscriptionId/);
  assert.match(tasksSource, /key=\{option\.key\}/);
  assert.match(
    tasksSource,
    /formAssigneeTab === "agent"[\s\S]{0,100}!formSelectedAgentSubscriptionId/,
  );
  assert.match(
    tasksSource,
    /option\.value === TaskType\.INTERACTIVE[\s\S]{0,160}formTaskType !== TaskType\.INTERACTIVE[\s\S]{0,220}setFormSelectedAgentSubscriptionId\(""\)/,
  );
});

test("interactive Task reassignment replaces the exact Host subscription", () => {
  assert.match(taskDetailSource, /workspaceAgentSubscriptions=\{workspaceAssignableAgents\?\.subscriptions/);
  assert.match(tasksSource, /workspaceAgentSubscriptions=\{workspaceAssignableAgents\?\.subscriptions/);
  assert.match(taskPropertiesSource, /owner_subscription_id: subscription\.id/);
  assert.match(taskPropertiesSource, /owner_service_key: ""/);
  assert.match(taskPropertiesSource, /const options = isSessionHostPicker[\s\S]{0,140}\? sessionOptions/);
  assert.match(workspaceChatSource, /taskSession\?\.hostAvailable === false/);
  assert.match(
    taskDetailSource,
    /session_host_available/,
  );
});

test("interactive Task keeps its Host and human assignee as separate properties", () => {
  const sessionOptionsStart = taskPropertiesSource.indexOf("const sessionOptions: Option[]");
  const humanOptionsStart = taskPropertiesSource.indexOf("const humanAssigneeOptions: Option[]");
  const sessionOptionsSource = taskPropertiesSource.slice(sessionOptionsStart, humanOptionsStart);
  assert.doesNotMatch(sessionOptionsSource, /assignee_id/);
  assert.match(taskPropertiesSource, /mode="session-host"/);
  assert.match(taskPropertiesSource, /mode=\{isTaskSession \? "human-assignee" : "assignment"\}/);
  assert.match(tasksSource, /formTaskType === TaskType\.INTERACTIVE[\s\S]{0,900}payload\.assignee_id = formSelectedStaffUserId/);
  assert.match(taskDetailSource, /const humanAssigneeLabel = friendlyPersonName\(task\.assignee_name/);
  assert.match(
    taskDetailSource,
    /task\.assignee_id && humanAssigneeLabel[\s\S]{0,500}name=\{humanAssigneeLabel\}[\s\S]{0,240}<span>\{humanAssigneeLabel\}<\/span>/,
  );
});

test("UserAvatar delegates Manor identity to the canonical avatar", () => {
  assert.match(userAvatarSource, /import ManorAvatar from "\.\/ManorAvatar"/);
  assert.match(userAvatarSource, /return <ManorAvatar size=\{size\} style=\{style\} \/>/);
  assert.doesNotMatch(userAvatarSource, /#5d7f77/);
});

test("Task properties fail closed when the session Host projection is unavailable", () => {
  assert.match(taskPropertiesSource, /const isSessionManor = Boolean\(/);
  assert.match(taskPropertiesSource, /task\.session_host_available === true/);
  assert.match(taskPropertiesSource, /isMasterAgent\(task\.session_host_agent_id\)/);
  assert.match(taskPropertiesSource, /const rawDisplayName = isSessionHostPicker/);
  assert.match(taskPropertiesSource, /if \(o\.id === "__manor__"\) return isSessionHostPicker \? isSessionManor : isManor/);
});
