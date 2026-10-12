import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function generatedOffice(extension: string, template = false, clone = false): Buffer {
  // Real generation + patch executor; the DB suite separately exercises the
  // native tool's approval, scoping, projection and rollback boundary.
  const script = `
import base64
import sys
import tempfile
from pathlib import Path
from tests.test_office_operation_generation import office_operations
from tests.test_spreadsheet_template_layout import invoice_operations
from tests.test_office_template_generation import template_bytes, template_patch
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
extension = sys.argv[1]
template = sys.argv[2] == "template"
operations = [normalize_file_patch_operation(op) for op in (invoice_operations() if template else office_operations(extension))]
if sys.argv[3] == "clone":
    result = _generate_office_operations_sync(extension, [normalize_file_patch_operation(template_patch(extension, "Proposal"))], template_bytes=template_bytes(extension))
else:
    result = _generate_office_operations_sync(extension, operations)
assert result.get("patched"), result
with tempfile.TemporaryDirectory(prefix="manor-office-browser-") as directory:
    path = Path(directory) / ("generated." + extension)
    path.write_bytes(result["_persisted_bytes"])
    patch = {"op": "text.replace", "old_text": "Proposal", "new_text": "Native patched"}
    if extension == "xlsx":
        patch = {"op": "cell.set", "sheet": "Sheet", "cell": "A1", "value": "Native patched"}
    patches = [patch]
    if template:
        patches.append({"op": "sheet.format", "format": {"column_widths": {"A": 48}, "row_heights": {"1": 48}}})
    result = _apply_office_patch_sequence_sync(str(path), [normalize_file_patch_operation(op) for op in patches])
    assert result.get("patched"), result
    print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`, ["-c", script, extension, template ? "template" : "plain", clone ? "clone" : "blank"], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: repoRoot },
    encoding: "utf8",
    timeout: 30_000,
  }).trim(), "base64");
}

for (const { extension, template, clone } of [
  ...["docx", "pptx", "xlsx"].map((extension) => ({ extension, template: false, clone: false })),
  { extension: "xlsx", template: true, clone: false },
  ...["docx", "pptx", "xlsx"].map((extension) => ({ extension, template: false, clone: true })),
]) {
  test(`operation-generated ${extension}${template ? " invoice template" : ""}${clone ? " template clone" : ""} remains editable after native patch, upload and browser save`, async ({ page }, testInfo) => {
    const api = await pwRequest.newContext({ baseURL: API });
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    let token = "";
    let documentId = "";
    try {
      const login = await api.post("/api/v1/auth/login", {
        data: { email: "demo@manor.local", password: "manor-demo" },
      });
      expect(login.ok()).toBeTruthy();
      token = (await login.json()).access_token;
      const headers = { Authorization: `Bearer ${token}` };
      const mime = {
        docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }[extension]!;
      const generated = generatedOffice(extension, template, clone);
      const upload = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: { file: { name: `native-generation-${Date.now()}.${extension}`, mimeType: mime, buffer: generated } },
      });
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;
      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const main = page.locator(".manor-editor-main");
      await expect(main.getByText("Native patched", { exact: true }).first()).toBeVisible();
      const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
      if (await rejectCookies.isVisible()) await rejectCookies.click();
      if (template) {
        const title = main.getByText("Native patched", { exact: true }).first();
        await expect(title).toHaveCSS("font-weight", "700");
        await expect(title).toHaveCSS("color", "rgb(23, 76, 70)");
        await expect(title).toHaveCSS("font-family", /Carlito/);
        expect(parseFloat(await title.evaluate((element) => getComputedStyle(element).fontSize))).toBeCloseTo(26 * 96 / 72, 2);
        const header = main.getByRole("cell", { name: "Description", exact: true });
        await expect(header, await header.getAttribute("style") || "native header style").toHaveCSS("border-bottom-width", "2px");
        await expect(header).toHaveCSS("border-bottom-color", "rgb(23, 76, 70)");
        const row = title.locator("xpath=ancestor::tr");
        expect((await row.boundingBox())!.height).toBeGreaterThanOrEqual(64);
        expect((await title.locator("xpath=ancestor::td").boundingBox())!.width).toBeGreaterThan(280);
        await page.screenshot({ path: testInfo.outputPath("xlsx-template-desktop.png"), animations: "disabled" });
      }
      if (extension === "docx") {
        const cell = main.locator("td").filter({ hasText: /^Service$/ });
        await expect(cell).toBeVisible();
        await cell.locator("p").fill("Browser saved");
      } else if (extension === "pptx") {
        const frame = page.locator(".presentation-editor-slide-frame");
        await frame.getByRole("textbox", { name: "Native patched", exact: true }).press("F2");
        const input = frame.locator('[contenteditable="true"]');
        await expect(input).toBeVisible();
        await input.fill("Browser saved");
        await input.blur();
      } else {
        await main.getByText("Native patched", { exact: true }).click();
        await main.locator("input").first().fill("Browser saved");
      }
      await page.getByRole("button", { name: "Save", exact: true }).click();
      const part = extension === "docx" ? "word/document.xml" : extension === "pptx" ? "ppt/slides/slide1.xml" : "xl/worksheets/sheet1.xml";
      await expect.poll(async () => {
        const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!download.ok()) return "";
        const zip = await JSZip.loadAsync(await download.body());
        return zip.file(part)?.async("string") ?? "";
      }).toContain("Browser saved");
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      const bytes = await download.body();
      const zip = await JSZip.loadAsync(bytes);
      const xml = await zip.file(part)!.async("string");
      if (clone) {
        const original = await JSZip.loadAsync(generated);
        const prefixes = extension === "docx" ? ["word/header", "word/footer", "word/theme/"]
          : extension === "pptx" ? ["ppt/slideMasters/", "ppt/charts/", "ppt/embeddings/", "ppt/notesSlides/"] : ["xl/charts/"];
        const retained = Object.keys(original.files).filter((name) => !original.files[name].dir && prefixes.some((prefix) => name.startsWith(prefix)));
        expect(retained.length).toBeGreaterThan(0);
        for (const name of retained) expect(await zip.file(name)!.async("base64"), name).toBe(await original.file(name)!.async("base64"));
        if (extension === "xlsx") {
          expect(xml).toContain("dataValidations");
          expect(xml).toContain('topLeftCell="B3"');
          expect(xml).toContain('width="38"');
        }
      }
      if (extension === "docx") {
        expect(xml).toContain("Native patched");
        expect(xml).toContain("TableGrid");
        expect(xml).toMatch(/<w:tbl[ >]/);
        expect(xml).toContain(">12<");
      } else if (extension === "pptx") {
        expect(xml).toContain('x="914400"');
        expect(xml).toContain('cx="4572000"');
        expect(xml).not.toContain(">Native patched<");
      } else {
        const workbook = XLSX.read(bytes, { type: "buffer", cellFormula: true, sheetStubs: true });
        expect(workbook.SheetNames).toEqual(["Sheet", template ? "Notes" : "Summary"]);
        expect(workbook.Sheets.Sheet.A1.v).toBe("Browser saved");
        if (template) {
          expect(workbook.Sheets.Sheet.C7.f).toBe("SUM(C4:C5)");
          expect(workbook.Sheets.Notes.A1.v).toBe("Keep notes");
          expect(xml).toMatch(/showGridLines="0"/);
          expect(xml).toMatch(/topLeftCell="B4"/);
          expect(xml).toMatch(/orientation="landscape"/);
          expect(xml).toMatch(/fitToWidth="1"/);
          expect(xml).toMatch(/width="48"/);
          expect(xml).toMatch(/ht="48"/);
          expect(await zip.file("xl/workbook.xml")!.async("string")).toContain("_xlnm.Print_Titles");
        } else expect(workbook.Sheets.Sheet.B1.f).toBe("2*6");
      }
      await page.reload();
      await expect(main.getByText("Browser saved", { exact: true }).first()).toBeVisible();
      if (extension === "docx") await expect(main.locator("td").filter({ hasText: /^Browser saved$/ })).toBeVisible();
      if (template) {
        await expect(main.getByText("Browser saved", { exact: true }).first()).toHaveCSS("font-weight", "700");
        const collapse = page.getByRole("button", { name: "Collapse sidebar", exact: true });
        if (await collapse.isVisible()) await collapse.click();
        await page.setViewportSize({ width: 390, height: 844 });
        await expect(main.getByText("Browser saved", { exact: true }).first()).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath("xlsx-template-mobile.png"), animations: "disabled" });
      }
      if (clone) {
        if (extension === "pptx") await expect(page.locator('.presentation-editor-slide-frame [role="img"] img')).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath(`${extension}-template-clone.png`), animations: "disabled" });
      }
      expect(pageErrors).toEqual([]);
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
