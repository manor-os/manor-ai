import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));

function formattedPresentation(): Buffer {
  const script = String.raw`
import base64
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

operations = [normalize_file_patch_operation(operation) for operation in [
    {"op": "slide.insert", "index": 0, "layout_index": 6},
    {"op": "slide.format", "slide": 1, "format": {"background_gradient": {
        "angle": 35.5,
        "stops": [
            {"position": 0, "color": "112244", "opacity": 1},
            {"position": 0.55, "color": "336699", "opacity": 0.75},
            {"position": 1, "color": "88CCEE", "opacity": 1},
        ],
    }}},
    {"op": "textbox.insert", "slide": 1, "text": "Editable title",
     "transform": {"x": 72, "y": 72, "width": 360, "height": 80}},
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

test("native PPT slide background survives generation, browser edit, save and reload", async ({ page }) => {
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
          name: `native-slide-format-${Date.now()}.pptx`,
          mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
          buffer: formattedPresentation(),
        },
      },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const frame = page.locator(".presentation-editor-slide-frame");
    await expect(frame).toBeVisible();
    await expect.poll(() => frame.evaluate((element) => getComputedStyle(element).backgroundImage)).toContain("linear-gradient");
    const background = await frame.evaluate((element) => getComputedStyle(element).backgroundImage);
    expect(background).toContain("17, 34, 68");
    expect(background).toContain("51, 102, 153");
    expect(background).toContain("136, 204, 238");

    const title = frame.getByRole("textbox", { name: "Editable title", exact: true });
    await title.click();
    await title.press("F2");
    const input = frame.locator('[contenteditable="true"]');
    await expect(input).toBeFocused();
    await input.fill("Background saved");
    await input.blur();
    await page.getByRole("button", { name: "Save", exact: true }).click();

    let xml = "";
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      xml = await (await JSZip.loadAsync(await download.body())).file("ppt/slides/slide1.xml")!.async("string");
      return xml;
    }).toContain("Background saved");
    expect(xml).toContain("<a:gradFill");
    expect(xml).toContain('ang="2130000"');
    expect(xml).toContain('pos="55000"');
    expect(xml).toContain('val="75000"');

    await page.reload();
    await expect(frame.getByRole("textbox", { name: "Background saved", exact: true })).toBeVisible();
    await expect.poll(() => frame.evaluate((element) => getComputedStyle(element).backgroundImage)).toContain("linear-gradient");
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
