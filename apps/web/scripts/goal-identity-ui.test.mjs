import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const identitySource = new URL("../src/lib/goalIdentity.ts", import.meta.url);
const graphSource = new URL("../src/components/ui/WorkspaceGoalGraph.tsx", import.meta.url);
const detailSource = new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url);
const numberSource = new URL("../src/lib/goalNumbers.ts", import.meta.url);

test("Goal UI deduplicates by stable identity before shared metrics or titles", async () => {
  const [identity, graph, detail] = await Promise.all([
    readFile(identitySource, "utf8"),
    readFile(graphSource, "utf8"),
    readFile(detailSource, "utf8"),
  ]);

  const idPosition = identity.indexOf("const id =");
  const goalKeyPosition = identity.indexOf("const goalKey =");
  const metricPosition = identity.indexOf("const metric =");
  const titlePosition = identity.indexOf("const title =");

  assert.ok(idPosition >= 0);
  assert.ok(idPosition < goalKeyPosition);
  assert.ok(goalKeyPosition < metricPosition);
  assert.ok(metricPosition < titlePosition);
  assert.match(identity, /if \(id\) return `id:\$\{id\}`/);
  assert.match(identity, /if \(goalKey\) return `goal:\$\{goalKey\}`/);
  assert.match(graph, /goalIdentityDedupeKey\(goal\)/);
  assert.match(detail, /goalIdentityDedupeKey\(goal\)/);
});

test("Goal UI preserves exact decimal strings while displaying and editing", async () => {
  const [graph, detail, numbers] = await Promise.all([
    readFile(graphSource, "utf8"),
    readFile(detailSource, "utf8"),
    readFile(numberSource, "utf8"),
  ]);

  assert.match(graph, /formatGoalNumber\(value\)/);
  assert.match(detail, /formatGoalNumber\(value\)/);
  assert.match(detail, /payload\.target_value = String\(goalForm\.target_value\)\.trim\(\)/);
  assert.doesNotMatch(detail, /payload\.target_value = Number\(goalForm\.target_value\)/);
  assert.match(numbers, /BigInt/);
  assert.match(numbers, /1_000_000n/);
  assert.doesNotMatch(graph, /const current = Number\(goal\?\.current_value/);
  assert.doesNotMatch(detail, /const current = Number\(g\?\.current_value/);
});

test("Goal execution graph resolves stat-bound measurement sources", async () => {
  const graph = await readFile(graphSource, "utf8");

  assert.match(graph, /api\.workspaces\.stats\.list\(workspaceId\)/);
  assert.match(graph, /const linkedStat = goal\.stat_id \? statsById\.get\(String\(goal\.stat_id\)\) : undefined/);
  assert.match(graph, /linkedStat\?\.name/);
  assert.match(graph, /goalOutcomeLabel\(goal, linkedStat\)/);
});
