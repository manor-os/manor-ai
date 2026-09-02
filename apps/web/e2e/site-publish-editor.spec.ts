import { expect, request as pwRequest, test } from "@playwright/test";

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_SITE_PUBLISH === "1";

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_SITE_PUBLISH=1 for the live document test");

test("HTML editor saves the latest code before publishing", async ({ page }) => {
  const api = await pwRequest.newContext({ baseURL: API });
  const suffix = Date.now();
  const filename = `site-editor-${suffix}.html`;
  const initialHtml = "<!doctype html><html><body>Before publish</body></html>";
  const autosaveHtml = "<!doctype html><html><body>Autosave in flight</body></html>";
  const editedHtml = "<!doctype html><html><body>Edited before publish</body></html>";
  let token = "";
  let documentId = "";
  let publishRequests = 0;
  let saveRequests = 0;
  let forPathRequests = 0;

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    const auth = await login.json();
    token = auth.access_token;
    const headers = { Authorization: `Bearer ${token}` };

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: {
        file: {
          name: filename,
          mimeType: "text/html",
          buffer: Buffer.from(initialHtml, "utf8"),
        },
      },
    });
    expect(upload.ok(), `upload failed: ${upload.status()}`).toBeTruthy();
    const document = await upload.json();
    documentId = document.id;
    expect(document.fs_path).toBeTruthy();

    const publishedUrl = `https://site-editor-${suffix}.sites.test.local`;
    const publishedSite = {
      id: `site-editor-${suffix}`,
      name: filename.replace(/\.html$/, ""),
      slug: `site-editor-${suffix}`,
      status: "active",
      revision: 1,
      url: publishedUrl,
      platform_url: publishedUrl,
      sites_domain: "sites.test.local",
      custom_domain: null,
      domain_status: null,
      published_at: new Date().toISOString(),
      source_path: document.fs_path,
      workspace_id: null,
      connections: {
        customer_service_channel_config_id: null,
        subscription_workflow_binding_id: null,
        lead_workflow_binding_id: null,
        analytics_enabled: true,
      },
    };

    let releaseFirstSave = () => undefined;
    const firstSaveRelease = new Promise<void>((resolve) => {
      releaseFirstSave = resolve;
    });
    let markFirstSaveStarted = () => undefined;
    const firstSaveStarted = new Promise<void>((resolve) => {
      markFirstSaveStarted = resolve;
    });
    let markFirstSaveFinished = () => undefined;
    const firstSaveFinished = new Promise<void>((resolve) => {
      markFirstSaveFinished = resolve;
    });
    let markSecondSaveFinished = () => undefined;
    const secondSaveFinished = new Promise<void>((resolve) => {
      markSecondSaveFinished = resolve;
    });

    await page.route(`**/api/v1/documents/${documentId}/file`, async (route) => {
      if (route.request().method() !== "PUT") {
        await route.continue();
        return;
      }
      const requestIndex = ++saveRequests;
      if (requestIndex === 1) {
        markFirstSaveStarted();
        await firstSaveRelease;
      }
      const response = await route.fetch();
      await route.fulfill({ response });
      if (requestIndex === 1) markFirstSaveFinished();
      if (requestIndex === 2) markSecondSaveFinished();
    });

    await page.route("**/api/v1/sites/for-path?*", async (route) => {
      forPathRequests += 1;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          publishable: true,
          target: document.fs_path,
          site: null,
          hosting_configured: true,
          auto_connection_plan: null,
          publish_snapshot_hash: "a".repeat(64),
        }),
      });
    });
    await page.route("**/api/v1/sites/publish", async (route) => {
      publishRequests += 1;
      expect(route.request().postDataJSON().auto_connect).toBe(false);
      expect(route.request().postDataJSON().expected_snapshot_hash).toBe("a".repeat(64));
      await firstSaveFinished;
      const savedContent = await api.get(`/api/v1/documents/${documentId}/content`, { headers });
      expect(savedContent.ok(), `content read failed: ${savedContent.status()}`).toBeTruthy();
      expect((await savedContent.json()).content).toBe(editedHtml);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ ...publishedSite, excluded: [], auto_connected: false }),
      });
    });
    await page.route(`**/api/v1/sites/${publishedSite.id}/connections`, async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          site_id: publishedSite.id,
          workspace_id: null,
          ...publishedSite.connections,
        }),
      });
    });
    await page.route(`**/api/v1/sites/${publishedSite.id}/analytics?*`, async (route) => {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          site_id: publishedSite.id,
          days: 30,
          analytics_enabled: true,
          page_views: 0,
          unique_sessions: 0,
          interactions: 0,
          form_submissions: 0,
          chat_opens: 0,
          top_pages: [],
        }),
      });
    });

    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
    }, token);
    await page.goto(`/editor/${documentId}`);

    const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
    if (await rejectCookies.isVisible()) await rejectCookies.click();
    const skipTour = page.getByRole("button", { name: "Skip", exact: true });
    if (await skipTour.isVisible()) await skipTour.click();

    const editor = page.locator(".doc-editor-ide-textarea");
    await expect(editor).toHaveValue(initialHtml);
    await editor.fill(autosaveHtml);
    await firstSaveStarted;
    await editor.fill(editedHtml);

    const publish = page.getByRole("button", { name: "Publish", exact: true });
    await expect(publish).toBeVisible();
    await publish.click();
    releaseFirstSave();
    await secondSaveFinished;
    await expect(page.getByRole("dialog", { name: "Publish website" })).toBeVisible();
    expect(publishRequests).toBe(0);
    expect(forPathRequests).toBeGreaterThanOrEqual(2);

    await page.getByRole("button", { name: "Confirm publish", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Site settings" })).toBeVisible();
    await expect(page.getByRole("link", { name: publishedUrl })).toBeVisible();
    expect(publishRequests).toBe(1);
    expect(saveRequests).toBe(2);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.getByRole("button", { name: new RegExp(`Published.*site-editor-${suffix}`) })).toBeVisible();
  } finally {
    if (token && documentId) {
      await api.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
    await api.dispose();
  }
});
