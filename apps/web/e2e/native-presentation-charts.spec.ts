import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeChart(): Buffer {
  const script = String.raw`
import base64, io, tempfile
from pathlib import Path
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

operations = [normalize_file_patch_operation(operation) for operation in [
    {"op": "slide.insert", "index": 0, "layout_index": 6},
    {"op": "chart.insert", "slide": 1, "chart_type": "scatter_lines_markers", "categories": [1, 2.5, 4],
     "series": [{"name": "Revenue", "values": [12, 15, 19]}, {"name": "Cost", "values": [8, 9, 11]}],
     "transform": {"x": 72, "y": 90, "width": 620, "height": 310},
     "format": {"title": "Quarterly performance", "style": 10, "legend_position": "bottom",
                "category_axis_title": "Month", "value_axis_title": "USD millions",
                "category_axis_min": 0, "category_axis_max": 7, "category_axis_major_unit": 1,
                "show_data_labels": True, "show_value": True,
                "series_colors": ["336699", "CC6600"],
                "trendlines": [{"series_index": 0, "type": "linear", "name": "Revenue trend",
                                "display_equation": True, "display_r_squared": True}],
                "error_bars": [{"series_index": 1, "type": "percentage", "value": 10,
                                "side": "plus", "end_style": "no_cap"}]}},
    {"op": "chart.data", "slide": 1, "shape_id": 2, "categories": [0.5, 3, 6],
     "series": [{"name": "Revenue", "values": [21, 24, 28]}, {"name": "Cost", "values": [12, 13, 15]}]},
    {"op": "chart.format", "slide": 1, "shape_id": 2,
     "format": {"title": "Updated quarterly performance", "value_axis_max": 35, "value_axis_major_unit": 5}},
    {"op": "slide.insert", "index": 1, "layout_index": 6},
    {"op": "chart.insert", "slide": 2, "chart_type": "combo_column_line",
     "categories": ["Apr", "May", "Jun"],
     "series": [{"name": "Revenue", "values": [21, 24, 28]},
                {"name": "Cost", "values": [12, 13, 15]},
                {"name": "Margin", "values": [9, 11, 13]}],
     "transform": {"x": 72, "y": 90, "width": 620, "height": 310},
     "format": {"title": "Revenue and margin", "legend_position": "bottom",
                "show_data_labels": True, "show_value": True,
                "series_colors": ["174C46", "336699", "CC6600"]}},
    {"op": "chart.data", "slide": 2, "shape_id": 2,
     "categories": ["Jul", "Aug", "Sep"],
     "series": [{"name": "Revenue", "values": [30, 34, 37]},
                {"name": "Cost", "values": [16, 18, 19]},
                {"name": "Margin", "values": [14, 16, 18]}]},
    {"op": "slide.insert", "index": 2, "layout_index": 6},
    {"op": "chart.insert", "slide": 3, "chart_type": "stock_vohlc",
     "categories": ["Apr", "May", "Jun"],
     "series": [{"name": "Volume", "values": [1200, 1800, 1500]},
                {"name": "Open", "values": [14, 17, 20]},
                {"name": "High", "values": [19, 22, 25]},
                {"name": "Low", "values": [10, 12, 14]},
                {"name": "Close", "values": [15, 18, 21]}],
     "transform": {"x": 72, "y": 90, "width": 620, "height": 310},
     "format": {"title": "Volume open high low close", "legend_position": "bottom",
                "value_axis_title": "Volume", "secondary_value_axis_title": "Price",
                "secondary_value_axis_min": 0, "secondary_value_axis_max": 30,
                "secondary_value_axis_major_unit": 5,
                "series_colors": ["A8A29E", "174C46", "336699", "CC6600", "7C3AED"]}},
    {"op": "chart.data", "slide": 3, "shape_id": 2,
     "categories": ["Jul", "Aug", "Sep"],
     "series": [{"name": "Volume", "values": [1900, 1700, 2200]},
                {"name": "Open", "values": [21, 23, 25]},
                {"name": "High", "values": [27, 29, 31]},
                {"name": "Low", "values": [18, 19, 22]},
                {"name": "Close", "values": [24, 22, 28]}]},
]]
result = _generate_office_operations_sync("pptx", operations)
assert result.get("patched"), result
with tempfile.TemporaryDirectory(prefix="manor-chart-browser-") as directory:
    path = Path(directory) / "chart.pptx"
    path.write_bytes(result["_persisted_bytes"])
    patched = _apply_office_patch_sequence_sync(str(path), [normalize_file_patch_operation({
        "op": "chart.format", "slide": 1, "shape_id": 2,
        "format": {"legend_include_in_layout": False, "show_major_gridlines": True},
    })])
    assert patched.get("patched"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

function chartFrameX(xml: string): string {
  const frame = xml.match(/<p:graphicFrame\b[\s\S]*?<\/p:graphicFrame>/)?.[0] ?? "";
  const x = frame.match(/<a:off\b[^>]*\bx="(-?\d+)"/)?.[1];
  if (!x) throw new Error("native chart frame x coordinate was not found");
  return x;
}

test("native chart generation and patch stay editable through browser save and presentation", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
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
    const original = nativeChart();
    const before = await JSZip.loadAsync(original);
    const slidePart = "ppt/slides/slide1.xml";
    const beforeSlide = await before.file(slidePart)!.async("string");
    const beforeX = chartFrameX(beforeSlide);
    const chartParts = Object.keys(before.files).filter((name) => (
      !before.files[name].dir && (name.startsWith("ppt/charts/") || name.startsWith("ppt/embeddings/"))
    ));
    expect(chartParts.length).toBeGreaterThan(1);

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-chart-${Date.now()}.pptx`,
        mimeType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        buffer: original,
      } },
    });
    expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();
    documentId = (await upload.json()).id;

    await page.goto("/login");
    await page.evaluate((value) => localStorage.setItem("manor_token", value), token);
    await page.goto(`/editor/${documentId}`);
    const reject = page.getByRole("button", { name: "Reject all", exact: true });
    if (await reject.isVisible()) await reject.click();
    const frame = page.locator(".presentation-editor-slide-frame");
    const chartShape = frame.locator(':scope > [data-presentation-shape-type="graphic"]');
    await expect(chartShape).toHaveCount(1);
    const preview = chartShape.locator('[role="img"][aria-label="Object"] img');
    await expect(preview).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => preview.evaluate((image) => (image as HTMLImageElement).naturalWidth), {
      timeout: 30_000,
    }).toBeGreaterThan(0);
    await page.getByRole("tab", { name: "Slide 2", exact: true }).click();
    const comboShape = frame.locator(':scope > [data-presentation-shape-type="graphic"]');
    await expect(comboShape).toHaveCount(1);
    const comboPreview = comboShape.locator('[role="img"][aria-label="Object"] img');
    await expect(comboPreview).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => comboPreview.evaluate((image) => (image as HTMLImageElement).naturalWidth), {
      timeout: 30_000,
    }).toBeGreaterThan(0);
    await page.getByRole("tab", { name: "Slide 3", exact: true }).click();
    const stockShape = frame.locator(':scope > [data-presentation-shape-type="graphic"]');
    await expect(stockShape).toHaveCount(1);
    const stockPreview = stockShape.locator('[role="img"][aria-label="Object"] img');
    await expect(stockPreview).toBeVisible({ timeout: 30_000 });
    await expect.poll(() => stockPreview.evaluate((image) => (image as HTMLImageElement).naturalWidth), {
      timeout: 30_000,
    }).toBeGreaterThan(0);
    await page.getByRole("tab", { name: "Slide 1", exact: true }).click();
    await page.screenshot({ path: testInfo.outputPath("native-chart-desktop.png"), animations: "disabled" });

    await chartShape.click({ position: { x: 12, y: 12 } });
    await page.keyboard.press("ArrowRight");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return beforeX;
      return chartFrameX(await (await JSZip.loadAsync(await download.body())).file(slidePart)!.async("string"));
    }).not.toBe(beforeX);

    const downloaded = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    const after = await JSZip.loadAsync(await downloaded.body());
    for (const name of chartParts) {
      expect(await after.file(name)!.async("base64"), name).toBe(await before.file(name)!.async("base64"));
    }
    const chartXmlName = chartParts.find((name) => /^ppt\/charts\/chart\d+\.xml$/.test(name));
    expect(chartXmlName).toBeTruthy();
    const chartXml = await after.file(chartXmlName!)!.async("string");
    expect(chartXml).toContain("Updated quarterly performance");
    expect(chartXml).toContain("Revenue");
    expect(chartXml).toContain("28");
    expect(chartXml).toContain("scatterChart");
    expect(chartXml).toContain("xVal");
    expect(chartXml).toContain("trendline");
    expect(chartXml).toContain("Revenue trend");
    expect(chartXml).toContain("errBars");
    const allChartXml = await Promise.all(chartParts
      .filter((name) => /^ppt\/charts\/chart\d+\.xml$/.test(name))
      .map((name) => after.file(name)!.async("string")));
    const comboXml = allChartXml.find((xml) => xml.includes("barChart") && xml.includes("lineChart"));
    expect(comboXml).toBeTruthy();
    expect(comboXml).toContain("Revenue and margin");
    expect(comboXml).toContain("37");
    const volumeStockXml = allChartXml.find((xml) => xml.includes("barChart") && xml.includes("stockChart"));
    expect(volumeStockXml).toBeTruthy();
    expect(volumeStockXml).toContain("Volume open high low close");
    expect(volumeStockXml).toContain("upDownBars");
    expect(volumeStockXml).toContain("2200");
    expect(volumeStockXml!.match(/<c:valAx>/g)).toHaveLength(2);

    await page.reload();
    const reloaded = frame.locator(':scope > [data-presentation-shape-type="graphic"]');
    await expect(reloaded.locator('[role="img"] img')).toBeVisible({ timeout: 30_000 });
    await page.getByRole("tab", { name: "Slide 2", exact: true }).click();
    await expect(reloaded.locator('[role="img"] img')).toBeVisible({ timeout: 30_000 });
    await page.getByRole("tab", { name: "Slide 3", exact: true }).click();
    await expect(reloaded.locator('[role="img"] img')).toBeVisible({ timeout: 30_000 });
    await page.getByRole("button", { name: "Present", exact: true }).click();
    await expect(page.locator('.presentation-editor-present-slide [role="img"] img')).toBeVisible();
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(reloaded).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-chart-mobile.png"), animations: "disabled" });
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
