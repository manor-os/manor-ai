import { expect, test, type Page } from "@playwright/test";

const name = "AI_SDE_20小时课程_Agenda与课程内容.md";
const id = "01M1B8KT380NRNZ9DGX8ZVGF7A";
const folderId = "01M1Y03Z6ZYDV7FN6TXA7G1QC8";
const workspace = "/workspaces/ws_fixture";
const body = "# Interview curriculum\n\nAlgorithms, system design, and behavioral practice.\n";
const document = {
  id, entity_id: "ent_fixture", name, fs_path: `Workspaces/Preparation/${name}`,
  file_type: "md", mime_type: "text/markdown", file_size: body.length,
  source: "agent", vector_status: "done", folder_id: folderId,
  current_user_capabilities: ["read", "preview", "download"],
};

async function installApi(page: Page, scenario = "success") {
  const calls: string[] = [];
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().isNavigationRequest()) {
      const response = await route.fetch({ url: `${url.origin}/e2e/fixtures/artifact-viewer.html` });
      return route.fulfill({ response });
    }
    if (!url.pathname.startsWith("/api/")) return route.continue();
    calls.push(`${url.pathname}${url.search}`);
    const json = (data: unknown, status = 200) => route.fulfill({ status, json: data });
    if (url.pathname === "/api/v1/documents") {
      expect(url.searchParams.get("workspace_id")).toBe("ws_fixture");
      expect(url.searchParams.get("include_generated_assets")).toBe("true");
      if (scenario === "lookup-error") return json({ detail: "Forbidden" }, 403);
      const candidates = scenario === "missing" ? [] : scenario === "ambiguous"
        ? [document, { ...document, id: "doc_second" }]
        : scenario === "encoded-name-collision"
          ? [document, { ...document, id: "doc_encoded", name: encodeURIComponent(name) }]
          : [document];
      const matches = candidates.filter((doc) => doc.name.includes(url.searchParams.get("search") || ""));
      // Two pages ensure uniqueness is checked across the complete result set.
      const offset = Number(url.searchParams.get("offset") || 0);
      const items = scenario === "incomplete" && offset ? [] : matches.slice(offset, offset + 1);
      return json({ items, total: scenario === "incomplete" ? 2 : matches.length });
    }
    if (url.pathname === `/api/v1/documents/${id}`) {
      if (scenario === "denied") return json({ detail: "Document access denied" }, 403);
      if (scenario === "deleted") return json({ detail: "Document not found" }, 404);
      return json(scenario === "root-folder" ? { ...document, folder_id: null } : document);
    }
    if ([`/api/v1/documents/${id}/preview/content`, `/api/v1/documents/${id}/download`].includes(url.pathname)) {
      return route.fulfill({ contentType: "text/markdown", body });
    }
    if (url.pathname === "/api/v1/comments") return json([]);
    return json({ detail: `Unexpected request: ${url.pathname}` }, 404);
  });
  return calls;
}

for (const width of [1280, 390]) {
  test(`viewer containing-folder link opens Knowledge at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    await page.goto(`${workspace}?case=canonical`);
    await page.getByRole("button", { name: new RegExp(name) }).click();
    const parentFolder = page.locator(".manor-editor-header-main").getByRole("link", { name: "Containing folder", exact: true });
    await expect(parentFolder).toBeVisible();
    await expect(parentFolder).toHaveAttribute("title", "Open containing folder");
    await expect(parentFolder).toHaveAttribute("href", `/knowledge?folder_id=${folderId}`);
    expect(await parentFolder.evaluate((link) => link.getBoundingClientRect().right <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: testInfo.outputPath(`viewer-parent-folder-${width}.png`) });
    await parentFolder.click();
    await expect(page).toHaveURL(`/knowledge?folder_id=${folderId}`);
  });

  test(`viewer back arrow returns to the workspace attachment at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    await page.goto(`${workspace}?case=canonical`);
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await page.getByRole("button", { name: "Go Back", exact: true }).click();
    await expect(page).toHaveURL(`${workspace}?case=canonical#completion`);
    await expect(page.getByRole("button", { name: new RegExp(name) })).toBeVisible();
  });

  test(`viewer close returns to the workspace attachment at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await installApi(page);
    await page.goto(`${workspace}?case=canonical`);
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(page).toHaveURL(`${workspace}?case=canonical#completion`);
    await expect(page.getByRole("button", { name: new RegExp(name) })).toBeVisible();
  });

  test(`legacy task attachment opens real Viewer and survives reload at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const calls = await installApi(page);
    await page.goto(`${workspace}?case=double`);
    const attachment = page.getByRole("button", { name: new RegExp(name) });
    await expect(attachment).toBeVisible();
    await attachment.focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(`/viewer/${id}`);
    await expect(page.getByRole("heading", { name: "Interview curriculum", exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath(`artifact-viewer-${width}.png`) });
    await page.reload();
    await expect(page.getByRole("heading", { name: "Interview curriculum", exact: true })).toBeVisible();
    await page.goBack();
    await expect(page).toHaveURL(`${workspace}?case=double#completion`);
    await expect(attachment).toBeVisible();
    expect(calls.some((call) => call.includes("/fs/"))).toBe(false);
    expect(calls.some((call) => call.startsWith("/api/v1/documents/AI_SDE"))).toBe(false);
    expect(errors).toEqual([]);
  });
}

test("viewer parent-folder navigation survives a direct URL and reload", async ({ page }) => {
  await installApi(page);
  await page.goto(`/viewer/${id}`);
  await page.reload();
  await page.getByRole("link", { name: "Containing folder", exact: true }).click();
  await expect(page).toHaveURL(`/knowledge?folder_id=${folderId}`);
});

test("viewer library link opens the Knowledge root for an unfiled document", async ({ page }) => {
  await installApi(page, "root-folder");
  await page.goto(`/viewer/${id}`);
  await page.getByRole("link", { name: "Open library", exact: true }).click();
  await expect(page).toHaveURL("/knowledge");
});

for (const variant of ["plain", "once", "attachment", "attachment-path", "canonical"]) {
  test(`${variant} reference uses the real document ID`, async ({ page }) => {
    const calls = await installApi(page);
    await page.goto(`${workspace}?case=${variant}`);
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await expect(page).toHaveURL(`/viewer/${id}`);
    await expect(page.getByRole("heading", { name: "Interview curriculum", exact: true })).toBeVisible();
    expect(calls.some((call) => call.includes("/fs/"))).toBe(false);
    if (variant === "canonical") expect(calls.some((call) => call.startsWith("/api/v1/documents?"))).toBe(false);
  });
}

for (const scenario of ["missing", "ambiguous", "encoded-name-collision", "incomplete", "lookup-error", "unscoped"]) {
  test(`${scenario} legacy reference fails closed without guessing or filesystem fallback`, async ({ page }) => {
    const calls = await installApi(page, scenario);
    const origin = scenario === "unscoped" ? "/e2e/fixtures/artifact-viewer.html" : workspace;
    await page.goto(origin);
    const attachment = page.getByRole("button", { name: new RegExp(name) });
    await attachment.click();
    await expect(attachment).toHaveAttribute("aria-disabled", "true");
    await expect(page).toHaveURL(origin);
    expect(calls.every((call) => call.startsWith("/api/v1/documents?"))).toBe(true);
    if (scenario === "unscoped") expect(calls).toEqual([]);
  });
}

for (const scenario of ["denied", "deleted"]) {
  test(`${scenario} document never falls back to storage bytes`, async ({ page }) => {
    const calls = await installApi(page, scenario);
    await page.goto(workspace);
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await expect(page).toHaveURL(`/viewer/${id}`);
    await expect(page.getByText(scenario === "denied" ? "Document access denied" : "Document not found", { exact: true })).toBeVisible();
    expect(calls.some((call) => call.includes("/fs/") || call.includes("/preview"))).toBe(false);
  });
}
