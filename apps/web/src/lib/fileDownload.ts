type SaveFileHandle = {
  createWritable: () => Promise<WritableStream<Uint8Array>>;
};

type SaveFileWindow = Window & {
  showSaveFilePicker?: (options: { suggestedName: string }) => Promise<SaveFileHandle>;
};

export type FileDownloadResult = "saved" | "requested" | "cancelled";

export function requestBlobDownload(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.hidden = true;
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    // The browser consumes the URL asynchronously. A click is only a request,
    // not evidence of a completed download (site settings may still block it).
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }
}

export async function saveFileResponse(
  name: string,
  loadResponse: () => Promise<Response>,
  signal?: AbortSignal,
  requestNativeDownload?: () => Promise<void>,
): Promise<FileDownloadResult> {
  const picker = (window as SaveFileWindow).showSaveFilePicker;
  if (!picker) {
    if (requestNativeDownload) {
      await requestNativeDownload();
      return "requested";
    }
    const response = await loadResponse();
    const blob = await response.blob();
    signal?.throwIfAborted();
    requestBlobDownload(blob, name);
    return "requested";
  }

  let handle: SaveFileHandle;
  try {
    // Must precede network I/O to preserve the download button's user gesture.
    // Knowledge names may contain a relative folder path; the picker accepts
    // only a filename, while the UI can retain the original document name.
    handle = await picker.call(window, { suggestedName: name.split(/[\\/]/).pop() || "download" });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return "cancelled";
    throw error;
  }

  signal?.throwIfAborted();
  const response = await loadResponse();
  if (!response.body) throw new Error("Download response has no body");
  let writable: WritableStream<Uint8Array>;
  try {
    writable = await handle.createWritable();
  } catch (error) {
    await response.body.cancel().catch(() => {});
    throw error;
  }
  // pipeTo closes on success and aborts the destination on transfer failure.
  await response.body.pipeTo(writable, { signal });
  return "saved";
}
