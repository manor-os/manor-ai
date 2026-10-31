import { lazy, Suspense, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import AudioWaveform from "../ui/AudioWaveform";
import Button from "../ui/Button";
import { IconMusicNote, IconPlay } from "../icons";
import Modal from "../ui/Modal";
import type { ReviewArtifactReference, ReviewArtifactKind } from "./WorkflowApprovalReview";

const VISUAL_KINDS = new Set<ReviewArtifactKind>(["image", "presentation", "document"]);

function thumbnailFor(kind: ReviewArtifactKind, documentId: string): Promise<string> {
  const options = { cache: true };
  if (kind === "image") return api.documents.imageThumbnail(documentId, options);
  if (kind === "video") return api.documents.videoThumbnail(documentId, options);
  if (kind === "presentation") return api.documents.presentationThumbnail(documentId, options);
  return api.documents.thumbnail(documentId, options);
}

function useThumbnail(kind: ReviewArtifactKind, documentId?: string) {
  return useQuery({
    queryKey: ["workflow-review-thumbnail", documentId, kind],
    queryFn: () => thumbnailFor(kind, documentId!),
    enabled: Boolean(documentId && kind),
    staleTime: Infinity,
    retry: false,
  });
}

/** A page or picture; pressing it enlarges it here, without leaving the review. */
function ReviewTile({ artifact, caption, onOpen, frame = false }: {
  artifact: ReviewArtifactReference; caption: string; onOpen: () => void;
  /** A frame card: the picture with what happens in it, the same size as its neighbours. */
  frame?: boolean;
}) {
  const thumbnail = useThumbnail(artifact.kind, artifact.documentId);
  const [broken, setBroken] = useState(false);
  const src = artifact.imageSrc || thumbnail.data;
  return (
    <button type="button" className={`workflow-review-tile${frame ? " workflow-review-tile--frame" : ""}`} onClick={onOpen} title={artifact.label}>
      {src && !broken
        ? <img src={src} alt={artifact.label || caption} loading="lazy" onError={() => setBroken(true)} />
        : <span className="workflow-review-tile__placeholder" aria-hidden="true" />}
      <span className="workflow-review-tile__caption">{caption}</span>
      {frame && artifact.description && <span className="workflow-review-tile__description">{artifact.description}</span>}
    </button>
  );
}

type ZoomItem = { artifact: ReviewArtifactReference; caption: string };

/** The enlarged page, with the others one arrow key away. The editor stays a deliberate "Open file". */
function ReviewZoom({ items, index, onIndex, onClose }: {
  items: ZoomItem[]; index: number; onIndex: (index: number) => void; onClose: () => void;
}) {
  const { artifact, caption } = items[index];
  // An image opens at full resolution; a slide or document page shows its rendered page.
  const full = useQuery({
    queryKey: ["workflow-review-zoom", artifact.documentId, artifact.kind],
    queryFn: () => artifact.kind === "image"
      ? api.documents.preview(artifact.documentId!, { cache: true })
      : thumbnailFor(artifact.kind, artifact.documentId!),
    enabled: Boolean(artifact.documentId),
    staleTime: Infinity,
    retry: false,
  });
  const thumbnail = useThumbnail(artifact.kind, artifact.documentId);
  const src = artifact.imageSrc || full.data || thumbnail.data;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "ArrowLeft" && index > 0) onIndex(index - 1);
      if (event.key === "ArrowRight" && index < items.length - 1) onIndex(index + 1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [index, items.length, onIndex]);
  const viewer = viewerPathForDocumentId(artifact.documentId);
  return (
    <Modal open onClose={onClose} title={caption || artifact.label || ""} maxWidth="min(92vw, 1100px)" className="workflow-review-zoom"
      footer={<div className="workflow-review-zoom__footer">
        <Button variant="ghost" size="sm" onClick={() => onIndex(index - 1)} disabled={index === 0}>{t("component.workflow_review.zoom_previous")}</Button>
        <span>{t("component.workflow_review.zoom_counter", { number: index + 1, total: items.length })}</span>
        <Button variant="ghost" size="sm" onClick={() => onIndex(index + 1)} disabled={index === items.length - 1}>{t("component.workflow_review.zoom_next")}</Button>
        {viewer && <a className="workflow-review-zoom__open" href={viewer} target="_blank" rel="noreferrer">{t("component.workflow_review.zoom_open")}</a>}
      </div>}>
      <div className={`workflow-review-zoom__stage${artifact.description ? " workflow-review-zoom__stage--described" : ""}`}>
        {src ? <img src={src} alt={artifact.label || caption} /> : <p>{full.isError ? t("component.workflow_review.play_failed") : t("component.workflow_review.loading")}</p>}
      </div>
      {artifact.description && <p className="workflow-review-zoom__description">{artifact.description}</p>}
    </Modal>
  );
}

function ratioValue(ratio: string): number {
  const [width, height] = ratio.split("/").map((part) => Number(part.trim()));
  return width && height ? width / height : 1;
}

/** A track to listen to: its name over its waveform; the file loads when the reviewer presses play. */
function ReviewTrack({ artifact }: { artifact: ReviewArtifactReference }) {
  const [requested, setRequested] = useState(false);
  const media = useQuery({
    queryKey: ["workflow-review-track", artifact.documentId],
    queryFn: () => api.documents.previewBlob(artifact.documentId!, { cache: true }),
    enabled: requested,
    staleTime: Infinity,
    retry: false,
  });
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    if (!media.data) return;
    const url = URL.createObjectURL(media.data);
    setSrc(url);
    return () => URL.revokeObjectURL(url);
  }, [media.data]);
  const label = artifact.label || t("component.workflow_review.recording");
  const state = media.isFetching ? "loading" : media.isError ? "failed" : "idle";
  return <figure className="workflow-review-track">
    <figcaption><span className="workflow-review-track__icon" aria-hidden="true"><IconMusicNote size={16} /></span><strong title={label}>{label}</strong></figcaption>
    {media.data && src ? <AudioWaveform src={src} data={media.data} label={label} autoPlay /> : (
      <button type="button" className={`workflow-review-track__start is-${state}`} onClick={() => setRequested(true)}
        aria-label={t("component.workflow_review.play", { name: label })} aria-busy={media.isFetching} disabled={media.isFetching}>
        <span className="audio-waveform__play" aria-hidden="true">
          {state === "loading" ? <span className="workflow-review-play__spinner" /> : <IconPlay size={16} />}
        </span>
        <span className="workflow-review-track__bars" aria-hidden="true">{TRACK_BARS.map((height, index) => <i key={index} style={{ height: `${height}%` }} />)}</span>
        <span className="workflow-review-track__hint">{state === "loading" ? t("component.workflow_review.loading")
          : state === "failed" ? t("component.workflow_review.play_failed") : t("component.workflow_review.listen")}</span>
      </button>
    )}
  </figure>;
}

// A still waveform shown before the track loads; the real one replaces it.
const TRACK_BARS = Array.from({ length: 64 }, (_, index) => Math.round(28 + 52 * Math.abs(Math.sin(index * 0.55) * Math.cos(index * 0.17))));

/** A recording plays in place; its bytes load only when the reviewer presses play. */
export function ReviewPlayer({ artifact }: { artifact: ReviewArtifactReference }) {
  if (artifact.kind === "audio") return <ReviewTrack artifact={artifact} />;
  return <ReviewVideo artifact={artifact} />;
}

function ReviewVideo({ artifact }: { artifact: ReviewArtifactReference }) {
  const poster = useThumbnail(artifact.kind === "video" ? "video" : null, artifact.documentId);
  const [requested, setRequested] = useState(false);
  const [ratio, setRatio] = useState<string | null>(null);
  const portrait = Boolean(ratio && ratioValue(ratio) < 1);
  const media = useQuery({
    queryKey: ["workflow-review-media", artifact.documentId],
    queryFn: () => api.documents.preview(artifact.documentId!, { cache: true }),
    enabled: requested,
    staleTime: Infinity,
    retry: false,
  });
  const label = artifact.label || t("component.workflow_review.recording");
  if (media.data) {
    return artifact.kind === "video"
      ? <video className={`workflow-review-player${portrait ? " is-portrait" : ""}`} src={media.data} controls autoPlay playsInline aria-label={label} />
      : <figure className="workflow-review-audio"><figcaption>{label}</figcaption><audio src={media.data} controls autoPlay /></figure>;
  }
  const state = media.isFetching ? "loading" : media.isError ? "failed" : "idle";
  return (
    <button type="button" className={`workflow-review-play is-${artifact.kind} is-${state}${portrait ? " is-portrait" : ""}`}
      style={ratio ? { aspectRatio: ratio } : undefined} onClick={() => setRequested(true)}
      aria-label={t("component.workflow_review.play", { name: label })} aria-busy={media.isFetching} disabled={media.isFetching}>
      {/* The whole frame, in its own shape: a vertical video is not cropped to a wide box. */}
      {poster.data && <img src={poster.data} alt="" onLoad={(event) => {
        const { naturalWidth, naturalHeight } = event.currentTarget;
        if (naturalWidth && naturalHeight) setRatio(`${naturalWidth} / ${naturalHeight}`);
      }} />}
      <span className="workflow-review-play__button" aria-hidden="true">
        {state === "loading"
          ? <span className="workflow-review-play__spinner" />
          : <svg viewBox="0 0 24 24" width="22" height="22"><path d="M8 5.5v13l10.5-6.5z" fill="currentColor" /></svg>}
      </span>
      {state !== "idle" && (
        <span className="workflow-review-play__label">
          {state === "loading" ? t("component.workflow_review.loading") : t("component.workflow_review.play_failed")}
        </span>
      )}
    </button>
  );
}

// Markdown rendering loads only when a review holds written work.
const ChatMarkdown = lazy(() => import("../ChatMarkdown"));

/** A written deliverable (a storyboard, captions, a script) is read in place. */
export function ReviewText({ artifact }: { artifact: ReviewArtifactReference }) {
  const text = useQuery({
    queryKey: ["workflow-review-text", artifact.documentId],
    queryFn: async () => (await api.documents.previewBlob(artifact.documentId!, { cache: true })).text(),
    staleTime: Infinity,
    retry: false,
  });
  return (
    <section className="workflow-review-text" aria-label={artifact.label}>
      {text.data
        ? <div className="workflow-review-text__body">
          <Suspense fallback={<pre className="workflow-review-write">{text.data}</pre>}>
            <ChatMarkdown content={text.data} enableFileCards={false} />
          </Suspense>
        </div>
        : <p className="workflow-review-text__state">{text.isError ? t("component.workflow_review.play_failed") : t("component.workflow_review.loading")}</p>}
      <InlineFileReferenceCard reference={artifact.reference} label={artifact.label} compact />
    </section>
  );
}

export default function ReviewDeliverables({ artifacts, pages, compact = false }: {
  artifacts: ReviewArtifactReference[];
  /** Rendered page images of a document deliverable, in order. */
  pages?: ReviewArtifactReference[];
  /** A short row of previews, for a review that lists its files beside its facts. */
  compact?: boolean;
}) {
  const pageTiles = (pages || []).filter((page) => page.documentId);
  const players = artifacts.filter((item) => item.documentId && (item.kind === "video" || item.kind === "audio"));
  // A document whose pages are shown needs no separate cover tile.
  const tiles = artifacts.filter((item) => item.imageSrc || (item.documentId && VISUAL_KINDS.has(item.kind)
    && !(pageTiles.length && item.kind !== "image")));
  const texts = artifacts.filter((item) => item.documentId && item.kind === "text");
  const shown = new Set([...players, ...tiles, ...texts]);
  const files = artifacts.filter((item) => !shown.has(item));
  const zoomItems: ZoomItem[] = [
    ...pageTiles.map((page, index) => ({ artifact: { ...page, kind: "image" as ReviewArtifactKind },
      caption: t("component.workflow_review.page", { number: index + 1 }) })),
    // A caption the step wrote wins; otherwise several pictures read as numbered pages.
    ...tiles.map((artifact, index) => ({ artifact, caption: artifact.caption || (artifact.kind === "image" && tiles.length > 1
      ? t("component.workflow_review.page", { number: index + 1 }) : artifact.label || "") })),
  ];
  const [zoom, setZoom] = useState<number | null>(null);
  // Pictures that say what happens in them (storyboard shots, slides with notes) read as one
  // sheet of equal frame cards, in order, so the whole sequence is visible at a glance.
  const frames = tiles.some((artifact) => artifact.description);
  if (!pageTiles.length && !players.length && !tiles.length && !texts.length && !files.length) return null;
  return (
    <section className={`workflow-review-deliverables${compact ? " workflow-review-deliverables--compact" : ""}`} aria-label={t("component.workflow_review.deliverables")}>
      {players.map((artifact) => <ReviewPlayer key={artifact.key} artifact={artifact} />)}
      {zoomItems.length > 0 && <div className={`workflow-review-tiles${frames ? " workflow-review-tiles--frames" : ""}`}>
        {zoomItems.map((item, index) => (
          <ReviewTile key={item.artifact.key} artifact={item.artifact} caption={item.caption} onOpen={() => setZoom(index)} frame={frames} />
        ))}
      </div>}
      {zoom !== null && zoomItems[zoom] && <ReviewZoom items={zoomItems} index={zoom} onIndex={setZoom} onClose={() => setZoom(null)} />}
      {texts.map((artifact) => <ReviewText key={artifact.key} artifact={artifact} />)}
      {files.length > 0 && <div className="chat-workflow-review-artifacts">
        {files.map((artifact) => <InlineFileReferenceCard key={artifact.key} reference={artifact.reference} label={artifact.label} compact />)}
      </div>}
    </section>
  );
}
