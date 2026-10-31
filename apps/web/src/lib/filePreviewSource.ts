import { getFilePreviewKind, type FilePreviewReference } from "./filePreviewKind";
import { fetchFileBlob } from "./fileTransfer";

export interface FilePreviewSourceInput {
  blob?: Blob | null;
  url?: string;
  /** Authorized downloaders own their inactivity timeout, including cache reads. */
  fetchBinary?: (signal: AbortSignal) => Promise<Blob>;
}

/** Bound parsing and renderer waits as well as network loading. */
export function withFilePreviewTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => { if (timer) clearTimeout(timer); });
}

/** A viewer-owned source. Auth stays in the supplied download function. */
export function createFilePreviewSource(input: FilePreviewSourceInput) {
  let loaded: Blob | undefined;
  async function getBlob(signal?: AbortSignal): Promise<Blob> {
    signal?.throwIfAborted();
    if (loaded) return loaded;
    const controller = new AbortController();
    const abort = () => controller.abort(signal?.reason);
    signal?.addEventListener("abort", abort, { once: true });
    let rejectAbort: () => void = () => {};
    try {
      const aborted = new Promise<never>((_, reject) => {
        rejectAbort = () => reject(controller.signal.reason);
        controller.signal.addEventListener("abort", rejectAbort, { once: true });
      });
      const download = async () => {
        if (input.blob) return input.blob;
        if (input.fetchBinary) return input.fetchBinary(controller.signal);
        if (!input.url) throw new Error("File preview source is unavailable");
        return (await fetchFileBlob(input.url, { signal: controller.signal })).blob;
      };
      const blob = await Promise.race([download(), aborted]);
      controller.signal.throwIfAborted();
      if (!blob.size) throw new Error("The file is empty");
      loaded = blob;
      return blob;
    } finally {
      signal?.removeEventListener("abort", abort);
      controller.signal.removeEventListener("abort", rejectAbort);
    }
  }
  return {
    getBlob,
    getText: async (signal?: AbortSignal) => (await getBlob(signal)).text(),
    getArrayBuffer: async (signal?: AbortSignal) => (await getBlob(signal)).arrayBuffer(),
  };
}

/** Download endpoints deliberately use octet-stream; media decoders need a safe MIME. */
export function filePreviewMediaBlob(blob: Blob, file: FilePreviewReference): Blob {
  const kind = getFilePreviewKind(file);
  const extension = (file.name || "").split(".").pop()?.toLowerCase() || "";
  const mediaMime: Record<string, string> = {
    pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
    gif: "image/gif", webp: "image/webp", svg: "image/svg+xml", bmp: "image/bmp", ico: "image/x-icon", avif: "image/avif",
    mp4: "video/mp4", m4v: "video/mp4", webm: "video/webm", mov: "video/quicktime", mkv: "video/x-matroska",
    mp3: "audio/mpeg", wav: "audio/wav", ogg: "audio/ogg", aac: "audio/aac", flac: "audio/flac", m4a: "audio/mp4",
  };
  if (!["pdf", "image", "video", "audio"].includes(kind)) return blob;
  const metadataMime = (file.mime_type || file.mimeType || "").split(";")[0].trim().toLowerCase();
  const mime = mediaMime[kind === "pdf" ? "pdf" : extension]
    || (metadataMime.startsWith(`${kind}/`) ? metadataMime : "")
    || (blob.type.startsWith(`${kind}/`) ? blob.type : "application/octet-stream");
  return mime === blob.type ? blob : new Blob([blob], { type: mime });
}
