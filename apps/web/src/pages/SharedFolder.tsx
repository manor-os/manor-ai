/**
 * SharedFolder — public anonymous viewer for a folder share link.
 *
 * URL: /shared-folder/:token  (matches backend's
 *      /api/v1/shared-folder/{token} URL returned by createShare)
 *
 * No auth required. The token itself is the entitlement; backend verifies
 * its sha256 hash, expiry, max_uses, and revocation state.
 *
 * Browsing and previews retain the original folder token. Every child read
 * revalidates ancestry, classification, lifecycle and the share's audience.
 */
import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { ClassificationBadge, WatermarkLayer } from "../components/permissions";
import { IconFolder, IconDocument } from "../components/icons";
import { t } from "../lib/i18n";
import ShareOtpGate from "../components/permissions/ShareOtpGate";
import ReadOnlyFilePreview from "../components/file-preview/ReadOnlyFilePreview";
import Button from "../components/ui/Button";
import Modal from "../components/ui/Modal";

interface PublicDoc {
  id: string;
  name: string;
  file_size?: number;
  file_type?: string;
  mime_type?: string;
  classification?: string;
}

interface SharedFolderResponse {
  folder_id: string;
  name: string;
  classification?: string;
  capabilities: string[];
  watermark: boolean;
  allow_download: boolean;
  expires_at?: string;
  parent_id?: string | null;
  documents: PublicDoc[];
  subfolders: { id: string; name: string }[];
}

function formatSize(bytes?: number): string {
  if (!bytes) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function SharedFolder() {
  const { token } = useParams<{ token: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const folderId = searchParams.get("folder");
  const [selectedDocument, setSelectedDocument] = useState<PublicDoc | null>(null);
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "verification" }
    | { kind: "ok"; data: SharedFolderResponse }
    | { kind: "error"; status: number; message: string }
  >({ kind: "loading" });
  const [verificationVersion, setVerificationVersion] = useState(0);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setState({ kind: "loading" });
    setSelectedDocument(null);
    (async () => {
      try {
        const res = await fetch(
          `/api/v1/shared-folder/${encodeURIComponent(token)}${folderId ? `?folder_id=${encodeURIComponent(folderId)}` : ""}`,
          { headers: { Accept: "application/json" }, signal: controller.signal },
        );
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          if (controller.signal.aborted) return;
          const detail = body?.detail;
          if (res.status === 401 && detail?.code === "permissions.error.share.verification_required") {
            setState({ kind: "verification" });
            return;
          }
          // Backend CodedError shape: { code, message, vars? }
          // Older clients / non-coded errors: detail is a plain string.
          let message: string;
          if (typeof detail === "object" && detail !== null && typeof (detail as any).code === "string") {
            const code = (detail as any).code as string;
            const vars = (detail as any).vars as Record<string, string | number> | undefined;
            const translated = t(code, vars);
            // t() echoes the key when no translation exists — fall back to
            // the backend's English message in that case.
            message = translated !== code ? translated : ((detail as any).message || code);
          } else if (typeof detail === "string") {
            message = detail;
          } else {
            message =
              res.status === 404
                ? t("page.shared_folder.error.not_found")
                : res.status === 410
                  ? t("page.shared_folder.error.expired")
                  : `${t("permissions.error.generic")} (${res.status})`;
          }
          setState({ kind: "error", status: res.status, message });
          return;
        }
        const data: SharedFolderResponse = await res.json();
        if (!controller.signal.aborted) setState({ kind: "ok", data });
      } catch (e: any) {
        if (controller.signal.aborted) return;
        setState({
          kind: "error",
          status: 0,
          message: e?.message || t("page.shared_folder.error.network"),
        });
      }
    })();
    return () => controller.abort();
  }, [token, folderId, verificationVersion]);

  const fileBaseUrl = token && selectedDocument
    ? `/api/v1/shared-folder/${encodeURIComponent(token)}/documents/${encodeURIComponent(selectedDocument.id)}`
    : "";
  const previewSource = useMemo(() => ({ contentUrl: `${fileBaseUrl}/content` }), [fileBaseUrl]);
  const canDownload = state.kind === "ok" && state.data.allow_download && state.data.capabilities.includes("download");

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
          maxWidth: 720,
          margin: "0 auto",
          background: "#ffffff",
          borderRadius: 12,
          boxShadow: "0 1px 3px rgba(28,25,23,0.06)",
          padding: 24,
        }}
      >
        <h1 style={{ fontSize: 14, fontWeight: 700, color: "#78716c", margin: "0 0 4px", letterSpacing: 0.5 }}>
          {t("page.shared_folder.brand_header")}
        </h1>

        {state.kind === "loading" && (
          <p style={{ fontSize: 14, color: "#a8a29e", padding: "32px 0", textAlign: "center" }}>
            {t("page.shared_folder.loading")}
          </p>
        )}

        {state.kind === "verification" && token && (
          <ShareOtpGate
            endpoint={`/api/v1/shared-folder/${encodeURIComponent(token)}`}
            onVerified={() => {
              setState({ kind: "loading" });
              setVerificationVersion((version) => version + 1);
            }}
          />
        )}

        {state.kind === "error" && (
          <div style={{ padding: "32px 0", textAlign: "center" }}>
            <p style={{ fontSize: 18, fontWeight: 600, color: "#292524", margin: "0 0 4px" }}>
              {state.status === 404 ? "🔒" : "⏰"} {state.message}
            </p>
            <p style={{ fontSize: 12, color: "#a8a29e", margin: "12px 0 0" }}>
              {t("page.shared_folder.contact_owner")}
            </p>
          </div>
        )}

        {state.kind === "ok" && (
          <>
            {state.data.parent_id && (
              <Button variant="ghost" onClick={() => setSearchParams({ folder: state.data.parent_id! })}>
                {t("page.file_viewer.go_back")}
              </Button>
            )}
            <h2 style={{ fontSize: 22, fontWeight: 700, color: "#1c1917", margin: "8px 0 4px" }}>
              {state.data.name}
            </h2>
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 18 }}>
              <ClassificationBadge level={state.data.classification} size="sm" />
              <span style={{ fontSize: 12, color: "#a8a29e" }}>
                {t("page.shared_folder.permission_prefix")} {state.data.capabilities.join(" / ")}
              </span>
              {state.data.expires_at && (
                <span style={{ fontSize: 12, color: "#a8a29e" }}>
                  · {t("page.shared_folder.expires_on", { date: new Date(state.data.expires_at).toLocaleDateString() })}
                </span>
              )}
            </div>

            {state.data.subfolders.length > 0 && (
              <section style={{ marginBottom: 20 }}>
                <h3 style={SECTION_LABEL_STYLE}>
                  {t("page.shared_folder.subfolders_section", { count: state.data.subfolders.length })}
                </h3>
                <ul style={LIST_STYLE}>
                  {state.data.subfolders.map((sf) => (
                    <li key={sf.id} style={ROW_STYLE}>
                      <IconFolder size={16} />
                      <Button variant="ghost" className="min-w-0 truncate" title={sf.name} onClick={() => setSearchParams({ folder: sf.id })}>
                        {sf.name}
                      </Button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section>
              <h3 style={SECTION_LABEL_STYLE}>
                {t("page.shared_folder.files_section", { count: state.data.documents.length })}
              </h3>
              {state.data.documents.length === 0 ? (
                <p style={{ fontSize: 13, color: "#a8a29e", margin: 0 }}>
                  {t("page.shared_folder.empty_files")}
                </p>
              ) : (
                <ul style={LIST_STYLE}>
                  {state.data.documents.map((d) => (
                    <li key={d.id} style={ROW_STYLE}>
                      <IconDocument size={16} />
                      <Button variant="ghost" className="min-w-0 truncate" title={d.name} onClick={() => setSelectedDocument(d)}>
                        {d.name}
                      </Button>
                      <ClassificationBadge level={d.classification} size="sm" />
                      <span style={{ fontSize: 11, color: "#a8a29e", marginLeft: "auto" }}>
                        {formatSize(d.file_size)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <div
              style={{
                marginTop: 20,
                paddingTop: 12,
                borderTop: "1px solid rgba(28,25,23,0.06)",
                fontSize: 11,
                color: "#a8a29e",
              }}
            >
              {t("page.shared_folder.footer_notice")}
            </div>
          </>
        )}
      </div>
      <Modal
        open={selectedDocument !== null}
        onClose={() => setSelectedDocument(null)}
        title={selectedDocument?.name || t("page.shared_folder.brand_header")}
        maxWidth="1000px"
        footer={canDownload && selectedDocument ? (
          <a className="btn-manor" href={`${fileBaseUrl}/download`} download={selectedDocument.name}>
            {t("page.shared_doc.download_button")}
          </a>
        ) : undefined}
      >
        {selectedDocument && (
          <div style={{ position: "relative", minWidth: 0 }}>
            <ReadOnlyFilePreview key={fileBaseUrl} file={selectedDocument} source={previewSource} canDownload={canDownload} />
            {state.kind === "ok" && state.data.watermark && (
              <WatermarkLayer viewerEmail={t("page.shared_folder.brand_header")} />
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}

const SECTION_LABEL_STYLE: React.CSSProperties = {
  fontSize: 11,
  fontWeight: 700,
  color: "#78716c",
  textTransform: "uppercase",
  letterSpacing: 0.4,
  margin: "0 0 8px",
};

const LIST_STYLE: React.CSSProperties = {
  listStyle: "none",
  padding: 0,
  margin: 0,
  display: "flex",
  flexDirection: "column",
  gap: 4,
};

const ROW_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "8px 10px",
  borderRadius: 6,
  background: "rgba(245,245,244,0.5)",
};
