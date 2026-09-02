/**
 * SharedDocument — public anonymous viewer for a single-document share link.
 *
 * The opaque token is the entitlement. Public previews are deliberately
 * read-only and obtain all bytes through token-scoped routes.
 */
import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { ClassificationBadge } from "../components/permissions";
import ShareOtpGate from "../components/permissions/ShareOtpGate";
import ReadOnlyFilePreview from "../components/file-preview/ReadOnlyFilePreview";
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

export default function SharedDocument() {
  const { token } = useParams<{ token: string }>();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "verification" }
    | { kind: "ok"; data: SharedDocResponse }
    | { kind: "error"; status: number; message: string }
  >({ kind: "loading" });
  const [verificationVersion, setVerificationVersion] = useState(0);

  useEffect(() => {
    if (!token) return;
    void (async () => {
      try {
        const response = await fetch(
          `/api/v1/shared-doc/${encodeURIComponent(token)}`,
          { headers: { Accept: "application/json" } },
        );
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          const detail = body?.detail;
          if (response.status === 401 && detail?.code === "permissions.error.share.verification_required") {
            setState({ kind: "verification" });
            return;
          }
          let message: string;
          if (typeof detail === "object" && detail !== null && typeof detail.code === "string") {
            const translated = t(detail.code, detail.vars);
            message = translated !== detail.code ? translated : (detail.message || detail.code);
          } else if (typeof detail === "string") {
            message = detail;
          } else {
            message = response.status === 404
              ? t("page.shared_doc.error.not_found")
              : response.status === 410
                ? t("page.shared_doc.error.expired")
                : `${t("permissions.error.generic")} (${response.status})`;
          }
          setState({ kind: "error", status: response.status, message });
          return;
        }
        setState({ kind: "ok", data: await response.json() as SharedDocResponse });
      } catch (error: any) {
        setState({
          kind: "error",
          status: 0,
          message: error?.message || t("page.shared_doc.error.network"),
        });
      }
    })();
  }, [token, verificationVersion]);

  const contentUrl = token ? `/api/v1/shared-doc/${encodeURIComponent(token)}/content` : "";
  const source = useMemo(() => {
    const baseUrl = token ? `/api/v1/shared-doc/${encodeURIComponent(token)}` : "";
    const getOfficePreview = async (kind: "pages" | "slides") => {
      const response = await fetch(`${baseUrl}/preview/${kind}`);
      if (!response.ok) throw new Error("Office preview is unavailable");
      return response.json();
    };
    return {
      contentUrl,
      getPages: () => getOfficePreview("pages"),
      getSlides: () => getOfficePreview("slides"),
    };
  }, [contentUrl, token]);

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

        {state.kind === "verification" && token && (
          <ShareOtpGate
            endpoint={`/api/v1/shared-doc/${encodeURIComponent(token)}`}
            onVerified={() => {
              setState({ kind: "loading" });
              setVerificationVersion((version) => version + 1);
            }}
          />
        )}

        {state.kind === "error" && (
          <div style={{ padding: "32px 0", textAlign: "center" }}>
            <p style={{ fontSize: 18, fontWeight: 600, color: "#292524", margin: "0 0 4px" }}>
              {state.status === 404 ? "Locked" : "Expired"} {state.message}
            </p>
            <p style={{ fontSize: 12, color: "#a8a29e", margin: "12px 0 0" }}>
              {t("page.shared_doc.contact_owner")}
            </p>
          </div>
        )}

        {state.kind === "ok" && (() => {
          const data = state.data;
          const canDownload = data.allow_download && data.capabilities.includes("download");
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

              <div style={{ borderTop: "1px solid rgba(28,25,23,0.06)", paddingTop: 18 }}>
                <ReadOnlyFilePreview file={data} source={source} canDownload={canDownload} />
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
