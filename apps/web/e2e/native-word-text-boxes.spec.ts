import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeWordTextBox(): Buffer {
  const script = String.raw`
import base64
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

operations = [
    {"op": "paragraph.insert", "index": 0, "text": "Editable body paragraph"},
    {"op": "textbox.insert", "index": 1, "text": "Editable callout", "preset": "roundRect",
     "transform": {"x": 72, "y": 42, "width": 320, "height": 108, "rotation": 4},
     "format": {"fill_color": "EAF5F0", "line_color": "174C46", "line_width": 2,
                "margin_left": 12, "margin_right": 12, "margin_top": 8, "margin_bottom": 8,
                "vertical_alignment": "middle", "alt_text": "Native editable callout"}},
    {"op": "paragraph.insert", "text_box_index": 0, "index": 1, "text": "Preserved callout detail"},
    {"op": "paragraph.format", "text_box_index": 0, "index": 0,
     "format": {"bold": True, "font_size": 20, "font_color": "174C46", "alignment": "center"}},
]
result = _generate_office_operations_sync(
    "docx", [normalize_file_patch_operation(operation) for operation in operations],
)
if result.get("error"):
    raise RuntimeError(result["error"])
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

function groupedNativeWordTextBox(): Buffer {
  const script = String.raw`
import base64
import io
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
from tests.test_word_text_box_operations import grouped_text_box_template

template_document = Document(io.BytesIO(grouped_text_box_template()))
resource_paragraph = template_document.add_paragraph()
resource_link = OxmlElement("w:hyperlink")
resource_link.set(qn("r:id"), template_document.part.relate_to(
    "https://example.com/body-resource", RT.HYPERLINK, is_external=True,
))
resource_run = OxmlElement("w:r")
resource_text = OxmlElement("w:t")
resource_text.text = "Body resource link"
resource_run.append(resource_text)
resource_link.append(resource_run)
resource_paragraph._p.append(resource_link)
template_buffer = io.BytesIO()
template_document.save(template_buffer)
operations = [normalize_file_patch_operation(operation) for operation in [
    {"op": "shape.delete", "text_box_index": 0},
    {"op": "shape.transform", "text_box_index": 0,
     "transform": {"x": 150, "y": 12, "width": 132, "height": 84, "rotation": 9}},
]]
result = _generate_office_operations_sync(
    "docx", operations, template_bytes=template_buffer.getvalue(),
)
if result.get("error"):
    raise RuntimeError(result["error"])
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

function textBoxShapeShell(documentXml: string): string {
  const drawing = documentXml.match(/<w:drawing[\s\S]*?<\/w:drawing>/)?.[0] ?? "";
  return drawing.replace(/<w:txbxContent\b[\s\S]*?<\/w:txbxContent>/, "<w:txbxContent/>");
}

test("native Word text box renders, edits and saves without rebuilding its shape", async ({ page }, testInfo) => {
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
    const original = nativeWordTextBox();
    const before = await JSZip.loadAsync(original);
    const beforeDocumentXml = await before.file("word/document.xml")!.async("string");

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-word-text-box-${Date.now()}.docx`,
        mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        buffer: original,
      } },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    await page.getByRole("button", { name: "Reject all", exact: true }).click({ timeout: 5_000 }).catch(() => undefined);

    const workspace = page.locator(".richtext-editor-workspace");
    const textBox = workspace.locator('.manor-docx-text-box[data-docx-text-box-index="0"]');
    const callout = textBox.locator('[data-docx-text-box-paragraph-index="0"]');
    await expect(textBox).toBeVisible();
    await expect(textBox).toHaveAttribute("aria-label", "Native editable callout");
    await expect(textBox).toHaveCSS("position", "absolute");
    await expect(textBox).toHaveCSS("width", "426.656px");
    await expect(textBox).toHaveCSS("height", "144px");
    await expect(textBox).toHaveCSS("background-color", "rgb(234, 245, 240)");
    await expect(textBox).toHaveCSS("justify-content", "center");
    await expect(callout).toHaveText("Editable callout");
    await expect(textBox.getByText("Preserved callout detail", { exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-word-text-box.png"), animations: "disabled" });

    await callout.fill("Browser edited callout");
    await workspace.getByText("Editable body paragraph", { exact: true }).fill("Browser edited body paragraph");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const zip = await JSZip.loadAsync(await download.body());
      return zip.file("word/document.xml")?.async("string") ?? "";
    }).toContain("Browser edited callout");

    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    expect(download.ok()).toBeTruthy();
    const after = await JSZip.loadAsync(await download.body());
    const afterDocumentXml = await after.file("word/document.xml")!.async("string");
    expect(afterDocumentXml).toContain("Browser edited body paragraph");
    expect(afterDocumentXml).toContain("Browser edited callout");
    expect(afterDocumentXml).toContain("Preserved callout detail");
    expect(textBoxShapeShell(afterDocumentXml)).toBe(textBoxShapeShell(beforeDocumentXml));

    await page.reload();
    await expect(workspace.getByText("Browser edited body paragraph", { exact: true })).toBeVisible();
    await expect(textBox.getByText("Browser edited callout", { exact: true })).toBeVisible();
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

test("grouped Word member keeps local geometry and its native group through browser save", async ({ page }, testInfo) => {
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
    const original = groupedNativeWordTextBox();
    const before = await JSZip.loadAsync(original);
    const beforeDocumentXml = await before.file("word/document.xml")!.async("string");

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-word-grouped-text-box-${Date.now()}.docx`,
        mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        buffer: original,
      } },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    await page.getByRole("button", { name: "Reject all", exact: true }).click({ timeout: 5_000 }).catch(() => undefined);

    const workspace = page.locator(".richtext-editor-workspace");
    const textBoxes = workspace.locator(".manor-docx-text-box");
    const grouped = workspace.locator('.manor-docx-text-box[data-docx-text-box-index="0"]');
    const paragraph = grouped.locator('[data-docx-text-box-paragraph-index="0"]');
    await expect(textBoxes).toHaveCount(1);
    await expect(grouped).toBeVisible();
    await expect(paragraph).toHaveText("Grouped second");
    await expect(grouped).toHaveCSS("left", "296px");
    await expect(grouped).toHaveCSS("top", "64px");
    await expect(grouped).toHaveCSS("width", "176px");
    await expect(grouped).toHaveCSS("height", "112px");
    await expect(grouped).toHaveAttribute("style", /transform:rotate\(9deg\)/);
    await page.screenshot({ path: testInfo.outputPath("native-word-grouped-text-box.png"), animations: "disabled" });

    const bodyResource = workspace.getByRole("link", { name: "Body resource link", exact: true });
    const bodyParagraph = bodyResource.locator("xpath=ancestor::*[@data-docx-paragraph-index][1]");
    await expect.poll(() => bodyParagraph.evaluate((element) => (element as HTMLElement).isContentEditable)).toBe(true);
    await bodyParagraph.fill("Browser replaced body resource");
    await paragraph.fill("Browser edited grouped member");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const zip = await JSZip.loadAsync(await download.body());
      return zip.file("word/document.xml")?.async("string") ?? "";
    }).toContain("Browser edited grouped member");

    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    expect(download.ok()).toBeTruthy();
    const after = await JSZip.loadAsync(await download.body());
    const afterDocumentXml = await after.file("word/document.xml")!.async("string");
    expect(afterDocumentXml.split("<wpg:wgp")).toHaveLength(2);
    expect(afterDocumentXml.split("<wps:wsp")).toHaveLength(2);
    expect(afterDocumentXml).toContain("Browser replaced body resource");
    expect(afterDocumentXml).not.toContain("Body resource link");
    expect(afterDocumentXml).toContain("Browser edited grouped member");
    expect(afterDocumentXml).not.toContain("Grouped first");
    expect(textBoxShapeShell(afterDocumentXml)).toBe(textBoxShapeShell(beforeDocumentXml));

    await page.reload();
    await expect(workspace.getByText("Browser replaced body resource", { exact: true })).toBeVisible();
    await expect(grouped.getByText("Browser edited grouped member", { exact: true })).toBeVisible();
    await expect(grouped).toHaveCSS("left", "296px");
    await expect(grouped).toHaveCSS("width", "176px");
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
