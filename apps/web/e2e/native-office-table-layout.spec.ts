import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));

function formattedTable(extension: string): Buffer {
  const script = `
import base64
import sys
import tempfile
from pathlib import Path
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation
from tests.test_office_table_patch import table_operations
from tests.test_office_table_format import table_format_operation
extension = sys.argv[1]
created = _generate_office_operations_sync(
    extension,
    [normalize_file_patch_operation(operation) for operation in table_operations(extension) + [table_format_operation(extension)]],
)
assert not created.get("error"), created
with tempfile.TemporaryDirectory(prefix="manor-table-layout-browser-") as directory:
    path = Path(directory) / ("layout." + extension)
    path.write_bytes(created["_persisted_bytes"])
    patched = _apply_office_patch_sequence_sync(
        str(path), [normalize_file_patch_operation(table_format_operation(extension, alternate=True))],
    )
    assert not patched.get("error"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script, extension],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

for (const extension of ["docx", "pptx"]) {
  test(`${extension} native table layout survives generate, patch, browser edit, save and reload`, async ({ page }) => {
    test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the local document API and Python Office dependencies");
    const api = await pwRequest.newContext({ baseURL: API });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let token = "";
    let documentId = "";
    try {
      const login = await api.post("/api/v1/auth/login", {
        data: { email: "demo@manor.local", password: "manor-demo" },
      });
      expect(login.ok()).toBeTruthy();
      token = (await login.json()).access_token;
      const headers = { Authorization: `Bearer ${token}` };
      const upload = await api.post("/api/v1/documents/upload", {
        headers,
        multipart: {
          file: {
            name: `native-table-layout-${Date.now()}.${extension}`,
            mimeType: extension === "docx"
              ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
              : "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            buffer: formattedTable(extension),
          },
        },
      });
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;

      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const table = page.locator(extension === "docx"
        ? '.richtext-editor-workspace [contenteditable="true"] table'
        : ".presentation-editor-slide-frame table");
      await expect(table).toBeVisible();
      const firstRow = table.locator("tr").nth(0);
      const secondRow = table.locator("tr").nth(1);
      const firstCell = firstRow.locator("td").nth(0);
      const secondCell = firstRow.locator("td").nth(1);
      await expect.poll(async () => {
        const first = await firstCell.boundingBox();
        const second = await secondCell.boundingBox();
        return first && second ? second.width / first.width : 0;
      }).toBeGreaterThan(2.2);
      await expect.poll(async () => {
        const first = await firstRow.boundingBox();
        const second = await secondRow.boundingBox();
        return first && second ? second.height / first.height : 0;
      }).toBeGreaterThan(1.8);

      const target = secondRow.locator("td").nth(1);
      if (extension === "docx") {
        await target.locator("p").fill("Layout saved");
      } else {
        await target.press("F2");
        await target.locator("textarea").fill("Layout saved");
        await target.locator("textarea").blur();
      }
      await page.getByRole("button", { name: "Save", exact: true }).click();
      await expect.poll(async () => {
        const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!download.ok()) return "";
        const zip = await JSZip.loadAsync(await download.body());
        const part = extension === "docx" ? "word/document.xml" : "ppt/slides/slide1.xml";
        return zip.file(part)?.async("string") ?? "";
      }).toContain("Layout saved");

      const stored = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      const zip = await JSZip.loadAsync(await stored.body());
      const part = extension === "docx" ? "word/document.xml" : "ppt/slides/slide1.xml";
      const xml = await zip.file(part)!.async("string");
      if (extension === "docx") {
        expect(xml).toContain('w:gridCol w:w="2000"');
        expect(xml).toContain('w:gridCol w:w="4800"');
        expect(xml).toContain('w:tblInd w:type="dxa" w:w="240"');
        expect(xml).toContain('w:trHeight w:val="1000" w:hRule="atLeast"');
        expect(xml).toContain('w:trHeight w:val="2000" w:hRule="atLeast"');
        expect(xml).not.toContain("w:tblHeader");
      } else {
        expect(xml).toContain('<a:gridCol w="1270000"');
        expect(xml).toContain('<a:gridCol w="3048000"');
        expect(xml).toContain('<a:tr h="635000"');
        expect(xml).toContain('<a:tr h="1270000"');
        expect(xml).toMatch(/<a:tblPr[^>]*lastRow="1"[^>]*firstCol="1"[^>]*bandRow="1"/);
      }

      await page.reload();
      await expect(table.getByText("Layout saved", { exact: true })).toBeVisible();
      await expect.poll(async () => {
        const first = await firstCell.boundingBox();
        const second = await secondCell.boundingBox();
        return first && second ? second.width / first.width : 0;
      }).toBeGreaterThan(2.2);
      expect(errors).toEqual([]);
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
