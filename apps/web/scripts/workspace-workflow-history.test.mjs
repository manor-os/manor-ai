import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { transform } from "esbuild";

const workspaceSource = await readFile(
  new URL("../src/components/workflows/WorkspaceWorkflows.tsx", import.meta.url),
  "utf8",
);
const detailSource = await readFile(
  new URL("../src/components/workflows/WorkflowRunDetail.tsx", import.meta.url),
  "utf8",
).catch(() => "");
const interventionSource = await readFile(
  new URL("../src/components/workflows/WorkflowRunIntervention.tsx", import.meta.url),
  "utf8",
);
const displaySource = await readFile(
  new URL("../src/components/workflows/workflowRunDisplay.ts", import.meta.url),
  "utf8",
);
const apiSource = await readFile(new URL("../src/lib/api.ts", import.meta.url), "utf8");
const cssSource = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const localeSources = await Promise.all(["en", "zh", "es"].map((locale) => readFile(
  new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url),
  "utf8",
)));

async function compilePureModule(source) {
  const compiled = await transform(source, {
    loader: "ts",
    format: "esm",
    target: "es2020",
  });
  return import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString("base64")}`);
}

const displayModule = await compilePureModule(displaySource);

test("workspace workflows switch between attached cards and aggregate execution History", () => {
  assert.match(workspaceSource, /TabSwitcher/);
  assert.match(workspaceSource, /"attached"/);
  assert.match(workspaceSource, /"history"/);
  assert.match(workspaceSource, /groupWorkflowRunFamilies/);
  assert.match(workspaceSource, /historyFamilies\.map/);
  assert.match(workspaceSource, /workflowById\.get\(family\.latestRun\.workflow_id \|\| ""\)/);
  assert.match(workspaceSource, /api\.workflows\.listRuns\(\{ workspace_id: workspaceId, limit: 100 \}\)/);
  assert.match(workspaceSource, /family\.attemptCount/);
  assert.match(workspaceSource, /family\.processedCount/);
  assert.match(workspaceSource, /family\.artifactCount/);
  assert.match(workspaceSource, /family\.totalCount > 0/);
  assert.match(workspaceSource, /component\.workflow_run_history\.progress_in_details/);

  const sorted = displayModule.sortWorkflowRunsNewestFirst([
    { id: "middle", created_at: "2026-07-28T11:00:00Z" },
    { id: "newest", started_at: "2026-07-28T12:00:00Z" },
    { id: "oldest", created_at: "2026-07-28T10:00:00Z" },
  ]);
  assert.deepEqual(sorted.map((run) => run.id), ["newest", "middle", "oldest"]);
});

test("retry attempts aggregate into one execution family summary", () => {
  const families = displayModule.groupWorkflowRunFamilies([
    {
      id: "retry-2",
      workflow_id: "workflow-1",
      workflow_name: "Create product video",
      retry_of_run_id: "run-1",
      attempt_number: 2,
      status: "completed",
      business_outcome: "needs_input",
      started_at: "2026-07-29T10:03:00Z",
      completed_at: "2026-07-29T10:05:00Z",
      definition_snapshot: {
        nodes: [
          { id: "start", name: "Start", type: "trigger", order: 0 },
          { id: "prepare", name: "Prepare", type: "agent", order: 1 },
          {
            id: "internal",
            name: "Persist",
            type: "transform",
            order: 2,
            chat_projection: "hidden",
          },
          { id: "capture", name: "Capture", type: "agent", order: 3 },
          { id: "end", name: "Input required", type: "end", order: 4 },
        ],
      },
      step_results: {
        start: { status: "completed" },
        prepare: { status: "completed" },
        internal: { status: "completed" },
        capture: { status: "completed" },
      },
      execution_trace: [{
        sequence: 1,
        node_id: "capture",
        status: "completed",
        artifact_refs: [{ document_id: "video-1", name: "final.mp4", mime_type: "video/mp4" }],
      }],
      intervention: { observed_problem: "Chrome session disconnected" },
    },
    {
      id: "run-1",
      workflow_id: "workflow-1",
      workflow_name: "Create product video",
      attempt_number: 1,
      status: "failed",
      started_at: "2026-07-29T10:00:00Z",
      completed_at: "2026-07-29T10:02:00Z",
    },
    {
      id: "other",
      workflow_id: "workflow-2",
      status: "completed",
      started_at: "2026-07-29T09:00:00Z",
    },
  ]);

  assert.equal(families.length, 2);
  const family = families.find((candidate) => candidate.id === "run-1");
  assert.deepEqual(family.runs.map((run) => run.id), ["run-1", "retry-2"]);
  assert.equal(family.latestRun.id, "retry-2");
  assert.equal(family.attemptCount, 2);
  assert.equal(family.status, "completed");
  assert.equal(family.businessOutcome, "needs_input");
  assert.equal(family.processedCount, 3);
  assert.equal(family.totalCount, 3);
  assert.equal(family.artifactRefs.length, 1);
  assert.equal(family.blocker, "Chrome session disconnected");
});

test("aggregate History derives legacy progress from the execution trace", () => {
  const [family] = displayModule.groupWorkflowRunFamilies([{
    id: "legacy-active",
    status: "paused",
    started_at: "2026-07-29T10:00:00Z",
    updated_at: "2026-07-29T10:04:00Z",
    step_results: { start: { output: "legacy compact result" } },
    execution_trace: [
      { sequence: 1, node_id: "start", node_type: "trigger", status: "running" },
      { sequence: 2, node_id: "start", node_type: "trigger", status: "completed" },
      { sequence: 3, node_id: "discover", node_type: "agent", status: "completed" },
      { sequence: 4, node_id: "approval", node_type: "wait", status: "paused" },
      { sequence: 5, node_id: "end", node_type: "end", status: "completed" },
    ],
  }]);

  assert.equal(family.processedCount, 2);
  assert.equal(family.totalCount, 3);
  assert.equal(family.durationMs, 4 * 60 * 1000);
});

test("aggregate History preserves legacy artifacts stored only in step results", () => {
  const [family] = displayModule.groupWorkflowRunFamilies([{
    id: "legacy-artifacts",
    status: "completed",
    step_results: {
      render: {
        status: "completed",
        artifact_refs: [{ document_id: "video-1", name: "final.mp4", mime_type: "video/mp4" }],
      },
    },
  }]);

  assert.equal(family.artifactRefs.length, 1);
  assert.equal(family.artifactRefs[0].document_id, "video-1");
});

test("aggregate History ends at the latest completion timestamp", () => {
  const [family] = displayModule.groupWorkflowRunFamilies([
    {
      id: "attempt-1",
      attempt_number: 1,
      status: "completed",
      started_at: "2026-07-29T10:00:00Z",
      completed_at: "2026-07-29T10:10:00Z",
    },
    {
      id: "attempt-2",
      retry_of_run_id: "attempt-1",
      attempt_number: 2,
      status: "completed",
      started_at: "2026-07-29T10:02:00Z",
      completed_at: "2026-07-29T10:05:00Z",
    },
  ]);

  assert.equal(family.completedAt, "2026-07-29T10:10:00Z");
  assert.equal(family.durationMs, 10 * 60 * 1000);
});

test("aggregate History uses compact persisted summary fields", () => {
  const [family] = displayModule.groupWorkflowRunFamilies([{
    id: "compact-summary",
    status: "completed",
    business_outcome: "needs_input",
    processed_count: 2,
    total_count: 3,
    artifact_count: 4,
  }]);

  assert.equal(family.businessOutcome, "needs_input");
  assert.equal(family.processedCount, 2);
  assert.equal(family.totalCount, 3);
  assert.equal(family.artifactCount, 4);
});

test("aggregate History does not turn an unavailable artifact count into zero", () => {
  const [family] = displayModule.groupWorkflowRunFamilies([{
    id: "legacy-summary",
    status: "paused",
    processed_count: null,
    total_count: null,
    artifact_count: null,
  }]);

  assert.equal(family.artifactCount, null);
});

test("History deep links select one aggregate record and details start with its summary", () => {
  assert.match(workspaceSource, /useSearchParams/);
  assert.match(workspaceSource, /searchParams\.get\("workflow_view"\)/);
  assert.match(workspaceSource, /searchParams\.get\("workflow_run"\)/);
  assert.match(workspaceSource, /workflow_view/);
  assert.match(workspaceSource, /workflow_run/);
  assert.match(detailSource, /workflow-run-history-summary/);
  assert.match(detailSource, /family\.attemptCount/);
  assert.match(detailSource, /family\.processedCount/);
  assert.match(detailSource, /family\.artifactRefs/);
  assert.match(detailSource, /<details className="workflow-run-history-technical"/);
  assert.doesNotMatch(detailSource, /<details className="workflow-run-history-technical" open/);
  assert.match(detailSource, /component\.workflow_run_history\.technical_details/);
  assert.match(detailSource, /controlRunId/);
  assert.match(detailSource, /sourceRun: controlRun/);
  assert.match(detailSource, /familyQuery[\s\S]*?refetchInterval/);
  assert.match(detailSource, /latestDetailQuery[\s\S]*?refetchInterval/);
  assert.match(workspaceSource, /setSection\(hasHistoryLocation \? "history" : "attached"\)/);
  assert.match(workspaceSource, /setSelectedRunId\(hasHistoryLocation \? requestedRunId : ""\)/);
  assert.match(workspaceSource, /family\.artifactCount !== null/);
});

test("run selection replaces the History list with a full-width detail in the workspace page", () => {
  assert.match(workspaceSource, /selectedRunId/);
  assert.match(workspaceSource, /<WorkflowRunDetail/);
  assert.match(workspaceSource, /onBack=/);
  assert.match(workspaceSource, /onSelectRun=/);
  assert.doesNotMatch(detailSource, /Modal|DetailDrawer|createPortal/);
  assert.match(detailSource, /api\.workflows\.getRun\(runId\)/);
  assert.match(detailSource, /refetchInterval:[\s\S]*?runIsActive/);
  assert.match(detailSource, /isWorkflowRunActive/);
  assert.match(detailSource, /headingRef\.current\?\.focus\(\);[\s\S]*?\[run\?\.id\]/);
  assert.doesNotMatch(workspaceSource, /role="listitem"/);
});

test("History detail hydrates retry families independently of the first history page", () => {
  assert.match(apiSource, /getRunFamily:\s*\(runId: string\)/);
  assert.match(apiSource, /\/workflows\/runs\/\$\{runId\}\/family/);
  assert.match(detailSource, /queryKey:\s*\["workflow-run-history-family", runId\]/);
  assert.match(detailSource, /api\.workflows\.getRunFamily\(runId\)/);
  assert.match(detailSource, /familyQuery\.data/);
  assert.doesNotMatch(detailSource, /workspaceRuns\.filter/);
});

test("legacy retry lineage is visibly marked untrusted and incomplete", () => {
  assert.match(
    displaySource,
    /lineage_status\?:\s*"canonical"\s*\|\s*"legacy_untrusted_incomplete"/,
  );
  assert.match(detailSource, /run\.lineage_status === "legacy_untrusted_incomplete"/);
  assert.match(workspaceSource, /run\.lineage_status === "legacy_untrusted_incomplete"/);
  for (const source of localeSources) {
    assert.match(source, /component\.workflow_run_history\.legacy_lineage_untrusted/);
    assert.match(source, /component\.workflow_run_history\.legacy_lineage_label/);
  }
});

test("immutable trace rows use snapshot identity and execution sequence", () => {
  const timeline = displayModule.buildWorkflowRunTimeline({
    id: "run-1",
    status: "completed",
    definition_snapshot: {
      name: "Frozen workflow",
      version: 7,
      fingerprint: "frozen-fingerprint",
      nodes: [
        { id: "capture", name: "Frozen capture", type: "browser", order: 0 },
        { id: "finish", name: "Frozen finish", type: "output", order: 1 },
      ],
    },
    execution_trace: [
      { sequence: 2, node_id: "capture", status: "completed", output_summary: { ok: true } },
      { sequence: 1, node_id: "capture", status: "running", input_summary: { url: "https://example.com" } },
      { sequence: 3, node_id: "finish", status: "completed" },
    ],
  }, [
    { id: "capture", name: "Changed live name", type: "changed" },
  ]);

  assert.deepEqual(timeline.map((entry) => entry.sequence), [1, 2, 3]);
  assert.equal(timeline[0].nodeName, "Frozen capture");
  assert.equal(timeline[0].nodeType, "browser");
  assert.equal(timeline[0].legacy, false);
  assert.match(detailSource, /definition_snapshot/);
  assert.match(detailSource, /workflow_definition_fingerprint/);
  assert.match(detailSource, /execution_trace/);
});

test("immutable snapshot rows include every frozen node and frozen edge in definition order", () => {
  const snapshot = displayModule.buildWorkflowSnapshotNodes({
    id: "run-snapshot",
    status: "completed",
    current_step_id: "publish",
    definition_snapshot: {
      nodes: [
        { id: "publish", name: "Frozen publish", type: "output", order: 2, targets: [] },
        { id: "branch", name: "Frozen branch", type: "condition", order: 1, targets: ["publish", "archive"] },
        { id: "start", name: "Frozen start", type: "trigger", order: 0, targets: ["branch"] },
        { id: "archive", name: "Frozen archive", type: "document", order: 3, targets: [] },
      ],
    },
    execution_trace: [
      { sequence: 1, node_id: "start", status: "running" },
      { sequence: 2, node_id: "start", status: "completed" },
      { sequence: 3, node_id: "branch", status: "completed" },
      { sequence: 4, node_id: "publish", status: "completed" },
    ],
  });

  assert.deepEqual(snapshot.map((node) => node.nodeId), ["start", "branch", "publish", "archive"]);
  assert.deepEqual(snapshot[1].targets, ["publish", "archive"]);
  assert.equal(snapshot[0].status, "completed");
  assert.equal(snapshot[3].status, "skipped");
  assert.match(detailSource, /buildWorkflowSnapshotNodes/);
  assert.match(detailSource, /component\.workflow_run_history\.definition_snapshot/);
  assert.match(detailSource, /component\.workflow_run_history\.frozen_targets/);
  assert.ok(
    detailSource.indexOf("workflow-run-history-snapshot")
      < detailSource.indexOf("workflow-run-history-trace"),
  );
});

test("immutable snapshot rows render every target retained by the bounded snapshot", () => {
  const targets = Array.from({ length: 80 }, (_value, index) => `target-${index}`);
  const snapshot = displayModule.buildWorkflowSnapshotNodes({
    id: "run-many-targets",
    status: "completed",
    definition_snapshot: {
      nodes: [
        { id: "route", name: "Frozen route", type: "switch", order: 0, targets },
      ],
    },
  });

  assert.deepEqual(snapshot[0].targets, targets);
});

test("legacy runs fall back to step results ordered by the current workflow definition", () => {
  const timeline = displayModule.buildWorkflowRunTimeline({
    id: "legacy-run",
    status: "failed",
    step_results: {
      finish: { status: "failed", error: { message: "No output" } },
      start: { status: "completed", output: { ok: true } },
      extra: { status: "completed" },
    },
  }, [
    { id: "start", name: "Start", type: "trigger" },
    { id: "finish", name: "Finish", type: "output" },
  ]);

  assert.deepEqual(timeline.map((entry) => entry.nodeId), ["start", "finish", "extra"]);
  assert.equal(timeline.every((entry) => entry.legacy), true);
  assert.match(detailSource, /component\.workflow_run_history\.legacy/);
  assert.match(displaySource, /step_results/);
});

test("trace-only runs stay legacy and prefer trace-frozen identity", () => {
  const run = {
    id: "trace-only-run",
    status: "completed",
    definition_snapshot: {},
    execution_trace: [
      {
        sequence: 1,
        node_id: "render",
        node_name: "Frozen render",
        node_type: "video",
        status: "completed",
      },
    ],
  };
  const timeline = displayModule.buildWorkflowRunTimeline(run, [
    { id: "render", name: "Changed render", type: "changed" },
  ]);

  assert.equal(displayModule.workflowRunIsLegacy(run), true);
  assert.equal(timeline[0].nodeName, "Frozen render");
  assert.equal(timeline[0].nodeType, "video");
  assert.equal(timeline[0].legacy, true);
  assert.match(detailSource, /const legacy = workflowRunIsLegacy\(run\);/);
});

test("History retry remains capability and intervention-schema gated", () => {
  assert.match(detailSource, /capabilities\?\.can_control === true/);
  assert.match(detailSource, /api\.workflows\.getRun\(controlRunId, false\)/);
  assert.match(detailSource, /WorkflowRunIntervention/);
  assert.match(detailSource, /compactRun\?\.intervention/);
  assert.match(interventionSource, /editable_input_schema/);
  assert.match(detailSource, /retry_from_step_id/);
  assert.match(detailSource, /api\.workflows\.retryRun/);
  assert.match(detailSource, /canRetryWithoutCorrection/);
  assert.match(detailSource, /showControlSurface/);
  assert.doesNotMatch(detailSource, /capabilities\?\.can_control !== false/);
});

test("History retry accepts only compatible editable schemas and explicit no-input schemas", () => {
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    truncated: true,
    preview: '{"type":"object","properties":{"revision_notes":',
  }), false);
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({ type: "object" }), false);
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    type: "object",
    properties: {
      revision_notes: { type: "null" },
    },
  }), false);
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    type: "object",
    properties: {},
  }), true);
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    type: "object",
    properties: {
      revision_notes: { type: "string", minLength: 1 },
      retry_segment_ids: { type: "array", items: { type: "string" } },
    },
    required: ["revision_notes"],
  }), true);
  assert.match(detailSource, /workflowRetrySchemaIsCompatible\(intervention\.editable_input_schema\)/);
  assert.match(interventionSource, /workflowRetrySchemaIsCompatible\(action\.editable_input_schema\)/);
});

test("History retry rejects unsupported schema composition and constraints recursively", () => {
  const unsupported = new Map([
    ["oneOf", [{ type: "string" }]],
    ["anyOf", [{ type: "string" }]],
    ["allOf", [{ type: "string" }]],
    ["not", { type: "string" }],
    ["if", { type: "string" }],
    ["then", { minLength: 2 }],
    ["else", { maxLength: 20 }],
    ["$ref", "#/$defs/correction"],
    ["$dynamicRef", "#correction"],
    ["dependentSchemas", { mode: { required: ["details"] } }],
    ["contains", { type: "string" }],
    ["prefixItems", [{ type: "string" }]],
    ["multipleOf", 2],
  ]);

  for (const [keyword, constraint] of unsupported) {
    assert.equal(displayModule.workflowRetrySchemaIsCompatible({
      type: "object",
      properties: {
        correction: {
          type: keyword === "contains" || keyword === "prefixItems" ? "array" : "string",
          [keyword]: constraint,
        },
      },
    }), false, `${keyword} must be rejected`);
  }
});

test("History retry ignores unsupported nested constraints in preserved hidden fields", () => {
  const hiddenArtifactField = {
    type: "array",
    items: {
      type: "object",
      allOf: [{ required: ["document_id"] }],
    },
    "x-ui": { hidden: true },
  };
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    type: "object",
    properties: { reference_assets: hiddenArtifactField },
  }), true);
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    type: "object",
    properties: {
      reference_assets: { ...hiddenArtifactField, "x-ui": { hidden: false } },
    },
  }), false);
});

test("History retry accepts the recursively supported editable schema contract", () => {
  assert.equal(displayModule.workflowRetrySchemaIsCompatible({
    $schema: "https://json-schema.org/draft/2020-12/schema",
    type: "object",
    title: "Retry corrections",
    description: "Supported editable constraints",
    properties: {
      source_url: {
        type: "string",
        title: "Source URL",
        description: "Public product URL",
        format: "uri",
        pattern: "^https?://",
        minLength: 8,
        maxLength: 200,
        default: "https://example.test",
      },
      attempt_count: {
        type: ["integer", "null"],
        minimum: 1,
        maximum: 10,
        exclusiveMinimum: 0,
        exclusiveMaximum: 11,
        default: 1,
      },
      segment_ids: {
        type: "array",
        items: { type: "string", minLength: 1, maxLength: 80 },
        minItems: 1,
        maxItems: 8,
        uniqueItems: true,
        "x-ui": { control: "line_list", rows: 4 },
      },
      options: {
        type: "object",
        properties: {
          approved: { type: "boolean", default: false },
          mode: { type: "string", enum: ["repair", "replace"] },
        },
        required: ["approved"],
        additionalProperties: false,
        "x-ui": { collapsible: true, collapsed: false, order: ["approved", "mode"] },
      },
      payload: {
        type: "string",
        "x-workflow-type": "json",
        "x-ui": { control: "textarea", rows: 5 },
      },
      session: {
        type: "string",
        const: "current",
        "x-ui": { hidden: true },
      },
    },
    required: ["source_url", "segment_ids", "options"],
    additionalProperties: false,
    "x-ui": {
      order: ["source_url", "attempt_count", "segment_ids", "options", "payload", "session"],
    },
  }), true);
});

test("History values stay bounded and redact credential-shaped fields", () => {
  const formatted = displayModule.formatWorkflowValue(Object.fromEntries([
    ["password", "do-not-show"],
    ["api_token", "do-not-show-either"],
    ...Array.from({ length: 24 }, (_value, index) => [`content_${index}`, "x".repeat(1_000)]),
  ]), "TRUNCATED");

  assert.doesNotMatch(formatted, /do-not-show/);
  assert.match(formatted, /\[REDACTED\]/);
  assert.match(formatted, /TRUNCATED/);
  assert.ok(Buffer.byteLength(formatted, "utf8") <= 8 * 1024);
  assert.doesNotMatch(detailSource, /run\.variables/);
  assert.doesNotMatch(detailSource, /JSON\.stringify\(/);

  const plainError = displayModule.formatWorkflowError(
    "Request failed: api_key=sk-live-secret Authorization: Bearer bearer-secret",
    "TRUNCATED",
  );
  assert.doesNotMatch(plainError, /sk-live-secret|bearer-secret/);
  assert.match(plainError, /\[REDACTED\]/);
});

test("legacy artifact references are allowlisted, bounded, and safe to open", () => {
  const normalized = displayModule.normalizeWorkflowArtifactRefs([
    {
      path: "Workspaces/demo/render.mp4?token=secret#authorization=Bearer-secret",
      name: "render.mp4",
      mime_type: "video/mp4",
      status: "ready",
      authorization: "Bearer do-not-render",
      url: "https://evil.example/render.mp4?token=do-not-open",
      nested: { password: "nested-secret", values: ["x".repeat(50_000)] },
    },
    {
      fs_path: "Workspaces/demo/authorization=Bearer-secret/report.pdf",
      name: "credential path",
    },
    {
      document_id: "01JSAFEARTIFACT00000000000",
      name: "x".repeat(10_000),
      credential: "do-not-render-either",
    },
    "https://evil.example/raw.mp4?token=secret",
  ]);

  assert.deepEqual(normalized[0], {
    name: "render.mp4",
    mime_type: "video/mp4",
    status: "ready",
  });
  assert.equal(normalized[1].document_id, "01JSAFEARTIFACT00000000000");
  assert.ok(normalized[1].name.length <= 256);
  assert.equal(normalized.length, 2);
  const encoded = JSON.stringify(normalized);
  assert.doesNotMatch(encoded, /secret|authorization|Bearer|evil\.example|password|credential/i);
  assert.ok(Buffer.byteLength(encoded, "utf8") < 2 * 1024);
  assert.match(detailSource, /normalizeWorkflowArtifactRefs/);
  assert.doesNotMatch(detailSource, /ref\.path\s*\|\||ref\.id\s*\|\||ref\.artifact_id/);
});

test("legacy artifact references fail closed for credential files, provider keys, and external URL labels", () => {
  const normalized = displayModule.normalizeWorkflowArtifactRefs([
    {
      path: "Workspaces/demo/credentials.json",
      name: "credentials.json",
    },
    {
      fs_path: "Workspaces/demo/secrets.env",
      name: "secrets.env",
    },
    {
      fs_path: "Workspaces/demo/api_key.txt",
      name: "api_key.txt",
    },
    {
      fs_path: "Workspaces/demo/sk-live-super-secret-provider-key/render.mp4",
      name: "sk-live-super-secret-provider-key",
    },
    {
      document_id: "01JSAFEARTIFACT00000000001",
      name: "sk-proj-super-secret-project-key",
    },
    {
      document_id: "01JSAFEARTIFACT00000000002",
      name: "gsk_super_secret_provider_key",
    },
    {
      document_id: "01JSAFEARTIFACT00000000003",
      name: "https://cdn.example.test/render.mp4?download=1",
    },
    {
      fs_path: "Workspaces/demo/render.mp4",
      name: "https://cdn.example.test/render.mp4#preview",
    },
  ]);

  assert.deepEqual(normalized, [
    { document_id: "01JSAFEARTIFACT00000000001" },
    { document_id: "01JSAFEARTIFACT00000000002" },
    { document_id: "01JSAFEARTIFACT00000000003" },
    { fs_path: "Workspaces/demo/render.mp4" },
  ]);
  assert.doesNotMatch(
    JSON.stringify(normalized),
    /api_key\.txt|credentials\.json|secrets\.env|sk-live-|sk-proj-|gsk_|https?:\/\//i,
  );
});

test("artifact labels reject URL-like references and query or fragment separators", () => {
  const unsafeLabels = [
    "https://cdn.example.test/render.mp4",
    "file:///tmp/private-key.pem",
    "data:text/plain,artifact",
    "//cdn.example.test/render.mp4",
    "render.mp4?download=1",
    "render.mp4#preview",
    "folder/render.mp4",
    "folder\\render.mp4",
  ];
  const normalized = displayModule.normalizeWorkflowArtifactRefs(
    unsafeLabels.map((name, index) => ({
      document_id: `safe-document-${index}`,
      name,
    })),
  );

  assert.deepEqual(normalized, unsafeLabels.map((_, index) => ({
    document_id: `safe-document-${index}`,
  })));
  assert.doesNotMatch(JSON.stringify(normalized), /https:|file:|data:|\/\/|\?|#/i);
});

test("artifact references reject sensitive stems and provider key prefixes in labels and paths", () => {
  const unsafeLabels = [
    "access_token.txt",
    "private_key.pem",
    "folder/api_key.txt",
    "credentials.prod.json",
    "secrets.env",
    "sk-ant-sensitive-key",
    "sk-live-sensitive-key",
    "sk-proj-sensitive-key",
    "sk-or-sensitive-key",
    "gsk_sensitive_key",
    "ark-sensitive-key",
  ];
  const labelRefs = unsafeLabels.map((name, index) => ({
    document_id: `safe-label-document-${index}`,
    name,
  }));
  const pathRefs = [
    "Workspaces/demo/access_token.txt",
    "Workspaces\\demo\\private_key.pem",
    "Workspaces/demo/folder/%61ccess_token.txt",
    "Workspaces/demo/sk-ant-sensitive-key/render.mp4",
    "Workspaces/demo/ark-sensitive-key/render.mp4",
  ].map((fs_path, index) => ({
    document_id: `safe-path-document-${index}`,
    fs_path,
  }));

  const normalizedLabels = displayModule.normalizeWorkflowArtifactRefs(labelRefs);
  const normalizedPaths = displayModule.normalizeWorkflowArtifactRefs(pathRefs);

  assert.deepEqual(normalizedLabels, labelRefs.map(({ document_id }) => ({ document_id })));
  assert.deepEqual(normalizedPaths, pathRefs.map(({ document_id }) => ({ document_id })));
  assert.doesNotMatch(
    JSON.stringify([normalizedLabels, normalizedPaths]),
    /access_token|private_key|api_key|credentials|secrets|sk-ant-|sk-live-|sk-proj-|sk-or-|gsk_|ark-/i,
  );
});

test("artifact references reject generic token values and sensitive identifier variants", () => {
  const unsafeLabels = [
    "sk-1234567890abcdef",
    "pk-1234567890abcdef",
    "gsk_1234567890abcdef",
    "ark-1234567890abcdef",
    "ark_1234567890abcdef",
    "AccessToken.txt",
    "refresh-token.json",
    "client.secret.pdf",
    "CLIENT-KEY.txt",
    "privateKey.pem",
    "api.key.txt",
    "AuthToken.md",
    "session-token.json",
  ];
  const unsafePaths = [
    "Workspaces/demo/sk-1234567890abcdef/render.mp4",
    "Workspaces/demo/pk-1234567890abcdef/frame.png",
    "Workspaces/demo/gsk_1234567890abcdef/audio.wav",
    "Workspaces/demo/ark_1234567890abcdef/captions.srt",
    "Workspaces/demo/access-token/report.pdf",
    "Workspaces/demo/refreshToken/manifest.json",
    "Workspaces/demo/client_secret/notes.md",
    "Workspaces/demo/session.token/transcript.txt",
  ];
  const labelRefs = unsafeLabels.map((name, index) => ({
    document_id: `safe-generic-label-${index}`,
    name,
  }));
  const pathRefs = unsafePaths.map((fs_path, index) => ({
    document_id: `safe-sensitive-path-${index}`,
    fs_path,
  }));

  assert.deepEqual(
    displayModule.normalizeWorkflowArtifactRefs(labelRefs),
    labelRefs.map(({ document_id }) => ({ document_id })),
  );
  assert.deepEqual(
    displayModule.normalizeWorkflowArtifactRefs(pathRefs),
    pathRefs.map(({ document_id }) => ({ document_id })),
  );
});

test("artifact paths require previewable extensions while useful artifacts and labels remain", () => {
  const safeBasenames = [
    "render.mp4",
    "frame.png",
    "mix.wav",
    "captions.srt",
    "report.pdf",
    "manifest.json",
    "notes.md",
    "transcript.txt",
  ];
  const safePaths = safeBasenames.map((name) => `Workspaces/demo/${name}`);
  const humanLabels = [
    "Final render",
    "Product hero frame",
    "Dialogue mix",
    "Review copy v2",
    "SK-2 camera pass",
  ];
  const unsafePaths = [
    "Workspaces/demo/secrets.env",
    "Workspaces/demo/certificate.pem",
    "Workspaces/demo/signing.key",
    "Workspaces/demo/settings.yaml",
    "Workspaces/demo/archive.zip",
    "Workspaces/demo/no-extension",
    "Workspaces/demo/render.mp4?download=1",
    "Workspaces/demo/render.mp4#preview",
    "https://cdn.example.test/render.mp4",
    "//cdn.example.test/render.mp4",
    "Workspaces/demo/../render.mp4",
    "Workspaces/demo/%2e%2e/render.mp4",
    "Workspaces/demo/%00render.mp4",
  ];

  assert.deepEqual(
    displayModule.normalizeWorkflowArtifactRefs(safePaths.map((fs_path) => ({ fs_path }))),
    safePaths.map((fs_path) => ({ fs_path })),
  );
  assert.deepEqual(
    displayModule.normalizeWorkflowArtifactRefs(safeBasenames.map((name, index) => ({
      document_id: `safe-basename-${index}`,
      name,
    }))),
    safeBasenames.map((name, index) => ({
      document_id: `safe-basename-${index}`,
      name,
    })),
  );
  assert.deepEqual(
    displayModule.normalizeWorkflowArtifactRefs(humanLabels.map((name, index) => ({
      document_id: `safe-human-label-${index}`,
      name,
    }))),
    humanLabels.map((name, index) => ({
      document_id: `safe-human-label-${index}`,
      name,
    })),
  );
  assert.deepEqual(
    displayModule.normalizeWorkflowArtifactRefs(unsafePaths.map((fs_path, index) => ({
      document_id: `safe-unsafe-path-${index}`,
      fs_path,
    }))),
    unsafePaths.map((_, index) => ({ document_id: `safe-unsafe-path-${index}` })),
  );
});

test("artifact references open through shared document/media patterns and child runs are bounded", () => {
  assert.match(detailSource, /InlineFileReferenceCard/);
  assert.match(detailSource, /MediaPreview/);
  assert.match(detailSource, /artifact_refs/);
  assert.match(detailSource, /child_run_ids/);
  assert.match(detailSource, /expandedChildRunId/);
  assert.match(detailSource, /allowChildExpansion=\{false\}/);
  assert.match(detailSource, /api\.workflows\.getRun\(childRunId\)/);
});

test("History ships localized states and responsive no-overflow styling", () => {
  for (const source of localeSources) {
    assert.match(source, /"component\.workflow_run_history\.attached"/);
    assert.match(source, /"component\.workflow_run_history\.history"/);
    assert.match(source, /"component\.workflow_run_history\.legacy"/);
    assert.match(source, /"component\.workflow_run_history\.empty"/);
    assert.match(source, /"component\.workflow_run_history\.load_error"/);
    assert.match(source, /"component\.workflow_run_history\.retry"/);
    assert.match(source, /"component\.workflow_run_history\.definition_snapshot"/);
    assert.match(source, /"component\.workflow_run_history\.frozen_targets"/);
    assert.match(source, /"component\.workflow_run_history\.legacy_list_metadata"/);
  }
  assert.match(cssSource, /\.workflow-run-history-detail\s*\{[\s\S]*?max-width:\s*100%/);
  assert.match(cssSource, /\.workflow-run-history-value\s*\{[\s\S]*?overflow:\s*auto/);
  assert.match(cssSource, /@media \(max-width: 640px\) \{[\s\S]*?\.workflow-run-history/);
  assert.doesNotMatch(cssSource, /\.workflow-run-history[^}]*#[0-9a-f]{3,8}/i);
});

test("History prefers immutable list names and labels current-definition fallback as legacy", () => {
  assert.match(workspaceSource, /run\.workflow_name\s*\|\|\s*workflow\?\.name/);
  assert.match(workspaceSource, /run\.current_step_name\s*\|\|\s*currentNode\?\.name/);
  assert.match(workspaceSource, /component\.workflow_run_history\.legacy_list_metadata/);
  assert.match(workspaceSource, /workflowRunHasImmutableListMetadata/);

  assert.equal(displayModule.workflowRunHasImmutableListMetadata({
    id: "new-run",
    status: "running",
    workflow_name: "Frozen workflow",
    current_step_name: "Frozen render",
  }), true);
  assert.equal(displayModule.workflowRunHasImmutableListMetadata({
    id: "legacy-run",
    status: "completed",
  }), false);
});
