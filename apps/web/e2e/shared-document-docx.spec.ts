import { expect, test } from "@playwright/test";
import JSZip from "jszip";

test("a view-only public DOCX share renders through Manor's reader", async ({ page }) => {
  const zip = new JSZip();
  zip.file(
    "word/document.xml",
    `<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Shared DOCX content</w:t></w:r></w:p><w:sectPr/></w:body></w:document>`,
  );
  const bytes = await zip.generateAsync({ type: "nodebuffer" });

  await page.route("**/api/v1/shared-doc/public-docx", async (route) => {
    await route.fulfill({
      json: {
        document_id: "doc",
        name: "shared.docx",
        capabilities: ["view"],
        watermark: true,
        allow_download: false,
        file_type: "docx",
        mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size: bytes.byteLength,
      },
    });
  });
  await page.route("**/api/v1/shared-doc/public-docx/content", async (route) => {
    await route.fulfill({
      body: bytes,
      contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
  });

  await page.goto("/shared-doc/public-docx");

  await expect(page.getByText("Shared DOCX content")).toBeVisible();
  await expect(page.locator("[contenteditable=true]")).toHaveCount(0);
});
