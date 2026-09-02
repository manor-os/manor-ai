import { expect, test } from "@playwright/test";
import * as XLSX from "xlsx";

const transparentPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9J3wsAAAAASUVORK5CYII=",
  "base64",
);

test("a view-only public PPTX share renders token-scoped slides", async ({ page }) => {
  await page.route("**/api/v1/shared-doc/public-pptx", async (route) => {
    await route.fulfill({
      json: {
        document_id: "doc",
        name: "shared presentation.pptx",
        capabilities: ["view"],
        watermark: true,
        allow_download: false,
        file_type: "pptx",
        mime_type: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        file_size: 1024,
      },
    });
  });
  await page.route("**/api/v1/shared-doc/public-pptx/preview/slides", async (route) => {
    await route.fulfill({
      json: {
        slides: [
          {
            index: 0,
            url: "/api/v1/shared-doc/public-pptx/preview/slides/0?version=fixture",
            width: 1280,
            height: 720,
            total: 1,
            version: "fixture",
          },
        ],
      },
    });
  });
  await page.route("**/api/v1/shared-doc/public-pptx/preview/slides/0**", async (route) => {
    await route.fulfill({ body: transparentPng, contentType: "image/png" });
  });

  await page.goto("/shared-doc/public-pptx");

  await expect(page.getByRole("img", { name: "shared presentation.pptx, slide 1" })).toBeVisible();
  await expect(page.locator("[contenteditable=true]")).toHaveCount(0);
});

test("a view-only public DOCX share renders token-scoped pages", async ({ page }) => {
  await page.route("**/api/v1/shared-doc/public-docx-pages", async (route) => {
    await route.fulfill({
      json: {
        document_id: "doc",
        name: "shared contract.docx",
        capabilities: ["view"],
        watermark: true,
        allow_download: false,
        file_type: "docx",
        mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      },
    });
  });
  await page.route("**/api/v1/shared-doc/public-docx-pages/preview/pages", async (route) => {
    await route.fulfill({
      json: {
        pages: [{
          index: 0,
          url: "/api/v1/shared-doc/public-docx-pages/preview/pages/0?version=fixture",
          width: 1280,
          height: 720,
        }],
      },
    });
  });
  await page.route("**/api/v1/shared-doc/public-docx-pages/preview/pages/0**", async (route) => {
    await route.fulfill({ body: transparentPng, contentType: "image/png" });
  });

  await page.goto("/shared-doc/public-docx-pages");

  await expect(page.getByRole("img", { name: "shared contract.docx, page 1" })).toBeVisible();
  await expect(page.locator("[contenteditable=true]")).toHaveCount(0);
});

async function mockTextShare(
  page: import("@playwright/test").Page,
  token: string,
  name: string,
  fileType: string,
  mimeType: string,
  content: string,
) {
  await page.route(`**/api/v1/shared-doc/${token}`, async (route) => {
    await route.fulfill({
      json: {
        document_id: "doc",
        name,
        capabilities: ["view"],
        watermark: false,
        allow_download: false,
        file_type: fileType,
        mime_type: mimeType,
        file_size: content.length,
      },
    });
  });
  await page.route(`**/api/v1/shared-doc/${token}/content`, async (route) => {
    await route.fulfill({ body: content, contentType: mimeType });
  });
}

async function mockBinaryShare(
  page: import("@playwright/test").Page,
  token: string,
  name: string,
  fileType: string,
  mimeType: string,
  content: Buffer,
) {
  await page.route(`**/api/v1/shared-doc/${token}`, async (route) => {
    await route.fulfill({
      json: {
        document_id: "doc",
        name,
        capabilities: ["view"],
        watermark: false,
        allow_download: false,
        file_type: fileType,
        mime_type: mimeType,
        file_size: content.byteLength,
      },
    });
  });
  await page.route(`**/api/v1/shared-doc/${token}/content`, async (route) => {
    await route.fulfill({ body: content, contentType: mimeType });
  });
}

test("public text, code, JSON, CSV, Markdown, and HTML shares use read-only previews", async ({ page }) => {
  await mockTextShare(page, "public-code", "snippet.ts", "ts", "text/plain", "const publicPreview = true;");
  await page.goto("/shared-doc/public-code");
  await expect(page.getByText("const publicPreview = true;")).toBeVisible();

  await mockTextShare(page, "public-json", "payload.json", "json", "application/json", '{"shared":true}');
  await page.goto("/shared-doc/public-json");
  await expect(page.getByText('"shared": true')).toBeVisible();

  await mockTextShare(page, "public-csv", "report.csv", "csv", "text/csv", "Name,Score\nAda,10");
  await page.goto("/shared-doc/public-csv");
  await expect(page.getByRole("columnheader", { name: "Name" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Ada" })).toBeVisible();

  await mockTextShare(page, "public-markdown", "notes.md", "md", "text/markdown", "# Shared heading");
  await page.goto("/shared-doc/public-markdown");
  await expect(page.getByRole("heading", { name: "Shared heading" })).toBeVisible();

  await mockTextShare(page, "public-html", "preview.html", "html", "text/html", "<h1>Shared HTML</h1>");
  await page.goto("/shared-doc/public-html");
  const htmlFrame = page.locator('iframe[title="preview.html"][sandbox]');
  await expect(htmlFrame).toBeVisible();
  await expect(htmlFrame.contentFrame().getByRole("heading", { name: "Shared HTML" })).toBeVisible();

  await expect(page.locator("[contenteditable=true]")).toHaveCount(0);
});

test("public spreadsheet, PDF, image, diagram, and media shares use read-only previews", async ({ page }) => {
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(
    workbook,
    XLSX.utils.aoa_to_sheet([["Name", "Score"], ["Ada", 10]]),
    "Scores",
  );
  const workbookBytes = XLSX.write(workbook, { type: "buffer", bookType: "xlsx" }) as Buffer;
  await mockBinaryShare(
    page,
    "public-xlsx",
    "scores.xlsx",
    "xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    workbookBytes,
  );
  await page.goto("/shared-doc/public-xlsx");
  await expect(page.getByText("Ada")).toBeVisible();

  await mockBinaryShare(page, "public-pdf", "report.pdf", "pdf", "application/pdf", Buffer.from("%PDF-1.4\n%%EOF"));
  await page.goto("/shared-doc/public-pdf");
  await expect(page.locator('iframe[title="report.pdf"]')).toBeVisible();

  await mockBinaryShare(page, "public-image", "cover.png", "png", "image/png", transparentPng);
  await page.goto("/shared-doc/public-image");
  await expect(page.getByRole("img", { name: "cover.png" })).toBeVisible();

  await mockTextShare(page, "public-diagram", "flow.mmd", "mmd", "text/plain", "flowchart LR\nA --> B");
  await page.goto("/shared-doc/public-diagram");
  await expect(page.locator("svg").first()).toBeVisible();

  await mockBinaryShare(page, "public-audio", "clip.mp3", "mp3", "audio/mpeg", Buffer.from([0]));
  await page.goto("/shared-doc/public-audio");
  await expect(page.locator("audio")).toBeVisible();

  await mockBinaryShare(page, "public-video", "clip.mp4", "mp4", "video/mp4", Buffer.from([0]));
  await page.goto("/shared-doc/public-video");
  await expect(page.locator("video")).toBeVisible();
  await expect(page.locator("[contenteditable=true]")).toHaveCount(0);
});

test("unsupported public archives stay unavailable without a download control", async ({ page }) => {
  await page.route("**/api/v1/shared-doc/public-archive", async (route) => {
    await route.fulfill({
      json: {
        document_id: "doc",
        name: "archive.zip",
        capabilities: ["view"],
        watermark: false,
        allow_download: false,
        file_type: "zip",
        mime_type: "application/zip",
      },
    });
  });

  await page.goto("/shared-doc/public-archive");

  await expect(page.getByText("This file type can't be previewed in the browser. Contact the share owner for access.")).toBeVisible();
  await expect(page.locator("a[download]")).toHaveCount(0);
  await expect(page.locator("[contenteditable=true]")).toHaveCount(0);
});
