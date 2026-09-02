import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { expect, request as pwRequest, test } from "@playwright/test";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const API = process.env.E2E_API ?? "http://localhost:8000";
const repoRoot = fileURLToPath(new URL("../../../", import.meta.url));
test.skip(process.env.E2E_DOCKER_DOC_EDITOR !== "1", "Requires the live document API and local Python test dependencies");

function nativeCharts(): Buffer {
  const script = String.raw`
import base64, tempfile
from pathlib import Path
from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import normalize_file_patch_operation

chart_types = [
    ("column_stacked", "Stacked revenue"), ("bar_stacked_100", "Revenue share"),
    ("line_markers", "Revenue trend"), ("area_stacked", "Cumulative revenue"),
    ("pie", "Revenue mix"), ("doughnut", "Channel mix"),
    ("scatter_smooth_markers", "Response curve"),
    ("combo_column_line", "Revenue and margin"),
    ("stock_hlc", "High low close"), ("stock_ohlc", "Open high low close"),
    ("stock_vhlc", "Volume high low close"),
    ("stock_vohlc", "Volume open high low close"),
]
operations = [
    {"op": "cell.set", "sheet": "Sheet", "cell": "A1", "value": "Native chart dashboard"},
    {"op": "cell.set", "sheet": "Sheet", "cell": "A2", "value": "Browser editable"},
    {"op": "cell.format", "sheet": "Sheet", "cell": "A1", "format": {"bold": True, "font_size": 20, "font_color": "174C46"}},
]
for index, (chart_type, title) in enumerate(chart_types):
    series = [{"name": "Revenue", "values": [12, 15, 19]}, {"name": "Cost", "values": [8, 9, 11]}]
    if chart_type in {"pie", "doughnut"}:
        series = series[:1]
    if chart_type in {"stock_hlc", "stock_vhlc"}:
        series = [{"name": "High", "values": [19, 22, 25]},
                  {"name": "Low", "values": [10, 12, 14]},
                  {"name": "Close", "values": [15, 18, 21]}]
    if chart_type in {"stock_ohlc", "stock_vohlc"}:
        series = [{"name": "Open", "values": [14, 17, 20]},
                  {"name": "High", "values": [19, 22, 25]},
                  {"name": "Low", "values": [10, 12, 14]},
                  {"name": "Close", "values": [15, 18, 21]}]
    if chart_type in {"stock_vhlc", "stock_vohlc"}:
        series.insert(0, {"name": "Volume", "values": [1200, 1800, 1500]})
    categories = [1, 2.5, 4] if chart_type.startswith("scatter") else ["Jan", "Feb", "Mar"]
    palette = ["174C46", "CC6600", "336699", "7C3AED"]
    chart_format = {"title": title, "legend_position": "bottom", "show_data_labels": True,
                    "show_value": True, "series_colors": palette[:len(series)]}
    if chart_type in {"stock_vhlc", "stock_vohlc"}:
        chart_format.update({"value_axis_title": "Volume", "secondary_value_axis_title": "Price",
                             "secondary_value_axis_min": 0, "secondary_value_axis_max": 30,
                             "secondary_value_axis_major_unit": 5})
    if chart_type.startswith("scatter"):
        chart_format.update({
            "category_axis_min": 0, "category_axis_max": 5, "category_axis_major_unit": 1,
            "trendlines": [{"series_index": 0, "type": "linear", "name": "Response trend",
                            "display_equation": True, "display_r_squared": True}],
            "error_bars": [{"series_index": 1, "type": "percentage", "value": 10,
                            "side": "plus", "end_style": "no_cap"}],
        })
    operations.append({
        "op": "chart.insert", "sheet": "Sheet", "chart_type": chart_type,
        "categories": categories, "series": series,
        "anchor": f"F{4 + index * 16}", "transform": {"width": 520, "height": 280},
        "format": chart_format,
    })
created = _generate_office_operations_sync("xlsx", [normalize_file_patch_operation(op) for op in operations])
assert created.get("patched"), created
with tempfile.TemporaryDirectory(prefix="manor-xlsx-chart-browser-") as directory:
    path = Path(directory) / "charts.xlsx"
    path.write_bytes(created["_persisted_bytes"])
    patches = [
        {"op": "chart.data", "sheet": "Sheet", "chart_index": 2,
         "categories": ["Apr", "May", "Jun"],
         "series": [{"name": "Revenue", "values": [21, 24, 28]}, {"name": "Cost", "values": [12, 13, 15]}]},
        {"op": "chart.format", "sheet": "Sheet", "chart_index": 2,
         "format": {"title": "Updated revenue trend", "anchor": "H36"}},
        {"op": "chart.format", "sheet": "Sheet", "chart_index": 5,
         "format": {"category_colors": ["174C46", "CC6600", "336699"]}},
        {"op": "chart.data", "sheet": "Sheet", "chart_index": 11,
         "categories": ["Jul", "Aug", "Sep"],
         "series": [{"name": "Volume", "values": [1900, 1700, 2200]},
                    {"name": "Open", "values": [21, 23, 25]},
                    {"name": "High", "values": [27, 29, 31]},
                    {"name": "Low", "values": [18, 19, 22]},
                    {"name": "Close", "values": [24, 22, 28]}]},
        {"op": "chart.format", "sheet": "Sheet", "chart_index": 11,
         "format": {"title": "Updated volume open high low close",
                    "secondary_value_axis_title": "Price", "secondary_value_axis_max": 40,
                    "secondary_value_axis_major_unit": 5}},
    ]
    patched = _apply_office_patch_sequence_sync(str(path), [normalize_file_patch_operation(op) for op in patches])
    assert patched.get("patched"), patched
    print(base64.b64encode(patched["_persisted_bytes"]).decode())
`;
  return Buffer.from(execFileSync(
    process.env.E2E_PYTHON ?? `${repoRoot}.venv/bin/python`,
    ["-c", script],
    { cwd: repoRoot, env: { ...process.env, PYTHONPATH: repoRoot }, encoding: "utf8", timeout: 30_000 },
  ).trim(), "base64");
}

test("native Excel chart families remain visible and editable through browser save", async ({ page }, testInfo) => {
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
    const original = nativeCharts();
    const before = await JSZip.loadAsync(original);
    const nativeParts = Object.keys(before.files).filter((name) => (
      !before.files[name].dir && (name.startsWith("xl/charts/") || name.startsWith("xl/drawings/"))
    ));
    expect(nativeParts.filter((name) => /^xl\/charts\/chart\d+\.xml$/.test(name))).toHaveLength(12);

    const upload = await api.post("/api/v1/documents/upload", {
      headers,
      multipart: { file: {
        name: `native-spreadsheet-charts-${Date.now()}.xlsx`,
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
    const pane = page.locator(".spreadsheet-editor-chart-pane--native");
    await expect(pane.locator("section")).toHaveCount(12, { timeout: 30_000 });
    for (const title of [
      "Stacked revenue", "Revenue share", "Updated revenue trend",
      "Cumulative revenue", "Revenue mix", "Channel mix", "Response curve",
      "Revenue and margin",
      "High low close", "Open high low close",
      "Volume high low close", "Updated volume open high low close",
    ]) {
      await expect(pane.getByRole("img", { name: title, exact: true })).toBeVisible();
    }
    await page.screenshot({ path: testInfo.outputPath("native-xlsx-charts-desktop.png"), animations: "disabled", fullPage: true });

    const main = page.locator(".manor-editor-main");
    await main.getByText("Native chart dashboard", { exact: true }).click();
    const cellEditor = main.locator("textarea").first();
    await expect(cellEditor).toHaveValue("Native chart dashboard");
    await cellEditor.fill("Browser saved dashboard");
    await cellEditor.blur();
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect.poll(async () => {
      const download = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
      if (!download.ok()) return "";
      const workbook = XLSX.read(await download.body(), { type: "buffer" });
      return String(workbook.Sheets.Sheet.A1.v || "");
    }).toBe("Browser saved dashboard");

    const downloaded = await api.get(`/api/v1/documents/${documentId}/download`, { headers });
    const bytes = await downloaded.body();
    const after = await JSZip.loadAsync(bytes);
    for (const name of nativeParts) {
      expect(await after.file(name)!.async("base64"), name).toBe(await before.file(name)!.async("base64"));
    }
    const workbook = XLSX.read(bytes, { type: "buffer", cellFormula: true });
    expect(workbook.Workbook?.Sheets?.find((sheet) => sheet.name === "_manor_chart_data")?.Hidden).toBe(2);
    expect(workbook.Sheets._manor_chart_data.A1.v).toBe("__MANOR_NATIVE_CHART_DATA_V1__");
    expect(workbook.Sheets._manor_chart_data.B13.v).toBe("Revenue");
    expect(workbook.Sheets._manor_chart_data.B16.v).toBe(28);
    expect(workbook.Sheets._manor_chart_data.B61.v).toBe(2200);
    const chartXml = await after.file("xl/charts/chart3.xml")!.async("string");
    expect(chartXml).toContain("Updated revenue trend");
    expect(chartXml).toContain("'_manor_chart_data'!$B$14:$B$16");
    const scatterXml = await after.file("xl/charts/chart7.xml")!.async("string");
    expect(scatterXml).toContain("scatterChart");
    expect(scatterXml).toContain("smoothMarker");
    expect(scatterXml).toContain("xVal");
    expect(scatterXml).toContain("trendline");
    expect(scatterXml).toContain("Response trend");
    expect(scatterXml).toContain("errBars");
    const comboXml = await after.file("xl/charts/chart8.xml")!.async("string");
    expect(comboXml).toContain("barChart");
    expect(comboXml).toContain("lineChart");
    const comboChart = pane.getByRole("img", { name: "Revenue and margin", exact: true });
    await expect(comboChart.locator('[data-chart-plot-type="column"]')).not.toHaveCount(0);
    await expect(comboChart.locator('[data-chart-plot-type="line"]')).not.toHaveCount(0);
    const hlcXml = await after.file("xl/charts/chart9.xml")!.async("string");
    const ohlcXml = await after.file("xl/charts/chart10.xml")!.async("string");
    const vhlcXml = await after.file("xl/charts/chart11.xml")!.async("string");
    const vohlcXml = await after.file("xl/charts/chart12.xml")!.async("string");
    expect(hlcXml).toContain("stockChart");
    expect(hlcXml).toContain("hiLowLines");
    expect(hlcXml).not.toContain("upDownBars");
    expect(ohlcXml).toContain("stockChart");
    expect(ohlcXml).toContain("upDownBars");
    expect(vhlcXml).toContain("barChart");
    expect(vhlcXml).toContain("stockChart");
    expect(vhlcXml).toContain("hiLowLines");
    expect(vhlcXml).not.toContain("upDownBars");
    expect(vhlcXml.match(/<valAx>/g)).toHaveLength(2);
    expect(vohlcXml).toContain("barChart");
    expect(vohlcXml).toContain("stockChart");
    expect(vohlcXml).toContain("upDownBars");
    expect(vohlcXml).toContain("Updated volume open high low close");
    expect(vohlcXml).toContain("'_manor_chart_data'!$B$59:$B$61");
    expect(vohlcXml).toContain('<max val="40"');
    expect(vohlcXml.match(/<valAx>/g)).toHaveLength(2);
    await expect(pane.getByRole("img", { name: "High low close", exact: true })
      .locator('[data-chart-plot-type="stock"]')).toHaveCount(3);
    await expect(pane.getByRole("img", { name: "Open high low close", exact: true })
      .locator('[data-chart-plot-type="stock"]')).toHaveCount(3);
    await expect(pane.getByRole("img", { name: "Volume high low close", exact: true })
      .locator('[data-chart-plot-type="volume"]')).toHaveCount(3);
    await expect(pane.getByRole("img", { name: "Volume high low close", exact: true })
      .locator('[data-chart-plot-type="stock"]')).toHaveCount(3);
    await expect(pane.getByRole("img", { name: "Updated volume open high low close", exact: true })
      .locator('[data-chart-plot-type="volume"]')).toHaveCount(3);
    await expect(pane.getByRole("img", { name: "Updated volume open high low close", exact: true })
      .locator('[data-chart-plot-type="stock"]')).toHaveCount(3);

    await page.reload();
    await expect(main.getByText("Browser saved dashboard", { exact: true })).toBeVisible();
    await expect(pane.locator("section")).toHaveCount(12);
    const collapse = page.getByRole("button", { name: "Collapse sidebar", exact: true });
    if (await collapse.isVisible()) await collapse.click();
    await page.setViewportSize({ width: 390, height: 844 });
    await pane.getByRole("img", { name: "Updated revenue trend", exact: true }).scrollIntoViewIfNeeded();
    await expect(pane.getByRole("img", { name: "Updated revenue trend", exact: true })).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("native-xlsx-charts-mobile.png"), animations: "disabled" });
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
