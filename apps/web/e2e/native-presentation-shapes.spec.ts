import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test, type Locator } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeShapes(height: number): Buffer {
  const script = `
import base64, sys, tempfile
from pathlib import Path
from tests.test_presentation_shape_operations import generated, shape_operations, patch, shape_format
operations = shape_operations()
operations[1]["format"]["height"] = int(sys.argv[1])
operations.append({"op": "shape.insert", "slide": 1, "preset": "rect", "transform": {"x": 72, "y": 400, "width": 350, "height": 60}, "text": "No wrap", "format": {"fill_color": None, "line_color": None, "word_wrap": False, "shadow": None}})
operations.append({"op": "shape.insert", "slide": 1, "preset": "flowChartTerminator", "transform": {"x": 600, "y": 400, "width": 250, "height": 60}, "text": "End", "format": {"fill_color": "DCEFE9", "line_color": "174C46", "line_width": 2}})
result = generated(operations)
assert result.get("patched"), result
with tempfile.TemporaryDirectory(prefix="manor-shape-browser-") as directory:
    path = Path(directory) / "shapes.pptx"
    path.write_bytes(result["_persisted_bytes"])
    result = patch(path, shape_format(fill_opacity=0.5, line_width=4, corner_radius=24))
    assert result.get("patched"), result
    print(base64.b64encode(result["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`, ["-c", script, String(height)], {
    cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000,
  }).trim(), "base64");
}

async function assertShapeStyles(slide: Locator, height: number, text: string, edited = false) {
  // Editing and presentation share the same direct shape geometry.
  const shape = slide.locator(":scope > div").filter({ hasText: text }).first();
  await expect(shape).toBeVisible();
  await expect(shape).toHaveCSS("background-color", edited ? "rgb(51, 102, 153)" : "rgba(23, 76, 70, 0.5)");
  await expect(shape).toHaveCSS("opacity", "1");
  const rect = shape.locator("svg rect").first();
  await expect(rect).toHaveCSS("stroke", "rgba(216, 155, 69, 0.7)");
  const slideHeight = (await slide.boundingBox())!.height;
  const metrics = await rect.evaluate((element) => {
    const style = getComputedStyle(element);
    return { width: style.strokeWidth, dash: style.strokeDasharray, radius: style.rx };
  });
  expect(parseFloat(metrics.width), JSON.stringify(metrics)).toBeCloseTo((edited ? 5 : 4) * slideHeight / height, 1);
  expect(metrics.dash).not.toBe("none");
  expect(metrics.radius).not.toBe("0px");
  const label = shape.getByText(text, { exact: true }).last();
  await expect(label).toHaveCSS("color", "rgb(255, 255, 255)");
  await expect(label).toHaveCSS("opacity", "1");
  expect(parseFloat(await label.evaluate((element) => getComputedStyle(element).fontSize))).toBeCloseTo(28 * slideHeight / height, 1);
  const line = slide.locator("svg line").last();
  await expect(line).toHaveCSS("stroke", "rgb(23, 76, 70)");
  expect(parseFloat(await line.evaluate((element) => getComputedStyle(element).strokeWidth))).toBeCloseTo(3 * slideHeight / height, 1);
  const unwrapped = slide.getByText("No wrap", { exact: true }).last();
  await expect(unwrapped).toHaveCSS("white-space", "pre");
  const terminator = slide.locator(":scope > div").filter({ hasText: "End" }).first();
  await expect(terminator).toBeVisible();
  const terminatorOutline = terminator.locator("svg rect");
  await expect(terminatorOutline).toHaveCSS("stroke", "rgb(23, 76, 70)");
  expect(parseFloat(await terminatorOutline.evaluate((element) => getComputedStyle(element).rx))).toBeGreaterThan(0);
}

for (const height of [540, 720]) {
  test(`native shapes at 960×${height} survive generation, patch, browser style edit and presentation`, async ({ page }, testInfo) => {
    const api = await pwRequest.newContext({ baseURL: API });
    let documentId = "";
    let token = "";
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      const login = await api.post("/api/v1/auth/login", { data: { email: "demo@manor.local", password: "manor-demo" } });
      expect(login.ok()).toBeTruthy();
      token = (await login.json()).access_token;
      const headers = { Authorization: `Bearer ${token}` };
      const original = nativeShapes(height);
      const before = await JSZip.loadAsync(original);
      const upload = await api.post("/api/v1/documents/upload", { headers, multipart: { file: {
        name: `native-shapes-${Date.now()}.pptx`, mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation", buffer: original,
      } } });
      expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
      documentId = (await upload.json()).id;
      await page.goto("/login");
      await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
      await page.goto(`/editor/${documentId}`);
      const frame = page.locator(".presentation-editor-slide-frame");
      const title = frame.getByRole("textbox", { name: "Native card", exact: true });
      await expect(title).toBeVisible();
      const reject = page.getByRole("button", { name: "Reject all", exact: true });
      if (await reject.isVisible()) await reject.click();
      await assertShapeStyles(frame, height, "Native card");
      await page.screenshot({ path: testInfo.outputPath("native-shapes-desktop.png"), animations: "disabled" });
      await title.click();
      await page.getByLabel("Fill:", { exact: true }).fill("#336699");
      await page.locator('input[type="number"][title="Stroke"]').fill("5");
      await title.press("F2");
      const input = frame.locator('[contenteditable="true"]');
      await expect(input).toBeFocused();
      await input.fill("Browser card");
      await input.blur();
      await page.getByRole("button", { name: "Save", exact: true }).click();
      const part = "ppt/slides/slide1.xml";
      await expect.poll(async () => {
        const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
        if (!download.ok()) return "";
        return (await JSZip.loadAsync(await download.body())).file(part)!.async("string");
      }).toContain("Browser card");
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      const after = await JSZip.loadAsync(await download.body());
      const xml = await after.file(part)!.async("string");
      const beforeXml = await before.file(part)!.async("string");
      const originalCard = beforeXml.match(/<p:sp\b[\s\S]*?<\/p:sp>/)![0];
      const updatedCard = xml.match(/<p:sp\b[\s\S]*?<\/p:sp>/)![0];
      for (const tag of ["prstGeom", "effectLst", "xfrm", "bodyPr"]) {
        const pattern = new RegExp(`<a:${tag}\\b[^>]*?(?:/>|>[\\s\\S]*?</a:${tag}>)`);
        expect(updatedCard.match(pattern)?.[0], tag).toBe(originalCard.match(pattern)?.[0]);
      }
      expect(updatedCard).toContain('w="63500"');
      expect(updatedCard).toContain('<a:prstDash val="dash"/>');
      expect(updatedCard).toContain('<a:alpha val="70000"/>');
      expect(updatedCard).toContain('val="336699"');
      expect(xml.match(/<p:cxnSp\b[\s\S]*?<\/p:cxnSp>/)?.[0]).toBe(beforeXml.match(/<p:cxnSp\b[\s\S]*?<\/p:cxnSp>/)?.[0]);
      expect(xml).toContain('wrap="none"');
      for (const name of Object.keys(before.files).filter((name) => !before.files[name].dir && name.startsWith("ppt/slideMasters/"))) {
        expect(await after.file(name)!.async("base64")).toBe(await before.file(name)!.async("base64"));
      }
      await page.reload();
      await expect(frame.getByRole("textbox", { name: "Browser card", exact: true })).toBeVisible();
      await assertShapeStyles(frame, height, "Browser card", true);
      await page.getByRole("button", { name: "Present", exact: true }).click();
      const present = page.locator(".presentation-editor-present-slide");
      await expect(present).toBeVisible();
      await assertShapeStyles(present, height, "Browser card", true);
      await page.screenshot({ path: testInfo.outputPath("native-shapes-present.png"), animations: "disabled" });
      await page.keyboard.press("Escape");
      await page.setViewportSize({ width: 390, height: 844 });
      await expect(frame.getByRole("textbox", { name: "Browser card", exact: true })).toBeVisible();
      await assertShapeStyles(frame, height, "Browser card", true);
      await page.screenshot({ path: testInfo.outputPath("native-shapes-mobile.png"), animations: "disabled" });
      expect(errors).toEqual([]);
    } finally {
      if (documentId && token) {
        const trashed = await api.post(`/api/v1/documents/${documentId}/trash`, { headers: { Authorization: `Bearer ${token}` } });
        expect(trashed.ok()).toBeTruthy();
      }
      await api.dispose();
    }
  });
}
