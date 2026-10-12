import { expect, test } from "@playwright/test";

const view = {
  workspace_id: "app-fixture", title: "Loading Studio", description: "The App is ready",
  status: "active", can_start: true, hero: "workflow:binding",
  config: { visual: "plain", layout: "feature" },
  actions: [{ key: "workflow:binding", kind: "workflow", binding_id: "binding", workflow_id: "workflow",
    title: "Article Flow", description: "Start with a topic", inputs: [] }],
  agent_options: [], workflow_options: [], automation_options: [],
};
const history = { items: [], has_more: false, next_cursor: null };

test.beforeEach(async ({ page }) => {
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
});

test("Chat and App appear before a slow Workspace sidebar list finishes", async ({ page }, testInfo) => {
  let finishList!: () => void;
  const listReady = new Promise<void>((resolve) => { finishList = resolve; });
  let listFinished = false;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/workspaces") {
      await listReady;
      listFinished = true;
      return route.fulfill({ json: [] });
    }
    if (path === "/api/v1/workspaces/app-fixture") return route.fulfill({ json: {
      id: "app-fixture", name: "Loading Studio", status: "active", settings: {}, operating_model: {},
    } });
    if (path.endsWith("/app-view")) return route.fulfill({ json: view });
    if (path.endsWith("/chat/messages/page")) return route.fulfill({ json: history });
    if (path.endsWith("/connection-status")) return route.fulfill({ json: { requirements: [] } });
    return route.fulfill({ json: [] });
  });
  try {
    await page.goto("/e2e/fixtures/workspace-chat-loading.html");
    await expect(page.getByText("The App is ready", { exact: true })).toBeVisible();
    await expect(page.locator('[data-pane-id="chat"]')).toBeVisible();
    expect(listFinished).toBe(false);
    await page.screenshot({ path: testInfo.outputPath("workspace-before-sidebar.png") });
    // A newer authoritative list can still remove a deleted/revoked Workspace.
    finishList();
    await expect(page.locator('[data-pane-id="app"]')).toHaveCount(0);
  } finally { finishList(); }
});

test("secondary reads wait for visible content and stats load only when opened", async ({ page }) => {
  let finishHistory!: () => void;
  let finishApp!: () => void;
  const historyReady = new Promise<void>((resolve) => { finishHistory = resolve; });
  const appReady = new Promise<void>((resolve) => { finishApp = resolve; });
  const requests: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    requests.push(url.pathname);
    if (url.pathname.endsWith("/app-view")) {
      expect(url.searchParams.get("include_catalog")).toBe("false");
      await appReady;
      return route.fulfill({ json: view });
    }
    if (url.pathname.endsWith("/chat/messages/page")) {
      await historyReady;
      return route.fulfill({ json: history });
    }
    return route.fulfill({ json: [] });
  });
  const secondary = () => requests.filter((path) => /\/(agents|staff|chat\/entrypoints|stats)(\/|$)/.test(path));
  try {
    await page.goto("/e2e/fixtures/workspace-app-pane.html");
    await expect.poll(() => requests.some((path) => path.endsWith("/app-view"))).toBe(true);
    expect(secondary()).toEqual([]);
    finishHistory();
    await expect(page.locator('[data-pane-id="chat"] [aria-busy="true"]')).toHaveCount(0);
    expect(secondary()).toEqual([]);
    finishApp();
    await expect(page.getByText("The App is ready", { exact: true })).toBeVisible();
    await expect.poll(() => requests.some((path) => path.endsWith("/staff"))).toBe(true);
    expect(requests.some((path) => path.includes("/stats"))).toBe(false);
    await page.getByTestId("workspace-chat-stats-trigger").click();
    await expect.poll(() => requests.some((path) => path.endsWith("/stats"))).toBe(true);
  } finally { finishHistory(); finishApp(); }
});

test("a closed App stays idle after switching Workspace and can be reopened", async ({ page }) => {
  const appRequests: string[] = [];
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) { appRequests.push(path); return route.fulfill({ json: view }); }
    if (path.endsWith("/chat/messages/page")) return route.fulfill({ json: history });
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  await expect(page.getByText("The App is ready", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Hide workspace app", exact: true }).click();
  await page.getByRole("button", { name: "Switch Workspace", exact: true }).click();
  await expect.poll(() => appRequests.length).toBe(1);
  await expect(page.locator('[data-pane-id="app"]')).toBeHidden();
  await page.getByRole("button", { name: "Open workspace app", exact: true }).click();
  await expect(page.getByText("The App is ready", { exact: true })).toBeVisible();
  expect(appRequests).toHaveLength(2);
});

test("an App error releases secondary reads and can be retried", async ({ page }) => {
  let unavailable = true;
  let staffRequested = false;
  await page.route("**/api/v1/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) return unavailable
      ? route.fulfill({ status: 503, json: { detail: "Database temporarily busy" } })
      : route.fulfill({ json: view });
    if (path.endsWith("/chat/messages/page")) return route.fulfill({ json: history });
    if (path.endsWith("/staff")) staffRequested = true;
    return route.fulfill({ json: [] });
  });
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  await expect(page.getByText("This page could not be loaded.", { exact: true })).toBeVisible();
  await expect.poll(() => staffRequested).toBe(true);
  unavailable = false;
  await page.locator(".workspace-app-view").getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByText("The App is ready", { exact: true })).toBeVisible();
});
