import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const workspacesSource = new URL("../src/pages/Workspaces.tsx", import.meta.url);
const marketplaceSource = new URL("../src/pages/BlueprintList.tsx", import.meta.url);
const routerSource = new URL("../src/router.tsx", import.meta.url);
const tabSwitcherSource = new URL("../src/components/ui/TabSwitcher.tsx", import.meta.url);
const retiredOfficeSource = new URL("../src/pages/ManorOffice.tsx", import.meta.url);
const retiredOfficeAssets = new URL("../public/assets/office", import.meta.url);

test("Workspace hub exposes only supported views and retires Office assets", async () => {
  const [workspaces, router] = await Promise.all([
    readFile(workspacesSource, "utf8"),
    readFile(routerSource, "utf8"),
  ]);

  assert.match(router, /path: "\/goals", element: <Navigate to="\/workspaces" replace \/>/);
  assert.doesNotMatch(workspaces, /key: "goals"/);
  assert.doesNotMatch(workspaces, /key: "office"/);
  assert.doesNotMatch(workspaces, /<ManorOffice/);
  assert.doesNotMatch(workspaces, /<WorkspaceGoalGraph/);
  assert.match(workspaces, /!requestedView \|\| requestedView === view/);
  await assert.rejects(access(retiredOfficeSource));
  await assert.rejects(access(retiredOfficeAssets));
});


test("mobile view tabs scroll only their own track", async () => {
  const tabSwitcher = await readFile(tabSwitcherSource, "utf8");

  assert.match(tabSwitcher, /const scroller = activeTab\?\.parentElement/);
  assert.match(tabSwitcher, /scroller\.scrollTo\(\{/);
  assert.doesNotMatch(tabSwitcher, /scrollIntoView/);
  assert.match(tabSwitcher, /prefers-reduced-motion: reduce/);
});
