import { expect, request as pwRequest, test } from "@playwright/test";
import PptxGenJS from "pptxgenjs";

const API = process.env.E2E_API ?? "http://localhost:8000";
const RUN_DOCKER_E2E = process.env.E2E_DOCKER_PPTX_VIEWER === "1";
const PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
const PIXEL_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

test.skip(!RUN_DOCKER_E2E, "Set E2E_DOCKER_PPTX_VIEWER=1 for the live PPTX viewer test");

test("PPTX CSS fallback thumbnails preserve 4:3 content and virtualize distant slides", async ({ page }) => {
  test.setTimeout(120_000);
  const api = await pwRequest.newContext({ baseURL: API });
  let token = "";
  let documentId = "";

  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok(), `login failed: ${login.status()}`).toBeTruthy();
    token = (await login.json()).access_token;

    const presentation = new PptxGenJS();
    presentation.defineLayout({ name: "E2E_4_3", width: 10, height: 7.5 });
    presentation.layout = "E2E_4_3";
    for (let slideIndex = 0; slideIndex < 36; slideIndex += 1) {
      const slide = presentation.addSlide();
      slide.background = { color: slideIndex % 2 === 0 ? "FFFDF8" : "F0F5F3" };
      slide.addText(`Slide ${slideIndex + 1}`, {
        x: 0.7,
        y: 0.55,
        w: 5.6,
        h: 0.8,
        fontSize: 30,
        bold: true,
        color: "17332F",
        margin: 18,
      });
      slide.addText("Scaled body copy stays inside the thumbnail.", {
        x: 0.9,
        y: 1.75,
        w: 5.5,
        h: 1.2,
        fontSize: 18,
        color: "49645F",
        margin: 12,
      });
      slide.addImage({ data: PIXEL_PNG, x: 6.8, y: 4.8, w: 2.1, h: 1.5 });
    }
    const output = await presentation.write({ outputType: "arraybuffer", compression: true });
    const upload = await api.post("/api/v1/documents/upload", {
      headers: { Authorization: `Bearer ${token}` },
      multipart: {
        file: {
          name: `pptx-fallback-thumbnails-${Date.now()}.pptx`,
          mimeType: PPTX_MIME,
          buffer: Buffer.from(output as ArrayBuffer),
        },
      },
    });
    expect(upload.ok(), `presentation upload failed: ${upload.status()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.route(`**/api/v1/documents/${documentId}/slides`, async (route) => {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "E2E forces the browser CSS fallback" }),
      });
    });
    await page.addInitScript((accessToken) => {
      window.localStorage.setItem("manor_token", accessToken as string);
    }, token);
    await page.setViewportSize({ width: 1180, height: 860 });
    await page.goto(`/viewer/${documentId}`);

    await expect(page.locator(".pptx-document-css-slide")).toBeVisible();
    const thumbnails = page.locator(".pptx-document-thumbnail");
    await expect(thumbnails).toHaveCount(36);

    const firstThumbnail = thumbnails.first();
    const firstPreview = firstThumbnail.locator(".pptx-document-thumbnail-preview");
    await expect(firstPreview).toBeVisible();
    await expect(firstPreview.locator("img")).toHaveCount(1);

    const geometry = await firstPreview.evaluate((element) => {
      const preview = element as HTMLElement;
      const box = preview.getBoundingClientRect();
      const firstShape = preview.querySelector(":scope > div") as HTMLElement | null;
      const firstParagraph = preview.querySelector("p") as HTMLElement | null;
      return {
        aspect: box.width / box.height,
        fontSize: firstParagraph ? Number.parseFloat(getComputedStyle(firstParagraph).fontSize) : 0,
        paddingLeft: firstShape ? Number.parseFloat(getComputedStyle(firstShape).paddingLeft) : 0,
      };
    });
    expect(geometry.aspect).toBeCloseTo(4 / 3, 1);
    expect(geometry.fontSize).toBeGreaterThan(1);
    expect(geometry.fontSize).toBeLessThan(6);
    expect(geometry.paddingLeft).toBeGreaterThan(0);
    expect(geometry.paddingLeft).toBeLessThan(4);

    const mountedPreviews = page.locator(".pptx-document-thumbnail-preview");
    await expect.poll(() => mountedPreviews.count()).toBeLessThan(36);
    expect(await mountedPreviews.count()).toBeGreaterThan(0);

    const lastThumbnail = thumbnails.last();
    await lastThumbnail.click();
    await expect(lastThumbnail).toHaveAttribute("aria-selected", "true");
    await expect(lastThumbnail.locator(".pptx-document-thumbnail-preview")).toHaveCount(1);
    await expect.poll(() => firstThumbnail.locator(".pptx-document-thumbnail-preview").count()).toBe(0);
    await expect.poll(() => mountedPreviews.count()).toBeLessThan(36);
  } finally {
    if (token && documentId) {
      await api.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
    }
    await api.dispose();
  }
});
