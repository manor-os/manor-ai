import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

async function source(path) {
  return readFile(new URL(`../${path}`, import.meta.url), "utf8");
}

test("agent detail exposes a dedicated runtime learning surface", async () => {
  const detail = await source("src/pages/AgentDetail.tsx");
  const panel = await source("src/components/AgentLearningPanel.tsx");

  assert.match(detail, /type Tab = [^;]*"learning"/);
  assert.match(detail, /api\.agents\.learningCandidates\(agentId!/);
  assert.match(detail, /api\.agents\.runtimeEvidence\(agentId!/);
  assert.match(detail, /<AgentLearningPanel/);

  assert.match(panel, /enum LearningCandidateStatus/);
  assert.match(panel, /enum LearningCandidateType/);
  assert.match(panel, /enum RuntimeEvidenceType/);
  assert.match(panel, /learning_what_it_learned/);
  assert.match(panel, /function isVisibleLearning/);
  assert.match(panel, /<Toggle/);
  assert.doesNotMatch(panel, /learning_review_queue/);
  assert.doesNotMatch(panel, /onOpenWorkspace/);
  assert.match(panel, /learning_explainer/);
});

test("agent learning API client requests evidence and all candidate states", async () => {
  const api = await source("src/lib/api.ts");

  assert.match(api, /`\/agents\/\$\{agentId\}\/runtime\/evidence\?\$\{q\}`/);
  assert.match(api, /`\/agents\/\$\{agentId\}\/learning-candidates\?\$\{q\}`/);
  assert.match(api, /params\.status === null \? "" : params\.status/);
});
