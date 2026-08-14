import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { Document } from "../lib/types";
import { preserveReturnToInHistory } from "../lib/chatRouteReferences";
import {
  decodeFileReferenceHref,
  fileExtensionFromReference,
  fileNameFromReference,
  fileReferenceKind,
  fileReferenceTypeLabel,
  isOpenableFileReference,
  looksLikeFileReference,
  viewerPathForDocumentId,
  type FileReferenceKind,
} from "../lib/fileReferences";
import { IconExternalLink } from "./icons";
import { getFileReferenceIcon } from "./contentTypeIcons";
import { t } from "../lib/i18n";
import CompactCard from "./ui/CompactCard";
import IconTile from "./ui/IconTile";

function normalize(value: string): string {
  return value.trim().toLowerCase().replace(/^\/+/, "");
}

function decodeUrlPathPart(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function pathWithoutQuery(reference: string): string {
  return reference.split(/[?#]/)[0] || reference;
}

function sameOriginPath(reference: string): string {
  try {
    const url = new URL(reference, window.location.origin);
    if (url.origin === window.location.origin) return url.pathname;
  } catch {
    // Treat plain relative paths as already being path-like.
  }
  return pathWithoutQuery(reference);
}

function fsPathFromReference(reference: string): string | null {
  const pathname = sameOriginPath(reference);
  const apiMatch = pathname.match(/^\/api\/v1\/fs\/[^/]+\/(.+)$/);
  if (apiMatch?.[1]) return decodeUrlPathPart(apiMatch[1]).replace(/^\/+/, "");
  const legacyViewerMatch = pathname.match(/^\/viewer\/(.+)$/);
  if (legacyViewerMatch?.[1]) {
    const legacyPath = decodeUrlPathPart(legacyViewerMatch[1]).replace(/^\/+/, "");
    if (legacyPath.includes("/") && looksLikeFileReference(legacyPath)) return legacyPath;
  }
  const trimmed = decodeUrlPathPart(pathWithoutQuery(reference)).replace(/^\/+/, "");
  return trimmed && looksLikeFileReference(trimmed) ? trimmed : null;
}

function documentMatchCandidates(reference: string, fileName: string): string[] {
  return Array.from(new Set([
    reference,
    pathWithoutQuery(reference),
    decodeUrlPathPart(pathWithoutQuery(reference)),
    fsPathFromReference(reference) || "",
    fileName,
  ].filter(Boolean).map(normalize)));
}

function documentMatchesReference(doc: Document, reference: string, fileName: string): boolean {
  const refs = documentMatchCandidates(reference, fileName);
  const docName = normalize(doc.name || "");
  const docPath = normalize(doc.fs_path || "");
  const exactFsPath = normalize(fsPathFromReference(reference) || "");
  if (exactFsPath.includes("/")) return Boolean(docPath && docPath === exactFsPath);
  return doc.id === reference || refs.some((ref) => (
    docName === ref || docPath === ref || (docName && ref.endsWith(`/${docName}`)) || (docPath && ref.endsWith(`/${docPath}`))
  ));
}

function getDocumentsFromResponse(response: any): Document[] {
  if (Array.isArray(response)) return response;
  if (Array.isArray(response?.items)) return response.items;
  if (Array.isArray(response?.documents)) return response.documents;
  return [];
}

function sameOriginViewerPath(reference: string): string | null {
  const validViewerPath = (path: string) => {
    const match = path.match(/^\/viewer\/([^?#]+)/);
    if (!match?.[1]) return false;
    const decoded = decodeUrlPathPart(match[1]);
    return !(decoded.includes("/") && looksLikeFileReference(decoded));
  };
  if (/^\/viewer\//.test(reference)) return validViewerPath(reference) ? reference : null;
  try {
    const url = new URL(reference, window.location.origin);
    if (
      url.origin === window.location.origin
      && url.pathname.startsWith("/viewer/")
      && validViewerPath(url.pathname)
    ) {
      return `${url.pathname}${url.search}${url.hash}`;
    }
  } catch {
    return null;
  }
  return null;
}

function displayNameFromReference(referenceName: string, label?: string): string {
  if (!label) return referenceName;
  const cleaned = label.trim().replace(/^(download|open|下载|打开)[:：\s]+/i, "").trim();
  return cleaned || referenceName;
}

function FileTypeIcon({ kind, size }: { kind: FileReferenceKind; size: number }) {
  const Icon = getFileReferenceIcon(kind);
  return <Icon size={size} />;
}

export default function InlineFileReferenceCard({
  reference,
  label,
  returnTo,
  compact = false,
  display = "inline",
  className = "",
  trustedReference = false,
  fileType,
  mimeType,
  navigationState,
}: {
  reference: string;
  label?: string;
  returnTo?: string;
  compact?: boolean;
  display?: "inline" | "card";
  className?: string;
  /** Structured file metadata from our API, even when it only has an fs_path. */
  trustedReference?: boolean;
  fileType?: string;
  mimeType?: string;
  /** Additional viewer state, such as an unsynced Task output preview. */
  navigationState?: Record<string, unknown>;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [isResolving, setIsResolving] = useState(false);
  const [unresolved, setUnresolved] = useState(false);
  const decoded = decodeFileReferenceHref(reference) || reference;
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const isExternal = useMemo(() => /^https?:\/\//i.test(decoded), [decoded]);
  const decodedFsPath = useMemo(() => fsPathFromReference(decoded), [decoded]);
  const fileName = useMemo(() => fileNameFromReference(decodedFsPath || decoded), [decoded, decodedFsPath]);
  const resolvedFileName = useMemo(() => {
    return displayNameFromReference(fileName, label);
  }, [fileName, label]);
  // Canonical Knowledge links use /viewer/<document_id>, so the visible
  // Markdown label carries the filename and therefore the card type.
  const kind = useMemo(
    () => fileReferenceKind(resolvedFileName, mimeType, fileType),
    [fileType, mimeType, resolvedFileName],
  );
  const typeLabel = useMemo(
    () => fileReferenceTypeLabel(resolvedFileName, mimeType, fileType),
    [fileType, mimeType, resolvedFileName],
  );
  const viewerNavigationState = {
    ...(navigationState || {}),
    returnTo: currentReturnTo,
    chatReturnTo: currentReturnTo,
  };

  async function openReference() {
    const viewerPath = sameOriginViewerPath(decoded);
    if (viewerPath) {
      preserveReturnToInHistory(currentReturnTo);
      navigate(viewerPath, { state: viewerNavigationState });
      return;
    }

    const idMatch = decoded.match(/\/documents\/([^/]+)/) || decoded.match(/^([0-9A-HJKMNP-TV-Z]{26})(?:$|[?#])/i);
    if (!isExternal && idMatch?.[1]) {
      preserveReturnToInHistory(currentReturnTo);
      navigate(viewerPathForDocumentId(idMatch[1])!, { state: viewerNavigationState });
      return;
    }

    if (!trustedReference && !isOpenableFileReference(decoded)) {
      if (isExternal) window.open(decoded, "_blank", "noopener,noreferrer");
      return;
    }
    if (isExternal) {
      window.open(decoded, "_blank", "noopener,noreferrer");
      return;
    }

    setIsResolving(true);
    setUnresolved(false);
    try {
      // An exact entity-FS URL is already a complete address. Workspace code
      // and task artifacts do not always have a Knowledge Document row, so
      // read that address directly and hand the bytes to FileViewer instead
      // of making a successful click depend on a secondary document search.
      if (decodedFsPath) {
        try {
          const result = await api.fs.read(decodedFsPath);
          preserveReturnToInHistory(currentReturnTo);
          navigate(viewerPathForDocumentId(`fs-preview:${fileName}`)!, {
            state: {
              ...viewerNavigationState,
              taskOutputPreview: {
                id: decodedFsPath,
                name: fileName,
                fs_path: decodedFsPath,
                file_type: fileType || fileExtensionFromReference(fileName),
                mime_type: mimeType || result.mime_type,
                encoding: result.encoding,
                content: result.content,
              },
            },
          });
          return;
        } catch {
          // Older references can carry stale FS paths while still matching a
          // synced Document. Preserve that compatibility via lookup below.
        }
      }

      const terms = Array.from(new Set([
        decodedFsPath,
        decodedFsPath ? fileNameFromReference(decodedFsPath) : "",
        fileName,
        decoded,
      ].filter((term): term is string => Boolean(term))));
      for (const term of terms) {
        const response = await api.documents.list({ search: term, include_generated_assets: true, limit: 20 });
        const docs = getDocumentsFromResponse(response);
        const match = docs.find((doc) => documentMatchesReference(doc, decoded, fileName));
        if (match?.id) {
          preserveReturnToInHistory(currentReturnTo);
          navigate(viewerPathForDocumentId(match.id)!, { state: viewerNavigationState });
          return;
        }
      }
      // Nothing resolved. This used to navigate to /knowledge?search=… , an
      // empty page indistinguishable from the Knowledge root and silent about
      // why. The real fix is upstream — the artifact gets a Document row now,
      // so it resolves — and this stays only for a reference with no
      // resolvable path at all.
      setUnresolved(true);
    } finally {
      setIsResolving(false);
    }
  }

  if (display === "card") {
    return (
      <CompactCard
        className={`inline-file-reference-card-surface inline-file-reference-card-surface--${kind}${isResolving ? " is-loading" : ""} ${className}`}
        icon={(
          <IconTile size={34} title={typeLabel}>
            <FileTypeIcon kind={kind} size={17} />
          </IconTile>
        )}
        title={resolvedFileName}
        subtitle={unresolved
          ? t("component.inline_file_reference.not_in_knowledge")
          : (decodedFsPath || decoded)}
        meta={isResolving ? "…" : typeLabel}
        onClick={() => void openReference()}
      />
    );
  }

  return (
    <button
      type="button"
      className={`inline-file-reference-card inline-file-reference-card--${kind}${compact ? " inline-file-reference-card--compact" : ""}${isResolving ? " inline-file-reference-card--loading" : ""} ${className}`}
      disabled={isResolving}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        void openReference();
      }}
      title={unresolved ? t("component.inline_file_reference.not_in_knowledge") : (decodedFsPath || decoded)}
      aria-disabled={unresolved}
    >
      <span className="inline-file-reference-card__icon" aria-hidden="true">
        <FileTypeIcon kind={kind} size={12} />
      </span>
      <span className="inline-file-reference-card__name">{resolvedFileName}</span>
      <span className="inline-file-reference-card__type">{typeLabel}</span>
      {unresolved && (
        <span className="inline-file-reference-card__unresolved">
          {t("component.inline_file_reference.not_available")}
        </span>
      )}
      {isExternal && <IconExternalLink size={10} className="inline-file-reference-card__external" />}
    </button>
  );
}
