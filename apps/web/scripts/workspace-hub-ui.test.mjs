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

test("Marketplace follows an image-led community gallery layout", async () => {
  const marketplace = await readFile(marketplaceSource, "utf8");

  assert.doesNotMatch(marketplace, /className="workspace-marketplace-hero"/);
  assert.match(marketplace, /className="workspace-marketplace-filters"/);
  assert.match(marketplace, /<FilterBar activeCount=/);
  assert.match(marketplace, /<FilterSelect/);
  assert.match(marketplace, /className="workspace-marketplace-gallery-section"/);
  assert.match(marketplace, /<WorkspaceAppCard/);
  assert.match(marketplace, /className="blueprint-marketplace-card-cover"/);
  assert.match(marketplace, /className="blueprint-marketplace-card-intro"/);
  assert.match(marketplace, /className="blueprint-marketplace-card-price"/);
  assert.match(marketplace, /className="blueprint-marketplace-card-media-pagination"/);
  assert.match(marketplace, /onMouseMove=\{\(event\) =>/);
  assert.match(marketplace, /video\.play\(\)/);
  assert.match(marketplace, /prefers-reduced-motion: reduce/);
  assert.match(marketplace, /\{summary\}/);
  assert.match(marketplace, /markFailed\(active\.id\)/);
  assert.match(marketplace, /availableMedia\.length > 1/);
  assert.match(marketplace, /className="workspace-marketplace-command"/);
  assert.doesNotMatch(marketplace, /<CompactCard/);
  assert.doesNotMatch(marketplace, /showDetails/);
  assert.doesNotMatch(marketplace, /matchMedia\("\(hover: none\)"\)/);
});

test("mobile view tabs scroll only their own track", async () => {
  const tabSwitcher = await readFile(tabSwitcherSource, "utf8");

  assert.match(tabSwitcher, /const scroller = activeTab\?\.parentElement/);
  assert.match(tabSwitcher, /scroller\.scrollTo\(\{/);
  assert.doesNotMatch(tabSwitcher, /scrollIntoView/);
  assert.match(tabSwitcher, /prefers-reduced-motion: reduce/);
});
