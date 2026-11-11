import { expect, test, type Page } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/config", (route) => route.fulfill({ json: {} }));
  await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/app-view")) return route.fulfill({ json: {
      workspace_id: "app-fixture", title: "Studio", description: "Prepare and review articles",
      status: "active", can_start: true, hero: "workflow:binding",
      config: { visual: "plain", layout: "feature" },
      actions: [{ key: "workflow:binding", kind: "workflow", binding_id: "binding", workflow_id: "workflow",
        title: "Article Flow", description: "Start with a topic", inputs: [] }],
      agent_options: [], workflow_options: [], automation_options: [],
    } });
    return route.fulfill({ json: [] });
  });
});

const group = (page: Page) => page.locator(".workspace-chat-output-panes");
const app = (page: Page) => group(page).locator('[data-pane-id="app"]');
const chat = (page: Page) => group(page).locator('[data-pane-id="chat"]');
const fraction = async (page: Page) => (await app(page).boundingBox())!.width / (await group(page).boundingBox())!.width;
// The header switch lives over the chat; an App alone on screen carries its own way out.
const display = async (page: Page, mode: "Chat" | "Split" | "App") => {
  const fromApp = mode === "Chat"
    ? page.getByRole("button", { name: "Open chat", exact: true })
    : mode === "Split" ? page.locator(".workspace-app-view__split") : null;
  if (fromApp && await fromApp.isVisible()) return fromApp.click();
  await page.locator(".workspace-app-display-switch").getByRole("button", { name: mode, exact: true }).click();
};
const closeApp = (page: Page) => display(page, "Chat");
// A wide window comes back side by side; a phone has room for one pane.
const reopenApp = (page: Page) => display(page, (page.viewportSize()?.width ?? 0) > 920 ? "Split" : "App");
const openFixture = async (page: Page) => {
  await page.goto("/e2e/fixtures/workspace-app-pane.html");
  await expect(page.getByText("Prepare and review articles", { exact: true })).toBeVisible();
};

async function dragToFraction(page: Page, fraction: number) {
  const box = (await group(page).getByRole("separator").boundingBox())!;
  const container = (await group(page).boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(container.x + container.width * fraction, box.y + box.height / 2);
  await page.mouse.up();
}

for (const width of [2048, 1440, 1000, 390]) {
  test(`App opens by default and preserves the selected display at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await openFixture(page);
    if (width > 920) {
      await expect(chat(page)).toBeVisible();
      await expect.poll(() => fraction(page)).toBeCloseTo(1 / 2, 2);
      const splitter = group(page).getByRole("separator");
      await splitter.focus();
      await expect(splitter).toBeFocused();
      await dragToFraction(page, 0.25);
      const expectedFraction = 1 - Math.max(0.25, 340 / (await group(page).boundingBox())!.width);
      await expect.poll(() => fraction(page)).toBeCloseTo(expectedFraction, 2);
      await page.reload();
      await expect(app(page)).toBeVisible();
      await expect.poll(() => fraction(page)).toBeCloseTo(expectedFraction, 2);
      await page.getByRole("button", { name: "Switch Workspace", exact: true }).click();
      await expect.poll(() => fraction(page)).toBeCloseTo(expectedFraction, 2);
    } else {
      await expect(chat(page)).toBeHidden();
      await expect(group(page).getByRole("separator")).toBeHidden();
      expect(await fraction(page)).toBeGreaterThan(0.99);
    }
    await closeApp(page);
    await expect(app(page)).toBeHidden();
    await expect(chat(page)).toBeVisible();
    await page.getByRole("button", { name: "Switch Workspace", exact: true }).click();
    await expect(app(page)).toBeHidden();
    await page.reload();
    await expect(app(page)).toBeHidden();
    await reopenApp(page);
    await expect(app(page)).toBeVisible();
    if (width > 920) {
      const expectedFraction = 1 - Math.max(0.25, 340 / (await group(page).boundingBox())!.width);
      await expect.poll(() => fraction(page)).toBeCloseTo(expectedFraction, 2);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`workspace-app-display-${width}.png`), animations: "disabled" });
  });
}

test("existing wide App preference is restored without a 50% cap", async ({ page }) => {
  await page.setViewportSize({ width: 2048, height: 900 });
  await page.addInitScript(() => localStorage.setItem("workspace-chat-app-panes-v2", JSON.stringify([20, 80])));
  await openFixture(page);
  await expect.poll(() => fraction(page)).toBeCloseTo(0.8, 2);
});

for (const side of ["app", "chat"] as const) {
  test(`dragging ${side} to the edge fills the other pane and remembers it`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openFixture(page);
    await expect.poll(() => fraction(page)).toBeCloseTo(1 / 2, 2);
    await dragToFraction(page, side === "app" ? 0.97 : 0.03);
    const remaining = side === "app" ? chat(page) : app(page);
    const collapsed = side === "app" ? app(page) : chat(page);
    await expect(collapsed).toBeHidden();
    expect((await remaining.boundingBox())!.width / (await group(page).boundingBox())!.width).toBeGreaterThan(0.99);
    await page.reload();
    await expect(collapsed).toBeHidden();
    await expect(remaining).toBeVisible();
    await page.getByRole("button", { name: "Switch Workspace", exact: true }).click();
    await expect(collapsed).toBeHidden();
    await reopenApp(page);
    await expect(app(page)).toBeVisible();
    await expect(chat(page)).toBeVisible();
    await expect.poll(() => fraction(page)).toBeCloseTo(1 / 2, 2);
  });
}

test("splitter keyboard controls can fill either pane", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openFixture(page);
  await group(page).getByRole("separator").press("Home");
  await expect(chat(page)).toBeHidden();
  expect(await fraction(page)).toBeGreaterThan(0.99);
  await reopenApp(page);
  await group(page).getByRole("separator").press("End");
  await expect(app(page)).toBeHidden();
  await expect(chat(page)).toBeVisible();
});


test("an opened Agent entry stops accepting input when its selected target becomes unavailable", async ({ page }) => {
  let available = true;
  await page.route("**/api/v1/**/app-view*", (route) => route.fulfill({ json: {
    workspace_id: "app-fixture", title: "Studio", description: "Prepare and review articles",
    status: "active", can_start: true, hero: "automation:review",
    config: { visual: "plain", layout: "feature" },
    actions: [{ key: "automation:review", kind: "chat", title: "Review Progress", source_title: "Review Progress",
      available, agent_id: available ? "agent" : undefined, description: "Review the release",
      inputs: [{ key: "brief", label: "Brief", type: "text", required: true, options: [] }] }],
    agent_options: [], workflow_options: [], automation_options: [],
  } }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await openFixture(page);
  await app(page).getByRole("button", { name: "Review Progress Review the release", exact: true }).click();
  await expect(app(page).getByRole("button", { name: "Next", exact: true })).toBeEnabled();
  available = false;
  await page.getByRole("button", { name: "Refresh App", exact: true }).click();
  await expect(app(page).getByRole("button", { name: "Next", exact: true })).toBeDisabled();
  await expect(app(page).getByRole("textbox", { name: "Brief *", exact: true })).toBeDisabled();
});
