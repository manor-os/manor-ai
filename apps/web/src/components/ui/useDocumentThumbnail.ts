import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";

export type DocumentThumbnailKind = "image" | "video" | "presentation" | "page";

/** Which preview a file name can have, or null when it has none worth loading. */
export function documentThumbnailKind(name: string): DocumentThumbnailKind | null {
  const extension = name.match(/\.([^.]+)$/)?.[1]?.toLowerCase() || "";
  if (/^(png|jpe?g|gif|webp|bmp|avif)$/.test(extension)) return "image";
  if (/^(mp4|mov|webm|m4v|mkv)$/.test(extension)) return "video";
  if (/^pptx?$/.test(extension)) return "presentation";
  if (/^(pdf|docx?|xlsx?)$/.test(extension)) return "page";
  return null;
}

/**
 * A document's own preview image (an object/data URL), loaded once and cached.
 * ``enabled: false`` waits, so a long list loads only the rows that are on screen.
 */
export function useDocumentThumbnail(documentId: string | null | undefined, name: string, { enabled = true } = {}) {
  const kind = documentThumbnailKind(name);
  const thumbnail = useQuery({
    queryKey: ["document-thumbnail", documentId, kind],
    queryFn: () => {
      const options = { cache: true };
      if (kind === "image") return api.documents.imageThumbnail(documentId!, options);
      if (kind === "video") return api.documents.videoThumbnail(documentId!, options);
      if (kind === "presentation") return api.documents.presentationThumbnail(documentId!, options);
      return api.documents.thumbnail(documentId!, options);
    },
    enabled: Boolean(enabled && kind && documentId),
    staleTime: Infinity,
    // A just-made file's preview is rendered on the first ask and can take a
    // while (a deck goes through LibreOffice); ask again a few times instead
    // of keeping the plain icon until the page reloads.
    retry: 4,
    retryDelay: (attempt) => Math.min(30_000, 4_000 * 2 ** attempt),
  });
  return { kind, src: thumbnail.data };
}
