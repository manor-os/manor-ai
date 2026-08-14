import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import test from "node:test";
import { build } from "esbuild";

// Bundle the TS module instead of importing .ts directly — the CI smoke
// runner is Node 20, which has no native type stripping.
const bundled = await build({
  stdin: {
    contents: `
      export {
        inferredWorkflowInputs,
        workflowStepOutputs,
      } from "../src/lib/workflowBindings.ts";
    `,
    loader: "ts",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  define: {
    "import.meta.env": JSON.stringify({ DEV: false }),
  },
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;

const { inferredWorkflowInputs, workflowStepOutputs } = await import(moduleUrl);

test("workflow bindings expose the variables a configured node actually reads", () => {
  assert.deepEqual(
    inferredWorkflowInputs({
      input: "Write {{topic_brief.title}} using {{article_knowledge}}.",
      review: { packet: "{{topic_brief}}" },
      output_schema: { properties: { ignored: { type: "string" } } },
    }),
    [
      { key: "topic_brief", value: "{{topic_brief}}", type: "any" },
      { key: "article_knowledge", value: "{{article_knowledge}}", type: "any" },
    ],
  );
});

test("workflow bindings infer bare condition variables without treating literals as inputs", () => {
  assert.deepEqual(
    inferredWorkflowInputs({
      expression: "hn_packet.eligible == true and hn_packet.status != 'blocked'",
    }),
    [{ key: "hn_packet", value: "{{hn_packet}}", type: "any" }],
  );
});

test("workflow bindings include implicit output and wait response variables", () => {
  assert.deepEqual(
    workflowStepOutputs({
      id: "approve_article",
      type: "wait",
      config: { response_variable: "article_decision" },
    }),
    [{ key: "article_decision", value: "", type: "any" }],
  );
  assert.deepEqual(
    workflowStepOutputs({
      id: "draft_article",
      type: "agent",
      config: { output_var: "canonical_article" },
    }),
    [{ key: "canonical_article", value: "", type: "any" }],
  );
});
