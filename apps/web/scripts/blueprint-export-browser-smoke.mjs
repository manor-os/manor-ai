import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";
import { chromium, expect } from "@playwright/test";

// Exercise the real modal, Query cache, and API serializer with bounded pages.
const bundle = await build({
  stdin: {
    contents: `
      import React from "react";
      import { createRoot } from "react-dom/client";
      import { MemoryRouter } from "react-router-dom";
      import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
      import ExportBlueprintModal from "../src/components/blueprints/ExportBlueprintModal.tsx";
      const queryClient = new QueryClient({defaultOptions: {queries: {retry: false}}});
      createRoot(document.getElementById("root")).render(
        <QueryClientProvider client={queryClient}><MemoryRouter>
          <ExportBlueprintModal open onClose={() => {}} workspaceId="review-workspace" workspaceName="Review Workspace" />
        </MemoryRouter></QueryClientProvider>
      );
    `,
    loader: "tsx",
    resolveDir: new URL(".", import.meta.url).pathname,
  },
  bundle: true, format: "iife", platform: "browser", write: false,
  define: { "import.meta.env": JSON.stringify({ DEV: false }) },
  logLevel: "silent",
});
const stylesheet = await readFile(new URL("../src/index.css", import.meta.url), "utf8");
const browser = await chromium.launch({ headless: true });
try {
  for (const viewport of [{ width: 1280, height: 900 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    const errors = [];
    let exported;
    let failNextPage = true;
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("http://blueprint.test/**", async (route) => {
      const url = new URL(route.request().url());
      let response = [];
      if (url.pathname === "/") {
        return route.fulfill({ contentType: "text/html", body: '<html><body><div id="root"></div></body></html>' });
      }
      if (url.pathname.endsWith("/knowledge-documents")) {
        const offset = Number(url.searchParams.get("offset"));
        assert.equal(url.searchParams.get("limit"), "50");
        if (offset === 50 && failNextPage) {
          failNextPage = false;
          return route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"Retry the page"}' });
        }
        response = Array.from({ length: Math.min(50, 75 - offset) }, (_, i) => ({
          id: `document-${offset + i}`, name: `guide-${offset + i}.md`, path: `guide-${offset + i}.md`,
          file_size: 20, groups: [{ id: "group", name: "Knowledge" }], groups_truncated: false,
        }));
      } else if (url.pathname.endsWith("/export-blueprint")) {
        exported = route.request().postDataJSON();
        response = { id: "exported-blueprint" };
      }
      return route.fulfill({ contentType: "application/json", body: JSON.stringify(response) });
    });
    await page.goto("http://blueprint.test/");
    await page.evaluate(() => localStorage.setItem("manor_locale", "en"));
    await page.addStyleTag({ content: stylesheet });
    await page.addScriptTag({ content: bundle.outputFiles[0].text });
    await page.getByRole("checkbox", { name: /^Knowledge starter files/ }).click();
    const picker = page.getByRole("group", { name: "Select Knowledge starter files" });
    await expect(picker.getByRole("checkbox")).toHaveCount(50);
    await expect(picker.getByText("Selected 50 · Maximum 64 files")).toBeVisible();
    await picker.getByRole("button", { name: "Next page", exact: true }).click();
    await expect(picker.getByRole("alert")).toBeVisible();
    await expect(page.getByRole("button", { name: "Create draft", exact: true })).toBeDisabled();
    await picker.getByRole("button", { name: "Retry", exact: true }).click();
    await expect(picker.getByRole("checkbox")).toHaveCount(25);
    await expect(picker.getByText("Selected 50 · Maximum 64 files")).toBeVisible();
    await picker.getByRole("button", { name: "Select page", exact: true }).click();
    await expect(picker.getByText("Selected 64 · Maximum 64 files")).toBeVisible();
    await expect(picker.getByRole("checkbox", { checked: true })).toHaveCount(14);
    await expect(picker.getByRole("checkbox").last()).toBeDisabled();
    await picker.getByRole("button", { name: "Previous page", exact: true }).click();
    await expect(picker.getByRole("checkbox", { checked: true })).toHaveCount(50);
    await picker.getByRole("button", { name: "Clear page", exact: true }).click();
    await expect(picker.getByText("Selected 14 · Maximum 64 files")).toBeVisible();
    await picker.getByRole("button", { name: "Next page", exact: true }).click();
    await expect(picker.getByRole("checkbox", { checked: true })).toHaveCount(14);
    await expect(picker.getByRole("button", { name: "Next page", exact: true })).toBeDisabled();
    await page.getByRole("button", { name: "Create draft", exact: true }).click();
    await expect.poll(() => exported?.knowledge_document_ids?.length).toBe(14);
    assert.deepEqual(exported.knowledge_document_ids, Array.from({ length: 14 }, (_, i) => `document-${i + 50}`));
    assert.deepEqual(errors, []);
    await page.close();
    console.log(`Blueprint export pagination passed: ${viewport.width}px`);
  }
} finally {
  await browser.close();
}
