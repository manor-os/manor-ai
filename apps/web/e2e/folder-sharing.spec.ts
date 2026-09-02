import { expect, test } from "@playwright/test";

// Real API + browser checks; deliberately require a disposable backend.
test.skip(!process.env.E2E_SHARING_ISOLATED, "Set E2E_SHARING_ISOLATED only for a disposable backend");

for (const width of [1280, 390]) {
  for (const allowDownload of [false, true]) {
    test(`folder share navigation and ${allowDownload ? "download" : "view-only"} at ${width}px`, async ({ page, request }, testInfo) => {
      const suffix = `${Date.now()}-${width}-${allowDownload}`;
      const registration = await request.post("/api/v1/auth/register", { data: {
        username: `sharing-${suffix}`, email: `sharing-${suffix}@example.test`,
        password: "local-disposable-test-only", entity_name: "Sharing E2E",
      } });
      expect(registration.ok()).toBe(true);
      const headers = { Authorization: `Bearer ${(await registration.json()).access_token}` };
      const createFolder = async (name: string, parent_id?: string) => {
        const response = await request.post("/api/v1/documents/folders", { headers, data: { name, parent_id } });
        expect(response.status()).toBe(201);
        return response.json();
      };
      const root = await createFolder("Shared root");
      const child = await createFolder("Nested documents", root.id);
      const uploaded = await request.post(`/api/v1/documents/upload?folder_id=${child.id}&visibility=private`, {
        headers, multipart: { file: { name: "shared-note.md", mimeType: "text/markdown", buffer: Buffer.from("# Shared preview\n\nFolder content is readable.") } },
      });
      expect(uploaded.status()).toBe(201);
      const document = await uploaded.json();
      const created = await request.post(`/api/v1/folders/${root.id}/shares`, { headers, data: {
        audience_type: "anonymous", capabilities: allowDownload ? ["view", "download"] : ["view"],
        allow_download: allowDownload,
      } });
      expect(created.status()).toBe(201);
      const share = await created.json();
      await page.addInitScript(() => localStorage.setItem("manor_locale", "en"));
      await page.setViewportSize({ width, height: 900 });
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto(`/shared-folder/${share.token}`);
      await page.getByRole("button", { name: "Nested documents", exact: true }).click();
      const file = page.getByRole("button", { name: "shared-note.md", exact: true });
      await file.focus();
      await file.press("Enter");
      const dialog = page.getByRole("dialog", { name: "shared-note.md" });
      await expect(dialog).toContainText("Folder content is readable.");
      await expect(dialog.locator("a[download]")).toHaveCount(allowDownload ? 1 : 0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      await page.screenshot({ path: testInfo.outputPath(`folder-${width}-${allowDownload}.png`) });
      if (allowDownload) {
        const downloaded = page.waitForEvent("download");
        await dialog.locator("a[download]").click();
        expect((await downloaded).suggestedFilename()).toBe("shared-note.md");
      }
      const downloadCheck = await request.get(`/api/v1/shared-folder/${share.token}/documents/${document.id}/download`);
      expect(downloadCheck.status()).toBe(allowDownload ? 200 : 403);
      await page.keyboard.press("Escape");
      await expect(file).toBeFocused();
      await page.getByRole("button", { name: "Go Back", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Shared root", exact: true })).toBeVisible();
      await page.getByRole("button", { name: "Nested documents", exact: true }).click();
      await expect(page.getByRole("button", { name: "shared-note.md", exact: true })).toBeVisible();
      expect((await request.delete(`/api/v1/folders/${root.id}/shares/${share.id}`, { headers })).status()).toBe(204);
      await page.getByRole("button", { name: "shared-note.md", exact: true }).click();
      await expect(page.getByRole("dialog")).not.toContainText("Folder content is readable.");
      expect((await request.get(`/api/v1/shared-folder/${share.token}/documents/${document.id}/content`)).status()).toBe(404);
      expect(errors).toEqual([]);
    });
  }
}
