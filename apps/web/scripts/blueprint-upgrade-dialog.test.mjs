import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/components/blueprints/BlueprintUpgradeDialog.tsx", import.meta.url),
  "utf8",
);
const workspaceDetail = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);

test("version-only blueprint updates can be acknowledged instead of showing Update 0", () => {
  assert.match(
    source,
    /const versionOnlySync = items\.length > 0 && visibleItems\.length === 0/,
  );
  assert.ok(source.includes('t("page.blueprints.upgrade_confirm_current")'));
  assert.ok(source.includes("disabled={busy || !canApply}"));
});

test("missing blueprint items remain visible and are not treated as version-only", () => {
  assert.match(
    source,
    /const visibleItems = items\.filter\(\(item\) => item\.action !== "unchanged"\)/,
  );
  assert.ok(source.includes('t("page.blueprints.upgrade_missing_items")'));
  assert.ok(source.includes('t("page.blueprints.upgrade_no_automatic_updates")'));
});

test("version-only confirmation does not offer an undo that has no restore point", () => {
  assert.ok(source.includes("canRevert: result.can_revert"));
  assert.ok(source.includes("{applied.canRevert && ("));
});

test("a blueprint-installed workspace can check real installed items even without a stale badge", () => {
  assert.ok(workspaceDetail.includes("ws.blueprint_update?.blueprint_id"));
  assert.ok(workspaceDetail.includes('t("page.workspaces.blueprint_check_updates")'));
  assert.ok(workspaceDetail.includes("setShowBlueprintUpgrade(true)"));
});

test("a failed upgrade-plan request is never presented as nothing to update", () => {
  assert.ok(source.includes("isError, error"));
  assert.ok(source.includes('t("page.blueprints.upgrade_load_failed")'));
  assert.match(source, /isLoading \? \([\s\S]+\) : isError \? \(/);
});
