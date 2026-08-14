import { api } from "./api";
import { getAuthToken } from "./authToken";
import { VideoGenerationMode, type Document } from "./types";

export type InsertableMediaKind = "image" | "video";

export type InsertableMediaAsset = {
  kind: InsertableMediaKind;
  name: string;
  source: "upload" | "knowledge" | "online" | "generated";
  document: Document;
  sourceUrl?: string;
  sourcePageUrl?: string;
  attribution?: string;
};

export type OnlineMediaResult = {
  id: string;
  kind: InsertableMediaKind;
  name: string;
  url: string;
  sourcePageUrl?: string;
  attribution?: string;
};

type MediaCandidate = {
  url?: string;
  documentId?: string;
  jobId?: string;
  name?: string;
};

const IMAGE_EXTENSIONS = new Set(["avif", "bmp", "gif", "heic", "heif", "jpeg", "jpg", "png", "svg", "webp"]);
const VIDEO_EXTENSIONS = new Set(["m4v", "mkv", "mov", "mp4", "mpeg", "mpg", "webm"]);

function extensionForName(value: string) {
  return value.split(/[?#]/, 1)[0].split(".").pop()?.toLowerCase() || "";
}

export function documentInsertableMediaKind(document: Pick<Document, "name" | "file_type" | "mime_type">): InsertableMediaKind | null {
  const mime = String(document.mime_type || "").toLowerCase();
  const fileType = String(document.file_type || "").toLowerCase();
  const extension = extensionForName(document.name || "");
  if (mime.startsWith("image/") || fileType === "image" || IMAGE_EXTENSIONS.has(extension)) return "image";
  if (mime.startsWith("video/") || fileType === "video" || VIDEO_EXTENSIONS.has(extension)) return "video";
  return null;
}

function parseJson(value: unknown): unknown {
  if (typeof value !== "string") return value;
  const trimmed = value.trim();
  if (!trimmed || (!trimmed.startsWith("{") && !trimmed.startsWith("["))) return value;
  try {
    return JSON.parse(trimmed);
  } catch {
    return value;
  }
}

function firstString(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return undefined;
}

function collectMediaCandidates(value: unknown, output: MediaCandidate[], depth = 0) {
  if (depth > 5 || value == null) return;
  const parsed = parseJson(value);
  if (parsed !== value) {
    collectMediaCandidates(parsed, output, depth + 1);
    return;
  }
  if (Array.isArray(parsed)) {
    parsed.forEach((item) => collectMediaCandidates(item, output, depth + 1));
    return;
  }
  if (typeof parsed !== "object") return;

  const record = parsed as Record<string, unknown>;
  const params = record.params && typeof record.params === "object"
    ? record.params as Record<string, unknown>
    : undefined;
  const documentRecord = record.document && typeof record.document === "object"
    ? record.document as Record<string, unknown>
    : undefined;
  const candidate: MediaCandidate = {
    url: firstString(record, ["result_url", "image_url", "video_url", "media_url", "file_url", "download_url", "url"]),
    documentId: firstString(record, ["result_document_id", "document_id", "documentId", "doc_id"])
      || (documentRecord ? firstString(documentRecord, ["id", "document_id"]) : undefined)
      || (params ? firstString(params, ["result_document_id", "document_id"]) : undefined),
    jobId: firstString(record, ["job_id", "media_job_id"]),
    name: firstString(record, ["name", "filename", "title"])
      || (documentRecord ? firstString(documentRecord, ["name", "filename", "title"]) : undefined),
  };
  if (candidate.url || candidate.documentId || candidate.jobId) output.push(candidate);

  for (const key of ["result", "output", "artifact", "artifacts", "files", "images", "videos", "attachments", "data", "document", "params"]) {
    if (record[key] != null) collectMediaCandidates(record[key], output, depth + 1);
  }
}

async function readChatMediaStream(response: Response, signal?: AbortSignal) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("The media generation stream is unavailable.");
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  const candidates: MediaCandidate[] = [];
  const cancel = () => { void reader.cancel(); };
  signal?.addEventListener("abort", cancel, { once: true });

  try {
    while (true) {
      if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6).trim();
        if (!data || data === "[DONE]") continue;
        try {
          const parsed = JSON.parse(data) as Record<string, unknown>;
          const token = parsed.text_delta ?? parsed.token ?? parsed.content;
          if (typeof token === "string") text += token;
          collectMediaCandidates(parsed, candidates);
        } catch {
          // A malformed progress frame must not discard a completed artifact.
        }
      }
    }
  } finally {
    signal?.removeEventListener("abort", cancel);
  }
  return { candidates, text };
}

async function pollMediaJob(jobId: string, signal?: AbortSignal): Promise<MediaCandidate> {
  const token = getAuthToken();
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    const response = await fetch(`/api/v1/media/jobs/${encodeURIComponent(jobId)}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!response.ok) throw new Error("Unable to read the video generation status.");
    const job = await response.json() as Record<string, unknown>;
    const candidates: MediaCandidate[] = [];
    collectMediaCandidates(job, candidates);
    const candidate = candidates.find((item) => item.documentId || item.url) || { jobId };
    const status = String(job.status || "").toLowerCase();
    if (status === "completed") return candidate;
    if (status === "failed") throw new Error(String(job.error || "Video generation failed."));
    await new Promise<void>((resolve, reject) => {
      const finish = () => {
        signal?.removeEventListener("abort", abort);
        resolve();
      };
      const timeout = window.setTimeout(finish, 3000);
      const abort = () => {
        window.clearTimeout(timeout);
        signal?.removeEventListener("abort", abort);
        reject(new DOMException("Cancelled", "AbortError"));
      };
      signal?.addEventListener("abort", abort, { once: true });
    });
  }
  throw new Error("Video generation is still running. It will remain available in Knowledge when complete.");
}

async function resolveGeneratedDocument(kind: InsertableMediaKind, candidates: MediaCandidate[], startedAt: number) {
  const directId = [...candidates].reverse().find((candidate) => candidate.documentId)?.documentId;
  if (directId) return api.documents.get(directId);

  const recent = await api.documents.list({ include_generated_assets: true, limit: 80 });
  const matching = recent.items
    .filter((document) => documentInsertableMediaKind(document) === kind)
    .filter((document) => !document.created_at || new Date(document.created_at).getTime() >= startedAt - 5000)
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  return matching[0] || null;
}

export async function generateInsertableMedia(
  kind: InsertableMediaKind,
  prompt: string,
  signal?: AbortSignal,
): Promise<InsertableMediaAsset> {
  const startedAt = Date.now();
  const response = await api.chat.stream(prompt.trim(), undefined, {
    chatMode: kind,
    chatModePayload: kind === "image"
      ? {
        task: "generate",
        aspect_ratio: "auto",
        resolution: "2k",
        reference_policy: "smart_references",
        text_policy: "avoid_text",
        model: "image_5_lite",
      }
      : {
        generation_mode: VideoGenerationMode.AUTO,
        aspect_ratio: "16:9",
        resolution: "720p",
        clip_duration_seconds: 5,
        audio_policy: "native_if_supported",
        generate_audio: true,
      },
    ephemeral: true,
  });
  const stream = await readChatMediaStream(response, signal);
  const candidates = [...stream.candidates];
  const jobId = [...candidates].reverse().find((candidate) => candidate.jobId)?.jobId;
  if (jobId) candidates.push(await pollMediaJob(jobId, signal));
  const document = await resolveGeneratedDocument(kind, candidates, startedAt);
  if (!document) throw new Error(`The ${kind} was generated but its Knowledge file could not be resolved yet.`);
  return {
    kind,
    name: document.name,
    source: "generated",
    document,
  };
}

function cleanUrl(value: string) {
  return value.trim().replace(/^[<(\[]+/, "").replace(/[>),\].;]+$/, "");
}

function mediaKindForUrl(url: string, requestedKind: InsertableMediaKind) {
  const extension = extensionForName(url);
  if (IMAGE_EXTENSIONS.has(extension)) return "image" as const;
  if (VIDEO_EXTENSIONS.has(extension)) return "video" as const;
  return requestedKind;
}

export async function searchOnlineInsertableMedia(
  kind: InsertableMediaKind,
  query: string,
  signal?: AbortSignal,
): Promise<OnlineMediaResult[]> {
  const prompt = [
    `Search the public web for reusable ${kind} media matching: ${query.trim()}`,
    "Use the existing web_search and web_fetch tools. Prefer Creative Commons, public-domain, official press-kit, or clearly reusable sources.",
    "Return up to 8 usable direct media URLs. Use exactly one result per line in this format:",
    "MEDIA_URL: https://... | TITLE: concise title | SOURCE: https://source-page... | ATTRIBUTION: license and creator",
    "Do not invent URLs. A page URL is not a MEDIA_URL.",
  ].join("\n");
  const response = await api.chat.stream(prompt, undefined, {
    chatMode: "research",
    chatModePayload: { depth: "source_backed", format: "brief", source_policy: "web_and_references" },
    ephemeral: true,
  });
  const { text } = await readChatMediaStream(response, signal);
  const results: OnlineMediaResult[] = [];
  for (const line of text.split("\n")) {
    const mediaMatch = line.match(/MEDIA_URL:\s*(https?:\/\/[^\s|]+)/i);
    if (!mediaMatch) continue;
    const url = cleanUrl(mediaMatch[1]);
    const title = line.match(/TITLE:\s*([^|]+)/i)?.[1]?.trim() || `${kind} result`;
    const sourcePageUrl = line.match(/SOURCE:\s*(https?:\/\/[^\s|]+)/i)?.[1];
    const attribution = line.match(/ATTRIBUTION:\s*(.+)$/i)?.[1]?.trim();
    results.push({
      id: `${url}-${results.length}`,
      kind: mediaKindForUrl(url, kind),
      name: title,
      url,
      sourcePageUrl: sourcePageUrl ? cleanUrl(sourcePageUrl) : undefined,
      attribution,
    });
  }
  return results.filter((result, index, all) => all.findIndex((candidate) => candidate.url === result.url) === index);
}

export async function importOnlineMedia(result: OnlineMediaResult): Promise<InsertableMediaAsset> {
  const document = await api.documents.createFromUrl({ url: result.url, name: result.name });
  const resolvedKind = documentInsertableMediaKind(document);
  if (!resolvedKind) throw new Error("The imported URL did not resolve to an image or video file.");
  return {
    kind: resolvedKind,
    name: document.name,
    source: "online",
    document,
    sourceUrl: result.url,
    sourcePageUrl: result.sourcePageUrl,
    attribution: result.attribution,
  };
}

export async function uploadInsertableMedia(file: File): Promise<InsertableMediaAsset> {
  const kind = documentInsertableMediaKind({ name: file.name, mime_type: file.type, file_type: "" });
  if (!kind) throw new Error("Choose an image or video file.");
  const document = await api.documents.upload(file);
  return { kind, name: document.name, source: "upload", document };
}

export function knowledgeInsertableMedia(document: Document): InsertableMediaAsset | null {
  const kind = documentInsertableMediaKind(document);
  if (!kind) return null;
  return { kind, name: document.name, source: "knowledge", document };
}
