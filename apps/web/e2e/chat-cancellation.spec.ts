import { expect, test } from "@playwright/test";

for (const [locale, width] of [["en", 1280], ["zh", 1280], ["en", 390], ["zh", 390]] as const) {
  test(`${locale}/${width}: stop restores the composer immediately and retries transport errors automatically`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 800 });
    page.on("pageerror", (error) => { throw error; });
    let releaseCancel!: () => void;
    const cancelReady = new Promise<void>((resolve) => { releaseCancel = resolve; });
    let cancelRequests = 0;
    await page.route("**/config", (route) => route.fulfill({ json: {} }));
    await page.route("https://fonts.googleapis.com/**", (route) => route.abort());
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.includes("agent-initiatives")) {
        return route.fulfill({ json: { items: [] } });
      }
      if (path === "/api/v1/chat/runs/run-cancel-fixture/cancel") {
        cancelRequests += 1;
        if (cancelRequests === 1) {
          await cancelReady;
          return route.fulfill({ status: 503, json: { detail: "Temporarily unavailable" } });
        }
        return route.fulfill({ json: { id: "run-cancel-fixture", status: "cancelled" } });
      }
      if (path.endsWith("/chat/messages/page")) {
        return route.fulfill({ json: { items: [], has_more: false, next_cursor: null } });
      }
      if (path.endsWith("/app-view")) {
        return route.fulfill({ json: {
          workspace_id: "app-fixture", title: "Studio", status: "active",
          can_start: true, config: { visual: "plain", layout: "feature" }, actions: [],
          agent_options: [], workflow_options: [], automation_options: [],
        } });
      }
      return route.fulfill({ json: [] });
    });
    await page.goto("/e2e/fixtures/workspace-app-pane.html", { waitUntil: "domcontentloaded" });
    await page.evaluate(async (selectedLocale) => {
      const storePath = "/src/stores/chatStream.ts";
      const localePath = "/src/lib/i18n.ts";
      const { useChatStreamStore } = await import(storePath);
      const { setLocale } = await import(localePath);
      await setLocale(selectedLocale);
      const key = "workspace-chat:app-fixture";
      useChatStreamStore.setState({
        sessions: { [key]: {
          key, convId: "conversation-cancel-fixture", streaming: true,
          runtimeRunId: "run-cancel-fixture", runtimeStatus: "running", messages: [{
            id: "message-cancel-fixture", role: "assistant", content: "",
            meta: { stream_status: "streaming" },
            assistant_blocks: [{ id: "process", type: "process", status: "running",
              lifecycle_status: "running", started_at_ms: Date.now() - 11000, steps: [] }],
          }],
        } },
      });
    }, locale);

    const stopLabel = locale === "zh" ? "停止生成" : "Stop generating";
    const stoppingLabel = locale === "zh" ? "正在取消…" : "Stopping…";
    try {
      const stopButton = page.getByRole("button", { name: stopLabel, exact: true });
      await stopButton.focus();
      await expect(stopButton).toBeFocused();
      await stopButton.press("Enter");
      await expect(page.getByText(stoppingLabel, { exact: true })).toHaveCount(0);
      await expect(stopButton).toHaveCount(0, { timeout: 1000 });
      const composer = page.getByRole("textbox");
      await expect(composer).toBeEditable({ timeout: 1000 });
      await composer.fill("Next request");
      await expect(page.locator(".chat-composer-send")).toBeEnabled();
      await expect.poll(() => cancelRequests).toBe(1);
      await page.screenshot({ path: testInfo.outputPath("cancel-immediate.png") });
      releaseCancel();
      await expect.poll(() => cancelRequests).toBe(2);
      await expect(page.getByRole("button", { name: locale === "zh" ? "重试取消" : "Retry stop", exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: stoppingLabel, exact: true })).toHaveCount(0);
      await expect(page.getByRole("button", { name: stopLabel, exact: true })).toHaveCount(0);
      await expect(page.getByRole("alert")).toHaveCount(0);
      await expect(composer).toBeEditable();
      await expect(composer).toHaveText("Next request");
      await page.screenshot({ path: testInfo.outputPath("cancel-recovered.png") });
    } finally {
      releaseCancel();
    }
  });
}
