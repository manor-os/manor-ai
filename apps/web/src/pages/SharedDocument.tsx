/**
 * SharedDocument — public anonymous viewer for a single-document share link.
 *
 * URL: /shared-doc/:token  (matches backend's
 *      /api/v1/shared-doc/{token} URL returned by createShare)
 *
 * No auth required. The token itself is the entitlement; backend verifies
 * its sha256 hash, expiry, max_uses, and revocation state.
 *
 * Renders the actual content inline when the file type supports it:
 *   - markdown  → react-markdown (GFM)
 *   - text/code → monospace <pre>
 *   - pdf       → <iframe>
 *   - image     → <img>
 *   - other     → "preview not available" + download button (if allowed)
 *
 * Content bytes come from /api/v1/shared-doc/{token}/content (inline
 * disposition). The `view` capability — present on every share — is what
 * gates this; download is a separate capability for saving the original.
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ClassificationBadge } from "../components/permissions";
import { IconDocument, IconDownload } from "../components/icons";
import { t } from "../lib/i18n";

interface SharedDocResponse {
  document_id: string;
  name: string;
  classification?: string;
  capabilities: string[];
  watermark: boolean;
  allow_download: boolean;
  expires_at?: string;
  file_type?: string | null;
  mime_type?: string | null;
  file_size?: number | null;
}

type RenderMode = "markdown" | "text" | "pdf" | "image" | "video" | "audio" | "unsupported";

const MARKDOWN_EXT = new Set(["md", "markdown", "mdx"]);
const TEXT_EXT = new Set([
  "txt", "log", "csv", "tsv", "json", "yaml", "yml", "xml", "html", "htm",
  "js", "ts", "tsx", "jsx", "py", "go", "rs", "java", "c", "cpp", "h", "sh",
  "sql", "toml", "ini", "env", "css", "scss",
]);
const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"]);
const VIDEO_EXT = new Set(["mp4", "webm", "ogv", "mov", "m4v"]);
const AUDIO_EXT = new Set(["mp3", "wav", "ogg", "m4a", "aac", "flac"]);

function _ext(name: string, fileType?: string | null): string {
  if (fileType) return fileType.toLowerCase().replace(/^\./, "");
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function _renderMode(data: SharedDocResponse): RenderMode {
  const ext = _ext(data.name, data.file_type);
  const mime = (data.mime_type || "").toLowerCase();
  if (MARKDOWN_EXT.has(ext)) return "markdown";
  if (ext === "pdf" || mime === "application/pdf") return "pdf";
  if (IMAGE_EXT.has(ext) || mime.startsWith("image/")) return "image";
  if (VIDEO_EXT.has(ext) || mime.startsWith("video/")) return "video";
  if (AUDIO_EXT.has(ext) || mime.startsWith("audio/")) return "audio";
  if (TEXT_EXT.has(ext) || mime.startsWith("text/")) return "text";
  return "unsupported";
}

export default function SharedDocument() {
  const { token } = useParams<{ token: string }>();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "ok"; data: SharedDocResponse }
    | { kind: "error"; status: number; message: string }
  >({ kind: "loading" });
  // Fetched text content for markdown/text render modes.
  const [textContent, setTextContent] = useState<string | null>(null);
  const [textError, setTextError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    (async () => {
      try {
        const res = await fetch(
          `/api/v1/shared-doc/${encodeURIComponent(token)}`,
          { headers: { Accept: "application/json" } },
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          const detail = body?.detail;
          let message: string;
          if (typeof detail === "object" && detail !== null && typeof (detail as any).code === "string") {
            const code = (detail as any).code as string;
            const vars = (detail as any).vars as Record<string, string | number> | undefined;
            const translated = t(code, vars);
            message = translated !== code ? translated : ((detail as any).message || code);
          } else if (typeof detail === "string") {
            message = detail;
          } else {
            message =
              res.status === 404
                ? t("page.shared_doc.error.not_found")
                : res.status === 410
                  ? t("page.shared_doc.error.expired")
                  : `${t("permissions.error.generic")} (${res.status})`;
          }
          setState({ kind: "error", status: res.status, message });
          return;
        }
        const data: SharedDocResponse = await res.json();
        setState({ kind: "ok", data });
      } catch (e: any) {
        setState({
          kind: "error",
          status: 0,
          message: e?.message || t("page.shared_doc.error.network"),
        });
      }
    })();
  }, [token]);

  // Once metadata resolves, fetch text content for markdown/text modes.
  useEffect(() => {
    if (state.kind !== "ok" || !token) return;
    const mode = _renderMode(state.data);
    if (mode !== "markdown" && mode !== "text") return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/v1/shared-doc/${encodeURIComponent(token)}/content`);
        if (!res.ok) {
          if (!cancelled) setTextError(t("page.shared_doc.preview_unavailable"));
          return;
        }
        const txt = await res.text();
        if (!cancelled) setTextContent(txt);
      } catch {
        if (!cancelled) setTextError(t("page.shared_doc.preview_unavailable"));
      }
    })();
    return () => { cancelled = true; };
  }, [state, token]);

  const contentUrl = token ? `/api/v1/shared-doc/${encodeURIComponent(token)}/content` : "";

  return (
    <div
      style={{
        minHeight: "100vh",
        padding: "32px 16px",
        background: "linear-gradient(180deg, #fafaf9 0%, #ffffff 100%)",
      }}
    >
      <div
        style={{
          maxWidth: 820,
          margin: "0 auto",
          background: "#ffffff",
          borderRadius: 12,
          boxShadow: "0 1px 3px rgba(28,25,23,0.06)",
          padding: 24,
        }}
      >
        <h1 style={{ fontSize: 14, fontWeight: 700, color: "#78716c", margin: "0 0 4px", letterSpacing: 0.5 }}>
          {t("page.shared_doc.brand_header")}
        </h1>

        {state.kind === "loading" && (
          <p style={{ fontSize: 14, color: "#a8a29e", padding: "32px 0", textAlign: "center" }}>
            {t("page.shared_doc.loading")}
          </p>
        )}

        {state.kind === "error" && (
          <div style={{ padding: "32px 0", textAlign: "center" }}>
            <p style={{ fontSize: 18, fontWeight: 600, color: "#292524", margin: "0 0 4px" }}>
              {state.status === 404 ? "🔒" : "⏰"} {state.message}
            </p>
            <p style={{ fontSize: 12, color: "#a8a29e", margin: "12px 0 0" }}>
              {t("page.shared_doc.contact_owner")}
            </p>
          </div>
        )}

        {state.kind === "ok" && (() => {
          const data = state.data;
          const mode = _renderMode(data);
          const canDownload = data.allow_download || data.capabilities.includes("download");
          return (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "12px 0 4px" }}>
                <IconDocument size={20} />
                <h2 style={{ fontSize: 22, fontWeight: 700, color: "#1c1917", margin: 0, wordBreak: "break-word" }}>
                  {data.name}
                </h2>
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 18, flexWrap: "wrap" }}>
                <ClassificationBadge level={data.classification} size="sm" />
                <span style={{ fontSize: 12, color: "#a8a29e" }}>
                  {t("page.shared_doc.permission_prefix")} {data.capabilities.join(" / ")}
                </span>
                {data.expires_at && (
                  <span style={{ fontSize: 12, color: "#a8a29e" }}>
                    · {t("page.shared_doc.expires_on", { date: new Date(data.expires_at).toLocaleDateString() })}
                  </span>
                )}
                {canDownload && token && (
                  <a
                    href={`/api/v1/shared-doc/${encodeURIComponent(token)}/download`}
                    download={data.name}
                    style={{
                      marginLeft: "auto",
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "6px 12px",
                      background: "#1c1917",
                      color: "#ffffff",
                      borderRadius: 8,
                      fontSize: 13,
                      fontWeight: 600,
                      textDecoration: "none",
                    }}
                  >
                    <IconDownload size={14} />
                    {t("page.shared_doc.download_button")}
                  </a>
                )}
              </div>

              {/* ── Content preview ── */}
              <div
                style={{
                  borderTop: "1px solid rgba(28,25,23,0.06)",
                  paddingTop: 18,
                }}
              >
                {mode === "markdown" && (
                  textError ? (
                    <PreviewFallback message={textError} />
                  ) : textContent == null ? (
                    <PreviewLoading />
                  ) : (
                    <div className="markdown-body" style={{ fontSize: 14, lineHeight: 1.7, color: "#292524" }}>
                      <Markdown remarkPlugins={[remarkGfm]}>{textContent}</Markdown>
                    </div>
                  )
                )}

                {mode === "text" && (
                  textError ? (
                    <PreviewFallback message={textError} />
                  ) : textContent == null ? (
                    <PreviewLoading />
                  ) : (
                    <pre
                      style={{
                        margin: 0,
                        padding: 16,
                        background: "#fafaf9",
                        borderRadius: 8,
                        fontSize: 13,
                        lineHeight: 1.6,
                        overflowX: "auto",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                        color: "#292524",
                      }}
                    >
                      {textContent}
                    </pre>
                  )
                )}

                {mode === "pdf" && (
                  <iframe
                    title={data.name}
                    src={contentUrl}
                    style={{ width: "100%", height: "75vh", border: "1px solid rgba(28,25,23,0.06)", borderRadius: 8 }}
                  />
                )}

                {mode === "image" && (
                  <img
                    src={contentUrl}
                    alt={data.name}
                    style={{ maxWidth: "100%", borderRadius: 8, display: "block", margin: "0 auto" }}
                  />
                )}

                {mode === "video" && (
                  <video
                    src={contentUrl}
                    controls
                    controlsList={canDownload ? undefined : "nodownload"}
                    style={{ width: "100%", maxHeight: "75vh", borderRadius: 8, background: "#000", display: "block" }}
                  >
                    {t("page.shared_doc.preview_unsupported")}
                  </video>
                )}

                {mode === "audio" && (
                  <audio
                    src={contentUrl}
                    controls
                    controlsList={canDownload ? undefined : "nodownload"}
                    style={{ width: "100%", display: "block" }}
                  >
                    {t("page.shared_doc.preview_unsupported")}
                  </audio>
                )}

                {mode === "unsupported" && (
                  <PreviewFallback
                    message={
                      canDownload
                        ? t("page.shared_doc.preview_unsupported_downloadable")
                        : t("page.shared_doc.preview_unsupported")
                    }
                  />
                )}
              </div>

              <div
                style={{
                  marginTop: 20,
                  paddingTop: 12,
                  borderTop: "1px solid rgba(28,25,23,0.06)",
                  fontSize: 11,
                  color: "#a8a29e",
                }}
              >
                {t("page.shared_doc.footer_logged")}
              </div>
            </>
          );
        })()}
      </div>
    </div>
  );
}

function PreviewLoading() {
  return (
    <p style={{ fontSize: 13, color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>
      {t("page.shared_doc.loading")}
    </p>
  );
}

function PreviewFallback({ message }: { message: string }) {
  return (
    <p style={{ fontSize: 13, color: "#a8a29e", textAlign: "center", padding: "24px 0" }}>
      {message}
    </p>
  );
}
