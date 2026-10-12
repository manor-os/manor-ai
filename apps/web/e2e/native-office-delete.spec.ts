import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function deletedOffice(extension: string): Buffer {
  const script = String.raw`
import base64, sys, tempfile
from pathlib import Path
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

extension = sys.argv[1]
operations = {
    "docx": [
        {"op": "paragraph.insert", "index": 0, "text": "Browser editable keep"},
        {"op": "paragraph.insert", "index": 1, "text": "Deleted Word content"},
    ],
    "pptx": [
        {"op": "slide.insert", "index": 0, "layout_index": 6},
        {"op": "textbox.insert", "slide": 1, "text": "Inserted predecessor",
         "transform": {"x": 72, "y": 72, "width": 420, "height": 72}},
        {"op": "paragraph.insert", "slide": 1, "shape_id": 2, "index": 1,
         "text": "Browser editable keep"},
        {"op": "shape.insert", "slide": 1, "preset": "chevron", "text": "Workflow chevron",
         "transform": {"x": 72, "y": 180, "width": 220, "height": 80},
         "format": {"fill_color": "174C46", "line_color": "FFFFFF"}},
        {"op": "slide.duplicate", "slide": 1, "index": 1},
        {"op": "text.set", "slide": 2, "shape_id": 2, "index": 1,
         "text": "Deleted PowerPoint content"},
    ],
    "xlsx": [
        {"op": "cell.set", "sheet": "Sheet", "cell": "A1", "value": "Browser editable keep"},
        {"op": "merge.set", "sheet": "Sheet", "range": "A1:C2"},
        {"op": "cell.set", "sheet": "Sheet", "cell": "E1", "value": "Item"},
        {"op": "cell.set", "sheet": "Sheet", "cell": "F1", "value": "Amount"},
        {"op": "cell.set", "sheet": "Sheet", "cell": "E2", "value": "Service"},
        {"op": "cell.set", "sheet": "Sheet", "cell": "F2", "value": 1200},
        {"op": "table.insert", "sheet": "Sheet", "range": "E1:F2", "table": "Services",
         "format": {"style_name": "TableStyleDark3", "show_row_stripes": True}},
        {"op": "validation.insert", "sheet": "Sheet", "range": "H2:H10",
         "validation": {"type": "list", "values": ["Planned", "Active", "Done"],
                        "allow_blank": True, "show_dropdown": True}},
        {"op": "conditional_format.insert", "sheet": "Sheet", "range": "J2:J10",
         "conditional_format": {"type": "cell", "operator": "greaterThan", "formulas": [0],
                                "stop_if_true": True,
                                "format": {"font_color": "FFFFFF", "fill_color": "C00000"}}},
        {"op": "sheet.add", "sheet": "Delete me"},
        {"op": "cell.set", "sheet": "Delete me", "cell": "A1", "value": "Deleted Excel content"},
    ],
}[extension]
deletions = {
    "docx": [{"op": "paragraph.delete", "index": 1}],
    "pptx": [
        {"op": "paragraph.delete", "slide": 1, "shape_id": 2, "index": 0},
        {"op": "slide.delete", "index": 1},
    ],
    "xlsx": [{"op": "sheet.delete", "sheet": "Delete me"}],
}[extension]
normalize = normalize_file_patch_operation
created = _generate_office_operations_sync(extension, [normalize(operation) for operation in operations])
assert created.get("patched"), created
with tempfile.TemporaryDirectory(prefix="manor-office-delete-browser-") as directory:
    path = Path(directory) / ("delete." + extension)
    path.write_bytes(created["_persisted_bytes"])
    patched = _apply_office_patch_sequence_sync(str(path), [normalize(operation) for operation in deletions])
    assert patched.get("patched"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script, extension],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

for (const extension of ["docx", "pptx", "xlsx"]) {
  test(`native ${extension} deletion survives Knowledge upload, browser edit, save and reopen`, async ({ page }) => {
    test.setTimeout(90_000);
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
      const upload = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: { file: {
          name: `native-delete-${Date.now()}.${extension}`,
          mimeType: mime,
          buffer: deletedOffice(extension),
        } },
      });
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;

      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const rejectCookies = page.getByRole("button", { name: "Reject all", exact: true });
      if (await rejectCookies.isVisible()) await rejectCookies.click();
      const main = page.locator(".manor-editor-main");
      await expect(main.getByText("Browser editable keep", { exact: true }).first()).toBeVisible();
      await expect(main.getByText(/Deleted (Word|PowerPoint|Excel) content/)).toHaveCount(0);

      if (extension === "docx") {
        await main.getByText("Browser editable keep", { exact: true }).fill("Browser saved keep");
      } else if (extension === "pptx") {
        await expect(page.locator(".presentation-editor-thumbnail-slide")).toHaveCount(1);
        const frame = page.locator(".presentation-editor-slide-frame");
        const chevron = frame.locator('[data-presentation-shape-type="shape"]', { hasText: "Workflow chevron" });
        await expect(chevron).toBeVisible();
        await expect(chevron.locator('div[aria-hidden="true"]').first()).toHaveCSS("clip-path", /polygon/);
        await frame.getByRole("textbox", { name: "Browser editable keep", exact: true }).press("F2");
        const input = frame.locator('[contenteditable="true"]');
        await input.fill("Browser saved keep");
        await input.blur();
      } else {
        await main.getByText("Browser editable keep", { exact: true }).click();
        await main.locator("input").first().fill("Browser saved keep");
      }
      await page.getByRole("button", { name: "Save", exact: true }).click();

      const part = extension === "docx"
        ? "word/document.xml"
        : extension === "pptx" ? "ppt/slides/slide1.xml" : "xl/worksheets/sheet1.xml";
      await expect.poll(async () => {
        const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!download.ok()) return "";
        const zip = await JSZip.loadAsync(await download.body());
        return zip.file(part)?.async("string") ?? "";
      }).toContain("Browser saved keep");

      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      expect(download.ok()).toBeTruthy();
      const bytes = await download.body();
      const zip = await JSZip.loadAsync(bytes);
      const packageText = await Promise.all(
        Object.keys(zip.files).filter((name) => name.endsWith(".xml")).map((name) => zip.file(name)!.async("string")),
      );
      expect(packageText.join("\n")).not.toContain(`Deleted ${extension === "docx" ? "Word" : extension === "pptx" ? "PowerPoint" : "Excel"} content`);
      if (extension === "pptx") {
        expect(packageText.join("\n")).not.toContain("Inserted predecessor");
        expect(packageText.join("\n")).toContain('prst="chevron"');
        expect(Object.keys(zip.files).filter((name) => /^ppt\/slides\/slide\d+\.xml$/.test(name))).toHaveLength(1);
      } else if (extension === "xlsx") {
        const workbook = XLSX.read(bytes, { type: "buffer" });
        expect(workbook.SheetNames).toEqual(["Sheet"]);
        expect(workbook.Sheets.Sheet["!merges"]).toEqual([{ s: { c: 0, r: 0 }, e: { c: 2, r: 1 } }]);
        const tableXml = await zip.file("xl/tables/table1.xml")?.async("string");
        expect(tableXml).toContain('displayName="Services"');
        expect(tableXml).toContain('ref="E1:F2"');
        expect(tableXml).toContain('name="TableStyleDark3"');
        const sheetXml = await zip.file("xl/worksheets/sheet1.xml")?.async("string");
        expect(sheetXml).toContain('sqref="H2:H10"');
        expect(sheetXml).toContain('"Planned,Active,Done"');
        expect(sheetXml).toContain('sqref="J2:J10"');
        expect(sheetXml).toContain('type="cellIs"');
        expect(sheetXml).toContain('operator="greaterThan"');
        expect(sheetXml).toContain("<formula>0</formula>");
        const stylesXml = await zip.file("xl/styles.xml")?.async("string");
        expect(stylesXml).toContain('rgb="FFFFFFFF"');
        expect(stylesXml).toContain('rgb="FFC00000"');
      }

      await page.reload();
      await expect(main.getByText("Browser saved keep", { exact: true }).first()).toBeVisible();
      expect(pageErrors).toEqual([]);
    } finally {
      try {
        if (token && documentId) {
          const trashed = await api.post(`/api/v1/documents/${documentId}/trash`, {
            headers: { Authorization: `Bearer ${token}` },
          });
          expect(trashed.ok()).toBeTruthy();
        }
      } finally {
        await api.dispose();
      }
    }
  });
}
