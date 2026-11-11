import { useEffect, useMemo, useRef, useState } from "react";
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
  isInternalFileTarget,
  isOpenableFileReference,
  legacyViewerFileCandidates,
  looksLikeFileReference,
  parseFileReference,
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
  if (isInternalFileTarget(reference)) return null;
  const parsed = parseFileReference(reference);
  if (parsed?.kind === "fs") return parsed.fs_path;
  const pathname = sameOriginPath(reference);
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
    const match = path.match(/^\/viewer\/([^/?#]+)(?:[?#].*)?$/);
    if (!match?.[1]) return false;
    return Boolean(viewerPathForDocumentId(decodeUrlPathPart(match[1])));
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

function routeWithAnchor(route: string, anchorId?: string): string {
  if (!anchorId) return route;
  return `${route.split("#")[0]}#${encodeURIComponent(anchorId)}`;
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
  creationTarget = false,
  fileType,
  mimeType,
  description,
  navigationState,
  sourceAnchorId,
}: {
  reference: string;
  label?: string;
  returnTo?: string;
  compact?: boolean;
  display?: "inline" | "card";
  className?: string;
  /** Structured file metadata from our API, even when it only has an fs_path. */
  trustedReference?: boolean;
  /** An approval target names a future file, not a readable artifact. */
  creationTarget?: boolean;
  fileType?: string;
  mimeType?: string;
  description?: string;
  /** Additional viewer state, such as an unsynced Task output preview. */
  navigationState?: Record<string, unknown>;
  /** Stable element id used to restore the exact source position on return. */
  sourceAnchorId?: string;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const sourceRef = useRef<HTMLElement | null>(null);
  const [isResolving, setIsResolving] = useState(false);
  const [unresolved, setUnresolved] = useState(false);
  const [legacyLookupFailed, setLegacyLookupFailed] = useState(false);
  const unavailableDetail = t(legacyLookupFailed
    ? "component.inline_file_reference.legacy_unresolved"
    : "component.inline_file_reference.not_in_knowledge");
  const decoded = decodeFileReferenceHref(reference) || reference;
  const returnRoute = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const currentReturnTo = routeWithAnchor(returnRoute, sourceAnchorId);
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

  useEffect(() => {
    if (!sourceAnchorId || typeof window === "undefined") return;
    let activeHash = window.location.hash.slice(1);
    try {
      activeHash = decodeURIComponent(activeHash);
    } catch {
      // Keep the raw hash when it is not valid percent-encoded text.
    }
    if (activeHash !== sourceAnchorId) return;
    const frame = window.requestAnimationFrame(() => {
      const source = sourceRef.current;
      if (!source) return;
      source.scrollIntoView({ block: "center", inline: "nearest" });
      const focusTarget = source.matches("button, a, [role='button']")
        ? source
        : source.querySelector<HTMLElement>("button, a, [role='button']");
      focusTarget?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [sourceAnchorId]);

  if (creationTarget) {
    const status = t("component.inline_file_reference.creation_target");
    const detail = t("component.inline_file_reference.creation_target_detail");
    return display === "card" ? (
      <CompactCard
        className={`inline-file-reference-card-surface inline-file-reference-card--creation-target ${className}`}
        icon={<IconTile size={34}><FileTypeIcon kind={kind} size={17} /></IconTile>}
        title={resolvedFileName}
        subtitle={detail}
        meta={status}
      />
    ) : (
      <span
        className={`inline-file-reference-card inline-file-reference-card--creation-target${compact ? " inline-file-reference-card--compact" : ""} ${className}`}
        title={detail}
      >
        <span className="inline-file-reference-card__icon" aria-hidden="true"><FileTypeIcon kind={kind} size={12} /></span>
        <span className="inline-file-reference-card__name">{resolvedFileName}</span>
        <span className="inline-file-reference-card__type">{typeLabel}</span>
        <span className="inline-file-reference-card__type">{status}</span>
      </span>
    );
  }

  async function openReference() {
    const viewerPath = sameOriginViewerPath(decoded);
    if (viewerPath) {
      preserveReturnToInHistory(currentReturnTo);
      navigate(viewerPath, { state: viewerNavigationState });
      return;
    }

    // Never send a historical filename to documents.get, or fall back to a
    // raw FS read (which could bypass the selected Document's access policy).
    let internalUrl: URL | null = null;
    try { internalUrl = new URL(decoded, window.location.origin); } catch { /* Not a URL. */ }
    if (internalUrl?.origin === window.location.origin && internalUrl.pathname.startsWith("/viewer/")) {
      const candidates = legacyViewerFileCandidates(internalUrl.pathname);
      const workspaceId = currentReturnTo.match(/^\/workspaces\/([^/?#]+)(?:[/?#]|$)/)?.[1];
      setUnresolved(false);
      setLegacyLookupFailed(true);
      setIsResolving(true);
      try {
        if (workspaceId && candidates.length) {
          const matches = new Map<string, Document>();
          const names = [...new Set(candidates.map((candidate) => candidate.split("/").pop()!))];
          for (const search of names) {
            const response = await api.documents.listAll({ search, workspace_id: workspaceId, include_generated_assets: true });
            // An incomplete page set cannot prove uniqueness.
            if (response.items.length < response.total) throw new Error("Incomplete document lookup");
            for (const doc of response.items) {
              if (viewerPathForDocumentId(doc.id) && candidates.some((candidate) => (
                candidate.includes("/") ? doc.fs_path === candidate : doc.name === candidate
              ))) matches.set(doc.id, doc);
            }
          }
          if (matches.size === 1) {
            const doc = [...matches.values()][0];
            preserveReturnToInHistory(currentReturnTo);
            navigate(viewerPathForDocumentId(doc.id)!, { state: viewerNavigationState });
            return;
          }
        }
        setUnresolved(true);
      } catch {
        setUnresolved(true);
      } finally {
        setIsResolving(false);
      }
      return;
    }

    const parsedReference = parseFileReference(decoded);
    const idMatch = decoded.match(/^([0-9A-HJKMNP-TV-Z]{26})(?:$|[?#])/i);
    const documentPath = viewerPathForDocumentId(
      parsedReference?.kind === "document" ? parsedReference.document_id : idMatch?.[1],
    );
    if (!isExternal && documentPath) {
      preserveReturnToInHistory(currentReturnTo);
      navigate(documentPath, { state: viewerNavigationState });
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
          navigate(`/viewer/${encodeURIComponent(`fs-preview:${fileName}`)}`, {
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
    const card = (
      <CompactCard
        className={`inline-file-reference-card-surface inline-file-reference-card-surface--${kind}${isResolving ? " is-loading" : ""} ${className}`}
        icon={(
          <IconTile size={34} title={typeLabel}>
            <FileTypeIcon kind={kind} size={17} />
          </IconTile>
        )}
        title={resolvedFileName}
        subtitle={unresolved
          ? unavailableDetail
          : (description || decodedFsPath || decoded)}
        meta={isResolving ? "…" : typeLabel}
        onClick={() => void openReference()}
      />
    );
    return sourceAnchorId ? (
      <div id={sourceAnchorId} ref={(node) => { sourceRef.current = node; }}>
        {card}
      </div>
    ) : card;
  }

  return (
    <button
      id={sourceAnchorId}
      ref={(node) => { sourceRef.current = node; }}
      type="button"
      className={`inline-file-reference-card inline-file-reference-card--${kind}${compact ? " inline-file-reference-card--compact" : ""}${isResolving ? " inline-file-reference-card--loading" : ""} ${className}`}
      disabled={isResolving}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        void openReference();
      }}
      title={unresolved ? unavailableDetail : (decodedFsPath || decoded)}
      aria-disabled={unresolved}
    >
      <span className="inline-file-reference-card__icon" aria-hidden="true">
        <FileTypeIcon kind={kind} size={12} />
      </span>
      <span className="inline-file-reference-card__name">{resolvedFileName}</span>
      {/* The type only when the name does not already say it: "song.mp3" needs no "MP3". */}
      {typeLabel !== "FILE" && !resolvedFileName.toLowerCase().endsWith(`.${typeLabel.toLowerCase()}`)
        && <span className="inline-file-reference-card__type">{typeLabel}</span>}
      {unresolved && (
        <span className="inline-file-reference-card__unresolved">
          {t(legacyLookupFailed ? "component.inline_file_reference.link_unavailable" : "component.inline_file_reference.not_available")}
        </span>
      )}
      {isExternal && <IconExternalLink size={10} className="inline-file-reference-card__external" />}
    </button>
  );
}
