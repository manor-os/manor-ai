import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const workspaceDetailSource = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);
const translations = await Promise.all(
  ["en", "es", "zh"].map((locale) =>
    readFile(new URL(`../src/lib/i18n/${locale}.ts`, import.meta.url), "utf8"),
  ),
);

const deleteCopy = translations.map((translation) => {
  const match = translation.match(
    /"page\.workspace_detail\.deleting_this_workspace_removes_its_operating_mo": "([^"]+)"/,
  );
  assert.ok(match, "workspace delete copy must exist in every locale");
  return match[1];
});

test("workspace delete copy describes the restore window without orphaning retained data", () => {
  assert.match(
    workspaceDetailSource,
    /t\("page\.workspace_detail\.deleting_this_workspace_removes_its_operating_mo"\)/,
  );
  for (const translation of deleteCopy) {
    assert.doesNotMatch(translation, /orphaned|hu[eé]rfanos|孤立项/i);
  }
  assert.match(deleteCopy[0], /restore.*30 days/i);
  assert.match(deleteCopy[1], /restaurar.*30 d[ií]as/i);
  assert.match(deleteCopy[2], /30 天内可恢复/);
});
