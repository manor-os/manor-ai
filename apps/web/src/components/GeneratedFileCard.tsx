import { useEffect, useRef, useState } from "react";
import { api, resolveDisplayMediaUrl } from "../lib/api";
import { fileReferenceKind, viewerPathForDocumentId } from "../lib/fileReferences";
import { resultFilePreviewTarget, type ResultFileMetadata } from "../lib/resultFiles";
import { t } from "../lib/i18n";
import InlineDocumentText, { inlineDocumentTextKind } from "./InlineDocumentText";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import AudioWaveform from "./ui/AudioWaveform";
import Card from "./ui/Card";
import { documentThumbnailKind, useDocumentThumbnail } from "./ui/useDocumentThumbnail";

/** A paged file's card: its first page above the file link; nothing shown until a page renders. */
function PagedFileCard({ documentId, name, card }: { documentId: string; name: string; card: JSX.Element }) {
  const thumbnail = useDocumentThumbnail(documentId, name);
  const [broken, setBroken] = useState(false);
  const viewer = viewerPathForDocumentId(documentId);
  if (!thumbnail.src || broken || !viewer) return card;
  return <Card minHeight={0} hoverable={false} borderColor="transparent"
    className="generated-file-card generated-file-card--paged" footer={card}>
    <a className="generated-file-card__preview" href={viewer} target="_blank" rel="noreferrer"
      aria-label={t("component.chat_input_footer.open_document", { name })}>
      <img src={thumbnail.src} alt={name} loading="lazy" onError={() => setBroken(true)} />
    </a>
  </Card>;
}

/** Inline media, written results and tables for the final files of a result. */
export default function GeneratedFileCard({ file, returnTo, seek }: {
  file: ResultFileMetadata & { reference: string };
  returnTo?: string;
  /** For a track: go to this second and play (a section picked beside it). */
  seek?: { at: number; n: number } | null;
}) {
  const { name, reference, fileType, mimeType } = file;
  const kind = fileReferenceKind(name, mimeType, fileType);
  const media = kind === "video" || kind === "image" || kind === "audio";
  const target = media || !documentThumbnailKind(name) ? null : resultFilePreviewTarget(reference, window.location.origin);
  const pageThumbnailSource = target && "documentId" in target ? target.documentId : null;
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

  const card = <InlineFileReferenceCard reference={reference} label={name}
    fileType={fileType} mimeType={mimeType} returnTo={returnTo}
    display="card" description={t("component.result_files.open_file")} trustedReference />;
  if (!media) {
    // A deck, PDF or document shows its first page, as the review does; pressing it opens the file.
    if (pageThumbnailSource) return <PagedFileCard documentId={pageThumbnailSource} name={name} card={card} />;
    const textKind = inlineDocumentTextKind(name, fileType);
    const textTarget = textKind ? resultFilePreviewTarget(reference, window.location.origin) : null;
    if (!textKind || !textTarget || !("documentId" in textTarget)) return card;
    return <Card minHeight={0} hoverable={false} borderColor="transparent"
      className="generated-file-card" footer={card}>
      <InlineDocumentText documentId={textTarget.documentId} kind={textKind} label={name} />
    </Card>;
  }
  return <Card minHeight={0} hoverable={false} borderColor="transparent"
    className="generated-file-card" footer={card}>
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
        : <p role="status">{t("component.result_files.loading_preview")}</p>}
    </div>
  </Card>;
}
