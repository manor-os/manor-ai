import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));

function reorderedPresentation(): Buffer {
  const script = String.raw`
import base64
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

operations = [normalize_file_patch_operation(operation) for operation in [
    {"op": "slide.insert", "index": 0, "layout_index": 6},
    {"op": "shape.insert", "slide": 1, "preset": "rect",
     "transform": {"x": 72, "y": 72, "width": 260, "height": 120}, "text": "Back"},
    {"op": "shape.insert", "slide": 1, "preset": "ellipse",
     "transform": {"x": 72, "y": 72, "width": 260, "height": 120}, "text": "Middle"},
    {"op": "shape.insert", "slide": 1, "preset": "roundRect",
     "transform": {"x": 72, "y": 72, "width": 260, "height": 120}, "text": "Front"},
    {"op": "shape.reorder", "slide": 1, "shape_id": 2, "z_index": 2},
]]
result = _generate_office_operations_sync("pptx", operations)
assert not result.get("error"), result
print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

test("native PPT z-order survives generation, browser edit, save and reload", async ({ page }) => {
  test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python dependencies");
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
    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: {
        file: {
          name: `native-z-order-${Date.now()}.pptx`,
          mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          buffer: reorderedPresentation(),
        },
      },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const frame = page.locator(".presentation-editor-slide-frame");
    const objectOrder = async () => frame.getByRole("textbox").evaluateAll((elements) => (
      elements.map((element) => element.getAttribute("aria-label"))
        .filter((label) => ["Middle", "Front", "Back", "Browser front"].includes(label || ""))
    ));
    await expect.poll(objectOrder).toEqual(["Middle", "Front", "Back"]);

    const back = frame.getByRole("textbox", { name: "Back", exact: true });
    await back.click();
    await back.press("F2");
    const input = frame.locator('[contenteditable="true"]');
    await expect(input).toBeFocused();
    await input.fill("Browser front");
    await input.blur();
    await page.getByRole("button", { name: "Save", exact: true }).click();

    const part = "ppt/slides/slide1.xml";
    let xml = "";
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      xml = await (await JSZip.loadAsync(await download.body())).file(part)!.async("string");
      return xml;
    }).toContain("Browser front");
    expect(xml.indexOf('id="3"')).toBeLessThan(xml.indexOf('id="4"'));
    expect(xml.indexOf('id="4"')).toBeLessThan(xml.indexOf('id="2"'));

    await page.reload();
    await expect.poll(objectOrder).toEqual(["Middle", "Front", "Browser front"]);
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
