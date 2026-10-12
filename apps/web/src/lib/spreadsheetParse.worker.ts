import { spreadsheetSheetsFromFile } from "./spreadsheetOoxml";
import * as XLSX from "xlsx";

self.onmessage = async (event: MessageEvent<{ buffer: ArrayBuffer }>) => {
  try {
    const workbook = XLSX.read(event.data.buffer, {
      type: "array",
      cellFormula: true,
      cellNF: true,
      cellStyles: true,
      cellText: true,
    });
    const sheets = await spreadsheetSheetsFromFile(XLSX, workbook, event.data.buffer);
    self.postMessage({ type: "success", sheets });
  } catch (error: any) {
    self.postMessage({ type: "error", message: error?.message || "Failed to parse spreadsheet" });
  }
};
