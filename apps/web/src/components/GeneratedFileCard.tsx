import { type ReactNode, useEffect, useRef, useState } from "react";
import { api, resolveDisplayMediaUrl } from "../lib/api";
import { requestBlobDownload, saveFileResponse } from "../lib/fileDownload";
import { fileReferenceKind, viewerPathForDocumentId } from "../lib/fileReferences";
import { resultFilePreviewTarget, type ResultFileMetadata } from "../lib/resultFiles";
import { t } from "../lib/i18n";
import InlineDocumentText, { inlineDocumentTextKind } from "./InlineDocumentText";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import AudioWaveform from "./ui/AudioWaveform";
import { documentThumbnailKind, useDocumentThumbnail } from "./ui/useDocumentThumbnail";
import { PageLoading } from "./ui/Skeleton";

type ResultFileReference = ResultFileMetadata & { reference: string };
type DownloadTarget = { documentId: string } | { url: string };

/** A Knowledge document or a file in this Manor's storage downloads; another site's link only opens. */
function downloadTarget(reference: string): DownloadTarget | null {
  const target = resultFilePreviewTarget(reference, window.location.origin);
  if (!target) return null;
  return "documentId" in target || target.url.startsWith("/") ? target : null;
}

async function downloadResultFile(target: DownloadTarget, name: string) {
  if ("documentId" in target) {
    await saveFileResponse(name, () => api.documents.downloadResponse(target.documentId));
    return;
  }
  const resolved = await resolveDisplayMediaUrl(target.url);
  try {
    requestBlobDownload(await (await fetch(resolved.url)).blob(), name);
  } finally {
    resolved.revoke();
  }
}

/** The file's name (which opens it) and Download, on one line above what it shows. */
function ResultFile({ file, returnTo, openLabel, children }: {
  file: ResultFileReference;
  returnTo?: string;
  openLabel?: string;
  children?: ReactNode;
}) {
  const [downloading, setDownloading] = useState(false);
  const target = downloadTarget(file.reference);
  return <section className="generated-file" aria-label={file.name}>
    <div className="generated-file__head">
      <InlineFileReferenceCard reference={file.reference} label={openLabel || file.name}
        fileType={file.fileType} mimeType={file.mimeType} returnTo={returnTo}
        className="generated-file__name" compact trustedReference />
      {target && <button type="button" className="generated-file__download" disabled={downloading}
        aria-label={t("component.result_files.download_named", { name: file.name })}
        onClick={() => {
          setDownloading(true);
          void downloadResultFile(target, file.name).catch(() => {}).finally(() => setDownloading(false));
        }}>
        {t("component.result_files.download")}
      </button>}
    </div>
    {children}
  </section>;
}

/** A deck, PDF or document's first page; pressing it opens the file. Nothing until a page renders. */
function PagedFilePreview({ documentId, name }: { documentId: string; name: string }) {
  const thumbnail = useDocumentThumbnail(documentId, name);
  const [broken, setBroken] = useState(false);
  const viewer = viewerPathForDocumentId(documentId);
  if (!thumbnail.src || broken || !viewer) return null;
  return <a className="generated-file-card__preview generated-file-card__preview--paged" href={viewer} target="_blank" rel="noreferrer"
    aria-label={t("component.chat_input_footer.open_document", { name })}>
    <img src={thumbnail.src} alt={name} loading="lazy" onError={() => setBroken(true)} />
  </a>;
}

/** Inline media, written results and tables for the final files of a result. */
export default function GeneratedFileCard({ file, returnTo, openLabel, seek }: {
  file: ResultFileMetadata & { reference: string };
  returnTo?: string;
  /** Under a row that already names the file, its open link says what it does instead. */
  openLabel?: string;
  /** For a track: go to this second and play (a section picked beside it). */
  seek?: { at: number; n: number } | null;
}) {
  const { name, reference, fileType, mimeType } = file;
  const kind = fileReferenceKind(name, mimeType, fileType);
  const media = kind === "video" || kind === "image" || kind === "audio";
  const target = media ? null : resultFilePreviewTarget(reference, window.location.origin);
  const documentId = target && "documentId" in target ? target.documentId : null;
  const previewRef = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<{ reference: string; url: string; failed: boolean; data?: Blob } | null>(null);
  const current = preview?.reference === reference ? preview : null;

  useEffect(() => {
    if (!media) return;
    const abort = new AbortController();
    let alive = true;
    let started = false;
    let revoke = () => {};
    setPreview(null);
    const load = async () => {
      if (started) return;
      started = true;
      try {
        const target = resultFilePreviewTarget(reference, window.location.origin);
        if (!target) throw new Error("Unresolved preview reference");
        // A track keeps its bytes too: its waveform cannot fetch a blob: URL.
        const data = "documentId" in target && kind === "audio"
          ? await api.documents.previewBlob(target.documentId, { signal: abort.signal }) : undefined;
        const resolved = data ? { url: URL.createObjectURL(data), revoke: () => {} }
          : "documentId" in target
            ? { url: await api.documents.preview(target.documentId, { signal: abort.signal }), revoke: () => {} }
            : await resolveDisplayMediaUrl(target.url);
        revoke = "documentId" in target
          ? () => URL.revokeObjectURL(resolved.url)
          : resolved.revoke;
        if (!alive) { revoke(); return; }
        setPreview({ reference, url: resolved.url, failed: false, ...(data ? { data } : {}) });
      } catch {
        if (alive) setPreview({ reference, url: "", failed: true });
      }
    };
    const observer = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      observer?.disconnect();
      void load();
    }, { rootMargin: "160px" });
    if (observer && previewRef.current) observer.observe(previewRef.current);
    else void load();
    return () => { alive = false; abort.abort(); observer?.disconnect(); revoke(); };
  }, [media, reference, kind]);

  if (!media) {
    const textKind = inlineDocumentTextKind(name, fileType);
    return <ResultFile file={file} returnTo={returnTo} openLabel={openLabel}>
      {documentId && (documentThumbnailKind(name)
        ? <PagedFilePreview documentId={documentId} name={name} />
        : textKind && <InlineDocumentText documentId={documentId} kind={textKind} />)}
    </ResultFile>;
  }
  return <ResultFile file={file} returnTo={returnTo} openLabel={openLabel}>
    <div ref={previewRef} className="generated-file-card__preview" data-kind={kind} aria-busy={!current}>
      {current?.failed ? <p role="alert">{t("component.result_files.preview_failed")}</p>
        : current?.url ? kind === "video"
          ? <video src={current.url} controls playsInline preload="metadata" aria-label={name}
              onError={() => setPreview({ reference, url: "", failed: true })} />
          : kind === "audio"
          ? <AudioWaveform src={current.url} data={current.data} label={name} seek={seek}
              onError={() => setPreview({ reference, url: "", failed: true })} />
          : <img src={current.url} alt={name} loading="lazy"
              onError={() => setPreview({ reference, url: "", failed: true })} />
        : <PageLoading label={t("component.result_files.loading_preview")} minHeight={96} />}
    </div>
  </ResultFile>;
}
