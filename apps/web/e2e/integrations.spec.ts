import { test, expect } from "@playwright/test";

/**
 * Browser end-to-end test for the e-commerce / marketplace MCP integration
 * cards (PR #160). Drives a real browser against a real backend:
 *
 *   1. log in with the seeded local demo account via the real API → JWT
 *   2. inject the token (localStorage "manor_token") the way the app stores it
 *   3. load /integrations — the page fetches GET /api/v1/integrations/mcp-servers
 *      through the Vite dev proxy
 *   4. assert all five new platform cards render
 *
 * Run: see e2e/README.md (needs the backend up + `playwright install chromium`).
 */
const API = process.env.E2E_API ?? "http://localhost:8000";

const PLATFORMS = ["Shopify", "WooCommerce", "Square", "TikTok Shop", "Amazon"];

test("Integrations page renders the e-commerce MCP cards from the live API", async ({ page, request }) => {
  // 1. authenticate against the real backend. Registration may require email
  // verification, while the local stack contract guarantees this demo account.
  const login = await request.post(`${API}/api/v1/auth/login`, {
    data: {
      email: "demo@manor.local",
      password: "manor-demo",
    },
  });
  expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
  const { access_token } = await login.json();
  expect(access_token).toBeTruthy();

  // 2. authenticate the browser session the way the app does
  await page.addInitScript((token) => {
    window.localStorage.setItem("manor_token", token as string);
  }, access_token);

  // 3. load the Integrations page (fetches the MCP catalog through the proxy)
  await page.goto("/integrations");

  // 4. every new platform card must be visible
  for (const name of PLATFORMS) {
    await expect(
      page.getByText(name, { exact: false }).first(),
      `expected the "${name}" integration card to be visible`,
    ).toBeVisible();
  }
});
