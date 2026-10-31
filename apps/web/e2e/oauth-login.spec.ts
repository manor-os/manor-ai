import { expect, test } from "@playwright/test";

test("cold OAuth login returns to consent after one sign-in under StrictMode", async ({ page }) => {
  await page.route("**/api/v1/oauth/clients/pms-fixture?*", route => route.fulfill({ json: {
    client_id: "pms-fixture", name: "PMS Fixture", redirect_uris: ["https://pms.example/auth/callback"], allowed_scopes: ["openid", "profile", "email"],
  } }));
  await page.goto("/e2e/fixtures/oauth-login.html");
  await expect(page).toHaveURL(/\/login\?next=/);
  const next = new URL(new URL(page.url()).searchParams.get("next")!, page.url());
  expect(next.pathname).toBe("/oauth/authorize");
  expect(next.searchParams.get("client_id")).toBe("pms-fixture");
  expect(next.searchParams.get("state")).toBe("fixture-state");
  await page.locator('input[type="email"]').fill("owner@example.com");
  await page.locator('input[type="password"]').fill("FixturePassword123!");
  await page.locator('button[type="submit"]').click();
  await expect(page).toHaveURL(/\/oauth\/authorize\?/);
  await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in to PMS Fixture" })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("redirect_uri")).toBe("https://pms.example/auth/callback");
  expect(new URL(page.url()).searchParams.get("state")).toBe("fixture-state");
});
