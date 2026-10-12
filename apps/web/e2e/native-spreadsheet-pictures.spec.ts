import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeSpreadsheetPicture(): Buffer {
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
first = {"path": "Assets/initial.png", "expected_sha256": hashlib.sha256(red).hexdigest()}
second = {"path": "Assets/replacement.jpg", "expected_sha256": hashlib.sha256(blue).hexdigest()}
normalize = normalize_file_patch_operation
created = _generate_office_operations_sync("xlsx", [normalize(operation) for operation in [
    {"op": "cell.set", "sheet": "Sheet", "cell": "A1", "value": "Editable cell"},
    {"op": "picture.insert", "sheet": "Sheet", "source": first, "anchor": "C4",
     "transform": {"width": 180}, "format": {"alt_text": "Initial worksheet illustration"}},
]], resources={(first["path"], first["expected_sha256"]): red})
assert created.get("patched"), created
with tempfile.TemporaryDirectory(prefix="manor-spreadsheet-picture-browser-") as directory:
    path = Path(directory) / "picture.xlsx"
    path.write_bytes(created["_persisted_bytes"])
    patched = _apply_office_patch_sequence_sync(str(path), [normalize(operation) for operation in [
        {"op": "picture.replace", "sheet": "Sheet", "picture_index": 0, "source": second},
        {"op": "picture.format", "sheet": "Sheet", "picture_index": 0,
         "format": {"anchor": "E6", "height": 60, "alt_text": "Updated worksheet illustration"}},
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

test("native generated and patched Excel picture renders and survives browser cell editing", async ({ page }, testInfo) => {
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
    const original = nativeSpreadsheetPicture();
    const before = await JSZip.loadAsync(original);
    const drawingPart = Object.keys(before.files).find((name) => /^xl\/drawings\/drawing\d+\.xml$/.test(name));
    expect(drawingPart).toBeTruthy();
    const relationshipsPart = `xl/drawings/_rels/${drawingPart!.split("/").pop()}.rels`;
    const mediaParts = Object.keys(before.files).filter((name) => /^xl\/media\/[^/]+$/.test(name));
    expect(mediaParts).toHaveLength(1);

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-spreadsheet-picture-${Date.now()}.xlsx`,
        mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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

    const picture = page.getByRole("img", { name: "Updated worksheet illustration", exact: true });
    await expect(picture).toBeVisible();
    await expect(picture).toHaveCSS("width", "160px");
    await expect(picture).toHaveCSS("height", "80px");
    await page.screenshot({ path: testInfo.outputPath("native-spreadsheet-picture-desktop.png"), animations: "disabled" });

    const firstCell = page.locator(".spreadsheet-editor-grid-pane tbody tr").first().locator("td").nth(1);
    await firstCell.click();
    const input = firstCell.locator("textarea");
    await expect(input).toHaveValue("Editable cell");
    await input.fill("Browser edited cell");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const zip = await JSZip.loadAsync(await download.body());
      return zip.file("xl/worksheets/sheet1.xml")?.async("string") ?? "";
    }).toContain("Browser edited cell");

    const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    expect(download.ok()).toBeTruthy();
    const after = await JSZip.loadAsync(await download.body());
    expect(await after.file(drawingPart!)!.async("string")).toBe(await before.file(drawingPart!)!.async("string"));
    expect(await after.file(relationshipsPart)!.async("string")).toBe(await before.file(relationshipsPart)!.async("string"));
    for (const name of mediaParts) {
      expect(await after.file(name)!.async("base64"), name).toBe(await before.file(name)!.async("base64"));
    }

    await page.reload();
    await expect(page.getByRole("img", { name: "Updated worksheet illustration", exact: true })).toBeVisible();
    await expect(page.locator(".spreadsheet-editor-grid-pane tbody tr").first().locator("td").nth(1)).toContainText("Browser edited cell");
    await page.setViewportSize({ width: 390, height: 844 });
    await picture.scrollIntoViewIfNeeded();
    await expect(picture).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-spreadsheet-picture-mobile.png"), animations: "disabled" });
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
