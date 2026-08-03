import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const canvasSource = await readFile(
  new URL("../src/components/workflows/WorkflowCanvas.tsx", import.meta.url),
  "utf8",
);
const panelSource = await readFile(
  new URL("../src/components/workflows/WorkflowNodeConfigPanel.tsx", import.meta.url),
  "utf8",
);
const mediaPreviewSource = await readFile(
  new URL("../src/components/workflows/MediaPreview.tsx", import.meta.url),
  "utf8",
);
const flowsSource = await readFile(
  new URL("../src/pages/Flows.tsx", import.meta.url),
  "utf8",
);
const stylesSource = await readFile(
  new URL("../src/index.css", import.meta.url),
  "utf8",
);
const textareaSource = await readFile(
  new URL("../src/components/ui/Textarea.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const validateSource = await readFile(
  new URL("../src/lib/workflowValidate.ts", import.meta.url),
  "utf8",
);
const confirmDialogSource = await readFile(
  new URL("../src/components/ui/ConfirmDialog.tsx", import.meta.url),
  "utf8",
);

test("the visible node plus is a real source connection handle", () => {
  assert.match(canvasSource, /<Handle[\s\S]*?type="source"[\s\S]*?className="workflow-source-handle"/);
  assert.match(canvasSource, /aria-label=\{`Connect from \$\{data\.label\}`\}/);
  assert.match(canvasSource, /connectionRadius=\{30\}/);
  assert.match(canvasSource, /connectOnClick/);
});

test("workflow nodes use compact cards with distinct semantic silhouettes", () => {
  assert.match(canvasSource, /\| "decision"/);
  assert.match(canvasSource, /\| "merge"/);
  assert.match(canvasSource, /\| "io"/);
  assert.match(canvasSource, /\| "code"/);
  assert.match(canvasSource, /\| "wait"/);
  assert.match(canvasSource, /\| "terminal"/);
  assert.match(canvasSource, /\["trigger", "webhook"\][\s\S]*?return "trigger"/);
  assert.match(canvasSource, /t === "agent"[\s\S]*?return "agent"/);
  assert.match(canvasSource, /\["llm", "rag", "tool"\][\s\S]*?return "configuration"/);
  assert.match(canvasSource, /\["condition", "switch", "filter", "classifier"\][\s\S]*?return "decision"/);
  assert.match(canvasSource, /\["merge", "aggregate"\][\s\S]*?return "merge"/);
  assert.match(canvasSource, /\["http", "connector", "notify", "subworkflow", "media", "image", "video", "audio"\][\s\S]*?return "io"/);
  assert.match(canvasSource, /trigger: "32px 12px 12px 32px"/);
  assert.match(canvasSource, /configuration: "999px"/);
  assert.match(canvasSource, /decision: "M18 1H192L209 50L192 99H18L1 50Z"/);
  assert.match(canvasSource, /merge: "M1 1H176L209 50L176 99H1L22 50Z"/);
  assert.match(canvasSource, /io: "M18 1H209L191 99H1Z"/);
  assert.match(canvasSource, /data-node-surface="polygon"/);
  assert.match(canvasSource, /preserveAspectRatio="none"/);
  assert.match(canvasSource, /data-node-variant=\{variant\}/);
  assert.match(canvasSource, /0 0 0 6px rgba\(15,118,110,0\.13\)/);
  assert.match(canvasSource, /configuration: \{ left: 30, right: 30/);
  assert.match(canvasSource, /data-node-output-footer/);
  assert.match(canvasSource, /padding: `0 \$\{padR\}px 12px \$\{padL\}px`/);
  assert.match(canvasSource, /data-node-output-preview/);
  assert.doesNotMatch(canvasSource, /margin: `0 \$\{padR\}px 10px \$\{padL\}px`/);
  assert.match(canvasSource, /maxWidth: NODE_W - padL - padR/);
});

test("adding a connected node remains available from the hover toolbar", () => {
  assert.match(canvasSource, /title="Add a connected node"/);
  assert.match(canvasSource, /addFrom\(id\)/);
  assert.match(canvasSource, /title="Test this node"/);
});

test("note deletion persists and notes stay out of run progress", () => {
  assert.match(canvasSource, /const deleteNodes = useCallback\(\(nodeIds: string\[\]\)/);
  assert.match(canvasSource, /const removed = new Set\(nodeIds\)/);
  assert.match(canvasSource, /\.filter\(\(s\) => !removed\.has\(s\.id\)\)/);
  assert.match(canvasSource, /onNodesDelete=\{editable \? onNodesDelete : undefined\}/);
  assert.match(flowsSource, /const executableSteps = steps\.filter\(\(step: any\) => step\.type !== "note"\)/);
  assert.match(flowsSource, /const doneCount = executableSteps\.filter\(\(step: any\) => statusById\[step\.id\] === "completed"\)\.length/);
  assert.match(flowsSource, /\{doneCount\}\/\{executableSteps\.length\}/);
  assert.doesNotMatch(flowsSource, /\{doneCount\}\/\{steps\.length\}/);
  assert.match(validateSource, /const executableSteps = steps\.filter\(\(step\) => step\.type !== "note"\)/);
  assert.match(validateSource, /for \(const s of executableSteps\)/);
});

test("standalone node results persist to the canvas and reveal the result panel", () => {
  assert.match(flowsSource, /setConfigStepId\(stepId\)/);
  assert.match(flowsSource, /onRunResult=\{recordSingleResult\}/);
  assert.match(panelSource, /onRunResult\?\.\(step\.id, res\)/);
  assert.match(panelSource, /scrollIntoView\(\{ behavior:/);
  assert.match(panelSource, /aria-live="polite"/);
  assert.match(panelSource, />Execution result</);
  assert.match(panelSource, />Result output</);
  assert.match(panelSource, /JSON\.stringify\(result, null, 2\)/);
  assert.match(panelSource, /Trigger test completed\. This node only starts the flow; it does not produce business data\./);
  assert.match(panelSource, /className="workflow-node-config-layout"/);
  assert.match(panelSource, /className="workflow-node-execution-result"/);
  assert.ok(panelSource.indexOf("<ExecutionResultPanel") < panelSource.indexOf('className="workflow-node-config-fields"'));
  assert.match(panelSource, /maxWidth="960px"/);
  assert.match(panelSource, /Test this node to see its result/);
  assert.match(stylesSource, /grid-template-areas: "config result"/);
  assert.match(stylesSource, /@media \(max-width: 760px\)[\s\S]*?"result"[\s\S]*?"config"/);
  assert.match(canvasSource, /data\.status === "failed" \? "Error" : "Output"/);
});

test("workflow editor header keeps identity, status, and actions consistent", () => {
  assert.match(flowsSource, /className="workflow-editor-header"/);
  assert.match(flowsSource, /className="workflow-editor-identity"/);
  assert.match(flowsSource, /aria-label="Edit workflow name, description, and icon"/);
  assert.match(flowsSource, /className="workflow-editor-heading"/);
  assert.match(flowsSource, /className="workflow-editor-meta" aria-label="Workflow status"/);
  assert.match(flowsSource, /className="workflow-editor-actions" aria-label="Workflow actions"/);
  assert.match(flowsSource, /className=\{`workflow-editor-validation is-\$\{state\}`\}/);
  assert.match(flowsSource, /<StatusBadge type=\{flow\.status === "active" \? "active" : "gray"\} dot>/);
  assert.match(flowsSource, /className="workflow-editor-action workflow-editor-action-history"/);
  assert.match(flowsSource, /className="workflow-editor-action workflow-editor-action-delete"/);
  assert.match(flowsSource, /title=\{t\("page\.flows\.delete_flow"\)\}/);
  assert.match(flowsSource, /loading=\{deleteMutation\.isPending\}/);
  assert.match(flowsSource, /closeOnConfirm=\{false\}/);
  assert.match(confirmDialogSource, /if \(closeOnConfirm\) onClose\(\)/);
  assert.match(flowsSource, /const triggerKind = flow\.trigger \|\| flow\.trigger_type \|\| "manual"/);
  assert.match(flowsSource, /t\(TRIGGER_LABELS\[triggerKind\] \|\| triggerKind\)/);
  assert.match(stylesSource, /\.workflow-editor-header \{[\s\S]*?grid-template-columns: 36px minmax\(260px, 1fr\)/);
  assert.match(stylesSource, /\.workflow-editor-actions \.workflow-editor-action \{[\s\S]*?height: 36px/);
  assert.match(stylesSource, /@media \(max-width: 1120px\)[\s\S]*?\.workflow-editor-controls \{[\s\S]*?grid-column: 2/);
  assert.match(stylesSource, /@media \(max-width: 720px\)[\s\S]*?grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(stylesSource, /\.workflow-editor-action-history \{[\s\S]*?grid-column: 1 \/ -1/);
  assert.match(stylesSource, /\.workflow-editor-action-delete \{[\s\S]*?grid-column: 1 \/ -1/);
});

test("workflow cards support direct open, contextual editing, and real metadata", () => {
  assert.match(flowsSource, /function WorkflowMetadataPanel/);
  assert.match(flowsSource, /api\.workflows\.metadata\(workflowId\)/);
  assert.match(flowsSource, /const openFlowEditor = \(flow: Flow\)/);
  assert.match(flowsSource, /onClick=\{\(\) => openFlowEditor\(flow\)\}/);
  assert.match(flowsSource, /onContextMenu=\{\(event\) => flowContextMenu\.show/);
  assert.match(flowsSource, /flowContextMenu\.showAt/);
  assert.match(flowsSource, /page\.flows\.view_details/);
  assert.match(flowsSource, /<WorkflowMetadataPanel workflowId=\{flow\.id\}/);
  assert.match(flowsSource, /page\.flows\.created_by/);
  assert.match(flowsSource, /page\.flows\.workspace_usage/);
  assert.match(flowsSource, /<Dropdown[\s\S]*?<PageHeaderAddButton[\s\S]*?caret/);
  assert.match(flowsSource, /key: "templates"/);
  assert.match(flowsSource, /key: "import"/);
  assert.match(flowsSource, /title=\{t\("page\.flows\.edit_workflow"\)\}/);
  assert.match(flowsSource, /role="radiogroup" aria-label="Workflow icon"/);
  assert.match(flowsSource, /role="radio"[\s\S]*?aria-checked=\{selected\}/);
  assert.match(flowsSource, /name,[\s\S]*?description: identityDescription\.trim\(\),[\s\S]*?icon: identityIcon/);
  assert.match(flowsSource, /workflowIconGlyph\(flow\.icon, 18\)/);
  assert.match(stylesSource, /\.workflow-icon-options \{[\s\S]*?grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(stylesSource, /\.workflow-icon-option\.is-selected \{/);
  assert.match(stylesSource, /\.workflow-metadata-grid \{/);
  assert.match(stylesSource, /\.workflow-card-action-button:focus-visible \{/);
});

test("inputs select connected upstream outputs and autocomplete in prompts", () => {
  assert.match(flowsSource, /targets: \[\.\.\.new Set\(/);
  assert.match(panelSource, /connectedUpstreamNodes\(nodes \|\| \[\], step\.id\)/);
  assert.match(panelSource, /Select an upstream output…/);
  assert.match(panelSource, /Entire output/);
  assert.match(panelSource, /Custom value…/);
  assert.match(panelSource, /function PromptInputTextarea/);
  assert.match(panelSource, /function CodeInputTextarea/);
  assert.match(panelSource, /codeInputToken\(normalizedLanguage, name\)/);
  assert.match(panelSource, /inputs\.get\(\$\{quotedName\}\)/);
  assert.match(panelSource, /inputs\[\$\{quotedName\}\]/);
  assert.match(panelSource, /WORKFLOW_INPUTS_FILE/);
  assert.match(panelSource, /Type <kbd>inputs\.<\/kbd> then <kbd>Tab<\/kbd>/);
  assert.match(panelSource, /aria-label="Code input parameters"/);
  assert.match(panelSource, /event\.key === "Enter" \|\| event\.key === "Tab"/);
  assert.match(panelSource, /role="listbox" aria-label="Input parameters"/);
  assert.match(panelSource, /Type <kbd>\{"\{"\}<\/kbd> then <kbd>Tab<\/kbd>/);
  assert.match(panelSource, /className="workflow-prompt-input-chip"/);
  assert.match(textareaSource, /onKeyDown=\{onKeyDown\}/);
  assert.doesNotMatch(panelSource, /Insert data from another step/);
  assert.match(panelSource, /"Test node"/);
  assert.match(panelSource, />Test inputs</);
  assert.match(panelSource, /Provide a value before testing this node\./);
  assert.match(panelSource, /resolveTestInputDefault\(input\.value, runVariables\)/);
  assert.match(panelSource, /config: \{ \.\.\.cleaned, inputs: testBindings\.length \? testBindings : undefined \}/);
  assert.match(panelSource, /Not saved/);
  assert.match(panelSource, /setForId\(undefined\)/);
  assert.match(flowsSource, /silently reusing stale workflow data/);
  assert.match(flowsSource, /resolveWorkflowFinalResult/);
  assert.match(flowsSource, />Final result</);
  assert.match(flowsSource, />\s*View result/);
  assert.match(flowsSource, /setRunResult\(runs\[0\]\)/);
  assert.match(flowsSource, /<WorkflowFinalResultPanel/);
  assert.match(flowsSource, /extractMediaRefs\(output, 3\)/);
  assert.match(flowsSource, /Object\.entries\(run\?\.trigger_data \|\| \{\}\)/);
  assert.match(flowsSource, />\s*Run inputs/);
  assert.match(stylesSource, /\.workflow-final-result-inputs/);
  assert.match(flowsSource, /<MediaPreview[\s\S]*?refItem=\{item\}/);
  assert.match(mediaPreviewSource, /aria-label=\{name \|\| "Video output"\}/);
  assert.match(mediaPreviewSource, /playsInline/);
  assert.match(stylesSource, /\.workflow-final-result/);
  assert.match(stylesSource, /\.workflow-binding-row/);
  assert.match(stylesSource, /\.workflow-code-textarea \.manor-textarea/);
});

test("full workflow runs collect trigger inputs and submit them to the stream", () => {
  assert.match(flowsSource, /function workflowRunInputs/);
  assert.match(flowsSource, /Provide the entry data for this run/);
  assert.match(flowsSource, /Provide a value before running this workflow\./);
  assert.match(flowsSource, /\{ trigger_data: triggerData \}/);
  assert.match(flowsSource, /requestWorkflowRun\(flow\)/);
  assert.match(apiSource, /body: JSON\.stringify\(data \|\| \{\}\)/);
  assert.match(apiSource, /trigger_data\?: Record<string, any>/);
  assert.match(validateSource, /s\.type === "agent".*!isEmpty\(s\.config\?\.prompt\)/);
});

test("workflow start edits the run contract and exposes its outputs", () => {
  assert.match(panelSource, /const isEntryNode = \["trigger", "webhook"\]\.includes\(step\.type\)/);
  assert.match(panelSource, /config\.run_inputs/);
  assert.match(panelSource, /<WorkflowRunInputRows/);
  assert.match(panelSource, /entryOutputRows/);
  assert.match(panelSource, /run_inputs: rows\.length \? rows : \[\]/);
  assert.match(panelSource, /value: `\{\{\$\{stepId\}\.\$\{key\}\}\}`/);
  assert.match(panelSource, /Schema \(JSON\)/);
  assert.match(panelSource, /row\.schema/);
  assert.match(panelSource, /k === "run_inputs"/);
});
