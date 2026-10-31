import { expect, test, type Page } from "@playwright/test";
import JSZip from "jszip";
import * as XLSX from "xlsx";

const accountId = "01CB24-ABCDEF0123456789ABCDEF0123456789-END";
const chinese = "这是必须完整显示的单元格文字，不能因为列宽或行高不足而隐藏后面的内容。";
const multiline = "First line\nSecond line\nThird line\nLast line";
const mergedText = "Merged cells must also grow to show every word. ".repeat(8);

async function openWorkbook(page: Page) {
  const rows = Array.from({ length: 10_000 }, (_, row) => [`Row ${row + 1}`, `Value ${row + 1}`, "", ""]);
  rows[0] = ["Account", "Number", "", ""];
  rows[1] = ["Google Billing", accountId, "", ""];
  rows[2][1] = chinese;
  rows[3][1] = multiline;
  rows[4][1] = "Large text must fit too";
  rows[5][0] = mergedText;
  rows[9_999][1] = accountId;
  const worksheet = XLSX.utils.aoa_to_sheet(rows);
  worksheet["!cols"] = Array.from({ length: 4 }, () => ({ wpx: 112 }));
  worksheet["!rows"] = Array.from({ length: rows.length }, () => ({ hpx: 32 }));
  worksheet["!merges"] = [XLSX.utils.decode_range("A6:B7")];
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, "Long text");
  XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([["Short", "Data"], ["Small", "Fits"]]), "Short text");
  const zip = await JSZip.loadAsync(XLSX.write(workbook, { type: "buffer", bookType: "xlsx" }));
  // Include explicit no-wrap and a large bottom-aligned font, as saved by Excel.
  const styles = await zip.file("xl/styles.xml")!.async("string");
  zip.file("xl/styles.xml", styles
    .replace(/<fonts count="1">/, '<fonts count="2">')
    .replace("</fonts>", '<font><sz val="22"/><name val="Calibri"/></font></fonts>')
    .replace(/<cellXfs count="1">/, '<cellXfs count="3">')
    .replace("</cellXfs>", '<xf fontId="0" applyAlignment="1"><alignment wrapText="0"/></xf><xf fontId="1" applyAlignment="1"><alignment vertical="bottom" wrapText="1"/></xf></cellXfs>'));
  const sheetXml = await zip.file("xl/worksheets/sheet1.xml")!.async("string");
  zip.file("xl/worksheets/sheet1.xml", sheetXml.replace('<c r="B2"', '<c r="B2" s="1"').replace('<c r="B5"', '<c r="B5" s="2"'));
  const body = await zip.generateAsync({ type: "nodebuffer" });
  await page.route("**/api/v1/shared-doc/readable-xlsx", (route) => route.fulfill({ json: {
    document_id: "readable-xlsx", name: "IDs.xlsx", capabilities: ["view"],
    watermark: false, allow_download: false, file_type: "xlsx",
    mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", file_size: body.length,
  } }));
  await page.route("**/api/v1/shared-doc/readable-xlsx/content", (route) => route.fulfill({ body }));
  await page.goto("/shared-doc/readable-xlsx");
  await expect(page.getByRole("cell", { name: accountId, exact: true })).toBeVisible();
}

async function clippedCells(page: Page) {
  return page.locator("[data-spreadsheet-preview-cell]").evaluateAll((cells) => cells.flatMap((cell) => {
    const range = document.createRange();
    range.selectNodeContents(cell);
    const bounds = cell.getBoundingClientRect();
    const clipped = [...range.getClientRects()].some((rect) => rect.width > 0 && rect.height > 0 && (
      rect.left < bounds.left - 1 || rect.right > bounds.right + 1
      || rect.top < bounds.top - 1 || rect.bottom > bounds.bottom + 1
    ));
    return clipped ? [cell.textContent] : [];
  }));
}

for (const width of [1280, 390]) {
  test(`spreadsheet text fits its cells at ${width}px without losing virtualization`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await openWorkbook(page);
    await expect.poll(() => clippedCells(page)).toEqual([]);
    const grid = page.locator("[data-spreadsheet-preview-grid]");
    const initialHeight = await page.getByRole("cell", { name: accountId, exact: true }).evaluate((cell) => cell.clientHeight);
    expect(initialHeight).toBeGreaterThan(32);
    expect(await page.locator("[data-spreadsheet-preview-cell]").count()).toBeLessThan(200);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    await page.screenshot({ path: testInfo.outputPath(`text-${width}.png`) });

    await page.getByRole("cell", { name: mergedText.trim(), exact: true }).scrollIntoViewIfNeeded();
    await expect.poll(() => clippedCells(page)).toEqual([]);
    await grid.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await expect(page.getByRole("cell", { name: "Row 10000", exact: true })).toBeVisible();
    await expect.poll(() => clippedCells(page)).toEqual([]);
    expect(await page.locator("[data-spreadsheet-preview-cell]").count()).toBeLessThan(200);

    // Switching sheets and returning must not reuse stale row measurements.
    await page.getByRole("button", { name: "Short text", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("cell", { name: "Fits", exact: true })).toBeVisible();
    expect(await page.getByRole("cell", { name: "Fits", exact: true }).evaluate((cell) => cell.clientHeight)).toBeLessThan(initialHeight);
    await page.getByRole("button", { name: "Long text", exact: true }).click();
    await expect(page.getByRole("cell", { name: accountId, exact: true })).toBeVisible();
    await expect.poll(() => clippedCells(page)).toEqual([]);
    expect(errors).toEqual([]);
  });
}
