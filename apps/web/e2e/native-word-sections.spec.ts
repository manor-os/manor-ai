import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function multiSectionWord(): Buffer {
  const script = String.raw`
import base64
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

operations = [
    {"op": "page.setup", "section_index": 0, "format": {
        "width": 612, "height": 792,
        "margin_top": 72, "margin_bottom": 72, "margin_left": 72, "margin_right": 72,
    }},
    {"op": "paragraph.insert", "index": 0, "text": "First section body"},
    {"op": "text.set", "story": "header", "section_index": 0, "index": 0, "text": "SECTION ONE"},
    {"op": "text.set", "story": "footer", "section_index": 0, "index": 0, "text": "FIRST FOOTER"},
    {"op": "section.insert", "index": 1, "start_type": "new_page", "inherit_headers_footers": False,
     "format": {
         "width": 792, "height": 612,
         "margin_top": 36, "margin_bottom": 36, "margin_left": 90, "margin_right": 90,
     }},
    {"op": "paragraph.insert", "index": 2, "text": "Second section body"},
    {"op": "text.set", "story": "header", "section_index": 1, "index": 0, "text": "SECTION TWO"},
    {"op": "text.set", "story": "footer", "section_index": 1, "index": 0, "text": "SECOND FOOTER"},
]
result = _generate_office_operations_sync(
    "docx",
    [normalize_file_patch_operation(operation) for operation in operations],
)
if result.get("error"):
    raise RuntimeError(result["error"])
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

function sectionXml(documentXml: string): string[] {
  return Array.from(documentXml.matchAll(/<w:sectPr\b[\s\S]*?<\/w:sectPr>/g), (match) => match[0]);
}

test("multi-section Word rendering keeps each section layout and page furniture through browser save", async ({ page }) => {
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
    const original = multiSectionWord();
    const before = await JSZip.loadAsync(original);
    const beforeDocumentXml = await before.file("word/document.xml")!.async("string");
    const beforeHeaders = await Promise.all(["word/header1.xml", "word/header2.xml"].map(
      async (name) => before.file(name)!.async("string"),
    ));
    const beforeFooters = await Promise.all(["word/footer1.xml", "word/footer2.xml"].map(
      async (name) => before.file(name)!.async("string"),
    ));

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-word-sections-${Date.now()}.docx`,
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
    const pages = workspace.locator(":scope .manor-docx-page");
    await expect(pages).toHaveCount(2);
    await expect(pages.nth(0)).toHaveAttribute("data-docx-section-index", "0");
    await expect(pages.nth(1)).toHaveAttribute("data-docx-section-index", "1");
    await expect(pages.nth(0)).toHaveCSS("width", "816px");
    await expect(pages.nth(0)).toHaveCSS("height", "1056px");
    await expect(pages.nth(1)).toHaveCSS("width", "1056px");
    await expect(pages.nth(1)).toHaveCSS("height", "816px");
    await expect(pages.nth(0).getByText("SECTION ONE", { exact: true })).toBeVisible();
    await expect(pages.nth(0).getByText("FIRST FOOTER", { exact: true })).toBeVisible();
    await expect(pages.nth(1).getByText("SECTION TWO", { exact: true })).toBeVisible();
    await expect(pages.nth(1).getByText("SECOND FOOTER", { exact: true })).toBeVisible();

    const editor = workspace.locator('[contenteditable="true"]');
    await editor.getByText("Second section body", { exact: true }).fill("Browser edited second section");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const zip = await JSZip.loadAsync(await download.body());
      return zip.file("word/document.xml")?.async("string") ?? "";
    }).toContain("Browser edited second section");

    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    expect(download.ok()).toBeTruthy();
    const after = await JSZip.loadAsync(await download.body());
    const afterDocumentXml = await after.file("word/document.xml")!.async("string");
    expect(sectionXml(afterDocumentXml)).toEqual(sectionXml(beforeDocumentXml));
    for (const [index, name] of ["word/header1.xml", "word/header2.xml"].entries()) {
      expect(await after.file(name)!.async("string"), name).toBe(beforeHeaders[index]);
    }
    for (const [index, name] of ["word/footer1.xml", "word/footer2.xml"].entries()) {
      expect(await after.file(name)!.async("string"), name).toBe(beforeFooters[index]);
    }
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
