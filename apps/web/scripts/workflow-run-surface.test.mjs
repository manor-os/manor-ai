import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { transform } from "esbuild";

const displaySource = await readFile(
  new URL("../src/components/workflows/workflowRunDisplay.ts", import.meta.url),
  "utf8",
);
const progressSource = await readFile(
  new URL("../src/components/workflows/WorkflowRunProgress.tsx", import.meta.url),
  "utf8",
);
const interventionSource = await readFile(
  new URL("../src/components/workflows/WorkflowRunIntervention.tsx", import.meta.url),
  "utf8",
);
const approvalReviewSource = await readFile(
  new URL("../src/components/workflows/WorkflowApprovalReview.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const hostSource = await readFile(
  new URL("../src/components/workflows/WorkspaceWorkflowRunHost.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const chatSource = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const schemaSource = await readFile(
  new URL("../src/components/workflows/WorkflowSchemaFields.tsx", import.meta.url),
  "utf8",
);
const schemaLogicSource = await readFile(
  new URL("../src/components/workflows/workflowSchema.ts", import.meta.url),
  "utf8",
).catch(() => "");
const actionSource = await readFile(
  new URL("../src/components/ui/ChatActionCard.tsx", import.meta.url),
  "utf8",
);
const workflowCanvasSource = await readFile(
  new URL("../src/components/workflows/WorkflowCanvas.tsx", import.meta.url),
  "utf8",
);
const nodeConfigSource = await readFile(
  new URL("../src/components/workflows/WorkflowNodeConfigPanel.tsx", import.meta.url),
  "utf8",
);
const localeSources = await Promise.all(["en", "zh", "es"].map((locale) => readFile(
  new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url),
  "utf8",
)));
const cssSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const cssStart = cssSource.indexOf("/* Workflow run surface */");
const cssEnd = cssSource.indexOf("/* End workflow run surface */", cssStart);
const workflowCss = cssSource.slice(cssStart, cssEnd);

async function compilePureModule(source) {
  const compiled = await transform(source, {
    loader: "ts",
    format: "esm",
    target: "es2020",
  });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString("base64")}`);
}

const displayModule = await compilePureModule(displaySource);
const schemaModule = await compilePureModule(schemaLogicSource);

test("stage nodes have a compact canvas identity and structured read-only details", () => {
  assert.match(workflowCanvasSource, /stage:\s*"logic"/);
  assert.match(workflowCanvasSource, /stage:\s*"STAGE"/);
  assert.match(workflowCanvasSource, /stage:\s*"M/);
  assert.match(nodeConfigSource, /function StageOperationsSummary/);
  assert.match(nodeConfigSource, /step\.type === "stage"/);
  assert.match(nodeConfigSource, /StageOperationsSummary config=\{config\}/);
  assert.match(nodeConfigSource, /Entry operation/);
  assert.match(nodeConfigSource, /Operations/);
  assert.match(nodeConfigSource, /Routes/);
  assert.match(nodeConfigSource, /disabled=\{running \|\| step\.type === "stage"/);
});

async function loadHostHelpers() {
  const startMarker = "/* Workspace workflow run host helpers */";
  const endMarker = "/* End workspace workflow run host helpers */";
  const start = hostSource.indexOf(startMarker);
  const end = hostSource.indexOf(endMarker, start);
  assert.notEqual(start, -1, "workflow run host helper block is missing");
  assert.notEqual(end, -1, "workflow run host helper end marker is missing");
  return compilePureModule(hostSource.slice(start + startMarker.length, end));
}
const validationMessages = {
  required: "required",
  invalidJson: "invalid-json",
  invalidUrl: "invalid-url",
  invalidFormat: "invalid-format",
  invalidNumber: "invalid-number",
  minLength: (minimum) => `min-length:${minimum}`,
  maxLength: (maximum) => `max-length:${maximum}`,
  minItems: (minimum) => `min-items:${minimum}`,
  maxItems: (maximum) => `max-items:${maximum}`,
  uniqueItems: "unique-items",
  minimum: (minimum) => `minimum:${minimum}`,
  maximum: (maximum) => `maximum:${maximum}`,
  exclusiveMinimum: (minimum) => `exclusive-minimum:${minimum}`,
  exclusiveMaximum: (maximum) => `exclusive-maximum:${maximum}`,
};

test("workflow run display exports stable statuses, view types, and helpers", () => {
  assert.match(displaySource, /export type WorkflowRunStatus\s*=/);
  for (const status of [
    "pending", "running", "completed", "paused", "failed", "skipped", "cancelled",
  ]) {
    assert.match(displaySource, new RegExp(`"${status}"`));
  }
  assert.match(displaySource, /export interface WorkflowRunView/);
  assert.match(displaySource, /export interface WorkflowRunNode/);
  assert.match(displaySource, /export interface WorkflowRunAction/);
  assert.match(displaySource, /export function formatWorkflowError/);
  assert.match(displaySource, /export function isWorkflowRunActive/);
  assert.match(displaySource, /export function processedNodeCount/);
  assert.match(displaySource, /export function currentNodeIndex/);
  assert.match(displaySource, /export function visibleLabelIndexes/);
});

test("only recoverable completed runs present as waiting", () => {
  assert.match(
    displaySource,
    /export type WorkflowRunStatusMotion\s*=\s*"running"\s*\|\s*"waiting"\s*\|\s*"static"/,
  );
  assert.match(displaySource, /motion:\s*WorkflowRunStatusMotion/);

  for (const status of ["pending", "running"]) {
    assert.deepEqual(
      displayModule.workflowRunStatusPresentation({ status }),
      { labelKey: status, iconStatus: status, motion: "running" },
    );
  }
  assert.deepEqual(
    displayModule.workflowRunStatusPresentation({ status: "paused" }),
    { labelKey: "paused", iconStatus: "paused", motion: "waiting" },
  );
  assert.deepEqual(
    displayModule.workflowRunStatusPresentation({
      status: "completed",
      businessOutcome: "needs_input",
    }),
    { labelKey: "needs_input", iconStatus: "paused", motion: "waiting" },
  );
  for (const businessOutcome of ["revision_required", "ready_for_acceptance", "completed"]) {
    assert.deepEqual(
      displayModule.workflowRunStatusPresentation({ status: "completed", businessOutcome }),
      { labelKey: "completed", iconStatus: "completed", motion: "static" },
    );
  }
  for (const status of ["completed", "failed", "skipped", "cancelled"]) {
    assert.deepEqual(
      displayModule.workflowRunStatusPresentation({
        status,
        businessOutcome: status === "failed" ? "needs_input" : "accepted",
      }),
      { labelKey: status, iconStatus: status, motion: "static" },
    );
  }

  assert.match(progressSource, /workflowRunStatusPresentation\(run\)/);
  assert.match(progressSource, /className="workflow-run-status-indicator"/);
  assert.match(progressSource, /data-motion=\{statusPresentation\.motion\}/);
  assert.match(hostSource, /workflowRunStatusPresentation\(run\)/);
  assert.match(workflowCss, /@keyframes workflow-status-orbit/);
  assert.match(workflowCss, /@keyframes workflow-status-breathe/);
  assert.match(
    workflowCss,
    /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?\.workflow-run-status-indicator[\s\S]*?animation:\s*none/,
  );
  for (const source of localeSources) {
    assert.match(source, /component\.workflow_run\.status\.needs_input/);
    assert.match(source, /component\.workflow_run\.status\.revision_required/);
    assert.match(source, /component\.workflow_run\.status\.ready_for_acceptance/);
  }
});

test("workflow error formatting extracts structured fields and bounds readable JSON", () => {
  assert.match(displaySource, /message/);
  assert.match(displaySource, /code/);
  assert.match(displaySource, /path/);
  assert.match(displaySource, /required_change/);
  assert.match(displaySource, /8\s*\*\s*1024/);
  assert.match(displaySource, /truncationText/);
  assert.match(displaySource, /JSON\.stringify/);
  assert.doesNotMatch(displaySource, /String\(value\)/);
  assert.doesNotMatch(displaySource, /\[object Object\]/);
});

test("structured workflow errors retain retry and compact artifact references", () => {
  const formatted = displayModule.formatWorkflowError({
    message: "Capture failed",
    code: "CAPTURE_FAILED",
    path: ["request", "start_url"],
    required_change: "Update the URL",
    retry_from_step_id: "capture-product",
    artifact_refs: [{
      artifact_id: "artifact-1",
      name: "capture.mp4",
      path: "/workspace/capture.mp4",
      payload: "RAW_PAYLOAD".repeat(2_000),
    }],
  }, "LOCALIZED_TRUNCATION");

  assert.match(formatted, /Capture failed/);
  assert.match(formatted, /CAPTURE_FAILED/);
  assert.match(formatted, /capture-product/);
  assert.match(formatted, /artifact-1/);
  assert.match(formatted, /capture\.mp4/);
  assert.doesNotMatch(formatted, /RAW_PAYLOAD/);
  assert.ok(Buffer.byteLength(formatted, "utf8") <= 8 * 1024);
});

test("workflow error truncation uses caller-provided localized text", () => {
  const formatted = displayModule.formatWorkflowError(
    { message: "x".repeat(12_000) },
    "LOCALIZED_TRUNCATION",
  );
  assert.match(formatted, /LOCALIZED_TRUNCATION$/);
  assert.doesNotMatch(displaySource, /\[truncated\]/);
  assert.ok(Buffer.byteLength(formatted, "utf8") <= 8 * 1024);
});

test("workflow error formatting bounds values before JSON serialization", () => {
  let rawToJsonCalled = false;
  const raw = Object.fromEntries(Array.from({ length: 32 }, (_value, index) => [
    `field_${index}`,
    `${index}:`.padEnd(4_000, "x"),
  ]));
  Object.defineProperty(raw, "toJSON", {
    enumerable: true,
    value() {
      rawToJsonCalled = true;
      throw new Error("raw object was serialized");
    },
  });

  const formatted = displayModule.formatWorkflowError(raw, "内容已截断");

  assert.equal(rawToJsonCalled, false);
  assert.match(formatted, /内容已截断$/);
  assert.ok(Buffer.byteLength(formatted, "utf8") <= 8 * 1024);
});

test("workflow error preview bounds key enumeration and oversized key names", () => {
  const oversizedKey = `oversized_${"x".repeat(4_000)}`;
  const target = Object.fromEntries([
    [oversizedKey, "visible"],
    ...Array.from({ length: 40 }, (_value, index) => [`field_${index}`, index]),
  ]);
  let ownKeysCalls = 0;
  const raw = new Proxy(target, {
    ownKeys(value) {
      ownKeysCalls += 1;
      return Reflect.ownKeys(value);
    },
  });

  const preview = displayModule.boundedJsonPreview(raw);
  const previewKeys = Object.keys(preview);

  assert.equal(ownKeysCalls, 1);
  assert.ok(previewKeys.length <= 25);
  assert.ok(previewKeys.every((key) => key.length <= 132));
  assert.ok(!previewKeys.includes(oversizedKey));
});

test("visible label indexes keep all nodes or the local dense neighborhood", () => {
  const nodes = Array.from({ length: 6 }, (_value, index) => ({
    id: `node-${index}`,
    name: `Node ${index}`,
    status: "pending",
  }));
  assert.deepEqual(displayModule.visibleLabelIndexes(nodes, 3, true), [0, 1, 2, 3, 4, 5]);
  assert.deepEqual(displayModule.visibleLabelIndexes(nodes, 3, false), [2, 3, 4]);
  assert.deepEqual(displayModule.visibleLabelIndexes(nodes, 0, false), [0, 1]);
});

test("workflow action identity is stable across polling object refreshes", () => {
  const action = {
    kind: "workflow_retry",
    workflow_run_id: "run-1",
    retry_from_step_id: "retry-node",
    step_id: "failed-node",
    action_id: "action-1",
    values: { note: "server value" },
  };
  assert.equal(
    displayModule.actionIdentity(action),
    displayModule.actionIdentity({ ...action, values: { note: "fresh poll value" } }),
  );
  for (const changed of [
    { ...action, kind: "workflow_starter_input" },
    { ...action, workflow_run_id: "run-2" },
    { ...action, retry_from_step_id: "other-retry" },
    { ...action, step_id: "other-step" },
    { ...action, action_id: "action-2" },
  ]) {
    assert.notEqual(displayModule.actionIdentity(action), displayModule.actionIdentity(changed));
  }
});

test("schema parser enforces minItems and numeric bounds", () => {
  const arrayErrors = {};
  const parsedArray = schemaModule.parseWorkflowSchemaDraft(
    { type: "array", items: { type: "string" }, minItems: 2 },
    "one",
    "inputs.tags",
    false,
    arrayErrors,
    validationMessages,
  );
  assert.deepEqual(parsedArray, ["one"]);
  assert.equal(arrayErrors["inputs.tags"], "min-items:2");

  for (const [draft, expected] of [["4", "minimum:5"], ["11", "maximum:10"]]) {
    const numberErrors = {};
    const parsedNumber = schemaModule.parseWorkflowSchemaDraft(
      { type: "number", minimum: 5, maximum: 10 },
      draft,
      "inputs.count",
      false,
      numberErrors,
      validationMessages,
    );
    assert.equal(parsedNumber, undefined);
    assert.equal(numberErrors["inputs.count"], expected);
  }
});

test("schema parser restores boolean enum values before coercion", () => {
  const errors = {};
  const parsed = schemaModule.parseWorkflowSchemaDraft(
    { type: "boolean", enum: [true, false] },
    "false",
    "inputs.enabled",
    true,
    errors,
    validationMessages,
  );

  assert.equal(parsed, false);
  assert.deepEqual(errors, {});
});

test("schema parser enforces declared string, numeric, and array constraints", () => {
  const cases = [
    [{ type: "string", minLength: 3 }, "ab", "min-length:3"],
    [{ type: "string", maxLength: 3 }, "abcd", "max-length:3"],
    [{ type: "number", exclusiveMinimum: 5 }, "5", "exclusive-minimum:5"],
    [{ type: "number", exclusiveMaximum: 10 }, "10", "exclusive-maximum:10"],
    [{ type: "array", items: { type: "string" }, maxItems: 2 }, "one\ntwo\nthree", "max-items:2"],
    [{ type: "array", items: { type: "string" }, uniqueItems: true }, "one\none", "unique-items"],
  ];

  for (const [schema, draft, expectedError] of cases) {
    const errors = {};
    schemaModule.parseWorkflowSchemaDraft(
      schema,
      draft,
      "inputs.value",
      false,
      errors,
      validationMessages,
    );
    assert.equal(errors["inputs.value"], expectedError);
  }
});

test("schema parser preserves required, pattern, and enum validation", () => {
  const requiredErrors = {};
  assert.equal(schemaModule.parseWorkflowSchemaDraft(
    { type: "string" }, "", "inputs.name", true, requiredErrors, validationMessages,
  ), undefined);
  assert.equal(requiredErrors["inputs.name"], "required");

  const patternErrors = {};
  assert.equal(schemaModule.parseWorkflowSchemaDraft(
    { type: "string", pattern: "^ok$" }, "no", "inputs.code", false,
    patternErrors, validationMessages,
  ), undefined);
  assert.equal(patternErrors["inputs.code"], "invalid-format");

  const enumErrors = {};
  assert.equal(schemaModule.parseWorkflowSchemaDraft(
    { type: "string", enum: ["one", "two"] }, "three", "inputs.choice", false,
    enumErrors, validationMessages,
  ), undefined);
  assert.equal(enumErrors["inputs.choice"], "required");
});

test("schema field IDs are namespaced and connect help and error descriptions", () => {
  const first = schemaModule.workflowSchemaFieldIds("instance-a", "inputs", ["request", "url"]);
  const second = schemaModule.workflowSchemaFieldIds("instance-b", "inputs", ["request", "url"]);
  assert.notEqual(first.fieldId, second.fieldId);
  assert.match(first.helpId, /instance-a/);
  assert.match(first.errorId, /instance-a/);
  assert.deepEqual(
    schemaModule.workflowSchemaDescribedBy(first, true, true),
    `${first.helpId} ${first.errorId}`,
  );
});

test("intervention node selection follows declared retry precedence", () => {
  const run = {
    id: "run-1",
    title: "Run",
    status: "failed",
    currentNodeId: "current-node",
    nodes: [
      { id: "failed-node", name: "Failed", status: "failed" },
      { id: "current-node", name: "Current", status: "paused" },
      { id: "action-node", name: "Action", status: "pending" },
      { id: "retry-node", name: "Retry", status: "pending" },
    ],
  };
  const action = {
    kind: "workflow_retry",
    retry_from_step_id: "retry-node",
    step_id: "action-node",
  };

  assert.equal(displayModule.interventionNodeIndex(run, action), 3);
  assert.equal(displayModule.interventionNodeIndex(run, { ...action, retry_from_step_id: "" }), 2);
  assert.equal(
    displayModule.interventionNodeIndex({ ...run, currentNodeId: "current-node" }, {
      ...action,
      retry_from_step_id: "",
      step_id: "",
    }),
    1,
  );
  assert.equal(
    displayModule.interventionNodeIndex({ ...run, currentNodeId: "" }, {
      ...action,
      retry_from_step_id: "",
      step_id: "",
    }),
    0,
  );
});

test("real execution progress excludes outcomes and explicit skips", () => {
  const nodes = [
    { id: "start", name: "Start", type: "trigger", status: "completed" },
    { id: "retry", name: "Check readiness", type: "tool", status: "completed" },
    { id: "branch", name: "Unused branch", type: "transform", status: "skipped" },
    { id: "future", name: "Produce video", type: "agent", status: "pending" },
    { id: "needs_input", name: "Input required", type: "end", status: "completed" },
  ];
  const progressNodes = displayModule.workflowProgressNodes(nodes);

  assert.deepEqual(progressNodes.map((node) => node.id), [
    "start",
    "retry",
    "branch",
    "future",
  ]);
  assert.equal(displayModule.processedNodeCount(progressNodes), 2);
  assert.equal(displayModule.progressNodeCount(progressNodes), 3);
  assert.equal(displayModule.notReachedNodeCount(progressNodes), 1);
});

test("retry interventions retain retry semantics after an actionable completion", () => {
  const run = {
    id: "run-needs-input",
    title: "Create product video",
    status: "completed",
    currentNodeId: "needs_input",
    businessOutcome: "needs_input",
    action: {
      kind: "workflow_retry",
      retry_from_step_id: "browser_preflight",
      step_id: "explore_product",
    },
    nodes: [
      { id: "browser_preflight", name: "Check browser readiness", status: "completed" },
      { id: "explore_product", name: "Explore product", status: "completed" },
      { id: "needs_input", name: "Input required", type: "end", status: "completed" },
    ],
  };

  assert.equal(displayModule.currentNodeIndex(run), 0);
  assert.equal(displayModule.workflowCurrentStepLabelKey(run), "retry_from");
  assert.equal(displayModule.workflowRunActionLabelKey(run, "retry"), "component.workflow_run.action.retry");
  assert.equal(
    displayModule.workflowRunInputFieldsLabelKey(run.action),
    "component.workflow_run.retry_fields",
  );
  assert.equal(
    displayModule.workflowCurrentStepLabelKey({ ...run, status: "failed" }),
    "retry_from",
  );
  assert.equal(
    displayModule.workflowRunActionLabelKey({ ...run, status: "failed" }, "retry_now"),
    "component.workflow_run.action.retry",
  );
  assert.equal(
    displayModule.workflowRunInputFieldsLabelKey(run.action),
    "component.workflow_run.retry_fields",
  );
  assert.equal(
    displayModule.workflowCurrentStepLabelKey({ ...run, action: null }),
    "current_step",
  );
  assert.equal(
    displayModule.workflowCurrentStepLabelKey({
      ...run,
      status: "paused",
      action: { kind: "workflow_resume", step_id: "browser_preflight" },
    }),
    "current_step",
  );
  assert.equal(
    displayModule.workflowRunActionLabelKey({ ...run, status: "paused" }, "resume"),
    "component.workflow_run.action.resume",
  );
  assert.equal(
    displayModule.currentNodeIndex({
      ...run,
      action: { ...run.action, retry_from_step_id: "needs_input" },
    }),
    2,
  );
});

test("workflow run card links to its definition and aggregate History record", () => {
  assert.match(progressSource, /workflowHref\?: string/);
  assert.match(progressSource, /historyHref\?: string/);
  assert.match(progressSource, /component\.workflow_run\.open_definition/);
  assert.match(progressSource, /component\.workflow_run\.open_history/);
  assert.match(hostSource, /`\/flows\?workflow=\$\{encodeURIComponent\(foregroundRun\.workflowId\)\}`/);
  assert.match(hostSource, /workflow_view=history/);
  assert.match(hostSource, /workflow_run=\$\{encodeURIComponent\(foregroundRun\.id\)\}/);
  for (const source of localeSources) {
    assert.match(source, /"component\.workflow_run\.open_definition"/);
    assert.match(source, /"component\.workflow_run\.open_history"/);
  }
});

test("workflow run header accepts a final action slot", () => {
  assert.match(progressSource, /type ReactNode/);
  assert.match(progressSource, /headerAction\?: ReactNode/);
  assert.match(
    progressSource,
    /className="workflow-run-header-actions"[\s\S]*?className="workflow-run-status"[\s\S]*?<\/span>\s*\{headerAction\}\s*<\/div>/,
  );
});

test("workflow progress uses one compact status strip for every graph size", () => {
  assert.match(progressSource, /workflow-run-progress-bar/);
  assert.match(progressSource, /const nodes = run\.nodes/);
  assert.match(progressSource, /const progressNodes = workflowProgressNodes\(nodes\)/);
  assert.match(progressSource, /currentNodeIndex\(run\)/);
  assert.match(progressSource, /processedNodeCount\(progressNodes\)/);
  assert.match(progressSource, /progressNodeCount\(progressNodes\)/);
  assert.match(progressSource, /notReachedNodeCount\(progressNodes\)/);
  assert.match(progressSource, /processed\s*\/\s*Math\.max\(progressTotal,\s*1\)/);
  assert.match(progressSource, /notReachedNodeCount/);
  assert.match(progressSource, /nodes\[activeIndex\]/);
  assert.match(progressSource, /role="progressbar"/);
  assert.ok(
    progressSource.indexOf('role="progressbar"')
      < progressSource.indexOf('className="workflow-run-summary-button"'),
  );
  assert.match(progressSource, /aria-valuenow=\{processed\}/);
  assert.match(progressSource, /aria-valuetext=\{progressValueText\}/);
  assert.match(progressSource, /const progressMaximum = Math\.max\(progressTotal, 1\)/);
  assert.match(progressSource, /aria-valuemax=\{progressMaximum\}/);
  assert.match(progressSource, /Math\.min\(100, Math\.max\(0, progress \* 100\)\)/);
  assert.doesNotMatch(progressSource, /ResizeObserver|DENSE_SEGMENT_WIDTH|workflow-run-segment/);
  assert.doesNotMatch(workflowCss, /\.workflow-run-rail-scroller|\.workflow-run-segment/);
  assert.doesNotMatch(progressSource, /Planning|Recording|Voiceover|Composition/);
});

test("workflow progress and intervention share one execution dock surface", () => {
  assert.match(hostSource, /workspace-workflow-run-panel/);
  assert.match(
    hostSource,
    /workspace-workflow-run-panel[\s\S]*?<WorkflowRunProgress[\s\S]*?<WorkflowRunIntervention/,
  );
  assert.match(workflowCss, /\.workspace-workflow-run-panel\s*\{[^}]*background:\s*var\(--glass-card\)/);
  assert.match(workflowCss, /\.workspace-workflow-run-panel\s*\{[^}]*box-shadow:\s*var\(--shadow-ambient\)/);
  assert.doesNotMatch(
    workflowCss,
    /\.workflow-run-progress\s*\{[^}]*background:\s*var\(--glass-card\)/,
  );
  assert.match(workflowCss, /\.workflow-run-intervention\s*\{[\s\S]*?border-top:/);
  assert.match(progressSource, /component\.workflow_run\.executed/);
  assert.match(progressSource, /component\.workflow_run\.not_reached/);
});

test("workflow run host is an independent compact card", () => {
  assert.match(
    cssSource,
    /\.embedded-chat-footer > \.chat-composer\s*\{[^}]*width:\s*min\(100%, var\(--chat-thread-max-width, 920px\)\)/,
  );
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-host\s*\{[^}]*padding:\s*0 24px 8px/,
  );
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-switcher\s*\{[^}]*width:\s*min\(100%, 760px\)[^}]*margin:\s*0 auto/,
  );
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-panel\s*\{[^}]*width:\s*min\(100%, 760px\)[^}]*margin:\s*0 auto/,
  );
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-panel\s*\{[^}]*box-shadow:\s*var\(--shadow-ambient\)/,
  );
});

test("workspace agent menu is a compact composer-aligned popover", () => {
  assert.match(chatSource, /className="workspace-chat-agent-menu"/);
  assert.match(chatSource, /className="workspace-chat-agent-menu-item"/);
  assert.match(chatSource, /data-active=\{idx === mentionActiveIdx\}/);
  assert.match(
    cssSource,
    /\.workspace-chat-agent-menu\s*\{[^}]*width:\s*min\(480px, calc\(100% - 48px\)\)/,
  );
  assert.match(
    cssSource,
    /@media\s*\(max-width:\s*640px\)[\s\S]*?\.workspace-chat-agent-menu\s*\{[^}]*width:\s*calc\(100% - 48px\)/,
  );
});

test("workflow card keeps its chrome fixed while details scroll", () => {
  assert.match(
    cssSource,
    /--shadow-ambient:\s*\n\s*0 0 0 1px[^;]+,\s*\n\s*0 0 [^;]+;/,
  );
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-panel\s*\{[^}]*overflow:\s*hidden/,
  );
  assert.doesNotMatch(
    workflowCss,
    /\.workspace-workflow-run-panel\s*\{[^}]*overflow-y:\s*auto/,
  );
  assert.match(
    workflowCss,
    /\.workflow-run-intervention-body\s*\{[^}]*max-height:[^}]*overflow-y:\s*auto[^}]*overscroll-behavior:\s*contain[^}]*scrollbar-width:\s*thin/,
  );
  assert.match(
    workflowCss,
    /\.workflow-run-node-list-shell\s*\{[^}]*max-height:[^}]*overflow-y:\s*auto/,
  );
});

test("progress expansion is button and keyboard accessible", () => {
  assert.match(progressSource, /type="button"/);
  assert.match(progressSource, /aria-expanded=\{expanded\}/);
  assert.match(progressSource, /aria-controls=\{listId\}/);
  assert.match(progressSource, /id=\{listId\}/);
  assert.match(progressSource, /aria-current=/);
  assert.match(progressSource, /WorkflowNodeStatusIcon/);
  assert.match(progressSource, /hiddenNodeCount > 0/);
  assert.match(progressSource, /aria-expanded=\{showAllNodes\}/);
  assert.match(
    workflowCss,
    /@container workflow-run \(max-width: 680px\)[\s\S]*?\.workflow-run-summary\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/,
  );
});

test("workflow intervention reuses schema fields and renders only allowed actions", () => {
  assert.match(
    interventionSource,
    /from "\.\/WorkflowSchemaFields"/,
  );
  assert.match(interventionSource, /WorkflowSchemaFields/);
  assert.match(interventionSource, /workflowSchemaDraft/);
  assert.match(interventionSource, /parseWorkflowSchemaDraft/);
  assert.match(interventionSource, /action\.options/);
  assert.match(interventionSource, /secondaryOptions\.map/);
  assert.match(interventionSource, /workflow_starter_input/);
  assert.match(interventionSource, /workflow_retry/);
  assert.match(interventionSource, /preserved_receipts/);
  assert.match(interventionSource, /required_change/);
  assert.match(interventionSource, /formatWorkflowError/);
  assert.match(interventionSource, /interventionNodeIndex/);
  assert.match(interventionSource, /actionIdentity/);
  assert.match(interventionSource, /stableActionIdentity/);
  assert.match(interventionSource, /\[stableActionIdentity\]/);
  assert.match(interventionSource, /disabled/);
  assert.match(interventionSource, /loading/);
  assert.match(interventionSource, /role="alert"/);
  assert.doesNotMatch(interventionSource, /Planning|Recording|Voiceover|Composition/);
});

test("workflow intervention starts as a compact actionable blocker", () => {
  assert.match(interventionSource, /const \[expanded, setExpanded\] = useState\(false\)/);
  assert.match(interventionSource, /workflow-run-intervention-summary/);
  assert.match(interventionSource, /workflow-run-intervention-preview/);
  assert.match(interventionSource, /aria-expanded=\{expanded\}/);
  assert.match(interventionSource, /workflow-run-intervention-body/);
  assert.match(interventionSource, /allowedOptions\.find/);
  assert.match(interventionSource, /setExpanded\(true\)/);
  assert.match(interventionSource, /expanded && \(/);
  assert.match(interventionSource, /action\.prompt/);
  assert.match(interventionSource, /secondaryOptions\.length > 0/);
  assert.match(interventionSource, /secondaryOptions\.map/);
  assert.match(interventionSource, /isPreviewScaffold/);
});

test("workflow approval interventions expose their structured review in details", () => {
  assert.match(approvalReviewSource, /export default function WorkflowApprovalReview/);
  assert.match(interventionSource, /import WorkflowApprovalReview from "\.\/WorkflowApprovalReview"/);
  assert.match(
    interventionSource,
    /const hasReview = action\.kind === "workflow_approval" && action\.review != null/,
  );
  assert.match(interventionSource, /hasDetails = Boolean\([\s\S]*?hasReview/);
  assert.match(interventionSource, /<WorkflowApprovalReview[\s\S]*?review=\{action\.review\}/);
  assert.match(interventionSource, /reviewTitle=\{action\.review_title as string \| undefined\}/);
  assert.ok(
    interventionSource.indexOf("<WorkflowApprovalReview")
      < interventionSource.indexOf("{hasFields && ("),
    "the plan review should appear before editable recovery fields",
  );
  assert.match(
    workflowCss,
    /\.workflow-run-intervention-body \.chat-workflow-review-scroll\s*\{[^}]*max-height:\s*none[^}]*overflow:\s*visible/,
  );
});

test("chat action cards and interventions share one schema implementation", () => {
  assert.match(actionSource, /from "\.\.\/workflows\/WorkflowSchemaFields"/);
  assert.match(actionSource, /WorkflowSchemaFields/);
  assert.match(actionSource, /parseWorkflowSchemaDraft/);
  assert.match(schemaSource, /export function WorkflowSchemaFields/);
  assert.match(schemaLogicSource, /export function parseWorkflowSchemaDraft/);
  assert.equal((schemaSource.match(/function WorkflowSchemaFields/g) || []).length, 1);
  assert.match(schemaSource, /parseWorkflowSchemaDraftPure/);
  assert.equal((schemaLogicSource.match(/function parseWorkflowSchemaDraft/g) || []).length, 1);
  assert.doesNotMatch(actionSource, /function WorkflowSchemaFields/);
  assert.doesNotMatch(interventionSource, /function WorkflowSchemaFields/);
});

test("schema fields namespace controls and expose validation semantics", () => {
  assert.match(schemaSource, /useId/);
  assert.match(schemaSource, /idNamespace/);
  assert.match(schemaSource, /workflowSchemaFieldIds/);
  assert.match(schemaSource, /aria-describedby=\{describedBy\}/);
  assert.match(schemaSource, /aria-required=\{isRequired\}/);
  assert.match(schemaSource, /required=\{isRequired\}/);
  assert.match(schemaSource, /aria-invalid=\{Boolean\(error\)\}/);
  assert.match(schemaSource, /id=\{fieldIds\.helpId\}/);
  assert.match(schemaSource, /id=\{fieldIds\.errorId\}/);
});

test("workflow surfaces localize their labels in English, Chinese, and Spanish", () => {
  for (const localeSource of localeSources) {
    for (const validationKey of [
      "workflow_input_min_length",
      "workflow_input_max_length",
      "workflow_input_max_items",
      "workflow_input_unique_items",
      "workflow_input_exclusive_minimum",
      "workflow_input_exclusive_maximum",
    ]) {
      assert.match(localeSource, new RegExp(`component\\.workspace_chat\\.${validationKey}`));
    }
    assert.match(localeSource, /"component\.workflow_run\.progress"/);
    assert.match(localeSource, /"component\.workflow_run\.processed"/);
    assert.match(localeSource, /"component\.workflow_run\.status\.cancelled"/);
    assert.match(localeSource, /"component\.workflow_run\.required_change"/);
    assert.match(localeSource, /"component\.workflow_run\.action\.retry"/);
    assert.match(localeSource, /"component\.workflow_run\.action\.continue"/);
    assert.match(localeSource, /"component\.workflow_run\.continue_from"/);
    assert.match(localeSource, /"component\.workflow_run\.continue_fields"/);
    assert.match(localeSource, /"component\.workflow_run\.error_truncated"/);
    assert.match(localeSource, /"component\.workflow_run\.switcher_label"/);
    assert.match(localeSource, /"component\.workflow_run\.refresh_error"/);
    assert.match(localeSource, /"component\.workflow_run\.cancel_confirm_title"/);
    assert.match(localeSource, /"component\.workflow_run\.cancel_confirm_message"/);
    assert.match(localeSource, /"component\.workflow_run\.cancel_confirm_action"/);
    assert.match(localeSource, /"component\.workflow_run\.cancel_confirm_keep_running"/);
  }
  assert.doesNotMatch(progressSource, />\s*(Pending|Running|Completed|Paused|Failed|Skipped|Cancelled)\s*</);
  assert.doesNotMatch(interventionSource, />\s*(Reason|Required change|Preserved artifacts)\s*</);
  assert.match(
    interventionSource,
    /const truncationText = t\("component\.workflow_run\.error_truncated"\)/,
  );
  assert.match(interventionSource, /formatWorkflowError\([\s\S]*?truncationText/);
});

test("workflow run CSS is token-based, responsive, and reduced-motion safe", () => {
  assert.notEqual(cssStart, -1);
  assert.notEqual(cssEnd, -1);
  assert.match(workflowCss, /var\(--glass-card\)/);
  assert.match(workflowCss, /var\(--shadow-sm\)/);
  assert.match(workflowCss, /var\(--radius-control\)/);
  assert.match(workflowCss, /@media \(max-width: 640px\)/);
  assert.match(workflowCss, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(workflowCss, /:focus-visible/);
  assert.doesNotMatch(workflowCss, /#[0-9a-f]{3,8}/i);
  assert.doesNotMatch(workflowCss, /rgba?\(/i);
  assert.doesNotMatch(workflowCss, /gradient\(/i);
  assert.doesNotMatch(workflowCss, /border:\s*1px/i);
});

test("workspace workflow host groups only run-owned runtime rows and actions", async () => {
  const hostModule = await loadHostHelpers();
  const messages = [
    {
      id: "activity-1",
      created_at: "2026-07-28T08:00:00Z",
      message_kind: "workflow_activity",
      meta: { workflow_run_id: "run-1", workflow_status: "running" },
    },
    {
      id: "starter-1",
      created_at: "2026-07-28T08:00:01Z",
      message_kind: "hitl_request",
      refs: [{ type: "workflow_run", id: "run-1" }],
      pending_action: { kind: "workflow_starter_input" },
    },
    {
      id: "retry-2",
      created_at: "2026-07-28T08:00:02Z",
      message_kind: "workflow_activity",
      meta: { workflow_run_id: "run-2", workflow_status: "failed" },
      pending_action: { kind: "workflow_retry", workflow_run_id: "run-2" },
    },
    {
      id: "approval-1",
      created_at: "2026-07-28T08:00:03Z",
      message_kind: "hitl_request",
      meta: { workflow_run_id: "run-1" },
      pending_action: { kind: "workflow_approval", workflow_run_id: "run-1" },
    },
    {
      id: "input-1",
      created_at: "2026-07-28T08:00:04Z",
      message_kind: "hitl_request",
      refs: [{ type: "workflow_run", id: "run-1" }],
      pending_action: { kind: "workflow_input" },
    },
    {
      id: "summary-1",
      created_at: "2026-07-28T08:00:05Z",
      message_kind: "agent_update",
      meta: { workflow_run_id: "run-1", workflow_final_output: true },
      body: "The final result is ready.",
    },
    {
      id: "text-1",
      created_at: "2026-07-28T08:00:06Z",
      message_kind: "text",
      refs: [{ type: "workflow_run", id: "run-1" }],
      body: "Keep this ordinary message.",
    },
    {
      id: "task-1",
      created_at: "2026-07-28T08:00:07Z",
      message_kind: "step_event",
      meta: { workflow_run_id: "run-1" },
      body: "Keep task activity.",
    },
    {
      id: "governance-1",
      created_at: "2026-07-28T08:00:08Z",
      message_kind: "hitl_request",
      meta: { workflow_run_id: "run-1" },
      pending_action: { kind: "governance_approval", workflow_run_id: "run-1" },
    },
  ];

  const groups = hostModule.buildWorkspaceWorkflowRunGroups(messages);
  assert.deepEqual(groups.map((group) => group.id), ["run-1", "run-2"]);
  assert.equal(groups[0].activityMessage.id, "activity-1");
  assert.equal(groups[0].actionMessage.id, "input-1");
  assert.equal(groups[1].actionMessage.id, "retry-2");

  const owned = hostModule.workflowHostOwnedMessageIds(groups);
  assert.deepEqual(
    [...owned].sort(),
    ["activity-1", "approval-1", "input-1", "retry-2", "starter-1"],
  );
  for (const ordinaryId of ["summary-1", "text-1", "task-1", "governance-1"]) {
    assert.equal(owned.has(ordinaryId), false, `${ordinaryId} must remain in Chat`);
  }
});

test("workspace workflow host foreground selection keeps only recoverable business states", async () => {
  const hostModule = await loadHostHelpers();
  for (const status of ["queued", "pending", "running", "paused", "failed"]) {
    assert.equal(hostModule.isWorkspaceWorkflowRunActionable({ status }), true);
  }
  assert.equal(
    hostModule.isWorkspaceWorkflowRunActionable({
      status: "completed",
      businessOutcome: "needs_input",
    }),
    true,
  );
  for (const businessOutcome of [
    undefined,
    "accepted",
    "completed",
    "revision_required",
    "ready_for_acceptance",
  ]) {
    assert.equal(
      hostModule.isWorkspaceWorkflowRunActionable({ status: "completed", businessOutcome }),
      false,
    );
  }
  assert.equal(hostModule.isWorkspaceWorkflowRunActionable({ status: "cancelled" }), false);

  const groups = [
    { id: "older", latestIndex: 2, projection: { status: "failed" } },
    { id: "newer", latestIndex: 8, projection: { status: "running" } },
    { id: "accepted", latestIndex: 10, projection: { status: "completed", businessOutcome: "accepted" } },
    {
      id: "revision",
      latestIndex: 12,
      projection: { status: "completed", businessOutcome: "revision_required" },
      actionMessage: { pending_action: { kind: "workflow_retry" } },
    },
  ];
  assert.equal(hostModule.selectForegroundWorkflowRunId(groups, ""), "newer");
  assert.equal(hostModule.selectForegroundWorkflowRunId(groups, "older"), "older");
  assert.equal(hostModule.selectForegroundWorkflowRunId(groups, "accepted"), "newer");
});

test("resolved cancelled workflow retry is not selected after message reload", async () => {
  const hostModule = await loadHostHelpers();
  const groups = hostModule.buildWorkspaceWorkflowRunGroups([
    {
      id: "activity-cancelled",
      created_at: "2026-07-28T08:00:00Z",
      updated_at: "2026-07-28T08:01:00Z",
      message_kind: "workflow_activity",
      refs: [{ type: "workflow_run", id: "run-cancelled" }],
      meta: {
        workflow_run_id: "run-cancelled",
        workflow_status: "cancelled",
        workflow_business_outcome: "needs_input",
      },
      pending_action: null,
      resolved_at: "2026-07-28T08:01:00Z",
    },
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].projection.status, "cancelled");
  assert.equal(groups[0].projection.action, null);
  assert.equal(groups[0].actionMessage, null);
  assert.equal(hostModule.selectForegroundWorkflowRunId(groups, ""), "");
});

test("workspace workflow host excludes attempts superseded by a newer retry", async () => {
  const hostModule = await loadHostHelpers();
  const groups = [
    {
      id: "failed-parent",
      latestIndex: 2,
      projection: { status: "failed" },
      retryOfRunId: null,
    },
    {
      id: "retry-child",
      latestIndex: 8,
      projection: { status: "running" },
      retryOfRunId: "failed-parent",
    },
  ];
  assert.deepEqual(
    hostModule.excludeSupersededWorkflowRunGroups(groups).map((group) => group.id),
    ["retry-child"],
  );

  const detailLinkedGroups = groups.map((group) => ({ ...group, retryOfRunId: null }));
  assert.deepEqual(
    hostModule.excludeSupersededWorkflowRunGroups(detailLinkedGroups, {
      "retry-child": { retry_of_run_id: "failed-parent" },
    }).map((group) => group.id),
    ["retry-child"],
  );
  assert.deepEqual(
    hostModule.excludeSupersededWorkflowRunGroups([
      { ...groups[0], latestIndex: 9 },
      groups[1],
    ]).map((group) => group.id),
    ["failed-parent", "retry-child"],
  );
});

test("workflow host resolves the full retry lineage before selecting actionable runs", async () => {
  const hostModule = await loadHostHelpers();
  const groups = [
    {
      id: "needs-input",
      latestIndex: 2,
      projection: { status: "completed", businessOutcome: "needs_input" },
      retryOfRunId: null,
    },
    {
      id: "revision-required",
      latestIndex: 5,
      projection: { status: "completed", businessOutcome: "revision_required" },
      retryOfRunId: "needs-input",
    },
    {
      id: "completed",
      latestIndex: 8,
      projection: { status: "completed", businessOutcome: "completed" },
      retryOfRunId: "revision-required",
    },
  ];

  assert.deepEqual(
    hostModule.actionableWorkflowRunGroups(groups).map((group) => group.id),
    [],
  );
});

test("workflow projection status wins only when its run timestamp is newer", async () => {
  const hostModule = await loadHostHelpers();
  const detail = {
    id: "run-1",
    status: "running",
    current_step_id: "old-step",
    error: "old detail error",
    updated_at: "2026-07-28T08:00:00Z",
  };
  const projection = {
    id: "run-1",
    title: "Run",
    status: "failed",
    currentNodeId: "new-step",
    error: "new projection error",
    nodes: [],
  };
  const freshProjection = hostModule.mergeWorkflowRunView(
    detail,
    projection,
    "2026-07-28T08:00:01Z",
  );
  assert.equal(freshProjection.status, "failed");
  assert.equal(freshProjection.currentNodeId, "new-step");
  assert.equal(freshProjection.error, "new projection error");

  const staleProjection = hostModule.mergeWorkflowRunView(
    detail,
    projection,
    "2026-07-28T07:59:59Z",
  );
  assert.equal(staleProjection.status, "running");
  assert.equal(staleProjection.currentNodeId, "old-step");
  assert.equal(staleProjection.error, "old detail error");
});

test("newer compact Run state replaces stale projected steps and intervention", async () => {
  const hostModule = await loadHostHelpers();
  const projection = {
    id: "run-compact",
    title: "Compact run",
    status: "failed",
    currentNodeId: "stale-step",
    businessOutcome: "needs_input",
    error: "stale error",
    nodes: [{ id: "stale-step", name: "Stale", status: "failed" }],
    action: { kind: "workflow_retry", step_id: "stale-step", options: ["retry"] },
  };
  const retryAction = {
    kind: "workflow_retry",
    step_id: "publish",
    retry_from_step_id: "publish",
    editable_input_schema: { type: "object", properties: {} },
    values: {},
    options: ["retry", "cancel"],
  };
  const authoritative = hostModule.mergeWorkflowRunView(
    {
      id: "run-compact",
      status: "failed",
      current_step_id: "publish",
      updated_at: "2026-07-28T08:02:00Z",
      workflow_steps: [
        { id: "start", name: "Start", type: "trigger", status: "completed" },
        { id: "publish", name: "Publish", type: "agent", status: "failed" },
      ],
      business_outcome: "revision_required",
      error: "authoritative error",
      intervention: retryAction,
    },
    projection,
    "2026-07-28T08:01:00Z",
  );
  assert.deepEqual(authoritative.nodes.map((node) => [node.id, node.status]), [
    ["start", "completed"],
    ["publish", "failed"],
  ]);
  assert.equal(authoritative.currentNodeId, "publish");
  assert.equal(authoritative.businessOutcome, "revision_required");
  assert.equal(authoritative.error, "authoritative error");
  assert.deepEqual(authoritative.action, retryAction);

  const cancelled = hostModule.mergeWorkflowRunView(
    {
      id: "run-compact",
      status: "cancelled",
      current_step_id: null,
      updated_at: "2026-07-28T08:03:00Z",
      workflow_steps: authoritative.nodes,
      business_outcome: "in_progress",
      error: null,
      intervention: null,
    },
    authoritative,
    "2026-07-28T08:02:00Z",
  );
  assert.equal(cancelled.status, "cancelled");
  assert.equal(cancelled.currentNodeId, null);
  assert.equal(cancelled.error, null);
  assert.equal(cancelled.action, null);
  assert.equal(hostModule.isWorkspaceWorkflowRunActionable(cancelled), false);
});

test("newer compact action overrides stale Chat action and owns resolution", async () => {
  const hostModule = await loadHostHelpers();
  const groups = hostModule.buildWorkspaceWorkflowRunGroups([
    {
      id: "activity-stale",
      created_at: "2026-07-28T08:00:00Z",
      updated_at: "2026-07-28T08:01:00Z",
      message_kind: "workflow_activity",
      meta: {
        workflow_run_id: "run-actions",
        workflow_status: "failed",
      },
    },
    {
      id: "chat-message-stale",
      created_at: "2026-07-28T08:00:30Z",
      updated_at: "2026-07-28T08:01:00Z",
      message_kind: "hitl_request",
      meta: { workflow_run_id: "run-actions" },
      pending_action: {
        kind: "workflow_approval",
        workflow_run_id: "run-actions",
        step_id: "review",
        options: ["approve", "cancel"],
      },
    },
  ]);
  const staleProjection = groups[0].projection;
  assert.equal(staleProjection.action.message_id, "chat-message-stale");
  assert.equal(staleProjection.action.source, "workspace_chat");

  const compactAction = {
    kind: "workflow_retry",
    workflow_run_id: "run-actions",
    message_id: "compact-message",
    source: "workspace_chat",
    retry_from_step_id: "publish",
    options: ["retry", "cancel"],
  };
  const foregroundRun = hostModule.mergeWorkflowRunView(
    {
      id: "run-actions",
      status: "failed",
      current_step_id: "publish",
      updated_at: "2026-07-28T08:02:00Z",
      workflow_steps: [],
      business_outcome: "revision_required",
      intervention: compactAction,
    },
    staleProjection,
    groups[0].projectionUpdatedAt,
  );
  const selectedAction = hostModule.selectWorkspaceWorkflowInterventionAction(foregroundRun);
  assert.equal(foregroundRun.action, compactAction);
  assert.equal(selectedAction, compactAction);
  assert.equal(
    hostModule.selectWorkspaceWorkflowInterventionAction({
      ...foregroundRun,
      status: "paused",
      action: null,
    }),
    null,
  );

  const calls = [];
  const files = [{ name: "brief.txt" }];
  const payload = { variables: { correction: "ready" } };
  const handled = await hostModule.resolveWorkspaceWorkflowMessageAction(
    selectedAction,
    "retry",
    "Try again",
    payload,
    files,
    (...args) => calls.push(args),
  );
  assert.equal(handled, true);
  assert.deepEqual(calls, [[
    "compact-message",
    "retry",
    "Try again",
    payload,
    files,
  ]]);
  assert.match(
    hostSource,
    /selectWorkspaceWorkflowInterventionAction\(foregroundRun\)/,
  );
  assert.doesNotMatch(hostSource, /messageAction \|\| foregroundRun\.action/);
  assert.match(
    hostSource,
    /resolveWorkspaceWorkflowMessageAction\([\s\S]*?onResolveMessage/,
  );
});

test("truncated compact action hydrates from the same message-backed Chat action", async () => {
  const hostModule = await loadHostHelpers();
  const fullRetryAction = {
    kind: "workflow_retry",
    workflow_run_id: "run-large-retry",
    message_id: "retry-message",
    source: "workspace_chat",
    retry_from_step_id: "browser_preflight",
    editable_input_schema: {
      type: "object",
      properties: {
        request: {
          type: "object",
          properties: { start_url: { type: "string", format: "uri" } },
        },
      },
    },
    values: { request: { start_url: "http://localhost:3010/dashboard" } },
    observed_problem: ["Workspace route was not observed"],
    required_change: "Correct the start URL and retry.",
    options: ["retry", "cancel"],
  };
  const projection = {
    id: "run-large-retry",
    title: "Create product video",
    status: "completed",
    currentNodeId: "needs_input",
    businessOutcome: "needs_input",
    nodes: [{ id: "needs_input", name: "Input required", status: "completed" }],
    action: fullRetryAction,
  };
  const foregroundRun = hostModule.mergeWorkflowRunView(
    {
      id: "run-large-retry",
      status: "completed",
      current_step_id: "needs_input",
      updated_at: "2026-07-29T04:00:00Z",
      workflow_steps: projection.nodes,
      business_outcome: "needs_input",
      intervention: {
        truncated: true,
        preview: '{"kind":"workflow_retry"',
        message_id: "retry-message",
        source: "workspace_chat",
      },
    },
    projection,
    "2026-07-29T03:59:00Z",
  );

  assert.equal(foregroundRun.action.kind, "workflow_retry");
  assert.equal(foregroundRun.action.message_id, "retry-message");
  assert.deepEqual(foregroundRun.action.editable_input_schema, fullRetryAction.editable_input_schema);
  assert.deepEqual(foregroundRun.action.values, fullRetryAction.values);
  assert.deepEqual(foregroundRun.action.options, ["retry", "cancel"]);

  const unrelated = hostModule.mergeWorkflowRunView(
    {
      id: "run-large-retry",
      status: "completed",
      updated_at: "2026-07-29T04:00:00Z",
      intervention: {
        kind: "workflow_retry",
        message_id: "new-retry-message",
        source: "workspace_chat",
        options: ["cancel"],
      },
    },
    projection,
    "2026-07-29T03:59:00Z",
  );
  assert.deepEqual(unrelated.action.options, ["cancel"]);
  assert.equal(unrelated.action.editable_input_schema, undefined);
});

test("workflow host keeps recoverable waiting states visible without polling them", async () => {
  const hostModule = await loadHostHelpers();
  for (const status of ["paused", "failed"]) {
    assert.equal(hostModule.isWorkspaceWorkflowRunActionable({ status }), true);
    assert.equal(displayModule.isWorkflowRunActive({ status }), false);
  }
  const recoverable = { status: "completed", businessOutcome: "needs_input" };
  assert.equal(hostModule.isWorkspaceWorkflowRunActionable(recoverable), true);
  assert.equal(displayModule.isWorkflowRunActive(recoverable), false);
  for (const businessOutcome of ["revision_required", "ready_for_acceptance", "completed"]) {
    const run = { status: "completed", businessOutcome };
    assert.equal(hostModule.isWorkspaceWorkflowRunActionable(run), false);
    assert.equal(displayModule.isWorkflowRunActive(run), false);
  }

  const normalizedQueuedRun = hostModule.mergeWorkflowRunView(
    { status: "queued" },
    { id: "run-queued", title: "Queued", status: "pending", nodes: [] },
  );
  assert.equal(normalizedQueuedRun.status, "pending");
  assert.equal(displayModule.isWorkflowRunActive(normalizedQueuedRun), true);
  assert.equal(displayModule.isWorkflowRunActive({ status: "running" }), true);

  const intervalBlock = hostSource.slice(
    hostSource.indexOf("refetchInterval:"),
    hostSource.indexOf("\n  });", hostSource.indexOf("refetchInterval:")),
  );
  assert.match(intervalBlock, /isWorkflowRunActive\(currentRun\) \? 1_000 : false/);
  assert.doesNotMatch(intervalBlock, /isWorkspaceWorkflowRunActionable/);
});

test("compact wait intervention preserves the current node blocker summary", async () => {
  const hostModule = await loadHostHelpers();
  const blocker = [
    "Browser readiness issue:",
    "Chrome session is not paired. Start the local browser worker, then retry.",
  ].join("\n");
  const foregroundRun = hostModule.mergeWorkflowRunView(
    {
      id: "run-browser-handoff",
      status: "paused",
      current_step_id: "browser_handoff",
      updated_at: "2026-07-29T07:26:22Z",
      definition_snapshot: {
        name: "Create product video",
        nodes: [{
          id: "browser_handoff",
          name: "Connect or sign in to the product",
          type: "wait",
        }],
      },
      intervention: {
        kind: "workflow_resume",
        workflow_run_id: "run-browser-handoff",
        step_id: "browser_handoff",
        observed_problem: blocker,
        options: ["resume", "cancel"],
      },
    },
    {
      id: "run-browser-handoff",
      title: "Create product video",
      status: "running",
      currentNodeId: "browser_handoff",
      nodes: [{
        id: "browser_handoff",
        name: "Connect or sign in to the product",
        status: "running",
      }],
    },
    "2026-07-29T07:26:21Z",
  );

  const action = hostModule.selectWorkspaceWorkflowInterventionAction(foregroundRun);
  assert.equal(action.kind, "workflow_resume");
  assert.equal(action.observed_problem, blocker);
  assert.deepEqual(action.options, ["resume", "cancel"]);
});

test("workspace workflow host renders one panel, an accessible switcher, and active polling", () => {
  assert.match(hostSource, /export default function WorkspaceWorkflowRunHost/);
  assert.match(hostSource, /<WorkflowRunProgress[\s\S]*?run=\{foregroundRun\}/);
  assert.match(hostSource, /<WorkflowRunIntervention/);
  assert.match(hostSource, /actionableGroups\.length > 1[\s\S]*?<Select/);
  assert.match(hostSource, /ariaLabel=\{t\("component\.workflow_run\.switcher_label"\)\}/);
  assert.equal((hostSource.match(/<WorkflowRunProgress/g) || []).length, 1);
  assert.match(hostSource, /queryKey:\s*\["workflow-run", foregroundRunId\]/);
  assert.match(hostSource, /queryFn:\s*\(\) => api\.workflows\.getRun\(foregroundRunId, false\)/);
  assert.match(hostSource, /refetchInterval:[\s\S]*?isWorkflowRunActive\(currentRun\) \? 1_000 : false/);
  assert.doesNotMatch(hostSource, /refetchIntervalInBackground:\s*true/);
  assert.match(hostSource, /mergeWorkflowRunView/);
  assert.match(hostSource, /isLoading/);
  assert.match(hostSource, /isError/);
});

test("workflow host scopes controls and errors to the selected authorized run", () => {
  assert.match(hostSource, /const canControl = Boolean\(runCapabilities\?\.can_control\)/);
  assert.match(hostSource, /canCancelRunningRun = canControl/);
  assert.match(hostSource, /disabled=\{resolving \|\| !canControl\}/);
  assert.match(hostSource, /actionMessageId === resolveMessageId/);
  assert.match(hostSource, /const actionMessageId = nonEmptyString\(interventionAction\?\.message_id\)/);
  assert.doesNotMatch(hostSource, /actionMessage\?\.id/);
  assert.match(hostSource, /onRunChange\?\.\(\)/);
  assert.doesNotMatch(hostSource, /key=\{`\$\{foregroundRun\.id\}[^`]*actionResetToken/);
  assert.doesNotMatch(hostSource, /onClick=\{\(\) => cancelMutation\.mutate\(\)\}/);
});

test("workflow run cancellation is confirmed from the compact header action", () => {
  assert.match(hostSource, /import ConfirmDialog from "\.\.\/ui\/ConfirmDialog"/);
  assert.doesNotMatch(hostSource, /import Button from "\.\.\/ui\/Button"/);
  assert.match(hostSource, /IconStop/);
  assert.match(hostSource, /const \[cancelConfirmationOpen, setCancelConfirmationOpen\] = useState\(false\)/);
  assert.match(hostSource, /headerAction=\{canCancelRunningRun/);
  assert.match(hostSource, /className="workflow-run-cancel-action"/);
  assert.match(hostSource, /title=\{cancelActionLabel\}/);
  assert.match(hostSource, /aria-label=\{cancelActionLabel\}/);
  assert.match(hostSource, /disabled=\{resolving\}/);
  assert.match(hostSource, /cancelMutation\.isPending\s*\? <LoadingSpinner size=\{13\} \/>/);
  assert.match(hostSource, /<IconStop size=\{13\}/);
  assert.match(hostSource, /<ConfirmDialog/);
  assert.match(hostSource, /open=\{cancelConfirmationOpen\}/);
  assert.match(hostSource, /title=\{t\("component\.workflow_run\.cancel_confirm_title"\)\}/);
  assert.match(hostSource, /message=\{t\("component\.workflow_run\.cancel_confirm_message"\)\}/);
  assert.match(hostSource, /confirmLabel=\{t\("component\.workflow_run\.cancel_confirm_action"\)\}/);
  assert.match(hostSource, /cancelLabel=\{t\("component\.workflow_run\.cancel_confirm_keep_running"\)\}/);
  assert.match(hostSource, /danger/);
  assert.match(hostSource, /loading=\{cancelMutation\.isPending\}/);
  assert.match(hostSource, /closeOnConfirm=\{false\}/);
  assert.match(
    workflowCss,
    /\.workflow-run-cancel-action\s*\{[^}]*width:\s*26px[^}]*height:\s*26px/,
  );
  assert.match(
    workflowCss,
    /\.workflow-run-cancel-action:hover:not\(:disabled\),\s*\.workflow-run-cancel-action:focus-visible\s*\{[^}]*var\(--editor-danger-bg\)[^}]*var\(--editor-danger-text\)/,
  );
  assert.match(
    workflowCss,
    /\.workflow-run-cancel-action:focus-visible\s*\{[^}]*var\(--accent-ring\)/,
  );
  assert.match(
    workflowCss,
    /\.workflow-run-cancel-action:disabled\s*\{[^}]*var\(--text-faint\)[^}]*cursor:\s*not-allowed/,
  );
  assert.doesNotMatch(hostSource, /workspace-workflow-run-direct-actions/);
  assert.doesNotMatch(workflowCss, /workspace-workflow-run-direct-actions/);
});

test("workflow cancellation confirmation tracks current eligibility before mutating", () => {
  assert.match(
    hostSource,
    /const \[cancelConfirmationRunId, setCancelConfirmationRunId\] = useState<string \| null>\(null\)/,
  );
  assert.match(hostSource, /if \(!cancelConfirmationOpen\) return/);
  assert.match(hostSource, /!canCancelRunningRun \|\| cancelConfirmationRunId !== foregroundRunId/);
  assert.match(hostSource, /setCancelConfirmationOpen\(false\)/);

  const eligibilityGuard = hostSource.indexOf("if (!cancelConfirmationOpen) return");
  const emptyHostGuard = hostSource.indexOf(
    "if (!foregroundGroup || !foregroundRun || serverConfirmedTerminal) return null",
  );
  assert.ok(eligibilityGuard >= 0 && eligibilityGuard < emptyHostGuard);

  const confirmStart = hostSource.indexOf("const confirmCancellation = async () => {");
  const confirmEnd = hostSource.indexOf("\n  };", confirmStart);
  const confirmSource = hostSource.slice(confirmStart, confirmEnd);
  assert.match(confirmSource, /!canCancelRunningRun/);
  assert.match(confirmSource, /cancelConfirmationRunId !== foregroundRunId/);
  assert.ok(
    confirmSource.indexOf("!canCancelRunningRun")
      < confirmSource.indexOf("cancelMutation.mutateAsync"),
  );
  assert.match(confirmSource, /runId: expectedConfirmation\.runId/);
  assert.match(confirmSource, /source: "direct"/);
});

test("workflow cancellation success closes only its captured confirmation generation", () => {
  assert.match(hostSource, /type CancelConfirmationIdentity/);
  assert.match(hostSource, /useRef<CancelConfirmationIdentity \| null>\(null\)/);
  assert.match(hostSource, /generation/);
  assert.match(hostSource, /currentConfirmation\.runId !== expectedConfirmation\.runId/);
  assert.match(
    hostSource,
    /currentConfirmation\.generation !== expectedConfirmation\.generation/,
  );

  const confirmStart = hostSource.indexOf("const confirmCancellation = async () => {");
  const confirmEnd = hostSource.indexOf("\n  };", confirmStart);
  const confirmSource = hostSource.slice(confirmStart, confirmEnd);
  const captureIndex = confirmSource.indexOf(
    "const expectedConfirmation = cancelConfirmationIdentityRef.current",
  );
  const mutateIndex = confirmSource.indexOf("cancelMutation.mutateAsync");
  const scopedCloseIndex = confirmSource.indexOf(
    "closeCancellationConfirmation(expectedConfirmation)",
  );
  assert.ok(captureIndex >= 0 && captureIndex < mutateIndex);
  assert.ok(scopedCloseIndex > mutateIndex);
});

test("workflow cancellation restores focus to a visible workflow control or composer", () => {
  assert.match(hostSource, /WORKFLOW_FOCUS_TARGET_SELECTOR/);
  assert.match(hostSource, /\.workspace-workflow-run-host/);
  assert.match(
    hostSource,
    /\.chat-composer-rich-editor\[contenteditable="true"\]/,
  );
  assert.match(hostSource, /element\.matches\(":disabled"\)/);
  assert.match(hostSource, /element\.getAttribute\("aria-disabled"\) === "true"/);
  assert.match(hostSource, /element\.getClientRects\(\)\.length > 0/);
  assert.match(
    hostSource,
    /querySelectorAll<HTMLElement>\(CHAT_COMPOSER_FOCUS_TARGET_SELECTOR\)/,
  );
  assert.match(hostSource, /restoreFocusFallback=\{focusNextWorkflowControlOrComposer\}/);
});

test("workflow cancellation failure is scoped to the open confirmation", () => {
  assert.match(hostSource, /cancelMutation\.variables\?\.source === "direct"/);
  assert.match(hostSource, /cancelMutation\.variables\?\.source === "intervention"/);
  assert.match(hostSource, /formatWorkflowError\(\s*cancelMutation\.error/);
  assert.match(hostSource, /error=\{cancelError\}/);
  assert.match(
    hostSource,
    /!cancelConfirmationOpen && !interventionAction && cancelError/,
  );

  const dialogStart = hostSource.lastIndexOf("<ConfirmDialog");
  const dialogEnd = hostSource.indexOf("/>", dialogStart);
  const dialogSource = hostSource.slice(dialogStart, dialogEnd);
  assert.doesNotMatch(dialogSource, /directActionError|resumeMutation|scopedResolveError/);
});

test("workspace websocket invalidates the changed Workflow Run projection", () => {
  assert.match(chatSource, /const changedRunId = workflowRunIdForMessage\(/);
  assert.match(chatSource, /queryKey:\s*\["workflow-run", changedRunId\]/);
});

test("workflow detail scroll regions shrink within mobile and landscape chat space", () => {
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-host\s*\{[\s\S]*?flex:\s*0 1 auto[\s\S]*?min-height:\s*0/,
  );
  assert.match(
    workflowCss,
    /\.workflow-run-intervention-body\s*\{[\s\S]*?max-height:\s*min\([^;]*dvh[^;]*calc\(100dvh - [^)]+\)\)/,
  );
  assert.match(
    workflowCss,
    /@media \(max-width: 640px\)[\s\S]*?\.workflow-run-intervention-body\s*\{[\s\S]*?max-height:\s*min\([^;]*28dvh/,
  );
  assert.match(workflowCss, /@media \(max-height: 520px\)/);
  assert.match(
    workflowCss,
    /\.workspace-workflow-run-panel\s*\{[\s\S]*?container-type:\s*inline-size/,
  );
  assert.match(workflowCss, /@container workflow-run \(max-width: 680px\)/);
});

test("workflow run cancellation uses the existing endpoint and invalidates run surfaces", () => {
  assert.match(
    apiSource,
    /cancelRun:\s*\(runId: string\)\s*=>\s*request<void>\(`\/workflows\/runs\/\$\{runId\}\/cancel`,\s*\{ method: "POST" \}\)/,
  );
  assert.match(hostSource, /api\.workflows\.cancelRun\(runId\)/);
  assert.match(hostSource, /queryKey:\s*\["workflow-run", runId\]/);
  assert.match(hostSource, /queryKey:\s*\["workspace-chat", workspaceId\]/);
  assert.match(hostSource, /queryKey:\s*\["workspace-workflow-runs", workspaceId\]/);
});

test("workspace chat mounts the workflow host in normal flow immediately before its composer", () => {
  const hostIndex = chatSource.indexOf("<WorkspaceWorkflowRunHost");
  const footerIndex = chatSource.indexOf("<ChatInputFooter", hostIndex);
  const typingIndex = chatSource.indexOf("typingLabel &&");
  const tipIndex = chatSource.indexOf("<InlineTips", typingIndex);
  assert.ok(hostIndex > tipIndex);
  assert.ok(footerIndex > hostIndex);
  assert.match(chatSource.slice(hostIndex, footerIndex), /workflowRunGroups/);
  assert.doesNotMatch(hostSource, /position:\s*(fixed|absolute)/);
});
