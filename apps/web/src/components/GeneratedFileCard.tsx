import { useEffect, useRef, useState } from "react";
import { api, resolveDisplayMediaUrl } from "../lib/api";
import { fileReferenceKind } from "../lib/fileReferences";
import { resultFilePreviewTarget, type ResultFileMetadata } from "../lib/resultFiles";
import { t } from "../lib/i18n";
import InlineFileReferenceCard from "./InlineFileReferenceCard";
import Card from "./ui/Card";

/** Inline media only for the final files selected by the result projection. */
export default function GeneratedFileCard({ file, returnTo }: {
  file: ResultFileMetadata & { reference: string };
  returnTo?: string;
}) {
  const { name, reference, fileType, mimeType } = file;
  const kind = fileReferenceKind(name, mimeType, fileType);
  const media = kind === "video" || kind === "image";
  const previewRef = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<{ reference: string; url: string; failed: boolean } | null>(null);
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
        const resolved = "documentId" in target
          ? { url: await api.documents.preview(target.documentId, { signal: abort.signal }), revoke: () => {} }
          : await resolveDisplayMediaUrl(target.url);
        revoke = "documentId" in target
          ? () => URL.revokeObjectURL(resolved.url)
          : resolved.revoke;
        if (!alive) { revoke(); return; }
        setPreview({ reference, url: resolved.url, failed: false });
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
  }, [media, reference]);

  const card = <InlineFileReferenceCard reference={reference} label={name}
    fileType={fileType} mimeType={mimeType} returnTo={returnTo}
    display="card" description={t("component.result_files.open_file")} trustedReference />;
  if (!media) return card;
  return <Card minHeight={0} hoverable={false} borderColor="transparent"
    className="generated-file-card" footer={card}>
    <div ref={previewRef} className="generated-file-card__preview" aria-busy={!current}>
      {current?.failed ? <p role="alert">{t("component.result_files.preview_failed")}</p>
        : current?.url ? kind === "video"
          ? <video src={current.url} controls playsInline preload="metadata" aria-label={name}
              onError={() => setPreview({ reference, url: "", failed: true })} />
          : <img src={current.url} alt={name} loading="lazy"
              onError={() => setPreview({ reference, url: "", failed: true })} />
        : <p role="status">{t("component.result_files.loading_preview")}</p>}
    </div>
  </Card>;
}
