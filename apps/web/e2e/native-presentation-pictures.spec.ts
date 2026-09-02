import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativePictureTemplate(): Buffer {
  const script = String.raw`
import base64, hashlib, io
from PIL import Image
from tests.test_office_template_generation import template_bytes
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

image = io.BytesIO()
Image.new("RGB", (400, 200), "#d94f45").save(image, "PNG")
data = image.getvalue()
source = {"path": "Assets/hero.png", "expected_sha256": hashlib.sha256(data).hexdigest()}
operations = [normalize_file_patch_operation(operation) for operation in [
    {"op": "picture.insert", "slide": 1, "source": source,
     "transform": {"x": 72, "y": 280, "width": 576, "height": 190, "rotation": 3},
     "fit": "cover", "format": {"opacity": 0.55, "alt_text": "Product dashboard"}},
    {"op": "shape.transform", "slide": 1, "shape_id": 4,
     "transform": {"flip_horizontal": True}},
]]
result = _generate_office_operations_sync(
    "pptx", operations, template_bytes=template_bytes("pptx"),
    resources={(source["path"], source["expected_sha256"]): data},
)
assert result.get("patched"), result
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

const replacementPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mNkYPj/n4GBgYGJAQoAHgQCAZV+9a0AAAAASUVORK5CYII=",
  "base64",
);

test("native generated picture stays accessible and editable through browser save, reload and presentation", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const api = await pwRequest.newContext({ baseURL: API });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let documentId = "";
  let token = "";
  try {
    const login = await api.post("/api/v1/auth/login", {
      data: { email: "demo@manor.local", password: "manor-demo" },
    });
    expect(login.ok()).toBeTruthy();
    token = (await login.json()).access_token;
    const headers = { Authorization: `Bearer ${token}` };
    const original = nativePictureTemplate();
    const before = await JSZip.loadAsync(original);
    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-picture-${Date.now()}.pptx`,
        mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        buffer: original,
      } },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const reject = page.getByRole("button", { name: "Reject all", exact: true });
    if (await reject.isVisible()) await reject.click();
    const frame = page.locator(".presentation-editor-slide-frame");
    const image = frame.getByRole("img", { name: "Product dashboard", exact: true });
    await expect(image).toBeVisible();
    await expect(image).toHaveCSS("opacity", "0.55");
    const imageShape = frame.locator('[data-presentation-shape-type="image"]');
    await expect(imageShape).toBeVisible();
    await imageShape.click();
    await page.getByRole("button", { name: "Format options", exact: true }).click();
    const panel = page.locator(".presentation-editor-format-panel");
    await expect(panel).toBeVisible();
    await panel.locator('input[type="range"]').fill("80");
    await panel.locator("select").selectOption("contain");
    const chooserPromise = page.waitForEvent("filechooser");
    await panel.getByRole("button", { name: "Replace image", exact: true }).click();
    const chooser = await chooserPromise;
    await chooser.setFiles({ name: "replacement.png", mimeType: "image/png", buffer: replacementPng });
    await expect(image).toHaveCSS("opacity", "0.8");
    await page.screenshot({ path: testInfo.outputPath("native-picture-desktop.png"), animations: "disabled" });

    await page.getByRole("button", { name: "Save", exact: true }).click();
    const part = "ppt/slides/slide1.xml";
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      return (await JSZip.loadAsync(await download.body())).file(part)!.async("string");
    }).toContain('amt="80000"');

    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    const after = await JSZip.loadAsync(await download.body());
    const xml = await after.file(part)!.async("string");
    expect(xml).toContain('descr="Product dashboard"');
    expect(xml).toContain('flipH="1"');
    expect(xml).not.toContain("<a:srcRect");
    for (const name of Object.keys(before.files).filter((entry) => (
      !before.files[entry].dir
      && ["ppt/charts/", "ppt/embeddings/", "ppt/slideMasters/", "ppt/notesSlides/"].some((prefix) => entry.startsWith(prefix))
    ))) {
      expect(await after.file(name)!.async("base64"), name).toBe(await before.file(name)!.async("base64"));
    }

    await page.reload();
    const reloaded = frame.getByRole("img", { name: "Product dashboard", exact: true });
    await expect(reloaded).toBeVisible();
    await expect(reloaded).toHaveCSS("opacity", "0.8");
    await page.getByRole("button", { name: "Present", exact: true }).click();
    const present = page.locator(".presentation-editor-present-slide");
    await expect(present.getByRole("img", { name: "Product dashboard", exact: true })).toBeVisible();
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(reloaded).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-picture-mobile.png"), animations: "disabled" });
    expect(errors).toEqual([]);
  } finally {
    if (documentId && token) {
      const cleanup = await api.post(`/api/v1/documents/${documentId}/trash`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(cleanup.ok()).toBeTruthy();
    }
    await api.dispose();
  }
});
