#!/usr/bin/env node
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { test } from "node:test";
import { build } from "esbuild";

const bundled = await build({
  stdin: {
    contents: `
      export { matchSubAgentRuns } from "../src/lib/subAgentDisplay.ts";
    `,
    loader: "ts",
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
const { matchSubAgentRuns } = await import(moduleUrl);

const publisherRun = {
  run_id: "run_publisher",
  agent_name: "Publisher",
  service_key: "social_publisher",
  content: "",
};
const researcherRun = {
  run_id: "run_researcher",
  agent_name: "Researcher",
  service_key: "market_research",
  content: "",
};

test("delegation steps match runs by structured service and run id", () => {
  const items = [
    {
      name: "workspace_list_knowledge",
      arguments_preview: { query: "launch" },
    },
    {
      name: "workspace_agent",
      arguments_preview: {
        action: "delegate_service",
        params: { service_key: "social_publisher" },
      },
    },
    {
      name: "workspace_agent",
      arguments_preview: JSON.stringify({
        action: "delegate_service",
        params: { service_key: "market_research" },
      }),
      result_preview: JSON.stringify({ run_id: "run_researcher" }),
    },
  ];

  const matches = matchSubAgentRuns(items, [researcherRun, publisherRun]);
  assert.equal(matches.size, 2);
  assert.equal(matches.get(1)?.run_id, "run_publisher");
  assert.equal(matches.get(2)?.run_id, "run_researcher");
});

test("ordinary workspace_agent actions are not treated as delegation", () => {
  const matches = matchSubAgentRuns(
    [
      {
        name: "workspace_agent",
        arguments: {
          action: "create_task",
          params: { title: "delegate service launch work" },
        },
      },
    ],
    [publisherRun],
  );
  assert.equal(matches.size, 0);
});
