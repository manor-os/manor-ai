import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const apiSource = await readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const chatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const hostSource = await readFile(
  new URL("../src/components/workflows/WorkspaceWorkflowRunHost.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const embeddedChatSource = await readFile(
  new URL("../src/components/EmbeddedChat.tsx", import.meta.url),
  "utf8",
);
const floatingChatSource = await readFile(
  new URL("../src/components/FloatingChat.tsx", import.meta.url),
  "utf8",
);
const actionSource = await readFile(
  new URL("../src/components/ui/ChatActionCard.tsx", import.meta.url),
  "utf8",
);
const schemaSource = await readFile(
  new URL("../src/components/workflows/WorkflowSchemaFields.tsx", import.meta.url),
  "utf8",
);
const schemaLogicSource = await readFile(
  new URL("../src/components/workflows/workflowSchema.ts", import.meta.url),
  "utf8",
);
const selectSource = await readFile(
  new URL("../src/components/ui/Select.tsx", import.meta.url),
  "utf8",
);
const cssSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");

test("workspace workflow starters use dedicated APIs without changing the main chat request", () => {
  assert.match(apiSource, /listEntrypoints:\s*\(wsId: string\)/);
  assert.match(apiSource, /streamEntrypoint:\s*async \(/);
  assert.match(apiSource, /\/chat\/entrypoints\/\$\{bindingId\}\/stream/);
  assert.match(apiSource, /streamEntrypoint:[\s\S]*?response\.status === 402[\s\S]*?useUpgradeStore\.getState\(\)\.show/);
  assert.doesNotMatch(apiSource, /form\.append\("workflow_binding_id"/);
  assert.doesNotMatch(apiSource, /form\.append\("workflow_intent_detection"/);
});

test("workspace chat explicitly selects one starter for one send", () => {
  assert.match(chatSource, /const \[selectedEntrypointId, setSelectedEntrypointId\] = useState/);
  assert.match(chatSource, /api\.workspaces\.chat\.listEntrypoints\(workspaceId\)/);
  assert.match(chatSource, /selectedEntrypointId[\s\S]*?api\.workspaces\.chat\.streamEntrypoint/);
  assert.match(chatSource, /setSelectedEntrypointId\(""\)/);
});

test("workspace workflow starters use a recordable accessible menu", () => {
  assert.match(chatSource, /import Select from "\.\/ui\/Select"/);
  assert.match(
    chatSource,
    /<Select[\s\S]*?ariaLabel=\{t\("component\.workspace_chat\.workflow_starter"\)\}/,
  );
  assert.doesNotMatch(
    chatSource,
    /<select[\s\S]*?aria-label=\{t\("component\.workspace_chat\.workflow_starter"\)\}/,
  );
  assert.match(selectSource, /aria-haspopup="listbox"/);
  assert.match(selectSource, /role="listbox"/);
  assert.match(selectSource, /role="option"/);
});

test("shared select keeps a portaled menu inside the viewport", () => {
  assert.match(
    selectSource,
    /const availableBelow = window\.innerHeight - r\.bottom - viewportPadding/,
  );
  assert.match(
    selectSource,
    /const availableAbove = r\.top - viewportPadding/,
  );
  assert.match(
    selectSource,
    /const openAbove = menuHeight > availableBelow && availableAbove > availableBelow/,
  );
  assert.match(selectSource, /maxHeight: coords\.maxHeight/);
});

test("workspace inline mentions resolve against the text being sent", () => {
  assert.match(chatSource, /function resolveInlineMention\(value: string\)/);
  assert.match(chatSource, /const resolvedAgent = resolveInlineMention\(rawText\)/);
  assert.match(
    chatSource,
    /atIdx === 0 \|\| \/\\s\/\.test\(val\[atIdx - 1\]\)/,
  );
  assert.match(
    chatSource,
    /atIdx > 0 && !\/\\s\/\.test\(val\[atIdx - 1\]\)/,
  );
  assert.doesNotMatch(chatSource, /function resolveInlineMention\(\)[\s\S]*?const val = input/);
  assert.doesNotMatch(chatSource, /val\[atIdx - 1\] !== " "/);
});

test("workflow input pending actions reuse the text response card", () => {
  assert.match(actionSource, /action\.kind === PendingActionKind\.WORKFLOW_STARTER_INPUT/);
  assert.match(actionSource, /WorkflowStarterInputCard/);
  assert.match(actionSource, /onResolve\("run", undefined, \{ inputs: parsed \}\)/);
  assert.match(actionSource, /previousResetTokenRef/);
  assert.match(chatSource, /actionResetToken=\{resolveMutation\.failureCount\}/);
  assert.match(actionSource, /action\.kind === PendingActionKind\.WORKFLOW_INPUT/);
  assert.match(actionSource, /HitlInputCard/);
  assert.match(actionSource, /localFiles\.length > 0 \? localFiles : undefined/);
  assert.match(chatSource, /api\.documents\.upload\(file\)/);
});

test("workflow starter forms preserve an explicitly cleared optional string", () => {
  assert.match(
    actionSource,
    /if \(type === "string"\) parsed\[input\.key\] = "";/,
  );
});

test("workflow starter forms hide internal context and use a checkbox for authorization", () => {
  assert.match(actionSource, /input\.hidden/);
  assert.match(actionSource, /type="checkbox"/);
  assert.doesNotMatch(actionSource, /<option value="false">False<\/option>/);
  assert.doesNotMatch(actionSource, /<option value="true">True<\/option>/);
});

test("workflow starter forms render structured JSON Schema inputs", () => {
  assert.match(actionSource, /WorkflowSchemaFields/);
  assert.match(actionSource, /input\.schema/);
  assert.match(schemaLogicSource, /workflowSchemaEntries/);
  assert.match(schemaLogicSource, /schema\["x-ui"\]\?\.order/);
  assert.match(schemaLogicSource, /schema\.format === "uri"/);
  assert.match(schemaLogicSource, /schema\.pattern/);
  assert.match(schemaLogicSource, /schema\.enum/);
  assert.match(schemaLogicSource, /schemaType === "array"/);
  assert.match(schemaSource, /type="number"/);
  assert.match(schemaSource, /workflow-starter-input-group/);
  assert.match(actionSource, /parseWorkflowSchemaDraft/);
  assert.match(actionSource, /action\.title \|\| t\("component\.workspace_chat\.workflow_inputs"\)/);
});

test("workflow starter renders mapped top-level schema inputs as one field grid", () => {
  assert.match(actionSource, /const structuredInputs = visibleInputs\.filter/);
  assert.match(
    actionSource,
    /const useUnifiedSchemaGrid = structuredInputs\.length > 0[\s\S]*?structuredInputs\.length === visibleInputs\.length/,
  );
  assert.match(actionSource, /properties: Object\.fromEntries/);
  assert.match(
    actionSource,
    /useUnifiedSchemaGrid && \([\s\S]*?<WorkflowSchemaFields[\s\S]*?value=\{values\}/,
  );
  assert.match(actionSource, /setWorkflowValueAtPath\(current, path, nextValue\)/);
});

test("mixed legacy workflow inputs keep their declared rendering order", () => {
  assert.match(
    actionSource,
    /!useUnifiedSchemaGrid && visibleInputs\.map\(\(input\) =>/,
  );
  assert.match(
    actionSource,
    /input\.schema && workflowSchemaType\(input\.schema\) === "object"/,
  );
});

test("structured workflow starter fields remain compact enough for one desktop viewport", () => {
  assert.match(
    cssSource,
    /\.workflow-starter-input-schema-fields\s*\{[\s\S]*?grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)/,
  );
  assert.doesNotMatch(
    cssSource,
    /\.workflow-starter-input-field:has\(textarea\)\s*\{\s*grid-column:\s*1\s*\/\s*-1/,
  );
  assert.match(schemaSource, /useState\(initiallyOpen\)/);
  assert.match(schemaSource, /open=\{open\}/);
  assert.match(schemaSource, /onToggle=\{\(event\) => setOpen\(event\.currentTarget\.open\)\}/);
  assert.doesNotMatch(schemaSource, /\sopen=\{!child\["x-ui"\]\?\.collapsed\}/);
  assert.match(
    cssSource,
    /\.workflow-starter-input-group\[open\]\s*\{[\s\S]*?display:\s*grid/,
  );
});

test("workspace chat delegates workflow runtime rows without filtering ordinary chat", () => {
  assert.match(chatSource, /buildWorkspaceWorkflowRunGroups\(sorted\)/);
  assert.match(chatSource, /workflowHostOwnedMessageIds\(workflowRunGroups\)/);
  assert.match(
    chatSource,
    /sorted\.filter\(\(msg\) => !hostOwnedWorkflowMessageIds\.has\(msg\.id\)\)/,
  );
  assert.match(hostSource, /WORKFLOW_HOST_ACTION_KINDS/);
  assert.match(hostSource, /"workflow_starter_input"/);
  assert.match(hostSource, /"workflow_retry"/);
  assert.match(hostSource, /"workflow_approval"/);
  assert.match(hostSource, /"workflow_input"/);
  assert.doesNotMatch(hostSource, /"governance_approval"/);
  assert.doesNotMatch(hostSource, /"agent_update"/);
  assert.doesNotMatch(hostSource, /"step_event"/);
});

test("generic pending-action count and oldest jump target exclude host-owned actions", () => {
  assert.match(
    chatSource,
    /sorted\.filter\(\(msg\)\s*=>\s*isOpenPendingAction\(msg\)\s*&&\s*!hostOwnedWorkflowMessageIds\.has\(msg\.id\)\s*\)/,
  );
  assert.match(chatSource, /const oldestPendingAction = pendingActions\[0\]/);
  assert.match(chatSource, /jumpToOldestPendingAction/);
});

test("dedicated workflow host is isolated to WorkspaceChat", () => {
  assert.match(chatSource, /from "\.\/workflows\/WorkspaceWorkflowRunHost"/);
  assert.doesNotMatch(embeddedChatSource, /WorkspaceWorkflowRunHost/);
  assert.doesNotMatch(floatingChatSource, /WorkspaceWorkflowRunHost/);
});

test("message-backed workflow interventions reuse WorkspaceChat resolution reset behavior", () => {
  assert.match(chatSource, /<WorkspaceWorkflowRunHost[\s\S]*?onResolveMessage=\{handleResolve\}/);
  assert.match(chatSource, /resolveMutation\.mutate\(/);
  assert.doesNotMatch(chatSource, /resolveMutation\.mutateAsync/);
  const hostProps = chatSource.slice(
    chatSource.indexOf("<WorkspaceWorkflowRunHost"),
    chatSource.indexOf("/>", chatSource.indexOf("<WorkspaceWorkflowRunHost")),
  );
  assert.doesNotMatch(hostProps, /actionResetToken|failureCount/);
  assert.match(chatSource, /resolveError=\{resolveMutation\.error\}/);
  assert.match(chatSource, /resolveMessageId=\{resolveMutation\.variables\?\.msgId/);
  assert.match(chatSource, /onRunChange=\{resolveMutation\.reset\}/);
  assert.match(chatSource, /onSuccess:[\s\S]*?resolveMutation\.reset\(\)/);
  assert.match(chatSource, /queryKey:\s*\["workflow-run"\]/);
  assert.match(chatSource, /queryKey:\s*\["workspace-workflow-runs", workspaceId\]/);
  assert.doesNotMatch(hostSource, /actionResetToken|failureCount/);
  assert.match(hostSource, /actionMessageId === resolveMessageId/);
  assert.match(hostSource, /const actionMessageId = nonEmptyString\(interventionAction\?\.message_id\)/);
  assert.doesNotMatch(hostSource, /actionMessage\?\.id/);
  assert.match(hostSource, /resolveWorkspaceWorkflowMessageAction\([\s\S]*?onResolveMessage/);
});

test("workflow API exposes linked retry attempts", () => {
  assert.match(apiSource, /retryRun:\s*\(runId: string/);
  assert.match(apiSource, /from_step_id/);
  assert.match(apiSource, /\/workflows\/runs\/\$\{runId\}\/retry/);
});

test("workspace chat aligns long pending-action forms from their start", () => {
  assert.match(chatSource, /latestPendingAction\?\.id/);
  assert.match(chatSource, /const latestPendingActionIsLatest =/);
  assert.match(
    chatSource,
    /if \(!latestPendingActionIsLatest\) scrollToBottom\(\);/,
  );
  assert.match(
    chatSource,
    /scrollIntoView\(\{ behavior: "smooth", block: "start" \}\)/,
  );
});

test("legacy workflow starter cards recover their workflow title from message refs", () => {
  assert.match(chatSource, /function workflowStarterAction/);
  assert.match(chatSource, /ref\.type === "workflow"/);
  assert.match(chatSource, /title: workflowRef\?\.title/);
});
