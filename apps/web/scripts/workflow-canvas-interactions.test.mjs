import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { transform } from "esbuild";

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
const workflowMediaSource = await readFile(
  new URL("../src/lib/workflowMedia.ts", import.meta.url),
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
const integrationCatalogSource = await readFile(
  new URL("../../../packages/core/services/integration_operation_catalog.py", import.meta.url),
  "utf8",
);
const integrationsRouterSource = await readFile(
  new URL("../../api/routers/integrations.py", import.meta.url),
  "utf8",
);

async function compilePureModule(source) {
  const compiled = await transform(source, {
    loader: "ts",
    format: "esm",
    target: "es2020",
  });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString("base64")}`);
}

const workflowMediaModule = await compilePureModule(workflowMediaSource);
const workflowValidateModule = await compilePureModule(validateSource);

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

test("intentional stop nodes and Workspace-bound agents validate cleanly", () => {
  const issues = workflowValidateModule.validateWorkflow([
    { id: "start", type: "trigger", next: ["publisher"] },
    {
      id: "publisher",
      type: "agent",
      config: { service_key: "distribution.linkedin.chrome" },
      next: ["unsupported"],
    },
    { id: "unsupported", type: "stop", config: { message: "Unsupported platform" } },
  ]);

  assert.deepEqual(issues, []);
});

test("subworkflow configuration links to the referenced publishing workflow", () => {
  assert.match(panelSource, /className="workflow-reference-card"/);
  assert.match(panelSource, /Open publishing workflow/);
  assert.match(panelSource, /openReferencedWorkflow/);
  assert.match(panelSource, /state: \{ returnTo \}/);
  assert.match(stylesSource, /\.workflow-reference-card:focus-visible/);
});

test("standalone node results persist to the canvas and reveal the result panel", () => {
  assert.match(flowsSource, /setConfigStepId\(stepId\)/);
  assert.match(flowsSource, /onRunResult=\{recordSingleResult\}/);
  assert.match(panelSource, /onRunResult\?\.\(step\.id, res\)/);
  assert.match(panelSource, /scrollIntoView\(\{ behavior:/);
  assert.match(panelSource, /if \(running \|\| !liveResult\?\.status \|\| !resultRef\.current\) return/);
  assert.match(panelSource, /aria-live="polite"/);
  assert.match(panelSource, />Test & result</);
  assert.match(panelSource, />Result output</);
  assert.match(panelSource, /JSON\.stringify\(result, null, 2\)/);
  assert.match(panelSource, /Trigger test completed\. This node only starts the flow; it does not produce business data\./);
  assert.match(panelSource, /className="workflow-node-config-layout"/);
  assert.match(panelSource, /className="workflow-node-execution-result"/);
  assert.ok(panelSource.indexOf('className="workflow-node-config-fields"') < panelSource.indexOf("<ExecutionResultPanel"));
  assert.match(panelSource, /maxWidth="960px"/);
  assert.match(panelSource, /Test this node to see its result/);
  assert.match(stylesSource, /grid-template-areas: "config result"/);
  assert.match(stylesSource, /@media \(max-width: 760px\)[\s\S]*?"config"[\s\S]*?"result"/);
  assert.match(canvasSource, /data\.status === "failed" \? "Error" : "Output"/);
});

test("connector nodes choose live MCP operations and render typed arguments", () => {
  assert.match(panelSource, /api\.integrations\.mcpServers\(\)/);
  assert.match(panelSource, /api\.integrations\.operations\(connectorServer\)/);
  assert.match(panelSource, /label="Account"/);
  assert.match(panelSource, /label="Resource"/);
  assert.match(panelSource, /filterable[\s\S]*?ariaLabel="Integration operation"/);
  assert.match(panelSource, /function ConnectorArgumentField/);
  assert.match(panelSource, /connectorOperationInputSchema\([\s\S]*?selectedOperation[\s\S]*?selectedAccount/);
  assert.match(panelSource, /This operation can remove or irreversibly change external data/);
  assert.match(panelSource, /className="workflow-connector-advanced"/);
  assert.match(panelSource, /k\.startsWith\("__raw_"\) \|\| k\.startsWith\("__connector_"\)/);
  assert.match(apiSource, /integrations\/mcp-servers\/\$\{encodeURIComponent\(serverKey\)\}\/tools/);
  assert.match(integrationsRouterSource, /async def list_mcp_server_tools/);
  assert.match(integrationCatalogSource, /module\.list_tools\(\)/);
  assert.match(integrationCatalogSource, /tools_cached/);
  assert.match(stylesSource, /\.workflow-connector-effect\.is-destructive/);
});

test("node dialog prioritizes configuration and keeps diagnostics compact", () => {
  assert.match(panelSource, /className="workflow-node-dialog"/);
  assert.match(panelSource, /bodyClassName="workflow-node-dialog-body"/);
  assert.match(panelSource, /title=\{name \|\| `\$\{m\.label\} node`\}/);
  assert.match(panelSource, />Configuration</);
  assert.match(panelSource, />Data mapping</);
  assert.match(panelSource, /<details className="workflow-node-settings">/);
  assert.match(panelSource, /aria-expanded=\{testInputsOpen\}/);
  assert.match(panelSource, /className="workflow-node-result-inputs"/);
  assert.match(stylesSource, /\.workflow-node-dialog \{[\s\S]*?border: 0 !important/);
  assert.match(stylesSource, /\.workflow-node-test-inputs-toggle:focus-visible/);
  assert.match(stylesSource, /\.workflow-node-settings > summary:focus-visible/);
});

test("workflow editor header keeps the shared AI edit control and one accessible action toolbar", () => {
  assert.match(flowsSource, /className="workflow-editor-header"/);
  assert.match(flowsSource, /className="workflow-editor-identity"/);
  assert.match(flowsSource, /aria-label="Edit workflow name, description, and icon"/);
  assert.match(flowsSource, /className="workflow-editor-heading"/);
  assert.match(flowsSource, /className="workflow-editor-meta" aria-label="Workflow status"/);
  assert.match(flowsSource, /className="workflow-editor-actions" role="toolbar" aria-label="Workflow actions"/);
  assert.match(flowsSource, /className=\{`workflow-editor-validation is-\$\{state\}`\}/);
  assert.match(flowsSource, /<StatusBadge type=\{flow\.status === "active" \? "active" : "gray"\} dot>/);
  assert.match(flowsSource, /<AiEditButton[\s\S]*?className="workflow-editor-action workflow-editor-action-ai"[\s\S]*?onClick=\{openWorkflowAiEdit\}/);
  assert.match(flowsSource, /ariaLabel="Add node"/);
  assert.match(flowsSource, /ariaLabel="Deploy workflow"/);
  assert.match(flowsSource, /<IconPlus size=\{17\}/);
  assert.match(flowsSource, /<IconUpload size=\{16\}/);
  assert.match(flowsSource, /<IconClock size=\{16\}/);
  assert.match(flowsSource, /title=\{t\("page\.flows\.delete_flow"\)\}/);
  assert.match(flowsSource, /loading=\{deleteMutation\.isPending\}/);
  assert.match(flowsSource, /closeOnConfirm=\{false\}/);
  assert.match(confirmDialogSource, /if \(closeOnConfirm\) onClose\(\)/);
  assert.match(flowsSource, /const triggerKind = flow\.trigger \|\| flow\.trigger_type \|\| "manual"/);
  assert.match(flowsSource, /t\(TRIGGER_LABELS\[triggerKind\] \|\| triggerKind\)/);
  assert.match(stylesSource, /\.workflow-editor-header \{[\s\S]*?grid-template-columns: 36px minmax\(260px, 1fr\)/);
  assert.match(stylesSource, /\.workflow-editor-actions \{[\s\S]*?flex-wrap: nowrap[\s\S]*?overflow-x: auto/);
  assert.match(stylesSource, /\.workflow-editor-actions \.workflow-editor-action \{[\s\S]*?width: 36px[\s\S]*?height: 36px/);
  assert.match(stylesSource, /\.workflow-editor-actions \.workflow-editor-action\.workflow-editor-action-ai \{[\s\S]*?width: auto[\s\S]*?flex: 0 0 auto/);
  assert.match(stylesSource, /@media \(max-width: 1120px\)[\s\S]*?\.workflow-editor-controls \{[\s\S]*?grid-column: 2/);
  assert.match(stylesSource, /@media \(max-width: 720px\)[\s\S]*?\.workflow-editor-actions \{[\s\S]*?justify-content: flex-start/);
  assert.doesNotMatch(flowsSource, /onAddNode=/);
  assert.doesNotMatch(canvasSource, />\s*Add node\s*</);
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
  assert.match(panelSource, /source: formatTestInputSource\(input\.value\)/);
  assert.match(panelSource, /return "structured JSON"/);
  assert.ok(
    panelSource.indexOf("const mappedValue = resolveTestInputDefault(input.value, runVariables)")
      < panelSource.indexOf("const previousValue = lastResult?.inputs?.[key]"),
    "complete run variables must win over truncated step-input previews",
  );
  assert.match(panelSource, /config: \{ \.\.\.cleaned, inputs: testBindings\.length \? testBindings : undefined \}/);
  assert.match(panelSource, /not saved/);
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
  assert.match(flowsSource, /!key \|\| row\?\.hidden \|\| seen\.has\(key\)/);
  assert.match(flowsSource, /Provide the entry data for this run/);
  assert.match(flowsSource, /Provide a value before running this workflow\./);
  assert.match(flowsSource, /\{ trigger_data: triggerData \}/);
  assert.match(flowsSource, /requestWorkflowRun\(flow\)/);
  assert.match(apiSource, /body: JSON\.stringify\(data \|\| \{\}\)/);
  assert.match(apiSource, /trigger_data\?: Record<string, any>/);
  assert.match(flowsSource, /rawType === "integer"/);
  assert.match(flowsSource, /input\.integer && !Number\.isInteger\(parsed\)/);
  assert.match(flowsSource, /min=\{input\.minimum\}/);
  assert.match(flowsSource, /max=\{input\.maximum\}/);
  assert.match(validateSource, /s\.type === "agent"[\s\S]*?!isEmpty\(s\.config\?\.prompt\)/);
});

test("ordinary research links stay in structured workflow results", () => {
  assert.deepEqual(workflowMediaModule.extractMediaRefs({
    canonical_url: "https://example.com/article",
    public_signal_refs: [
      "https://news.ycombinator.com/item?id=1 — public signal only",
      { url: "https://www.reddit.com/r/startups/comments/example" },
    ],
  }), []);
  assert.deepEqual(workflowMediaModule.extractMediaRefs({
    image_url: "https://cdn.example.com/approved-image.png",
  }), [{
    url: "https://cdn.example.com/approved-image.png",
    type: "image",
    name: "approved-image.png",
  }]);
  assert.deepEqual(workflowMediaModule.extractMediaRefs({
    url: "/api/v1/fs/download/approved-asset",
  }), [{
    url: "/api/v1/fs/download/approved-asset",
    type: "file",
    name: undefined,
  }]);
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

test("terminal nodes expose explicit structured input and output mappings", () => {
  assert.match(panelSource, /Terminal node — map the Workflow result below\./);
  assert.match(panelSource, /step\.type !== "unsupported"/);
  assert.doesNotMatch(panelSource, /step\.type !== "end" && step\.type !== "unsupported"/);
  assert.match(panelSource, /workflow-binding-structured-value/);
  assert.match(panelSource, /JSON\.parse\(raw\)/);
  assert.match(panelSource, /hasBindingValueErrors/);
  assert.match(panelSource, /"image", "video", "audio"/);
  assert.match(stylesSource, /\.workflow-binding-structured-value \{/);
});
