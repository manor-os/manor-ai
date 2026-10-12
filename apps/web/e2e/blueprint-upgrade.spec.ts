import { expect, test, type Page } from "@playwright/test";

const fixtureUrl = "/e2e/fixtures/blueprint-upgrade.html";
const workspaceId = "blueprint-upgrade-fixture";
const upgradePath = `/api/v1/workspaces/${workspaceId}/blueprint/upgrade/v3`;

function planFixture() {
  return {
    upgrade_protocol_version: 3, workspace_id: workspaceId,
    workspace_name: "Faceless Stickman Video Studio", blueprint_slug: "stickman-studio",
    blueprint_fingerprint: "published-v2", plan_fingerprint: "reviewed-plan-v2",
    baseline_known: false, can_revert: true,
    restore_point: { applied_at: "2026-09-01T12:00:00Z", blueprint_version: "1.0" },
    active_runs: [] as Array<{ id: string; name: string; kind: string; status: string }>,
    setup_preflight: { ready: true, requirements: [] as Array<Record<string, unknown>> },
    setup_preview: { required_variables: [] as Array<Record<string, unknown>>, optional_variables: [] },
    variables_ready: true, resolved_variable_keys: [] as string[],
    resolved_variable_values: {} as Record<string, unknown>,
    items: [{
      key: "workspace:settings", kind: "workspace", slug: "settings", name: "Workspace settings",
      action: "keep_yours", changes: ["timezone", "weekly_video_target"], baseline_known: false,
      current_content: { timezone: "America/Los_Angeles", weekly_video_target: 3 },
      new_content: { timezone: "America/Los_Angeles", weekly_video_target: 5 },
      baseline_content: null as unknown,
    }, {
      key: "agent:editor", kind: "agent", slug: "editor", name: "Video editor",
      action: "missing", changes: ["Add the missing editor Agent"], baseline_known: true,
      current_content: null as unknown, new_content: { name: "Video editor", description: "Review rendered videos" },
      baseline_content: null as unknown,
    }],
  };
}

async function mockUpgrade(page: Page) {
  const state = {
    plan: planFixture(), previewStatus: 200, applyStatus: 200, revertStatus: 200,
    applies: [] as Record<string, any>[], previews: [] as Record<string, any>[], reverts: 0,
    unexpected: [] as string[], pageErrors: [] as string[],
  };
  page.on("pageerror", (error) => state.pageErrors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.removeItem("manor_token");
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === upgradePath && request.method() === "GET") {
      await route.fulfill({ json: state.plan });
    } else if (path === `${upgradePath}/preview`) {
      state.previews.push(request.postDataJSON());
      await route.fulfill(state.previewStatus === 200 ? { json: state.plan }
        : { status: state.previewStatus, json: { detail: "Could not validate updated settings" } });
    } else if (path === upgradePath && request.method() === "POST") {
      const body = request.postDataJSON();
      state.applies.push(body);
      const kept = body.configuration_resolutions.filter((item: { resolution: string }) => item.resolution === "keep_yours");
      await route.fulfill(state.applyStatus === 200 ? { json: {
        workspace_id: workspaceId, upgrade_protocol_version: 3, can_revert: true,
        updated: body.configuration_resolutions.filter((item: { resolution: string }) => item.resolution === "use_blueprint"),
        kept_yours: kept, fully_synchronized: true, synchronization_status: kept.length ? "customized" : "current",
      } } : { status: state.applyStatus, json: { detail: "Workspace changed since preview; review again" } });
    } else if (path === `/api/v1/workspaces/${workspaceId}/blueprint/revert`) {
      state.reverts += 1;
      await route.fulfill(state.revertStatus === 200 ? { json: { workspace_id: workspaceId, reverted: [] } }
        : { status: state.revertStatus, json: { detail: "Later edits prevent undo; your changes were preserved" } });
    } else {
      state.unexpected.push(`${request.method()} ${path}`);
      await route.fulfill({ status: 501, json: { detail: `Unexpected fixture request: ${path}` } });
    }
  });
  return state;
}

for (const width of [1440, 390]) {
  test(`reconfigure differences can be reviewed and upgraded in place at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await mockUpgrade(page);
    await page.goto(fixtureUrl);
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText(/Workspace ID, conversations, tasks/)).toBeVisible();
    await expect(dialog.getByLabel("Business Name")).toHaveCount(0);
    await expect(dialog.getByLabel("Operator Timezone")).toHaveCount(0);
    await expect(dialog.getByText(/No automatic updates/)).toHaveCount(0);
    await expect(dialog.getByText(/predates saved configuration baselines/)).toBeVisible();
    const settings = dialog.getByRole("region", { name: "Workspace settings", exact: true });
    expect((await settings.getByRole("heading", { name: "Workspace settings", exact: true }).boundingBox())!.height).toBeLessThan(50);
    await expect(settings.getByRole("button", { name: "Resolve conflict for Workspace settings" })).toHaveText("Keep my version");
    await settings.locator("summary").click();
    await expect(settings.locator("pre").nth(0)).toContainText('"weekly_video_target": 3');
    await expect(settings.locator("pre").nth(1)).toContainText('"weekly_video_target": 5');
    await expect(settings.locator("pre").nth(2)).toHaveText("No previous configuration was saved.");
    await settings.getByRole("button", { name: "Resolve conflict for Workspace settings" }).focus();
    await page.keyboard.press("Enter");
    await page.getByRole("option", { name: "Use Blueprint version", exact: true }).click();
    const apply = dialog.getByRole("button", { name: "Update 2 item(s)", exact: true });
    await expect(apply).toBeEnabled();
    await dialog.screenshot({ path: testInfo.outputPath(`upgrade-review-${width}.png`) });
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    const bounds = await apply.boundingBox();
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    await apply.click();
    await expect(dialog.getByText("The Workspace is synchronized with the latest Blueprint version.")).toBeVisible();
    expect(state.applies).toEqual([{
      expected_blueprint_fingerprint: "published-v2", expected_plan_fingerprint: "reviewed-plan-v2",
      variable_values: {}, channel_config_ids: {}, configuration_resolutions: [
        { key: "workspace:settings", resolution: "use_blueprint" }, { key: "agent:editor", resolution: "use_blueprint" },
      ],
    }]);
    expect(state.unexpected).toEqual([]);
    expect(state.pageErrors).toEqual([]);
  });
}

test("all-conflict keep-only upgrade synchronizes while preserving customization", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.items = state.plan.items.slice(0, 1);
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Confirm and keep yours", exact: true }).click();
  await expect(page.getByText(/selected customizations preserved/)).toBeVisible();
  expect(state.applies[0].configuration_resolutions).toEqual([{ key: "workspace:settings", resolution: "keep_yours" }]);
});

test("reopening an already customized Workspace does not claim identical content", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.items = [];
  state.plan.baseline_known = true;
  Object.assign(state.plan, { synchronization_status: "customized" });
  await page.goto(fixtureUrl);
  await expect(page.getByText(/selected customizations preserved/)).toBeVisible();
  await expect(page.getByText(/content already matches/)).toHaveCount(0);
  expect(state.applies).toEqual([]);
});

test("active work blocks apply and undo until refreshed, without cancelling runs", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.active_runs = [{ id: "render-1", name: "Render episode 12", kind: "workflow", status: "waiting" }];
  await page.goto(fixtureUrl);
  await expect(page.getByText("Render episode 12 · waiting")).toBeVisible();
  await expect(page.getByRole("button", { name: "Update 1 item(s)", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Undo this update", exact: true })).toBeDisabled();
  state.plan.active_runs = [];
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(page.getByRole("button", { name: "Update 1 item(s)", exact: true })).toBeEnabled();
  await expect(page.getByText("Render episode 12 · waiting")).toHaveCount(0);
  expect(state.applies).toEqual([]);
  expect(state.reverts).toBe(0);
  expect(state.unexpected).toEqual([]);
});

test("saved typed values prefill, invalid input blocks, and preview errors remain visible", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.setup_preview.required_variables = [{ key: "weekly_target", label: "Weekly target", default: 1, required: true, purpose: "Updates the weekly video goal." }];
  state.plan.resolved_variable_keys = ["weekly_target"];
  state.plan.resolved_variable_values = { weekly_target: 4 };
  await page.goto(fixtureUrl);
  const input = page.getByRole("textbox", { name: "Weekly target" });
  const apply = page.getByRole("button", { name: "Update 1 item(s)", exact: true });
  await expect(input).toHaveValue("4");
  await input.fill("invalid");
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(apply).toBeDisabled();
  await input.fill("");
  await expect(apply).toBeDisabled();
  state.previewStatus = 422;
  await input.fill("6");
  await expect(page.getByRole("alert")).toContainText("Could not validate updated settings");
  await expect(apply).toBeDisabled();
  state.previewStatus = 200;
  await page.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(apply).toBeEnabled();
  await apply.click();
  expect(state.applies[0].variable_values).toEqual({ weekly_target: 6 });
  expect(state.previews.at(-1)?.variable_values).toEqual({ weekly_target: 6 });
});

test("an old Workspace with no differences can confirm a new baseline", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.items = [];
  state.plan.can_revert = false;
  await page.goto(fixtureUrl);
  await expect(page.getByText(/predates saved configuration baselines/)).toBeVisible();
  await page.getByRole("button", { name: "Confirm latest version", exact: true }).click();
  expect(state.applies[0].configuration_resolutions).toEqual([]);
});

test("revert identifies the applied upgrade and preserves the dialog on later-edit rejection", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.revertStatus = 409;
  await page.goto(fixtureUrl);
  await expect(page.getByText(/Latest applied upgrade:.*Blueprint version 1.0/)).toBeVisible();
  await page.getByRole("button", { name: "Undo this update", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Later edits prevent undo; your changes were preserved");
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(state.applies).toEqual([]);
  expect(state.reverts).toBe(1);
  state.revertStatus = 200;
  await page.getByRole("button", { name: "Undo this update", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test("a stale preview is rejected visibly and remains reviewable", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.applyStatus = 409;
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Update 1 item(s)", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Workspace changed since preview; review again");
  await expect(page.getByRole("dialog")).toBeVisible();
  expect(state.applies).toHaveLength(1);
});

test("long configuration values wrap on mobile and diff remains keyboard accessible", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  const state = await mockUpgrade(page);
  state.plan.items = state.plan.items.slice(0, 1);
  state.plan.items[0].current_content = { output_path: "video-".repeat(80) };
  await page.goto(fixtureUrl);
  const dialog = page.getByRole("dialog");
  const summary = dialog.locator("summary");
  await summary.focus();
  await summary.press("Enter");
  const current = dialog.locator("pre").first();
  await expect(current).toBeVisible();
  expect(await current.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  const apply = page.getByRole("button", { name: "Confirm and keep yours", exact: true });
  await apply.focus();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
});

test("implicit channel defaults do not change the inputs bound to the reviewed fingerprint", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.setup_preflight.requirements = [{
    kind: "channel", requirement_key: "publishing", label: "Publishing account", required: true,
    ready: true, resource_id: "saved-channel", resource_options: [],
  }];
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Update 1 item(s)", exact: true }).click();
  expect(state.applies[0].channel_config_ids).toEqual({});
  expect(state.applies[0].expected_plan_fingerprint).toBe("reviewed-plan-v2");
});

test("selecting a required channel rebuilds the plan before enabling apply", async ({ page }) => {
  const state = await mockUpgrade(page);
  state.plan.setup_preflight = { ready: false, requirements: [{
    kind: "channel", requirement_key: "publishing", label: "Publishing account", required: true,
    ready: false, resource_options: [{ id: "chosen-channel", label: "Studio publishing" }],
  }] };
  await page.route("**/blueprint/upgrade/v3/preview", async (route) => {
    state.previews.push(route.request().postDataJSON());
    await route.fulfill({ json: { ...state.plan, plan_fingerprint: "channel-reviewed-plan",
      setup_preflight: { ready: true, requirements: [{ ...state.plan.setup_preflight.requirements[0],
        ready: true, resource_id: "chosen-channel" }] },
    } });
  });
  await page.goto(fixtureUrl);
  const apply = page.getByRole("button", { name: "Update 1 item(s)", exact: true });
  await expect(apply).toBeDisabled();
  await page.getByRole("button", { name: "Publishing account", exact: true }).click();
  await page.getByRole("option", { name: "Studio publishing", exact: true }).click();
  await expect(apply).toBeEnabled();
  await apply.click();
  expect(state.previews[0].channel_config_ids).toEqual({ publishing: "chosen-channel" });
  expect(state.applies[0].channel_config_ids).toEqual({ publishing: "chosen-channel" });
  expect(state.applies[0].expected_plan_fingerprint).toBe("channel-reviewed-plan");
});

test("a file-backed starter document explains why it is kept and does not block other upgrades", async ({ page }) => {
  const state = await mockUpgrade(page);
  Object.assign(state.plan.items[0], {
    name: "Production guide", action: "update",
    blocked_reason: "This starter document is now a stored file; keep it or reconcile it through the document editor",
  });
  await page.goto(fixtureUrl);
  await expect(page.getByText(/This starter document is now a stored file/)).toBeVisible();
  const selection = page.getByRole("button", { name: "Resolve conflict for Production guide" });
  await expect(selection).toHaveText("Keep my version");
  await expect(selection).toBeDisabled();
  await page.getByRole("button", { name: "Update 1 item(s)", exact: true }).click();
  expect(state.applies[0].configuration_resolutions[0]).toEqual({ key: "workspace:settings", resolution: "keep_yours" });
});

test("keeping runtime requirements does not demand a newly added unused connection", async ({ page }) => {
  const state = await mockUpgrade(page);
  Object.assign(state.plan.items[0], {
    key: "runtime_requirements", kind: "runtime_requirements", name: "Runtime requirements",
  });
  state.plan.setup_preflight = { ready: false, requirements: [{
    kind: "session", label: "New publishing browser", required: true, ready: false,
    resource_options: [], reason: "Sign in to the new publishing account",
  }] };
  Object.assign(state.plan, { kept_setup_preflight: { ready: true, requirements: [] } });
  await page.goto(fixtureUrl);
  await expect(page.getByRole("button", { name: "Update 1 item(s)", exact: true })).toBeEnabled();
  await expect(page.getByText("New publishing browser", { exact: true })).toHaveCount(0);
  const choice = page.getByRole("button", { name: "Resolve conflict for Runtime requirements" });
  await choice.click();
  await page.getByRole("option", { name: "Use Blueprint version", exact: true }).click();
  await expect(page.getByRole("button", { name: "Update 2 item(s)", exact: true })).toBeDisabled();
  await expect(page.getByText("New publishing browser", { exact: true })).toBeVisible();
  await choice.click();
  await page.getByRole("option", { name: "Keep my version", exact: true }).click();
  await page.getByRole("button", { name: "Update 1 item(s)", exact: true }).click();
  expect(state.applies[0].configuration_resolutions[0]).toEqual({ key: "runtime_requirements", resolution: "keep_yours" });
});
