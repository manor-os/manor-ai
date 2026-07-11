#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { build } from "esbuild";

const entryPoint = `
  export { shouldExpandAssistantProcessBlock } from "../src/lib/assistantProcessFlow.ts";
`;

const bundled = await build({
  stdin: {
    contents: entryPoint,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true,
  format: "esm",
  platform: "browser",
  write: false,
  logLevel: "silent",
});

const moduleUrl = `data:text/javascript;base64,${Buffer.from(
  bundled.outputFiles[0].text,
).toString("base64")}`;

const { shouldExpandAssistantProcessBlock } = await import(moduleUrl);

const failedCompletedBlock = {
  type: "process",
  status: "completed",
  default_collapsed: true,
  steps: [
    {
      seq: 1,
      kind: "tool",
      name: "generate_file",
      status: "error",
    },
  ],
};

assert.equal(
  shouldExpandAssistantProcessBlock(failedCompletedBlock),
  false,
  "summary-started process blocks should collapse even when a step failed",
);

assert.equal(
  shouldExpandAssistantProcessBlock({
    ...failedCompletedBlock,
    default_collapsed: false,
  }),
  true,
  "process blocks stay expanded before summary_start marks them collapsed",
);

assert.equal(
  shouldExpandAssistantProcessBlock({
    ...failedCompletedBlock,
    steps: [{ ...failedCompletedBlock.steps[0], status: "running" }],
  }),
  true,
  "actively running process blocks stay expanded",
);

console.log("assistant process flow collapse checks passed");
