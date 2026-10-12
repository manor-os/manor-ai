/** Isolated upload transport fixture: test files are synthetic and never leave Playwright routing. */
import React, { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api, DocumentUploadStalledError, type DocumentUploadProgress } from "../../src/lib/api";
import Button from "../../src/components/ui/Button";
import { formatFileSize } from "../../src/lib/format";
import "../../src/index.css";

type FixtureStatus = "idle" | "uploading" | "processing" | "success" | "failed" | "cancelled";

function Fixture() {
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<FixtureStatus>("idle");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const controllerRef = useRef<AbortController | null>(null);
  const idempotencyKeyRef = useRef("fixture-upload-request-0001");

  const start = async () => {
    if (!file) return;
    const controller = new AbortController();
    controllerRef.current = controller;
    setStatus("uploading");
    setProgress(0);
    setError("");
    try {
      await api.documents.upload(file, null, undefined, {
        signal: controller.signal,
        stallTimeoutMs: 2_000,
        processingTimeoutMs: 2_000,
        receiptReconcileTimeoutMs: 750,
        idempotencyKey: idempotencyKeyRef.current,
        onProgress: (event: DocumentUploadProgress) => {
          setStatus(event.phase);
          setProgress(event.percent);
        },
      });
      setStatus("success");
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") {
        setStatus("cancelled");
      } else {
        setStatus("failed");
        setError(reason instanceof DocumentUploadStalledError ? "Upload stalled" : (reason as Error).message);
      }
    } finally {
      controllerRef.current = null;
    }
  };

  return (
    <main className="min-h-screen bg-stone-50 p-6 text-stone-800">
      <section className="mx-auto max-w-xl rounded-[var(--radius-panel)] bg-white/70 p-6 shadow-[var(--shadow-md)]">
        <h1 className="m-0 text-xl font-bold">Knowledge upload lifecycle</h1>
        <p className="mt-2 text-sm text-stone-500">Synthetic fixture; requests are intercepted by the test.</p>
        <input
          aria-label="Choose upload file"
          className="mt-5 block w-full text-sm"
          type="file"
          onChange={(event) => setFile(event.target.files?.[0] || null)}
        />
        {file && <p className="mt-3 font-mono text-xs text-stone-500">{file.name} · {formatFileSize(file.size)}</p>}
        <div className="mt-4 h-2 overflow-hidden rounded-full bg-stone-200" aria-label="Upload progress">
          <div className="h-full bg-manor-700 transition-[width]" style={{ width: `${progress}%` }} />
        </div>
        <p role="status" className="mt-3 text-sm font-semibold">{status}</p>
        {error && <p role="alert" className="text-sm text-red-700">{error}</p>}
        <div className="mt-5 flex gap-2">
          <Button onClick={() => void start()} disabled={!file || status === "uploading" || status === "processing"}>
            {status === "failed" ? "Retry" : "Start upload"}
          </Button>
          <Button variant="ghost" onClick={() => controllerRef.current?.abort()} disabled={status !== "uploading"}>
            Cancel
          </Button>
        </div>
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
