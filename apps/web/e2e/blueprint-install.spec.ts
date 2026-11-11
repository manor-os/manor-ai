import { expect, test, type Page } from "@playwright/test";

const blueprintPath = "/api/v1/blueprints/builtin:video-studio";
const fixtureUrl = "/e2e/fixtures/blueprint-install.html";

async function mockInstall(page: Page) {
  const state = {
    blueprint: {
      id: "builtin:video-studio", slug: "video-studio", title: "Video Studio",
      summary: "Create videos", status: "published", is_owner: false,
      install_count: 1, display_install_count: 1, tags: [], showcase_assets: [],
      author_display_name: "Manor AI", payload_version: "1.1", content_version: "1",
      payload: { manifest: {}, recipe: { subscriptions: [
        { service_key: "distribution.medium.chrome", agent_slug: "opc-browser-publisher-distribution-medium-chrome-v1" },
        { service_key: "distribution.x.chrome", agent_slug: "opc-browser-publisher-distribution-x-chrome-v1" },
      ] }, components: {} },
      setup_preview: {
        required_variables: [] as Record<string, unknown>[],
        optional_variables: [] as Record<string, unknown>[],
        required_channels: [], optional_channels: [], required_sessions: [], optional_sessions: [],
        required_integrations: [], optional_integrations: [], services: [], rules: [],
        first_week_outputs: [], validation_evidence: [], acceptance_criteria: [], not_included: [],
        author_hard_blocks: [], blocking_todos_expected: 0,
      },
    },
    requirements: [{ kind: "browser_session", provider: "chrome", label: "Chrome capture",
      required: false, blocking: false, ready: false, purpose: "Optional video capture" }],
    preflightStatus: 200, modes: [] as string[], installs: [] as Record<string, unknown>[],
    pageErrors: [] as string[], unexpected: [] as string[],
  };
  page.on("pageerror", (error) => state.pageErrors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem("manor_locale", "en");
    localStorage.removeItem("manor_token");
  });
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === blueprintPath) await route.fulfill({ json: state.blueprint });
    else if (url.pathname === "/api/v1/workspaces") await route.fulfill({ json: [] });
    else if (url.pathname === "/api/v1/blueprints/governance-presets") {
      await route.fulfill({ json: [
        { key: "standard", title: "Standard", summary: "Use the author's approval guidance." },
        { key: "safe", title: "Safe", summary: "Review every action." },
      ] });
    } else if (url.pathname === `${blueprintPath}/install-preflight`) {
      state.modes.push(url.searchParams.get("mode")!);
      await route.fulfill(state.preflightStatus === 200 ? { json: {
        ready: !state.requirements.some((item) => item.blocking && !item.ready),
        requirements: state.requirements,
      } } : { status: 503, json: { detail: "Readiness unavailable" } });
    } else if (url.pathname === `${blueprintPath}/install`) {
      state.installs.push(route.request().postDataJSON());
      await route.fulfill({ json: { workspace_id: "installed-studio", todos: [] } });
    } else {
      state.unexpected.push(url.pathname);
      await route.fulfill({ status: 501, json: { detail: "Unexpected fixture request" } });
    }
  });
  return state;
}

for (const width of [2560, 390]) {
  test(`centered detail and compact live install at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1080 });
    const state = await mockInstall(page);
    await page.goto(fixtureUrl);
    const title = page.getByRole("heading", { name: "Video Studio", exact: true });
    await expect(title).toBeVisible();
    const content = page.locator('[data-testid="content-shell"] > div').nth(1).locator(":scope > div > div").first();
    const bounds = (await content.boundingBox())!;
    expect(Math.abs(bounds.x - (width - bounds.x - bounds.width))).toBeLessThan(2);
    const titleBounds = (await title.boundingBox())!;
    expect(Math.abs(titleBounds.x - bounds.x)).toBeLessThan(2);
    await page.getByRole("button", { name: "Get for free", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("radio", { name: "Live", exact: true })).toBeChecked();
    await expect(dialog.getByRole("button", { name: "Install Live", exact: true })).toBeEnabled();
    await expect(dialog.getByRole("button", { name: "Advanced settings", exact: true })).toHaveAttribute("aria-expanded", "false");
    await expect(dialog.getByText("Governance preset", { exact: true })).toHaveCount(0);
    await expect(dialog.getByText(/Chrome capture/)).toHaveCount(0);
    expect((await dialog.boundingBox())!.height).toBeLessThan(350);
    expect(await dialog.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
    await dialog.screenshot({ path: testInfo.outputPath(`compact-live-${width}.png`) });
    await dialog.getByRole("button", { name: "Install Live", exact: true }).click();
    await expect(page.getByText("Workspace installed", { exact: true })).toBeVisible();
    expect(state.installs).toEqual([{
      mode: "live", governance_preset: "standard", install_author_hard_blocks: false,
      variable_values: {}, channel_config_ids: {},
    }]);
    expect(state.modes[0]).toBe("live");
    expect(state.pageErrors).toEqual([]);
    expect(state.unexpected).toEqual([]);
  });
}

test("keyboard selection and reopening return to live with advanced settings closed", async ({ page }) => {
  const state = await mockInstall(page);
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Get for free", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("radio", { name: "Live", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(dialog.getByRole("radio", { name: "Simulation", exact: true })).toBeChecked();
  await expect(dialog.getByRole("radio", { name: "Simulation", exact: true })).toBeFocused();
  await dialog.getByRole("button", { name: "Advanced settings", exact: true }).click();
  await expect(dialog.getByText(/Chrome capture/)).toBeVisible();
  await expect(dialog.getByText(/distribution\.(medium|x)\.chrome/)).toHaveCount(0);
  await expect(dialog.getByText(/Blueprint Agent:|Use Blueprint default|placeholder agents/i)).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
  await dialog.getByRole("button", { name: "Governance preset", exact: true }).click();
  await page.getByRole("option", { name: /^Safe,/ }).click();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Get for free", exact: true })).toBeFocused();
  await page.getByRole("button", { name: "Get for free", exact: true }).click();
  await expect(dialog.getByRole("radio", { name: "Live", exact: true })).toBeChecked();
  await expect(dialog.getByText("Governance preset", { exact: true })).toHaveCount(0);
});

test("required settings and connections remain visible and block live installation", async ({ page }) => {
  const state = await mockInstall(page);
  state.blueprint.setup_preview.required_variables = [
    { key: "studio", label: "Studio name", default: "", required: true },
  ];
  state.requirements = [{ kind: "browser_session", provider: "chrome", label: "Publishing browser",
    required: true, blocking: true, ready: false, purpose: "Connect the publishing account" }];
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Get for free", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const install = dialog.getByRole("button", { name: "Install Live", exact: true });
  await expect(install).toBeDisabled();
  await dialog.getByRole("textbox", { name: "Studio name" }).fill("My studio");
  await expect(dialog.getByText(/Publishing browser/)).toBeVisible();
  await expect(install).toBeDisabled();
  state.requirements[0].ready = true;
  await dialog.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(install).toBeEnabled();
  await install.click();
  await expect(page.getByText("Workspace installed", { exact: true })).toBeVisible();
  expect(state.installs[0].variable_values).toEqual({ studio: "My studio" });
});

test("failed live preflight can be refreshed without allowing installation", async ({ page }) => {
  const state = await mockInstall(page);
  state.preflightStatus = 503;
  await page.goto(fixtureUrl);
  await page.getByRole("button", { name: "Get for free", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("alert")).toContainText("Could not verify Integration readiness");
  await expect(dialog.getByRole("button", { name: "Install Live", exact: true })).toBeDisabled();
  state.preflightStatus = 200;
  await dialog.getByRole("button", { name: "Refresh status", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Install Live", exact: true })).toBeEnabled();
  expect(state.installs).toEqual([]);
});
