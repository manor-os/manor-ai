import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeWordPicture(): Buffer {
  const script = String.raw`
import base64, hashlib, io, tempfile
from pathlib import Path
from PIL import Image
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

def image(color, size, format):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format)
    return output.getvalue()

red = image("#D94F45", (400, 200), "PNG")
blue = image("#3976C5", (160, 320), "JPEG")
green = image("#2E8B57", (240, 120), "PNG")
first = {"path": "Assets/initial.png", "expected_sha256": hashlib.sha256(red).hexdigest()}
second = {"path": "Assets/replacement.jpg", "expected_sha256": hashlib.sha256(blue).hexdigest()}
brand = {"path": "Assets/brand.png", "expected_sha256": hashlib.sha256(green).hexdigest()}
normalize = normalize_file_patch_operation
created = _generate_office_operations_sync("docx", [normalize(operation) for operation in [
    {"op": "paragraph.insert", "index": 0, "text": "Editable proposal paragraph"},
    {"op": "picture.insert", "index": 0, "source": first, "transform": {"width": 240},
     "format": {"alt_text": "Initial illustration", "alignment": "center"}},
    {"op": "paragraph.insert", "story": "header", "section_index": 0, "index": 0,
     "text": "Native header title"},
    {"op": "picture.insert", "story": "header", "section_index": 0, "index": 0,
     "source": brand, "transform": {"width": 72},
     "format": {"layout": "floating", "position_x": 450, "position_y": 18,
                "relative_from_horizontal": "page", "relative_from_vertical": "page",
                "wrap": "none", "behind_text": False, "alt_text": "Header floating brand"}},
    {"op": "table.insert", "story": "footer", "section_index": 0, "index": 0,
     "rows": [["Review", "Draft"]], "style": "Table Grid"},
]], resources={
    (first["path"], first["expected_sha256"]): red,
    (brand["path"], brand["expected_sha256"]): green,
})
assert created.get("patched"), created
with tempfile.TemporaryDirectory(prefix="manor-word-picture-browser-") as directory:
    path = Path(directory) / "picture.docx"
    path.write_bytes(created["_persisted_bytes"])
    patched = _apply_office_patch_sequence_sync(str(path), [normalize(operation) for operation in [
        {"op": "picture.replace", "picture_index": 0, "source": second},
        {"op": "picture.format", "picture_index": 0,
         "format": {"height": 90, "alt_text": "Updated illustration", "alignment": "right"}},
        {"op": "text.set", "story": "header", "section_index": 0, "index": 1,
         "text": "Updated header title"},
        {"op": "paragraph.delete", "story": "header", "section_index": 0, "index": 2},
        {"op": "cell.set", "story": "footer", "section_index": 0,
         "table_index": 0, "cell": "B1", "value": "Approved"},
    ]], resources={(second["path"], second["expected_sha256"]): blue})
    assert patched.get("patched"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

function drawingXml(documentXml: string): string {
  return documentXml.match(/<w:drawing[\s\S]*?<\/w:drawing>/)?.[0] ?? "";
}

test("native generated and patched Word picture stays intact through browser editing and save", async ({ page }, testInfo) => {
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
    const original = nativeWordPicture();
    const before = await JSZip.loadAsync(original);
    const beforeDocumentXml = await before.file("word/document.xml")!.async("string");
    const beforeRelationships = await before.file("word/_rels/document.xml.rels")!.async("string");
    const beforeHeaderXml = await before.file("word/header1.xml")!.async("string");
    const beforeHeaderRelationships = await before.file("word/_rels/header1.xml.rels")!.async("string");
    const beforeFooterXml = await before.file("word/footer1.xml")!.async("string");
    const mediaParts = Object.keys(before.files).filter((name) => /^word\/media\/[^/]+$/.test(name));
    expect(mediaParts).toHaveLength(2);

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-word-picture-${Date.now()}.docx`,
        mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        buffer: original,
      } },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const reject = page.getByRole("button", { name: "Reject all", exact: true });
    await reject.click({ timeout: 5_000 }).catch(() => undefined);

    const editor = page.locator('.richtext-editor-workspace [contenteditable="true"]');
    const workspace = page.locator(".richtext-editor-workspace");
    const image = editor.getByRole("img", { name: "Updated illustration", exact: true });
    const headerImages = workspace.getByRole("img", { name: "Header floating brand", exact: true });
    const headerImage = headerImages.first();
    await expect(image).toBeVisible();
    await expect(image).toHaveCSS("width", "240px");
    await expect(image).toHaveCSS("height", "120px");
    await expect(image.locator("xpath=ancestor::p[1]")).toHaveCSS("text-align", "right");
    await expect(headerImage).toBeVisible();
    expect(await headerImages.count()).toBeGreaterThanOrEqual(1);
    await expect(headerImage).toHaveClass(/manor-docx-floating-picture/);
    await expect(headerImage).toHaveCSS("position", "absolute");
    await expect(headerImage).toHaveCSS("width", "96px");
    await expect(headerImage.locator("xpath=parent::*")).toHaveClass(/manor-docx-page/);
    await expect(workspace.getByText("Updated header title", { exact: true }).first()).toBeVisible();
    await expect(workspace.getByText("Approved", { exact: true }).first()).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-word-picture-desktop.png"), animations: "disabled" });

    const paragraph = editor.getByText("Editable proposal paragraph", { exact: true });
    await paragraph.fill("Browser edited proposal paragraph");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const zip = await JSZip.loadAsync(await download.body());
      return zip.file("word/document.xml")?.async("string") ?? "";
    }).toContain("Browser edited proposal paragraph");

    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    expect(download.ok()).toBeTruthy();
    const after = await JSZip.loadAsync(await download.body());
    const afterDocumentXml = await after.file("word/document.xml")!.async("string");
    expect(drawingXml(afterDocumentXml)).toBe(drawingXml(beforeDocumentXml));
    expect(afterDocumentXml).toContain('descr="Updated illustration"');
    expect(afterDocumentXml).toContain('cx="2286000" cy="1143000"');
    expect(afterDocumentXml).not.toContain("Editable proposal paragraph");
    expect(await after.file("word/_rels/document.xml.rels")!.async("string")).toBe(beforeRelationships);
    expect(await after.file("word/header1.xml")!.async("string")).toBe(beforeHeaderXml);
    expect(await after.file("word/_rels/header1.xml.rels")!.async("string")).toBe(beforeHeaderRelationships);
    expect(await after.file("word/footer1.xml")!.async("string")).toBe(beforeFooterXml);
    for (const name of mediaParts) {
      expect(await after.file(name)!.async("base64"), name).toBe(await before.file(name)!.async("base64"));
    }

    await page.reload();
    await expect(editor.getByText("Browser edited proposal paragraph", { exact: true })).toBeVisible();
    await expect(editor.getByRole("img", { name: "Updated illustration", exact: true })).toBeVisible();
    await expect(workspace.getByRole("img", { name: "Header floating brand", exact: true }).first()).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    await image.scrollIntoViewIfNeeded();
    await expect(image).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-word-picture-mobile.png"), animations: "disabled" });
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
