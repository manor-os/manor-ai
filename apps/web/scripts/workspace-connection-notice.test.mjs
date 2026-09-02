import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const read = (file) => readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
const component = read("components/workspaces/WorkspaceConnectionNotice.tsx");

test("details and main chat share the live notice, independent of creation origin", () => {
  for (const path of ["pages/WorkspaceDetail.tsx", "components/WorkspaceChat.tsx"]) {
    assert.match(read(path), /<WorkspaceConnectionNotice workspaceId=/);
  }
  assert.doesNotMatch(component, /flagged_integrations|resolveIntegrations|_blueprint/);
  assert.doesNotMatch(read("pages/WorkspaceDetail.tsx"), /Missing integrations — flagged by the architect/);
});

test("only required failures alert; the query is actor-scoped and refreshed", () => {
  assert.match(component, /item\.required && !item\.ready/);
  assert.match(component, /user\?\.entity_id, user\?\.id, workspaceId/);
  assert.match(component, /refetchInterval: 30_000/);
  assert.match(component, /refetchOnWindowFocus: "always"/);
  assert.match(component, /status\.isError/);
  assert.match(component, /status\.refetch\(\)/);
});

test("connection guidance remains keyboard-accessible and height-bounded", () => {
  assert.match(component, /<details>/);
  assert.match(component, /<summary>/);
  assert.match(component, /role="status"/);
  assert.match(component, /<Link to=/);
  const css = read("components/workspaces/WorkspaceConnectionNotice.css");
  assert.match(css, /max-height: min\(35vh, 280px\)/);
  assert.match(css, /focus-visible/);
});
