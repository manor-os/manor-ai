import { test, expect, type Page, type Route } from "@playwright/test";

const draft = "Read my email and summarize the attached notes";
const attachment = "connection-notes.txt";

async function prepare(page: Page, outcome = "success", delay?: "start" | "sync") {
  await page.route("**/api/v1/**", route => route.fulfill({ json: [] }));
  await page.goto(`/e2e/fixtures/integration-connect.html${delay ? `?delay=${delay}` : ""}`);
  await page.getByRole("combobox", { name: "Authorization outcome" }).selectOption(outcome);
  await page.getByRole("textbox").fill(draft);
  await page.locator('input[type="file"]').setInputFiles({
    name: attachment, mimeType: "text/plain", buffer: Buffer.from("Retain this file across connection setup"),
  });
  await page.getByRole("button", { name: "Connectors", exact: true }).click();
  await page.getByRole("button", { name: "Gmail Read and send email Connect", exact: true }).click();
  return page.getByRole("dialog", { name: "Gmail", exact: true });
}

async function expectRetained(page: Page) {
  await expect(page.getByLabel("Current route")).toHaveText("/chat");
  await expect(page.getByLabel("Chat draft")).toHaveText(draft);
  await expect(page.locator(".chat-composer").getByText(attachment, { exact: true })).toBeVisible();
}

async function holdResponses(page: Page, phase: "start" | "sync") {
  const pending: Route[] = [];
  await page.route(`**/e2e/fixtures/integration-${phase}`, route => { pending.push(route); });
  return async () => {
    await expect.poll(() => pending.length).toBeGreaterThan(0);
    await pending.shift()!.fulfill({ body: "ok" });
  };
}

async function reopenSetup(page: Page) {
  await page.getByRole("button", { name: "Connectors", exact: true }).click();
  await page.getByRole("button", { name: "Gmail Read and send email Connect", exact: true }).click();
  return page.getByRole("dialog", { name: "Gmail", exact: true });
}

test("closing connection setup preserves the route-owned draft and real file", async ({ page }) => {
  const dialog = await prepare(page);
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("Current route")).toHaveText("/chat");
  await expect(page.getByLabel("Request counts")).toContainText("Authorization requests: 0");
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expectRetained(page);
  await page.getByRole("textbox").press("Enter");
  await expect(page.getByLabel("Sent draft")).toHaveText(JSON.stringify({ text: draft, files: [attachment] }));
});

for (const outcome of ["success", "cancel", "sync-error"]) {
  test(`${outcome}: shared authorization preserves draft and attachment`, async ({ page }) => {
    const dialog = await prepare(page, outcome);
    const opened = page.waitForEvent("popup");
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    const popup = await opened;
    await popup.getByRole("button", { name: "Return to Chat" }).click();
    await expect(page.getByLabel("Request counts")).toContainText("Sync requests: 1");
    if (outcome === "success") {
      await expect(dialog).toHaveCount(0);
      await page.getByRole("button", { name: "Connectors", exact: true }).click();
      await expect(page.getByRole("button", { name: "Gmail Read and send email Use", exact: true })).toBeVisible();
      await expect(page.getByLabel("Request counts")).toContainText("Catalog requests: 2");
      await page.getByRole("button", { name: "Connectors", exact: true }).click();
    } else {
      await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
      if (outcome === "sync-error") await expect(page.getByText("Fixture sync failed", { exact: true })).toBeVisible();
      await dialog.getByRole("button", { name: "Close", exact: true }).click();
    }
    await expectRetained(page);
  });
}

test("authorization startup failure can retry without discarding the composer", async ({ page }) => {
  const dialog = await prepare(page, "start-error");
  await dialog.getByRole("button", { name: "Connect", exact: true }).click();
  await expect(page.getByText("Fixture authorization failed", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
  await expect(page.getByLabel("Request counts")).toContainText("Sync requests: 0");
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await expectRetained(page);
});

test("connection dialog fits mobile and traps keyboard focus", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const dialog = await prepare(page);
  await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeFocused();
  const bounds = await dialog.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.keyboard.press("Escape");
  await expectRetained(page);
  await expect(page.getByRole("button", { name: "Connectors", exact: true })).toBeFocused();
});

for (const dismiss of ["button", "escape", "backdrop"]) {
  test(`${dismiss} dismissal closes the active OAuth popup and cancels its polling`, async ({ page }) => {
    const dialog = await prepare(page);
    await page.clock.install();
    const opened = page.waitForEvent("popup");
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    const popup = await opened;
    await expect(popup.getByRole("button", { name: "Return to Chat" })).toBeVisible();
    if (dismiss === "escape") await page.keyboard.press("Escape");
    else if (dismiss === "backdrop") await page.locator(".manor-dialog-overlay").click({ position: { x: 5, y: 5 } });
    else await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect.poll(() => popup.isClosed(), { timeout: 2000 }).toBe(true);
    await page.clock.runFor(1200);
    await expect(page.getByLabel("Request counts")).toContainText("Sync requests: 0");
    await expectRetained(page);
    await expect(page.getByRole("button", { name: "Connectors", exact: true })).toBeFocused();
    await reopenSetup(page);
    await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeEnabled();
  });
}

for (const outcome of ["success", "start-error"]) {
  test(`late start ${outcome} cannot affect a reopened authorization session`, async ({ page }) => {
    const release = await holdResponses(page, "start");
    const dialog = await prepare(page, outcome, "start");
    const opened = page.waitForEvent("popup");
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    const oldPopup = await opened;
    await expect(page.getByLabel("Request counts")).toContainText("Authorization requests: 1");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expect.poll(() => oldPopup.isClosed(), { timeout: 2000 }).toBe(true);
    await page.getByRole("combobox", { name: "Authorization outcome" }).selectOption("success");
    await reopenSetup(page);
    const reopened = page.waitForEvent("popup");
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    const newPopup = await reopened;
    await expect(page.getByLabel("Request counts")).toContainText("Authorization requests: 2");
    await release();
    await expect(page.getByLabel("Response counts")).toContainText("Authorization responses: 1");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeDisabled();
    expect(newPopup.url()).toBe("about:blank");
    expect(page.context().pages()).toHaveLength(2);
    await expect(page.getByText("Fixture authorization failed", { exact: true })).toHaveCount(0);
    await release();
    await newPopup.getByRole("button", { name: "Return to Chat" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.getByLabel("Request counts")).toContainText("Sync requests: 1");
    await expectRetained(page);
  });
}

for (const outcome of ["success", "sync-error"]) {
  test(`late sync ${outcome} cannot dismiss or show errors in a new setup`, async ({ page }) => {
    const release = await holdResponses(page, "sync");
    const dialog = await prepare(page, outcome, "sync");
    const opened = page.waitForEvent("popup");
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    const popup = await opened;
    await popup.getByRole("button", { name: "Return to Chat" }).click();
    await expect(page.getByLabel("Request counts")).toContainText("Sync requests: 1");
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await reopenSetup(page);
    const reopened = page.waitForEvent("popup");
    await dialog.getByRole("button", { name: "Connect", exact: true }).click();
    const newPopup = await reopened;
    await expect(newPopup.getByRole("button", { name: "Return to Chat" })).toBeVisible();
    await release();
    await expect(page.getByLabel("Response counts")).toContainText("Sync responses: 1");
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Connect", exact: true })).toBeDisabled();
    await expect(page.getByText("Fixture sync failed", { exact: true })).toHaveCount(0);
    expect(newPopup.isClosed()).toBe(false);
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    await expectRetained(page);
    if (outcome === "success") {
      await page.getByRole("button", { name: "Connectors", exact: true }).click();
      await expect(page.getByRole("button", { name: "Gmail Read and send email Use", exact: true })).toBeVisible();
      await expect(page.getByLabel("Request counts")).toContainText("Catalog requests: 2");
    }
  });
}
