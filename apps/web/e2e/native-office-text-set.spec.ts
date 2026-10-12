import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the local document API and Python Office dependencies");

function nativeParagraph(extension: string, template: boolean): Buffer {
  // Current host executor; the DB suite covers the real tool/save transaction.
  const script = `
import base64
import sys
import tempfile
from pathlib import Path
from tests.test_office_text_set import text_operations, text_operation, open_paragraphs, apply_text
from tests.test_office_template_layout import template_operations, paragraph_operation, page_operation
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
extension = sys.argv[1]
template = sys.argv[2] == "template"
operations = template_operations(extension) if template else text_operations(extension) + [text_operation(extension, "Generated value")]
created = _generate_office_operations_sync(extension, [normalize_file_patch_operation(op) for op in operations])
assert not created.get("error"), created
with tempfile.TemporaryDirectory(prefix="manor-paragraph-browser-") as directory:
    path = Path(directory) / ("paragraph." + extension)
    path.write_bytes(created["_persisted_bytes"])
    document, paragraphs = open_paragraphs(str(path), extension)
    paragraphs[1].runs[0].font.bold = True
    document.save(path)
    patches = [text_operation(extension, "Native patched\\nSecond line")]
    if template:
        patches.append(paragraph_operation(extension, {"font_size": 24, "font_color": "174C46", "bold": True, "italic": True,
            "alignment": "center", "indent_left": 18, "indent_right": 36, "first_line_indent": 9,
            "space_before": 0, "space_after": 0, "line_spacing": 1.25}))
        page = page_operation(extension)
        page["format"] = {"margin_left": 72, "margin_top": 72} if extension == "docx" else {"width": 1280, "height": 720}
        patches.append(page)
    patched = apply_text(path, patches)
    assert patched.get("patched"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`, ["-c", script, extension, template ? "template" : "precise"], {
    cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000,
  }).trim(), "base64");
}

for (const extension of ["docx", "pptx"]) for (const template of [false, true]) {
  test(`${extension} ${template ? "template layout" : "precise paragraph"} patch survives browser edit, save and reopen`, async ({ page }, testInfo) => {
    const api = await pwRequest.newContext({ baseURL: API });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let token = "";
    let documentId = "";
    try {
      const login = await api.post("/api/v1/auth/login", { data: { email: "demo@manor.local", password: "manor-demo" } });
      expect(login.ok()).toBeTruthy();
      token = (await login.json()).access_token;
      const headers = { Authorization: `Bearer ${token}` };
      const upload = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: { file: {
          name: `native-paragraph-${Date.now()}.${extension}`,
          mimeType: extension === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            : "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          buffer: nativeParagraph(extension, template),
        } },
      });
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;
      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const main = page.locator(".manor-editor-main");
      const target = extension === "docx"
        ? main.locator('.richtext-editor-workspace [contenteditable="true"] p').filter({ hasText: /^Native patched\s*Second line$/ })
        : page.locator(".presentation-editor-slide-frame").getByRole("textbox", { name: /^Native patched/ });
      await expect(target).toBeVisible();
      const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
      if (await rejectCookies.isVisible()) await rejectCookies.click();
      if (template) {
        await expect(target).toHaveCSS("text-align", "center");
        await expect(target).toHaveCSS("margin-top", "0px");
        await expect(target).toHaveCSS("margin-bottom", "0px");
        if (extension === "docx") {
          await expect(main.locator(".manor-docx-native")).toHaveCSS("--docx-margin-left", "96px");
        } else {
          const frame = page.locator(".presentation-editor-slide-frame");
          await expect(frame).toHaveCSS("--pptx-point-scale", "0.75");
          const height = (await frame.boundingBox())!.height;
          const styles = await target.evaluate((element) => {
            const css = getComputedStyle(element);
            return { font: parseFloat(css.fontSize), right: parseFloat(css.paddingRight), first: parseFloat(css.textIndent) };
          });
          expect(styles.font / height).toBeCloseTo(24 / 720, 3);
          expect(styles.right / height).toBeCloseTo(36 / 720, 3);
          expect(styles.first / height).toBeCloseTo(9 / 720, 3);
        }
        await page.screenshot({ path: testInfo.outputPath(`${extension}-template-before.png`), animations: "disabled" });
      }
      if (extension === "docx") {
        await target.fill("Browser saved");
      } else {
        await target.press("F2");
        const input = page.locator('.presentation-editor-slide-frame [contenteditable="true"]');
        await expect(input).toBeVisible();
        await input.fill("Browser saved");
        await input.blur();
      }
      await page.getByRole("button", { name: "Save", exact: true }).click();
      const part = extension === "docx" ? "word/document.xml" : "ppt/slides/slide1.xml";
      await expect.poll(async () => {
        const response = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!response.ok()) return "";
        const zip = await JSZip.loadAsync(await response.body());
        return zip.file(part)?.async("string") ?? "";
      }).toContain("Browser saved");
      const response = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      const zip = await JSZip.loadAsync(await response.body());
      const xml = await zip.file(part)!.async("string");
      if (template) {
        expect(xml).toContain("Service proposal");
        expect(xml).toContain("A focused engagement");
      } else expect(xml.match(/>same</g)).toHaveLength(extension === "docx" ? 2 : 3);
      expect(xml).not.toContain("Native patched");
      if (extension === "pptx") {
        expect(xml).toContain('x="914400"');
        expect(xml).toContain(template ? 'cx="10363200"' : 'cx="6096000"');
        if (template) {
          const presentationXml = await zip.file("ppt/presentation.xml")!.async("string");
          expect(presentationXml).toContain('cx="16256000" cy="9144000"');
          expect(xml).toContain('marR="457200"');
          expect(xml).toContain('indent="114300"');
        }
      }
      await page.reload();
      const savedText = main.getByText("Browser saved", { exact: true }).first();
      await expect(savedText).toBeVisible();
      await expect(savedText).toHaveCSS("font-weight", "700");
      if (template && extension === "pptx") {
        await page.getByRole("button", { name: "Present", exact: true }).click();
        const presentation = page.getByRole("dialog", { name: "Presentation mode" });
        const text = presentation.getByText("Browser saved", { exact: true });
        await expect(text).toHaveCSS("margin-top", "0px");
        const height = (await presentation.locator(".presentation-editor-present-slide").boundingBox())!.height;
        expect((await text.evaluate((element) => parseFloat(getComputedStyle(element).fontSize))) / height).toBeCloseTo(24 / 720, 3);
        await page.keyboard.press("Escape");
      }
      await page.screenshot({ path: testInfo.outputPath(`${extension}-paragraph.png`), animations: "disabled" });
      expect(errors).toEqual([]);
    } finally {
      try {
        if (token && documentId) {
          const cleanup = await api.post(`/api/v1/documents/${documentId}/trash`, { headers: { Authorization: `Bearer ${token}` } });
          expect(cleanup.ok()).toBeTruthy();
        }
      } finally {
        await api.dispose();
      }
    }
  });
}
