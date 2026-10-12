import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import * as XLSX from "xlsx";
import { PDFDocument, StandardFonts, rgb } from "pdf-lib";
import { PdfJsViewer, XlsxViewer } from "../../src/pages/FileViewer";
import "../../src/index.css";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

type FixtureFiles = { pdf: Blob; pdfUrl: string; workbook: Blob; workbookUrl: string; smallWorkbook: Blob; smallWorkbookUrl: string };

async function createFixtureFiles(): Promise<FixtureFiles> {
  const pdf = await PDFDocument.create();
  const font = await pdf.embedFont(StandardFonts.HelveticaBold);
  const pageSizes: Array<[number, number]> = [[612, 792], [792, 612], [595, 842], [842, 595]];
  for (let index = 0; index < 40; index += 1) {
    const [width, height] = pageSizes[index % pageSizes.length];
    const page = pdf.addPage([width, height]);
    page.drawRectangle({ x: 0, y: 0, width, height, color: rgb(0.98, 0.97, 0.94) });
    page.drawText(`Mixed-size page ${index + 1}`, { x: 42, y: height - 72, size: 22, font, color: rgb(0.12, 0.26, 0.3) });
  }
  const pdfBlob = new Blob([await pdf.save()], { type: "application/pdf" });

  const rows = Array.from({ length: 10_000 }, (_, row) => Array.from({ length: 12 }, (_, column) => (
    column === 0 ? `Row ${row + 1}` : column === 11 ? row + 1 : `R${row + 1}C${column + 1}`
  )));
  const worksheet = XLSX.utils.aoa_to_sheet(rows);
  worksheet["L2"] = { t: "n", f: "SUM(L3:L10000)", v: 50_004_999 };
  worksheet["!merges"] = [XLSX.utils.decode_range("A1:A10000")];
  worksheet["!cols"] = Array.from({ length: 12 }, (_, index) => ({ wch: index === 0 ? 24 : 15 }));
  worksheet["!rows"] = Array.from({ length: 10_000 }, (_, index) => ({ hpt: index % 11 === 0 ? 28 : 18 }));
  const workbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(workbook, worksheet, "10k rows");
  const workbookBytes = XLSX.write(workbook, { type: "array", bookType: "xlsx", cellStyles: true });
  const workbookBlob = new Blob([workbookBytes], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
  const smallWorkbook = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(smallWorkbook, XLSX.utils.aoa_to_sheet(
    Array.from({ length: 60 }, (_, row) => [`Small row ${row + 1}`, row + 1, `Value ${row + 1}`]),
  ), "60 rows");
  const smallWorkbookBlob = new Blob([XLSX.write(smallWorkbook, { type: "array", bookType: "xlsx" })], { type: workbookBlob.type });

  return {
    pdf: pdfBlob,
    pdfUrl: URL.createObjectURL(pdfBlob),
    workbook: workbookBlob,
    workbookUrl: URL.createObjectURL(workbookBlob),
    smallWorkbook: smallWorkbookBlob,
    smallWorkbookUrl: URL.createObjectURL(smallWorkbookBlob),
  };
}

function scrollPreview(kind: "pdf" | "xlsx", target: "middle" | "end" | "right") {
  const element = document.querySelector<HTMLElement>(kind === "pdf" ? ".pdf-editor-pages" : "[data-spreadsheet-preview-grid]");
  if (!element) return;
  element.scrollTo(target === "right"
    ? { left: element.scrollWidth, behavior: "instant" as ScrollBehavior }
    : { top: target === "end" ? element.scrollHeight : element.scrollHeight / 2, behavior: "instant" as ScrollBehavior });
}

function PreviewPerformanceFixture() {
  const [files, setFiles] = useState<FixtureFiles | null>(null);
  const [kind, setKind] = useState<"pdf" | "xlsx" | "xlsx-small">("pdf");
  const [metrics, setMetrics] = useState({ canvases: 0, pixels: 0, cells: 0 });

  useEffect(() => {
    let cancelled = false;
    void createFixtureFiles().then((next) => {
      if (cancelled) {
        URL.revokeObjectURL(next.pdfUrl);
        URL.revokeObjectURL(next.workbookUrl);
        URL.revokeObjectURL(next.smallWorkbookUrl);
      } else setFiles(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => () => {
    if (files) {
      URL.revokeObjectURL(files.pdfUrl);
      URL.revokeObjectURL(files.workbookUrl);
      URL.revokeObjectURL(files.smallWorkbookUrl);
    }
  }, [files]);

  useEffect(() => {
    const update = () => {
      const canvases = [...document.querySelectorAll<HTMLCanvasElement>(".pdf-editor-pages canvas")];
      setMetrics({
        canvases: canvases.length,
        pixels: canvases.reduce((total, canvas) => total + canvas.width * canvas.height, 0),
        cells: document.querySelectorAll("[data-spreadsheet-preview-cell]").length,
      });
    };
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true });
    return () => observer.disconnect();
  }, [kind, files]);

  return (
    <main style={{ maxWidth: 1180, margin: "0 auto", padding: 20, fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ fontSize: 20 }}>Local preview performance fixture</h1>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <button type="button" onClick={() => setKind("pdf")}>40-page PDF</button>
        <button type="button" onClick={() => setKind("xlsx")}>10k-row workbook</button>
        <button type="button" onClick={() => setKind("xlsx-small")}>60-row workbook</button>
        <button type="button" onClick={() => scrollPreview(kind === "pdf" ? "pdf" : "xlsx", "middle")}>Scroll to middle</button>
        <button type="button" onClick={() => scrollPreview(kind === "pdf" ? "pdf" : "xlsx", "end")}>Scroll to end</button>
        <button type="button" onClick={() => scrollPreview(kind === "pdf" ? "pdf" : "xlsx", "right")}>Scroll right</button>
      </div>
      <output data-preview-performance-metrics>{`canvases=${metrics.canvases} pixels=${metrics.pixels} cells=${metrics.cells}`}</output>
      <section style={{ height: kind === "pdf" ? "78vh" : undefined, marginTop: 12, border: "1px solid #d6d3d1", overflow: kind === "pdf" ? "hidden" : undefined }}>
        {!files ? <p style={{ padding: 24 }}>Building local fixtures...</p> : kind === "pdf" ? (
          <PdfJsViewer url={files.pdfUrl} blob={files.pdf} docName="mixed-40-pages.pdf" onDownload={() => undefined} canEdit={false} />
        ) : <XlsxViewer url={kind === "xlsx-small" ? files.smallWorkbookUrl : files.workbookUrl} blob={kind === "xlsx-small" ? files.smallWorkbook : files.workbook} />}
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode><QueryClientProvider client={queryClient}><MemoryRouter><PreviewPerformanceFixture /></MemoryRouter></QueryClientProvider></StrictMode>,
);
