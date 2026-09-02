import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import type { APIRequestContext } from "@playwright/test";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));

function reorderedOfficeFile(extension: "pptx" | "xlsx"): Buffer {
  const script = String.raw`
import base64, sys
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

extension = sys.argv[1]
if extension == "pptx":
    raw = []
    for index, title in enumerate(("First", "Second", "Third")):
        raw.extend([
            {"op": "slide.insert", "index": index, "layout_index": 6},
            {"op": "textbox.insert", "slide": index + 1, "text": title,
             "transform": {"x": 72, "y": 72, "width": 320, "height": 60}},
        ])
    raw.append({"op": "slide.reorder", "slide": 1, "index": 2})
else:
    raw = [
        {"op": "cell.set", "sheet": "Sheet", "cell": "A1", "value": "First"},
        {"op": "sheet.add", "sheet": "Second"},
        {"op": "cell.set", "sheet": "Second", "cell": "A1", "value": "Second"},
        {"op": "sheet.add", "sheet": "Third"},
        {"op": "cell.set", "sheet": "Third", "cell": "A1", "value": "Third"},
        {"op": "cell.set", "sheet": "Sheet", "cell": "B1", "value": "='Third'!A1"},
        {"op": "sheet.reorder", "sheet": "Sheet", "index": 2},
        {"op": "sheet.rename", "sheet": "Third", "new_sheet": "Metrics"},
    ]
operations = [normalize_file_patch_operation(operation) for operation in raw]
result = _generate_office_operations_sync(extension, operations)
assert not result.get("error"), result
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script, extension],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

async function upload(api: APIRequestContext, token: string, extension: "pptx" | "xlsx") {
  const response = await api.post("/api/v1/documents/upload", {
    headers: { Authorization: `Bearer ${token}` },
    multipart: { file: {
      name: `native-container-reorder-${Date.now()}.${extension}`,
      mimeType: extension === "pptx"
        ? "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      buffer: reorderedOfficeFile(extension),
    } },
  });
  expect(response.ok(), `upload failed: ${response.status()} ${await response.text()}`).toBeTruthy();
  return (await response.json()).id as string;
}

test("native PPT slide order survives browser edit, save and reload", async ({ page }) => {
  test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python dependencies");
  const api = await pwRequest.newContext({ baseURL: API });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let token = "";
  let documentId = "";
  try {
    const login = await api.post("/api/v1/auth/login", { data: { email: "demo@manor.local", password: "manor-demo" } });
    expect(login.ok()).toBeTruthy();
    token = (await login.json()).access_token;
    documentId = await upload(api, token, "pptx");
    const headers = { Authorization: `Bearer ${token}` };

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const tabs = page.getByRole("tab");
    await expect(tabs).toHaveCount(3);
    await expect.poll(() => tabs.evaluateAll((items) => items.map((item) => item.textContent || ""))).toEqual([
      expect.stringContaining("Second"),
      expect.stringContaining("Third"),
      expect.stringContaining("First"),
    ]);

    const frame = page.locator(".presentation-editor-slide-frame");
    const title = frame.getByRole("textbox", { name: "Second", exact: true });
    await title.click();
    await title.press("F2");
    const input = frame.locator('[contenteditable="true"]');
    await input.fill("Browser Second");
    await input.blur();
    await page.getByRole("button", { name: "Save", exact: true }).click();

    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const zip = await JSZip.loadAsync(await download.body());
      return (await zip.file("ppt/slides/slide2.xml")?.async("string")) || "";
    }).toContain("Browser Second");
    const stored = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    const zip = await JSZip.loadAsync(await stored.body());
    const presentationXml = await zip.file("ppt/presentation.xml")!.async("string");
    const slideIds = [...presentationXml.matchAll(/<p:sldId\b[^>]*r:id="([^"]+)"/g)].map((match) => match[1]);
    const relationships = await zip.file("ppt/_rels/presentation.xml.rels")!.async("string");
    const targetById = new Map([...relationships.matchAll(/<Relationship\b[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"/g)]
      .map((match) => [match[1], match[2]]));
    const orderedText = await Promise.all(slideIds.map(async (id) => {
      const target = targetById.get(id)!;
      return zip.file(`ppt/${target}`)!.async("string");
    }));
    expect(orderedText[0]).toContain("Browser Second");
    expect(orderedText[1]).toContain("Third");
    expect(orderedText[2]).toContain("First");

    await page.reload();
    await expect(frame.getByRole("textbox", { name: "Browser Second", exact: true })).toBeVisible();
    await expect.poll(() => tabs.evaluateAll((items) => items.map((item) => item.textContent || ""))).toEqual([
      expect.stringContaining("Browser Second"),
      expect.stringContaining("Third"),
      expect.stringContaining("First"),
    ]);
    expect(errors).toEqual([]);
  } finally {
    if (documentId && token) {
      await api.post(`/api/v1/documents/${documentId}/trash`, { headers: { Authorization: `Bearer ${token}` } });
    }
    await api.dispose();
  }
});

test("native Excel sheet order survives browser edit, save and reload", async ({ page }) => {
  test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python dependencies");
  const api = await pwRequest.newContext({ baseURL: API });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let token = "";
  let documentId = "";
  try {
    const login = await api.post("/api/v1/auth/login", { data: { email: "demo@manor.local", password: "manor-demo" } });
    expect(login.ok()).toBeTruthy();
    token = (await login.json()).access_token;
    documentId = await upload(api, token, "xlsx");
    const headers = { Authorization: `Bearer ${token}` };

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const sheetButtons = page.locator("button").filter({ hasText: /^(Second|Metrics|Sheet)$/ });
    await expect(sheetButtons).toHaveCount(3);
    await expect.poll(() => sheetButtons.allTextContents()).toEqual(["Second", "Metrics", "Sheet"]);

    await page.getByText("First", { exact: true }).click();
    const cell = page.locator(".spreadsheet-editor-grid-pane textarea").first();
    await expect(cell).toHaveValue("First");
    await cell.fill("Browser First");
    await cell.blur();
    await page.getByRole("button", { name: "Save", exact: true }).click();

    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return [];
      const bytes = await download.body();
      const workbook = XLSX.read(bytes, { type: "buffer", cellFormula: true, sheetStubs: true });
      return [
        workbook.SheetNames.join("|"),
        String(workbook.Sheets.Sheet.A1.v || ""),
        String(workbook.Sheets.Sheet.B1?.f || ""),
      ];
    }).toEqual(["Second|Metrics|Sheet", "Browser First", "'Metrics'!A1"]);

    await page.reload();
    await expect.poll(() => sheetButtons.allTextContents()).toEqual(["Second", "Metrics", "Sheet"]);
    await expect(page.getByText("Browser First", { exact: true })).toBeVisible();
    expect(errors).toEqual([]);
  } finally {
    if (documentId && token) {
      await api.post(`/api/v1/documents/${documentId}/trash`, { headers: { Authorization: `Bearer ${token}` } });
    }
    await api.dispose();
  }
});
