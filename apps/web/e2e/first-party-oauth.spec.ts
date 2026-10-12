import { test, expect } from "@playwright/test";

for (const [key, name] of [["manor_pms", "Manor PMS"], ["manor_mcp_minutes", "Manor Minutes"]]) {
  for (const width of [1280, 390]) {
  test(`${name} connects through OAuth directly from Integrations at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
    await page.route("**/test-oauth/authorize?*", route => route.fulfill({ contentType: "text/html", body: "<h1>Application OAuth consent destination</h1>" }));
    await page.goto("/e2e/fixtures/first-party-oauth.html?unconfigured");
    await page.getByRole("button", { name: new RegExp(`^${name} Your Manor application`) }).click();
    const detail = page.getByRole("dialog", { name, exact: true });
    const openApp = detail.getByRole("link", { name: new RegExp(`^Open ${name}`) });
    await expect(openApp).toHaveAttribute("href", key === "manor_pms" ? "https://portfolio.manorai.xyz/management" : "https://minutes.manorai.xyz/");
    await expect(openApp).toHaveAttribute("target", "_blank");
    await expect(openApp).toHaveAttribute("rel", "noopener noreferrer");
    await expect(detail.getByLabel("Access Token")).toHaveCount(0);
    await expect(detail.getByRole("button", { name: "Configure client", exact: true })).toHaveCount(0);
    await expect(detail.getByText(/Subscribe/i)).toHaveCount(0);
    await expect(detail.getByRole("button", { name: "Connect", exact: true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await detail.getByRole("button", { name: "Connect", exact: true }).focus();
    await page.screenshot({ path: test.info().outputPath(`${key}-${width}.png`), fullPage: true, animations: "disabled" });
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(new RegExp(`/test-oauth/authorize\\?provider=${key}&connection=$`));
    await expect(page.getByRole("heading", { name: "Application OAuth consent destination" })).toBeVisible();
  });
  }
}

test("OAuth start failure remains visible and does not show a connected account", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.goto("/e2e/fixtures/first-party-oauth.html?start-error");
  await page.getByRole("button", { name: /^Manor Minutes Your Manor application/ }).click();
  await page.getByRole("dialog", { name: "Manor Minutes", exact: true }).getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText(/OAuth app is not configured/i)).toBeVisible();
  await expect(page).toHaveURL(/first-party-oauth.html\?start-error$/);
  await expect(page.getByText("Test PMS account", { exact: true })).toHaveCount(0);
});

test("remote revoke failure updates local availability and permits retry", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  page.on("dialog", dialog => dialog.accept());
  await page.goto("/e2e/fixtures/first-party-oauth.html?connected&revoke-error");
  await page.getByRole("button", { name: /^Manor PMS Your Manor application/ }).click();
  const detail = page.getByRole("dialog", { name: "Manor PMS", exact: true });
  await detail.getByRole("button", { name: "More actions", exact: true }).click();
  await page.getByText("Disconnect", { exact: true }).click();
  await expect(page.getByText("Could not finish disconnecting", { exact: true })).toBeVisible();
  await expect(detail.getByText("Disconnected locally; remote revocation is pending.", { exact: true })).toBeVisible();
  await detail.getByRole("button", { name: "More actions", exact: true }).click();
  await page.getByText("Disconnect", { exact: true }).click();
  await expect(detail.getByText("Test PMS account", { exact: true })).toHaveCount(0);
  await expect(detail.getByRole("button", { name: "Connect", exact: true })).toBeVisible();
});

test("an expired application connection reconnects the selected OAuth account", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.route("**/test-oauth/authorize?*", route => route.fulfill({ contentType: "text/html", body: "<h1>Reconnect destination</h1>" }));
  await page.goto("/e2e/fixtures/first-party-oauth.html?connected&expired");
  await page.getByRole("button", { name: /^Manor PMS Your Manor application/ }).click();
  await page.getByRole("dialog", { name: "Manor PMS", exact: true }).getByRole("button", { name: "Reconnect", exact: true }).click();
  await expect(page).toHaveURL(/provider=manor_pms&connection=fixture-oauth-pms$/);
});

test("an existing manual PMS connection can add its first OAuth account", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.route("**/test-oauth/authorize?*", route => route.fulfill({ contentType: "text/html", body: "<h1>OAuth destination</h1>" }));
  await page.goto("/e2e/fixtures/first-party-oauth.html?legacy");
  await page.getByRole("button", { name: /^Manor PMS Your Manor application/ }).click();
  const detail = page.getByRole("dialog", { name: "Manor PMS", exact: true });
  await expect(detail.getByText("Legacy PMS account", { exact: true })).toBeVisible();
  await expect(detail.getByRole("button", { name: "Configure client", exact: true })).toHaveCount(0);
  await detail.getByRole("button", { name: /Add account/ }).click();
  await expect(page).toHaveURL(/provider=manor_pms&connection=$/);
});

test("cancelled OAuth reports cancellation and retains the existing connection", async ({ page }) => {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.goto("/e2e/fixtures/first-party-oauth.html?connected&cancelled");
  await expect(page.getByText("Authorization cancelled. Your connections were not changed.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^Manor PMS Your Manor application/ }).click();
  await expect(page.getByRole("dialog", { name: "Manor PMS", exact: true }).getByText("Test PMS account", { exact: true })).toBeVisible();
});
