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

function documentMatchesReference(doc: Document, reference: string, fileName: string): boolean {
  const ref = normalize(reference.split(/[?#]/)[0] || reference);
  const name = normalize(fileName);
  const docName = normalize(doc.name || "");
  const docPath = normalize(doc.fs_path || "");
  return doc.id === reference || docName === name || docPath === ref || docPath.endsWith(`/${name}`);
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

function sameOriginFileApiPath(reference: string): string | null {
  try {
    const url = new URL(reference, window.location.origin);
    if (url.origin === window.location.origin && url.pathname.startsWith("/api/v1/fs/")) {
      return `${url.pathname}${url.search}${url.hash}`;
    }
  } catch {
    return null;
  }
  return null;
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
  const fileName = label || fileNameFromReference(decoded);
  const currentReturnTo = returnTo || `${location.pathname}${location.search}${location.hash}`;
  const isExternal = useMemo(() => /^https?:\/\//i.test(decoded), [decoded]);

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

    const apiFilePath = sameOriginFileApiPath(decoded);
    setIsResolving(true);
    try {
      const terms = Array.from(new Set([fileName, decoded].filter(Boolean)));
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
      if (apiFilePath) {
        preserveReturnToInHistory(currentReturnTo);
        window.location.assign(apiFilePath);
        return;
      }
      preserveReturnToInHistory(currentReturnTo);
      navigate(`/knowledge?search=${encodeURIComponent(fileName)}`, { state: { returnTo: currentReturnTo } });
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
      title={decoded}
    >
      <span className="inline-file-reference-card__icon"><IconDocument size={12} /></span>
      <span className="inline-file-reference-card__name">{fileName}</span>
      {isExternal && <IconExternalLink size={10} className="inline-file-reference-card__external" />}
    </button>
  );
}
