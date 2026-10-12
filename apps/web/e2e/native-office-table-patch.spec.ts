import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the local document API and Python Office dependencies");

function nativeTable(extension: string): Buffer {
  const script = `
import base64
import sys
import tempfile
from pathlib import Path
from tests.test_office_table_patch import table_operations, cell_operation
from tests.test_office_cell_format import format_operation
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
extension = sys.argv[1]
operations = table_operations(extension) + [format_operation(extension, {"bold": False}, cell="A1"), format_operation(extension, {"bold": False, "font_size": 14, "fill_color": "EEEEEE"}), cell_operation(extension, value="Generated value")]
created = _generate_office_operations_sync(extension, [normalize_file_patch_operation(op) for op in operations])
assert not created.get("error"), created
with tempfile.TemporaryDirectory(prefix="manor-cell-browser-") as directory:
    path = Path(directory) / ("table." + extension)
    path.write_bytes(created["_persisted_bytes"])
    patches = [cell_operation(extension, value="Native patched"), format_operation(extension)]
    patched = _apply_office_patch_sequence_sync(str(path), [normalize_file_patch_operation(op) for op in patches])
    assert patched.get("patched"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`, ["-c", script, extension], {
    cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000,
  }).trim(), "base64");
}

for (const extension of ["docx", "pptx"]) {
  test(`${extension} table cells stay editable after native generate and precise patch`, async ({ page }) => {
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
          name: `native-cell-${Date.now()}.${extension}`,
          mimeType: extension === "docx" ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            : "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          buffer: nativeTable(extension),
        } },
      });
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;
      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const table = page.locator(extension === "docx" ? '.richtext-editor-workspace [contenteditable="true"] table'
        : ".presentation-editor-slide-frame table");
      const target = table.locator("tr").nth(1).locator("td").nth(1);
      await expect(target).toHaveText("Native patched");
      const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
      if (await rejectCookies.isVisible()) await rejectCookies.click();
      await expect(target).toHaveCSS("background-color", "rgb(243, 217, 170)");
      const renderedText = target.getByText("Native patched", { exact: true });
      await expect(renderedText).toHaveCSS("color", "rgb(17, 34, 51)");
      await expect(renderedText).toHaveCSS("font-style", "italic");
      await expect(renderedText).toHaveCSS("font-family", /Arial/);
      await expect(renderedText).toHaveCSS("font-weight", "700");
      if (extension === "pptx") {
        await expect(table.locator("tr").first().locator("td").first()).toHaveCSS("font-weight", "400");
        await expect(target).toHaveAttribute("style", /font-size: calc\(3\.796cqh/);
      }
      await expect(table.locator("td").filter({ hasText: /^same$/ })).toHaveCount(3);
      if (extension === "docx") {
        await target.locator("p").fill("Browser saved");
      } else {
        await target.press("F2");
        const input = target.locator("textarea");
        await expect(input).toHaveValue("Native patched");
        await expect(input).toHaveCSS("color", "rgb(17, 34, 51)");
        await expect(input).toHaveCSS("font-style", "italic");
        await input.fill("Browser saved");
        await input.blur();
      }
      await page.getByRole("button", { name: "Save", exact: true }).click();
      const part = extension === "docx" ? "word/document.xml" : "ppt/slides/slide1.xml";
      await expect.poll(async () => {
        const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!download.ok()) return "";
        const zip = await JSZip.loadAsync(await download.body());
        return zip.file(part)?.async("string") ?? "";
      }).toContain("Browser saved");
      const stored = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      const zip = await JSZip.loadAsync(await stored.body());
      const xml = await zip.file(part)!.async("string");
      expect(xml.match(/>same</g)).toHaveLength(3);
      expect(xml).not.toContain(">Native patched<");
      if (extension === "docx") {
        expect(xml).toContain("TableGrid");
        expect(xml).toContain('w:fill="F3D9AA"');
        expect(xml).toContain('w:sz w:val="41"');
      } else {
        expect(xml).toContain('x="914400"');
        expect(xml).toContain('y="1828800"');
        expect(xml).toContain('cx="4572000"');
        expect(xml).toContain('val="F3D9AA"');
        expect(xml).toContain('sz="2050"');
      }
      await page.reload();
      await expect(table.locator("td").filter({ hasText: /^Browser saved$/ })).toBeVisible();
      await expect(target).toHaveCSS("background-color", "rgb(243, 217, 170)");
      await expect(target.getByText("Browser saved", { exact: true })).toHaveCSS("color", "rgb(17, 34, 51)");
      await expect(table.locator("td").filter({ hasText: /^same$/ })).toHaveCount(3);
      await page.screenshot({ animations: "disabled", path: test.info().outputPath(`${extension}-desktop.png`) });
      if (extension === "pptx") {
        await page.getByRole("button", { name: "Present", exact: true }).click();
        const presented = page.getByRole("dialog", { name: "Presentation mode" }).getByText("Browser saved", { exact: true });
        await expect(presented).toHaveCSS("font-style", "italic");
        await expect(presented).toHaveCSS("font-family", /Arial/);
        await expect(presented).toHaveCSS("color", "rgb(17, 34, 51)");
        await page.keyboard.press("Escape");
      }
      const collapseSidebar = page.getByRole("button", { name: "Collapse sidebar", exact: true });
      if (await collapseSidebar.isVisible()) await collapseSidebar.click();
      await page.setViewportSize({ width: 390, height: 844 });
      await target.scrollIntoViewIfNeeded();
      await expect(target).toBeVisible();
      await page.screenshot({ animations: "disabled", path: test.info().outputPath(`${extension}-mobile.png`) });
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
