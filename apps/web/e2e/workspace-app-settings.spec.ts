import { expect, test } from "@playwright/test";

const flow = { binding_id: "video", workflow_id: "video-flow", title: "Make a video", description: "", placeholder: "",
  inputs: [{ key: "brief", label: "Brief", type: "string", required: true }] };
const defaults = [
  { id: "actions", block: "actions", source: null, view: "default", limit: null, implicit: true, available: true, source_title: null },
  { id: "queue", block: "queue", source: "approvals:pending", view: "list", limit: null, implicit: true, available: true, source_title: null },
  { id: "metrics", block: "metrics", source: "workspace", view: "tiles", limit: 6, implicit: true, available: true, source_title: null },
  { id: "outputs", block: "outputs", source: "workspace", view: "list", limit: 8, implicit: true, available: true, source_title: null },
];
const config = {
  version: 1, eyebrow: null, title: null, description: null, hero: null, order: [], hidden: [], labels: {},
  descriptions: {}, forms: {}, targets: {}, entries: {}, prompts: {}, visual: "auto", layout: "feature",
  motion_cover_url: null, motion_cover_source_url: null, sections: null,
};
// The block catalog exactly as the server publishes it (app_block_catalog).
const blockCatalog = [
  { block: "connections", views: ["default"], default_source: null, source_kinds: [], multiple: false, removable: true, limit: null },
  { block: "actions", views: ["default"], default_source: null, source_kinds: [], multiple: false, removable: false, limit: null },
  { block: "queue", views: ["list"], default_source: "approvals:pending", source_kinds: ["approvals"], multiple: false, removable: true, limit: null },
  { block: "metrics", views: ["tiles"], default_source: "workspace", source_kinds: ["workspace"], multiple: false, removable: true, limit: { default: 4, max: 12 } },
  { block: "outputs", views: ["auto", "list", "gallery"], default_source: "workspace", source_kinds: ["workspace", "workflow"], multiple: true, removable: true, limit: { default: 8, max: 24 } },
];
const action = { key: "workflow:video", kind: "workflow", binding_id: "video", workflow_id: "video-flow",
  title: "Make a video", source_title: "Make a video", description: "", source_description: "", inputs: [] };

test("page blocks are arranged by hand and saved with the page", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  const drafts: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) return route.fulfill({ json: {
      workspace_id: "app-fixture", eyebrow: null, title: "Studio", description: "", status: "active",
      cover_image_url: null, cover_source: "none", hero: "workflow:video", actions: [action], available_actions: [action],
      sections: defaults, blocks: blockCatalog, workflow_options: [flow], automation_options: [], agent_options: [],
      attachable_workflow_options: [], config, can_start: true,
    } });
    if (path.endsWith("/operation/drafts")) {
      drafts.push(route.request().postDataJSON());
      return route.fulfill({ json: { id: "draft", validation: { valid: true } } });
    }
    if (path.endsWith("/apply")) return route.fulfill({ json: { status: "applied" } });
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/workspace-app-settings.html");

  const blocks = page.getByRole("list").filter({ has: page.getByRole("group", { name: "All actions" }) });
  await expect(page.getByText("Default blocks — a block with nothing to show stays hidden.", { exact: false })).toBeVisible();
  // The actions block moves but cannot be removed.
  await expect(page.getByRole("button", { name: "Remove block All actions" })).toHaveCount(0);
  await page.getByRole("button", { name: "Remove block Waiting for you" }).click();
  await page.getByRole("button", { name: "Add a block" }).click();
  await page.getByRole("option", { name: "Recent outputs" }).click();
  const added = blocks.getByRole("group").last();
  await added.getByRole("button", { name: /^Show outputs from/ }).click();
  await page.getByRole("option", { name: "Make a video" }).click();
  await page.getByRole("button", { name: "Display Make a video" }).click();
  await page.getByRole("option", { name: "Gallery" }).click();
  // Prefill the entry's Flow form, as a Blueprint does for each deck type.
  await page.getByRole("textbox", { name: "Prefill Brief" }).fill("Pitch deck for [company]");
  await page.getByRole("button", { name: "Save" }).click();

  await expect.poll(() => drafts.length).toBe(1);
  const update = (drafts[0] as { patches: Array<{ op: string; payload: { sections?: unknown } }> }).patches[0];
  expect(update.op).toBe("app_view.update");
  expect((update.payload as { presets?: unknown }).presets).toEqual({ "workflow:video": { brief: "Pitch deck for [company]" } });
  expect(update.payload.sections).toEqual([
    { id: "actions", block: "actions", source: null, view: "default", limit: null },
    { id: "metrics", block: "metrics", source: "workspace", view: "tiles", limit: 6 },
    { id: "outputs", block: "outputs", source: "workspace", view: "list", limit: 8 },
    { id: "outputs-2", block: "outputs", source: "workflow:video", view: "gallery", limit: null },
  ]);
});
