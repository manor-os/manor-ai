import { isCodeLikeFile, type CodeFileReference } from "./codeFiles";
import { fileReferenceKind } from "./fileReferences";

export type FilePreviewKind =
  | "text"
  | "markdown"
  | "code"
  | "html"
  | "image"
  | "video"
  | "audio"
  | "pdf"
  | "csv"
  | "json"
  | "diagram"
  | "docx"
  | "xlsx"
  | "pptx"
  | "unsupported";

export type FilePreviewReference = CodeFileReference & {
  name?: string | null;
  file_size?: number | null;
};

export function getFilePreviewKind(file: FilePreviewReference): FilePreviewKind {
  const fileType = String(file.file_type || file.fileType || "")
    .trim()
    .toLowerCase()
    .replace(/^\./, "");
  const persistedKind = fileType ? fileReferenceKind("", undefined, fileType) : "file";
  const ext = persistedKind !== "file" ? fileType : (
    (file.name || "").split(".").pop()?.toLowerCase() || ""
  );
  const mime = persistedKind !== "file" ? "" : (
    (file.mime_type || file.mimeType || "").split(";")[0].trim().toLowerCase()
  );

  if (fileReferenceKind(file.name || "", file.mime_type || file.mimeType || undefined, file.file_type || file.fileType || undefined) === "diagram") return "diagram";
  if (["docx", "doc", "wps"].includes(ext) || ["docx", "doc"].includes(fileType) || mime === "application/vnd.openxmlformats-officedocument.wordprocessingml.document") return "docx";
  if (["xlsx", "xls", "et"].includes(ext) || ["xlsx", "xls"].includes(fileType) || mime === "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") return "xlsx";
  if (["pptx", "ppt", "dps"].includes(ext) || ["pptx", "ppt"].includes(fileType) || mime === "application/vnd.openxmlformats-officedocument.presentationml.presentation") return "pptx";
  if (ext === "pdf" || fileType === "pdf" || mime === "application/pdf") return "pdf";
  if (["md", "markdown"].includes(ext)) return "markdown";
  if (["html", "htm"].includes(ext) || mime === "text/html") return "html";
  if (ext === "json" || mime === "application/json") return "json";
  if (ext === "csv" || mime === "text/csv") return "csv";
  if (persistedKind === "page") return "html";
  if (["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico"].includes(ext) || fileType === "image" || mime.startsWith("image/")) return "image";
  if (["mp4", "webm", "mov", "avi", "mkv"].includes(ext) || fileType === "video" || mime.startsWith("video/")) return "video";
  if (["mp3", "wav", "ogg", "aac", "flac", "m4a"].includes(ext) || fileType === "audio" || mime.startsWith("audio/")) return "audio";
  if (persistedKind === "code" || isCodeLikeFile(file)) return "code";
  if (["txt", "log", "env", "gitignore", "dockerignore", "editorconfig"].includes(ext) || mime.startsWith("text/")) return "text";

  return "unsupported";
}
