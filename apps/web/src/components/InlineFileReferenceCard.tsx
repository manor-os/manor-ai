import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { Document } from "../lib/types";
import { preserveReturnToInHistory } from "../lib/chatRouteReferences";
import { decodeFileReferenceHref, fileNameFromReference, looksLikeFileReference } from "../lib/fileReferences";
import { IconDocument, IconExternalLink } from "./icons";

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
  if (/^\/viewer\//.test(reference)) return reference;
  try {
    const url = new URL(reference, window.location.origin);
    if (url.origin === window.location.origin && url.pathname.startsWith("/viewer/")) {
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

export default function InlineFileReferenceCard({
  reference,
  label,
  returnTo,
  compact = false,
}: {
  reference: string;
  label?: string;
  returnTo?: string;
  compact?: boolean;
}) {
  const navigate = useNavigate();
  const location = useLocation();
  const [isResolving, setIsResolving] = useState(false);
  const decoded = decodeFileReferenceHref(reference) || reference;
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const isExternal = useMemo(() => /^https?:\/\//i.test(decoded), [decoded]);
  const decodedFsPath = useMemo(() => fsPathFromReference(decoded), [decoded]);
  const fileName = useMemo(() => fileNameFromReference(decodedFsPath || decoded), [decoded, decodedFsPath]);
  const resolvedFileName = useMemo(() => {
    return displayNameFromReference(fileName, label);
  }, [fileName, label]);

  async function openReference() {
    const viewerPath = sameOriginViewerPath(decoded);
    if (viewerPath) {
      preserveReturnToInHistory(currentReturnTo);
      navigate(viewerPath, { state: { returnTo: currentReturnTo, chatReturnTo: currentReturnTo } });
      return;
    }

    const idMatch = decoded.match(/\/documents\/([^/]+)/) || decoded.match(/^([0-9A-HJKMNP-TV-Z]{26})(?:$|[?#])/i);
    if (idMatch?.[1]) {
      preserveReturnToInHistory(currentReturnTo);
      navigate(`/viewer/${encodeURIComponent(idMatch[1])}`, { state: { returnTo: currentReturnTo, chatReturnTo: currentReturnTo } });
      return;
    }

    if (!looksLikeFileReference(decoded)) {
      if (isExternal) window.open(decoded, "_blank", "noopener,noreferrer");
      return;
    }

    setIsResolving(true);
    try {
      const terms = Array.from(new Set([
        decodedFsPath,
        decodedFsPath ? fileNameFromReference(decodedFsPath) : "",
        fileName,
        decoded,
      ].filter((term): term is string => Boolean(term))));
      for (const term of terms) {
        const response = await api.documents.list({ search: term, include_generated_assets: true, limit: 20 });
        const docs = getDocumentsFromResponse(response);
        const match = docs.find((doc) => documentMatchesReference(doc, decoded, fileName)) || docs[0];
        if (match?.id) {
          preserveReturnToInHistory(currentReturnTo);
          navigate(`/viewer/${encodeURIComponent(match.id)}`, { state: { returnTo: currentReturnTo, chatReturnTo: currentReturnTo } });
          return;
        }
      }
      if (isExternal) {
        window.open(decoded, "_blank", "noopener,noreferrer");
        return;
      }
      preserveReturnToInHistory(currentReturnTo);
      navigate(`/knowledge?search=${encodeURIComponent(fileNameFromReference(decodedFsPath || fileName))}`, { state: { returnTo: currentReturnTo } });
    } finally {
      setIsResolving(false);
    }
  }

  return (
    <button
      type="button"
      className={`inline-file-reference-card${compact ? " inline-file-reference-card--compact" : ""}${isResolving ? " inline-file-reference-card--loading" : ""}`}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        void openReference();
      }}
      title={decodedFsPath || decoded}
    >
      <span className="inline-file-reference-card__icon"><IconDocument size={12} /></span>
      <span className="inline-file-reference-card__name">{resolvedFileName}</span>
      {isExternal && <IconExternalLink size={10} className="inline-file-reference-card__external" />}
    </button>
  );
}
