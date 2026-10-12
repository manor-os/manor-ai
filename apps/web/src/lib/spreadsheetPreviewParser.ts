import type { SpreadsheetSheetModel } from "./spreadsheetOoxml";
import { spreadsheetSheetsFromFile } from "./spreadsheetOoxml";

type WorkerResult = { type: "success"; sheets: SpreadsheetSheetModel[] } | { type: "error"; message: string };

async function parseSpreadsheetPreviewOnMainThread(buffer: ArrayBuffer): Promise<SpreadsheetSheetModel[]> {
  const XLSX = await import("xlsx");
  const workbook = XLSX.read(buffer, {
    type: "array",
    cellFormula: true,
    cellNF: true,
    cellStyles: true,
    cellText: true,
  });
  return spreadsheetSheetsFromFile(XLSX, workbook, buffer);
}

function spreadsheetPreviewAbortError(): DOMException {
  return new DOMException("Spreadsheet preview aborted", "AbortError");
}

async function parseSpreadsheetPreviewFallback(
  buffer: ArrayBuffer,
  signal?: AbortSignal,
): Promise<SpreadsheetSheetModel[]> {
  if (signal?.aborted) throw spreadsheetPreviewAbortError();
  const sheets = await parseSpreadsheetPreviewOnMainThread(buffer);
  if (signal?.aborted) throw spreadsheetPreviewAbortError();
  return sheets;
}

export async function parseSpreadsheetPreview(
  buffer: ArrayBuffer,
  signal?: AbortSignal,
): Promise<SpreadsheetSheetModel[]> {
  if (signal?.aborted) throw spreadsheetPreviewAbortError();
  let worker: Worker;
  try {
    worker = new Worker(new URL("./spreadsheetParse.worker.ts", import.meta.url), { type: "module" });
  } catch {
    return parseSpreadsheetPreviewFallback(buffer, signal);
  }
  return new Promise((resolve, reject) => {
    let settled = false;
    let workerFinished = false;
    let fallbackStarted = false;
    const finishWorker = () => {
      if (workerFinished) return;
      workerFinished = true;
      worker.onmessage = null;
      worker.onerror = null;
      worker.onmessageerror = null;
      worker.terminate();
    };
    const settle = (complete: () => void) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      finishWorker();
      complete();
    };
    const abort = () => {
      settle(() => reject(spreadsheetPreviewAbortError()));
    };
    const fallback = () => {
      if (settled || fallbackStarted) return;
      fallbackStarted = true;
      finishWorker();
      void parseSpreadsheetPreviewFallback(buffer, signal).then(
        (sheets) => settle(() => resolve(sheets)),
        (error) => settle(() => reject(error)),
      );
    };
    worker.onmessage = (event: MessageEvent<WorkerResult>) => {
      const result = event.data;
      if (result?.type === "success") settle(() => resolve(result.sheets));
      else fallback();
    };
    worker.onerror = () => {
      fallback();
    };
    worker.onmessageerror = () => {
      fallback();
    };
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) {
      abort();
      return;
    }
    try {
      const workerBuffer = buffer.slice(0);
      worker.postMessage({ buffer: workerBuffer }, [workerBuffer]);
    } catch {
      fallback();
    }
  });
}

export { parseSpreadsheetPreviewOnMainThread };
