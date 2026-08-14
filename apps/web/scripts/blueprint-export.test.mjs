import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (relativePath) => readFile(new URL(relativePath, import.meta.url), "utf8");

const [modalSource, workspaceDetailSource, apiSource] = await Promise.all([
  read("../src/components/blueprints/ExportBlueprintModal.tsx"),
  read("../src/pages/WorkspaceDetail.tsx"),
  read("../src/lib/api.ts"),
]);

test("Blueprint export exposes the portable workspace sections and re-freeze control", () => {
  for (const key of [
    "include_workflows",
    "include_embedded_agents",
    "include_embedded_skills",
    "include_knowledge_packs",
    "include_starter_memory",
    "include_memory_files",
  ]) {
    assert.match(modalSource, new RegExp(`\\b${key}\\b`));
    assert.match(apiSource, new RegExp(`\\b${key}\\?`));
  }
  assert.match(modalSource, /replace_existing:\s*replaceExisting/);
  assert.match(apiSource, /replace_existing\?:\s*boolean/);
  assert.match(modalSource, /knowledge_pack_mode:\s*includes\.include_memory_files\s*\?\s*"inline_text"\s*:\s*"skeleton"/);
});

test("Blueprint export uses the shared accessible checkbox and management gate", () => {
  assert.match(modalSource, /import Checkbox from "\.\.\/ui\/Checkbox"/);
  assert.doesNotMatch(modalSource, /<input\s+type="checkbox"/);
  assert.match(
    workspaceDetailSource,
    /\{canManageWs && \(\s*<Button[\s\S]*?setExportOpen\(true\)[\s\S]*?export_as_blueprint/,
  );
  assert.match(
    workspaceDetailSource,
    /\{canManageWs && \(\s*<ExportBlueprintModal/,
  );
});
