import { expect, test, type Page } from "@playwright/test";

const name = "AI_SDE_20小时课程_Agenda与课程内容.md";
const id = "01M1B8KT380NRNZ9DGX8ZVGF7A";
const folderId = "01M1Y03Z6ZYDV7FN6TXA7G1QC8";
const workspace = "/workspaces/ws_fixture";
const body = "# Interview curriculum\n\nAlgorithms, system design, and behavioral practice.\n";
const docxMime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
const docxPage = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="612" height="792"><text x="20" y="40">Server DOCX page</text></svg>');
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

type ViewerBehavior = "text-delayed" | "docx-server" | "docx-comments" | "docx-manifest-failure" | "docx-page-failure" | "route-switch" | "task-output";

async function installViewerBehaviorApi(page: Page, behavior: ViewerBehavior) {
  const calls: string[] = [];
  const releases = new Map<string, () => void>();
  const docxId = behavior === "docx-comments" ? "docx-comments"
    : behavior === "docx-manifest-failure" ? "docx-manifest-failure"
      : behavior === "docx-page-failure" ? "docx-page-failure" : "docx-server";
  const docs: Record<string, typeof document> = {
    [id]: document,
    "doc-b": { ...document, id: "doc-b", name: "Document B.md", file_size: 20 },
    [docxId]: {
      ...document,
      id: docxId,
      name: `${docxId}.docx`,
      file_type: "docx",
      mime_type: docxMime,
      current_user_capabilities: ["read", "preview", "download", "edit", "comment"],
    },
  };
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().isNavigationRequest()) {
      const response = await route.fetch({ url: `${url.origin}/e2e/fixtures/artifact-viewer.html` });
      return route.fulfill({ response });
    }
    if (!url.pathname.startsWith("/api/")) return route.continue();
    calls.push(url.pathname);
    const json = (data: unknown, status = 200) => route.fulfill({ status, json: data });
    const docMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)$/);
    if (docMatch) {
      if (behavior === "task-output" && docMatch[1] === "task-output-docx") return json({ detail: "Not found" }, 404);
      const next = docs[docMatch[1]];
      return next ? json(next) : json({ detail: "Not found" }, 404);
    }
    if (url.pathname === "/api/v1/comments") {
      return json(behavior === "docx-comments" ? [{
        id: "docx-comment", content: "Review this", parent_id: null,
        anchor: { mode: "docx", quote: "Selectable DOCX fixture" }, status: "open",
      }] : []);
    }
    const previewMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/preview\/content$/);
    if (previewMatch) {
      const target = previewMatch[1];
      if (behavior === "text-delayed" || (behavior === "route-switch" && target === id)) {
        await new Promise<void>((resolve) => releases.set(target, resolve));
      }
      const text = target === "doc-b" ? "# Document B" : target === id ? "# Document A" : "<p>Selectable DOCX fixture</p>";
      return route.fulfill({ contentType: target.startsWith("docx") ? "text/html" : "text/markdown", body: text });
    }
    const pageMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/pages$/);
    if (pageMatch) {
      if (behavior === "docx-manifest-failure") return json({ detail: "Renderer unavailable" }, 503);
      return json({ pages: [{ index: 0, url: `/documents/${pageMatch[1]}/pages/0`, width: 612, height: 792 }], total: 1, version: "fixture" });
    }
    const imageMatch = url.pathname.match(/^\/api\/v1\/documents\/([^/]+)\/pages\/0$/);
    if (imageMatch) {
      if (behavior === "docx-page-failure") return route.fulfill({ status: 503, body: "renderer unavailable" });
      return route.fulfill({ contentType: "image/svg+xml", body: docxPage });
    }
    if (url.pathname.endsWith("/download")) return route.fulfill({ contentType: "application/octet-stream", body: "download fixture" });
    return json({ detail: `Unexpected request: ${url.pathname}` }, 404);
  });
  return { calls, release: (target = id) => releases.get(target)?.() };
}

test("metadata shell remains usable while a normal text preview is pending", async ({ page }) => {
  const api = await installViewerBehaviorApi(page, "text-delayed");
  await page.goto(`/viewer/${id}`);
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
  await expect.poll(() => api.calls.includes(`/api/v1/documents/${id}/preview/content`)).toBe(true);
  await expect(page.locator(".manor-editor-viewer-surface")).toHaveAttribute("aria-busy", "true");
  await expect(page.getByRole("heading", { name: "Document A", exact: true })).toHaveCount(0);
  api.release();
  await expect(page.getByRole("heading", { name: "Document A", exact: true })).toBeVisible();
});

test("DOCX server pages avoid the original preview download while edit and download remain available", async ({ page }) => {
  const api = await installViewerBehaviorApi(page, "docx-server");
  await page.goto("/viewer/docx-server");
  await expect(page.getByRole("img", { name: /docx-server\.docx — page 1/ })).toBeVisible();
  expect(api.calls).toContain("/api/v1/documents/docx-server/pages");
  expect(api.calls).not.toContain("/api/v1/documents/docx-server/preview/content");
  await expect(page.getByRole("link", { name: "Edit", exact: true })).toHaveAttribute("href", "/editor/docx-server");
  await expect(page.getByRole("button", { name: "Download", exact: true })).toBeEnabled();
});

test("DOCX comments request one protected original preview for selectable legacy rendering", async ({ page }) => {
  const api = await installViewerBehaviorApi(page, "docx-comments");
  await page.goto("/viewer/docx-comments");
  await expect(page.getByRole("img", { name: /docx-comments\.docx — page 1/ })).toBeVisible();
  await page.getByRole("button", { name: "Comments", exact: true }).click();
  await expect(page.locator(".docx-viewer-stage").getByText("Selectable DOCX fixture", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Comments", exact: true }).click();
  await page.getByRole("button", { name: "Comments", exact: true }).click();
  await expect(page.locator(".docx-viewer-stage").getByText("Selectable DOCX fixture", { exact: true })).toBeVisible();
  expect(api.calls.filter((call) => call === "/api/v1/documents/docx-comments/preview/content")).toHaveLength(1);
});

test("DOCX manifest and page failures each fall back to the protected original", async ({ page }) => {
  for (const behavior of ["docx-manifest-failure", "docx-page-failure"] as const) {
    const api = await installViewerBehaviorApi(page, behavior);
    const target = behavior.replace("docx-", "");
    await page.goto(`/viewer/docx-${target}`);
    await expect(page.getByText("Selectable DOCX fixture", { exact: true })).toBeVisible();
    expect(api.calls).toContain(`/api/v1/documents/docx-${target}/preview/content`);
    await page.unrouteAll({ behavior: "ignoreErrors" });
  }
});

test("route changes do not reveal a stale preview after the prior request settles", async ({ page }) => {
  const api = await installViewerBehaviorApi(page, "route-switch");
  const failedRequests: string[] = [];
  page.on("requestfailed", (request) => failedRequests.push(new URL(request.url()).pathname));
  await page.goto(`/viewer/${id}`);
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
  await expect.poll(() => api.calls.includes(`/api/v1/documents/${id}/preview/content`)).toBe(true);
  await page.getByRole("link", { name: "Switch fixture document", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Document B", exact: true })).toBeVisible();
  const oldPreviewCalls = api.calls.filter((call) => call === `/api/v1/documents/${id}/preview/content`).length;
  api.release(id);
  await expect.poll(() => failedRequests.includes(`/api/v1/documents/${id}/preview/content`)).toBe(true);
  expect(api.calls.filter((call) => call === `/api/v1/documents/${id}/preview/content`)).toHaveLength(oldPreviewCalls);
  await expect(page.getByRole("heading", { name: "Document A", exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Document B", exact: true })).toBeVisible();
});

test("task-output DOCX renders supplied bytes after metadata failure without durable preview reads", async ({ page }) => {
  const api = await installViewerBehaviorApi(page, "task-output");
  await page.goto(workspace);
  await page.getByRole("link", { name: "Open task output DOCX", exact: true }).click();
  await expect(page.getByText("Task output Word", { exact: true })).toBeVisible();
  expect(api.calls).toEqual(["/api/v1/documents/task-output-docx"]);
});

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
