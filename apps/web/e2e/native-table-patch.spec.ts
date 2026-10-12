import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function patchedTable(extension: string): Buffer {
  // Use the actual native transformer and the same fixture as the Knowledge
  // transaction tests; no model call or alternate spreadsheet patcher.
  const script = `
import base64
import tempfile
from pathlib import Path
from test_spreadsheet_append import _table_source
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
import sys
with tempfile.TemporaryDirectory(prefix="manor-table-browser-") as directory:
    path = Path(directory) / ("orders." + sys.argv[1])
    _table_source(path)
    result = _apply_office_patch_sequence_sync(str(path), [
        {"operation": "append_row", "sheet": "Sales", "table": "Orders", "values": ["After", 5, 2]}
    ])
    assert result.get("patched"), result
    print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`, ["-c", script, extension], {
    cwd: repoRoot,
    env: { ...process.env, PYTHONPATH: `${repoRoot}:${repoRoot}tests` },
    encoding: "utf8",
    timeout: 30_000,
  }).trim(), "base64");
}

for (const extension of ["xlsx", "xlsm"]) {
  const name = extension === "xlsm" ? "native-patched XLSM remains subject to Knowledge upload policy"
    : "native-patched XLSX table stays editable through Knowledge save and reopen";
  test(name, async ({ page }) => {
    const api = await pwRequest.newContext({ baseURL: API });
    const pageErrors: string[] = [];
    const consoleErrors: string[] = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    page.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text());
    });
    let token = "";
    let documentId = "";
    try {
      const login = await api.post("/api/v1/auth/login", {
        data: { email: "demo@manor.local", password: "manor-demo" },
      });
      expect(login.ok()).toBeTruthy();
      token = (await login.json()).access_token;
      const headers = { Authorization: `Bearer ${token}` };
      const patchedBytes = patchedTable(extension);
      expect(patchedBytes.subarray(0, 2).toString()).toBe("PK");
      const initialSheet = XLSX.read(patchedBytes, { type: "buffer", cellFormula: true, sheetStubs: true }).Sheets.Sales;
      expect(initialSheet.C4.v).toBe("After");
      expect(initialSheet.F4.f).toBe("D4*E4+$A$1");
      const upload = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: { file: {
          name: `native-table-${Date.now()}.${extension}`,
          mimeType: extension === "xlsm" ? "application/vnd.ms-excel.sheet.macroEnabled.12"
            : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          buffer: patchedBytes,
        } },
      });
      if (extension === "xlsm") {
        // Native patch support does not implicitly relax the upload allowlist.
        expect(upload.status()).toBe(400);
        expect((await upload.json()).detail).toBe("Unsupported upload file type: .xlsm");
        return;
      }
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;
      const stored = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      expect(stored.ok(), `download failed: ${stored.status()}`).toBeTruthy();
      expect(XLSX.read(await stored.body(), { type: "buffer" }).Sheets.Sales.C4.v).toBe("After");
      const preview = await api.get(`/api/v1/documents/${documentId}/preview/content`, { headers });
      expect(preview.ok(), `preview failed: ${preview.status()} ${preview.ok() ? "" : await preview.text()}`).toBeTruthy();
      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const appendedCell = page.locator(".manor-editor-main").getByText("After", { exact: true });
      const loadFailure = page.getByText("Failed to load document for editing", { exact: false });
      await expect(appendedCell.or(loadFailure)).toBeVisible();
      expect(await loadFailure.isVisible(), consoleErrors.join("\n")).toBeFalsy();
      await expect(appendedCell).toBeVisible();
      await appendedCell.click();
      const formulaInput = page.locator(".manor-editor-main input").first();
      await expect(formulaInput).toHaveValue("After");
      await formulaInput.fill("Browser saved");
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect.poll(async () => {
        const response = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!response.ok()) return null;
        return XLSX.read(await response.body(), { type: "buffer" }).Sheets.Sales.C4?.v;
      }).toBe("Browser saved");

      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      expect(download.ok()).toBeTruthy();
      const bytes = await download.body();
      // Native writers leave formula caches empty. SheetJS otherwise drops
      // these cells even though their <f> nodes remain in the workbook.
      const sheet = XLSX.read(bytes, { type: "buffer", cellFormula: true, sheetStubs: true }).Sheets.Sales;
      expect(sheet.F4.f).toBe("D4*E4+$A$1");
      expect(sheet.C3.v).toBe("Before");
      expect(sheet.J20.v).toBe("Unrelated note below the table");
      const archive = await JSZip.loadAsync(bytes);
      const table = await archive.file("xl/tables/table1.xml")!.async("string");
      expect(table).toMatch(/<table\b[^>]*\bref="C2:F4"/);
      expect(table).toMatch(/<autoFilter\b[^>]*\bref="C2:F4"/);
      expect(table).toContain('name="TableStyleMedium2"');
      const xml = await archive.file("xl/worksheets/sheet1.xml")!.async("string");
      expect(xml).toMatch(/<c\b[^>]*\br="F4"[^>]*>\s*<f>D4\*E4\+\$A\$1<\/f>/);
      const style = (cell: string) => xml.match(new RegExp(`<c\\b[^>]*\\br="${cell}"[^>]*>`))?.[0].match(/\bs="(\d+)"/)?.[1];
      expect(style("F4")).toBeTruthy();
      expect(style("F4")).toBe(style("F3"));
      await page.reload();
      await expect(page.locator(".manor-editor-main").getByText("Browser saved", { exact: true })).toBeVisible();
      const formulaCell = page.locator(".manor-editor-main").getByTitle("=D4*E4+$A$1", { exact: true });
      await expect(formulaCell).toHaveText("15.00");
      await formulaCell.click();
      await expect(formulaInput).toHaveValue("=D4*E4+$A$1");
      await formulaInput.fill("=D4*E4+$A$1+1");
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect.poll(async () => {
        const response = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!response.ok()) return null;
        return XLSX.read(await response.body(), { type: "buffer", cellFormula: true, sheetStubs: true }).Sheets.Sales.F4?.f;
      }).toBe("D4*E4+$A$1+1");
      await page.reload();
      await expect(page.locator(".manor-editor-main").getByTitle("=D4*E4+$A$1+1", { exact: true })).toHaveText("16.00");
      expect(pageErrors).toEqual([]);
    } finally {
      try {
        if (token && documentId) {
          const cleanup = await api.post(`/api/v1/documents/${documentId}/trash`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          expect(cleanup.ok()).toBeTruthy();
        }
      } finally {
        await api.dispose();
      }
    }
  });
}
