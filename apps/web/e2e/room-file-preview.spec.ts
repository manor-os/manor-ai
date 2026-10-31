import { chromium, expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

const endpoint = "**/api/v1/chat/rooms/fixture-room/files/fixture-file";
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jG1sAAAAASUVORK5CYII=", "base64");

for (const width of [1280, 390]) {
  test(`loading, keyboard retry and persistent offline reuse at ${width}px`, async ({ page }, testInfo) => {
    const screenshotImage = await readFile(new URL("../public/assets/samples/previews/slides/manor_technical_architecture.png", import.meta.url));
    await page.setViewportSize({ width, height: 850 });
    let calls = 0;
    let bodyDownloads = 0;
    let releaseFirst!: () => void;
    const first = new Promise<void>((resolve) => { releaseFirst = resolve; });
    await page.route(endpoint, async (route) => {
      calls++;
      if (calls === 1) {
        await first;
        await route.fulfill({ status: 404 });
      } else {
        bodyDownloads++;
        await route.fulfill({ status: 200, contentType: "application/octet-stream", body: screenshotImage, headers: { "Cache-Control": "private, no-store" } });
      }
    });
    await page.goto("/e2e/fixtures/room-file-preview.html");
    await expect(page.getByRole("status")).toHaveText("Loading preview…");
    releaseFirst();
    await expect(page.getByRole("status")).toHaveText("Preview could not load");
    await page.screenshot({ path: testInfo.outputPath(`error-${width}.png`) });
    await page.getByRole("button", { name: "Retry", exact: true }).focus();
    await page.keyboard.press("Enter");
    const image = page.getByRole("img", { name: "image.png" });
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
    const enlarge = page.getByRole("button", { name: "View full screen image.png", exact: true });
    await enlarge.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "image.png", exact: true });
    await expect(dialog).toBeVisible();
    const fullImage = dialog.getByRole("img", { name: "image.png" });
    await expect(fullImage).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`expanded-${width}.png`), animations: "disabled" });
    await dialog.getByRole("button", { name: "Actual size (100%)" }).click();
    await expect.poll(() => fullImage.evaluate((element) => element.getBoundingClientRect().width)).toBe(1600);
    await dialog.getByRole("button", { name: "Zoom in", exact: true }).click();
    await expect.poll(() => fullImage.evaluate((element) => element.getBoundingClientRect().width)).toBe(2000);
    const viewport = dialog.getByRole("region", { name: "image.png" });
    if (width === 1280) {
      const bounds = (await viewport.boundingBox())!;
      await page.mouse.move(bounds.x + 300, bounds.y + 250);
      await page.mouse.down();
      await page.mouse.move(bounds.x + 150, bounds.y + 150);
      await page.mouse.up();
      await expect.poll(() => viewport.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    }
    await dialog.getByRole("button", { name: "Fit to window", exact: true }).click();
    await expect.poll(() => viewport.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(enlarge).toBeFocused();
    await page.getByRole("button", { name: "image.png 46 KB", exact: true }).click();
    await expect(page.getByText("Preview opened")).toBeVisible();
    await page.reload();
    await expect(image).toBeVisible();
    await page.context().setOffline(true);
    await page.getByRole("button", { name: "Hide attachment" }).click();
    await page.getByRole("button", { name: "Show attachment" }).click();
    await expect(image).toBeVisible();
    await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
    await enlarge.click();
    await expect(dialog.getByRole("img", { name: "image.png" })).toBeVisible();
    await dialog.getByRole("button", { name: "Close", exact: true }).click();
    expect(calls).toBe(2);
    expect(bodyDownloads).toBe(1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`loaded-${width}.png`) });
  });
}

test("cached attachment survives closing and restarting the browser", async ({ baseURL }, testInfo) => {
  let downloads = 0;
  const profile = testInfo.outputPath("browser-profile");
  for (let attempt = 0; attempt < 2; attempt++) {
    const context = await chromium.launchPersistentContext(profile, { headless: true, baseURL });
    try {
      await context.route(endpoint, async (route) => {
        downloads++;
        if (attempt > 0) await route.abort();
        else await route.fulfill({ status: 200, contentType: "application/octet-stream", body: png });
      });
      const page = await context.newPage();
      await page.goto("/e2e/fixtures/room-file-preview.html");
      const image = page.getByRole("img", { name: "image.png" });
      await expect(image).toBeVisible();
      await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
    } finally { await context.close(); }
  }
  expect(downloads).toBe(1);
});

test("switching accounts or companies downloads separate copies; returning reuses the first", async ({ page }) => {
  let downloads = 0;
  await page.route(endpoint, async (route) => {
    downloads++;
    await route.fulfill({ status: 200, contentType: "application/octet-stream", body: png });
  });
  for (const [query, expected] of [["", 1], ["?account=second", 2], ["?company=second", 3], ["", 3]] as const) {
    await page.goto(`/e2e/fixtures/room-file-preview.html${query}`);
    await expect(page.getByRole("img", { name: "image.png" })).toBeVisible();
    expect(downloads).toBe(expected);
  }
});

test("leaving the room cancels an unfinished attachment download", async ({ page }) => {
  const failed: string[] = [];
  page.on("requestfailed", (request) => { if (request.url().includes("/files/fixture-file")) failed.push(request.failure()?.errorText || "failed"); });
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  await page.route(endpoint, async (route) => { await blocked; await route.abort().catch(() => {}); });
  await page.goto("/e2e/fixtures/room-file-preview.html");
  await expect(page.getByRole("status")).toHaveText("Loading preview…");
  await page.getByRole("button", { name: "Hide attachment" }).click();
  await expect.poll(() => failed.length).toBe(1);
  release();
});

test("retry repairs an unreadable cached preview and persists the replacement", async ({ page }) => {
  let downloads = 0;
  await page.route(endpoint, async (route) => {
    downloads++;
    await route.fulfill({ status: 200, contentType: "application/octet-stream", body: downloads === 1 ? Buffer.from("broken image") : png });
  });
  await page.goto("/e2e/fixtures/room-file-preview.html");
  await expect(page.getByRole("status")).toHaveText("Preview could not load");
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  const image = page.getByRole("img", { name: "image.png" });
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  await page.reload();
  await expect.poll(() => image.evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  expect(downloads).toBe(2);
});

test("reopening a timed-out attachment clears the conversation error", async ({ page }) => {
  await page.clock.install();
  await page.route("**/config", (route) => route.fulfill({ json: { deployment_mode: "oss" } }));
  const createdAt = "2026-10-12T10:00:00Z";
  const file = { id: "fixture-file", message_id: "message", name: "notes.txt", mime_type: "text/plain", size: 21, sender_name: "Teammate", created_at: createdAt };
  const room = {
    id: "fixture-room", kind: "group", title: "Download recovery", owner_scope: "personal",
    member_count: 2, members: [{ user_id: "fixture", name: "Fixture", role: "member" }, { user_id: "teammate", name: "Teammate", role: "member" }],
    agents: [], can_manage_agents: false, ai_tool_execution_enabled: false, ai_tool_execution_revision: 0,
    can_manage_ai_execution: false, notifications_muted: false, created_at: createdAt, last_message_at: createdAt,
  };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path.endsWith("/fixture-room") ? room
      : path.endsWith("/messages") ? [{ id: "message", room_id: room.id, sender_user_id: "teammate", sender_name: "Teammate", content: "", mentioned_user_ids: [], attachments: [file], created_at: createdAt }]
      : path.endsWith("/presence") ? { online_user_ids: [] }
      : path.endsWith("/message-retractions") ? { message_ids: [] } : [];
    await route.fulfill({ json: body });
  });
  let calls = 0;
  let release!: () => void;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  await page.route(endpoint, async (route) => {
    if (++calls === 1) {
      await blocked;
      await route.abort().catch(() => {});
    } else await route.fulfill({ contentType: "text/plain", body: "Recovered attachment." });
  });
  await page.goto("/e2e/fixtures/room-file-preview.html?conversation");
  const attachment = page.getByRole("button", { name: /^notes\.txt/ });
  await attachment.click();
  await expect.poll(() => calls).toBe(1);
  await page.clock.fastForward(60_001);
  const error = page.getByText("File download stalled for 60 seconds. Please retry.", { exact: true });
  await expect(error).toBeVisible();
  release();
  await attachment.click();
  await expect(page.getByText("Recovered attachment.", { exact: true })).toBeVisible();
  await expect(error).toBeHidden();
  expect(calls).toBe(2);
});
