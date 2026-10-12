import { test, expect, type Page } from "@playwright/test";

async function openCredentials(page: Page, missingPreview = false) {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.goto(`/e2e/fixtures/lark-integration.html?existing${missingPreview ? "&missing-preview" : ""}`);
  const detail = page.getByRole("dialog", { name: "Lark", exact: true });
  await expect(detail.locator('[data-integration-brand="lark"]')).toBeVisible();
  await detail.getByRole("button", { name: "More actions", exact: true }).click();
  await page.getByText("Edit", { exact: true }).click();
  return page.getByRole("dialog", { name: "Edit Lark account", exact: true });
}

for (const width of [1280, 390]) {
  test(`Lark connects through the administrator's OAuth app at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
    await page.route("**/test-lark-oauth/authorize?*", route => route.fulfill({ contentType: "text/html", body: "<h1>Lark OAuth consent destination</h1>" }));
    await page.goto("/e2e/fixtures/lark-integration.html?cloud");
    const detail = page.getByRole("dialog", { name: "Lark", exact: true });
    await expect(detail.getByText(/Tools use your account permissions/)).toBeVisible();
    await expect(detail.getByLabel("App ID", { exact: true })).toHaveCount(0);
    await expect(detail.getByLabel("App Secret", { exact: true })).toHaveCount(0);
    await expect(detail.getByRole("combobox", { name: "Platform", exact: true })).toHaveCount(0);
    const connect = detail.getByRole("button", { name: "Connect", exact: true });
    await expect(connect).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await connect.focus();
    await detail.screenshot({ path: `/tmp/manor-lark-oauth-${width}.png` });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/test-lark-oauth\/authorize\?provider=lark&connection=$/);
    await expect(page.getByRole("heading", { name: "Lark OAuth consent destination" })).toBeVisible();
  });
}

test("unconfigured Lark links to platform Admin instead of requesting app secrets", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.goto("/e2e/fixtures/lark-integration.html?unconfigured&cloud");
  const detail = page.getByRole("dialog", { name: "Lark", exact: true });
  await expect(detail.getByRole("link", { name: "Configure Lark in Admin", exact: true })).toHaveAttribute("href", "/admin/integrations#oauth-clients");
  await expect(detail.getByRole("button", { name: "Configure client", exact: true })).toHaveCount(0);
  await expect(detail.getByLabel("App Secret", { exact: true })).toHaveCount(0);
});

test("self-hosted Lark retains an accessible OAuth app configuration flow", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.goto("/e2e/fixtures/lark-integration.html?unconfigured");
  const detail = page.getByRole("dialog", { name: "Lark", exact: true });
  await expect(detail.getByRole("link", { name: "Configure Lark in Admin", exact: true })).toHaveCount(0);
  await detail.getByRole("button", { name: "Configure client", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "Lark OAuth client", exact: true });
  await expect(modal.getByRole("link", { name: "Lark developer console" })).toHaveAttribute("href", "https://open.larksuite.com/app");
  await modal.getByLabel("App ID", { exact: true }).fill("cli_oss_fixture");
  await modal.getByLabel("App Secret", { exact: true }).fill("fixture-oss-secret");
  await modal.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByLabel("Saved OAuth client")).toHaveText(JSON.stringify({ client_id: "cli_oss_fixture", client_secret: "fixture-oss-secret" }));
  await expect(modal).toHaveCount(0);
});

test("an existing application connection can add its first Lark OAuth account", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.route("**/test-lark-oauth/authorize?*", route => route.fulfill({ contentType: "text/html", body: "<h1>Lark OAuth consent destination</h1>" }));
  await page.goto("/e2e/fixtures/lark-integration.html?existing");
  const detail = page.getByRole("dialog", { name: "Lark", exact: true });
  await expect(detail.getByText("Team app", { exact: true })).toBeVisible();
  await detail.getByRole("button", { name: /Add account/ }).click();
  await expect(page).toHaveURL(/test-lark-oauth\/authorize\?provider=lark&connection=$/);
});

test("editing a legacy Lark connection preserves stored secrets and saved Feishu platform", async ({ page }) => {
  const modal = await openCredentials(page);
  await expect(modal.getByLabel("App ID", { exact: true })).toHaveValue("cli_fixture");
  await expect(modal.getByLabel("App Secret", { exact: true })).toHaveValue("");
  const platform = modal.getByRole("combobox", { name: "Platform", exact: true });
  await expect(platform).toHaveText("Feishu (China)");
  await modal.getByLabel("Account name (optional)", { exact: true }).fill("Legacy Feishu app");
  await modal.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByLabel("Updated connection")).toHaveText(JSON.stringify({
    credentials: { app_id: "cli_fixture", app_secret: "__unchanged__", platform: "feishu" }, config: { name: "Legacy Feishu app" },
  }));
});

test("editing Lark without a credential preview requires an explicit platform", async ({ page }) => {
  const modal = await openCredentials(page, true);
  const platform = modal.getByRole("combobox", { name: "Platform", exact: true });
  const save = modal.getByRole("button", { name: "Save", exact: true });
  await expect(platform).toHaveText("");
  await expect(platform).toHaveAttribute("aria-required", "true");
  await expect(save).toBeDisabled();
  await modal.getByLabel("App ID", { exact: true }).fill("cli_replacement");
  await modal.getByLabel("App Secret", { exact: true }).fill("replacement-app-secret");
  await expect(platform).toHaveText("");
  await expect(save).toBeDisabled();
  await expect(page.getByLabel("Updated connection")).toHaveText("none");
  await platform.click();
  await page.getByRole("option", { name: "Feishu (China)", exact: true }).click();
  await expect(save).toBeEnabled();
  await save.click();
  await expect(page.getByLabel("Updated connection")).toHaveText(JSON.stringify({
    credentials: { app_id: "cli_replacement", app_secret: "replacement-app-secret", platform: "feishu" }, config: {},
  }));
});
