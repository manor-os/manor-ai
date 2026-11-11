/**
 * Media in a Workspace App: a file in its own frame, the viewer a gallery opens (versions, compare,
 * zoom, details, download, comments pinned to a spot or a moment, a file action), and the players a
 * media field shows in place (captions, transcript, chapters, a picked span), clips and an edit's timeline.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";

import { api, type WorkspaceAppBlockMedia, type WorkspaceAppCardAction, type WorkspaceAppMediaFile, type WorkspaceAppPresentedField } from "../../lib/api";
import { copyText } from "../../lib/clipboard";
import { formatDateLong } from "../../lib/format";
import { getLocale, t } from "../../lib/i18n";
import type { Comment } from "../../lib/types";
import { useToastStore } from "../../stores/toast";
import CommentThread from "../CommentThread";
import ImagePreview from "../file-preview/ImagePreview";
import { IconChevronLeft, IconChevronRight, IconComment, IconDocument, IconDownload, IconLink, IconMusicNote, IconPlay } from "../icons";
import AudioWaveform from "../ui/AudioWaveform";
import Button from "../ui/Button";
import TabSwitcher from "../ui/TabSwitcher";
import { useSignedMediaUrl } from "../ui/useSignedMediaUrl";
import { formatDuration, formatFileSize } from "./workspaceAppFieldFormat";
import {
  activeCueIndex, aspectRatioCss, clockLabel, fileActionValues, mayAnimate, mediaAnchor, parseCaptions, pixelSizeLabel,
  regionFromDrag, shapeLabel, timelineLanes, type MediaCommentAnchor, type MediaCue,
} from "./workspaceAppMediaPresentation";
import "./WorkspaceAppMedia.css";

export type MediaKind = "image" | "video" | "audio" | "file";

export function mediaKind(mimeType?: string | null): MediaKind {
  const kind = String(mimeType || "").toLowerCase().split("/")[0];
  return kind === "image" || kind === "video" || kind === "audio" ? kind : "file";
}

/** A picture or a frame of a video, from its signed thumbnail or the documents API; its first letters while none loads. */
export function PackCover({ documentId, label, video = false, thumbnailUrl, onMeasure }: {
  documentId?: string | null; label: string; video?: boolean; thumbnailUrl?: string | null;
  /** The loaded picture's natural size: its shape, for a frame that follows the file. */
  onMeasure?: (width: number, height: number) => void;
}) {
  const signed = useSignedMediaUrl(thumbnailUrl);
  const thumbnail = useQuery({
    queryKey: ["workspace-app-pack-cover", documentId, video],
    queryFn: () => video
      ? api.documents.videoThumbnail(documentId!, { cache: true })
      : api.documents.imageThumbnail(documentId!, { cache: true }),
    // A signed URL loads in the browser itself; the API path is the fallback.
    enabled: Boolean(documentId) && !signed.src && !signed.waiting,
    staleTime: Infinity,
    retry: false,
  });
  const measure = onMeasure ? (event: { currentTarget: HTMLImageElement }) => {
    const { naturalWidth, naturalHeight } = event.currentTarget;
    if (naturalWidth && naturalHeight) onMeasure(naturalWidth, naturalHeight);
  } : undefined;
  if (signed.src) return <img src={signed.src} alt="" loading="lazy" decoding="async" onError={signed.onError} onLoad={measure} />;
  return thumbnail.data
    ? <img src={thumbnail.data} alt="" onLoad={measure} />
    : <span className="workspace-app-data__pack-placeholder" aria-hidden="true">{label.slice(0, 3)}</span>;
}

/** The file itself (a moving GIF, a picture at full size): an object URL, released when it is no longer shown. */
function useOriginal(documentId: string | null | undefined, enabled: boolean) {
  const query = useQuery({
    queryKey: ["workspace-app-media-original", documentId],
    queryFn: () => api.documents.preview(documentId!, { cache: true }),
    enabled: Boolean(documentId) && enabled,
    staleTime: Infinity,
    retry: false,
  });
  return query.data || "";
}

/** Where a clip or a track plays from while it downloads; asked for only once the person wants to play it. */
function useStream(documentId: string | null | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["workspace-app-media-stream", documentId],
    queryFn: () => api.documents.streamUrl(documentId!),
    enabled: Boolean(documentId) && enabled,
    staleTime: 10 * 60_000,
    retry: false,
  });
}

/** A captions file's lines (.vtt or .srt), read once. */
function useCaptions(documentId: string | null | undefined) {
  return useQuery({
    queryKey: ["workspace-app-media-captions", documentId],
    queryFn: async () => parseCaptions(await (await api.documents.previewBlob(documentId!, { cache: true })).text()),
    enabled: Boolean(documentId),
    staleTime: Infinity,
    retry: false,
  });
}

/** What a frame shows a file in: its declared frame, else its own shape (recorded or measured). */
function frameStyle(aspect: string | undefined, file: { width?: number; height?: number; aspect?: string } | undefined,
  measured: { width: number; height: number } | null): CSSProperties | undefined {
  if (aspect && aspect !== "auto") return { aspectRatio: aspectRatioCss(aspect) };
  if (!aspect) return undefined;
  if (file?.width && file.height) return { aspectRatio: `${file.width} / ${file.height}` };
  if (file?.aspect && aspectRatioCss(file.aspect)) return { aspectRatio: aspectRatioCss(file.aspect) };
  return measured ? { aspectRatio: `${measured.width} / ${measured.height}` } : undefined;
}

/** One file in its frame: never cropped once a frame is chosen, a checkerboard behind a cut-out, its pixel size,
 *  a clip that plays on hover and a GIF that moves. */
export function MediaFrame({ file, label, aspect, transparent = false, sizeLabel = false, hoverPlay = false, className = "" }: {
  file: WorkspaceAppMediaFile; label: string; aspect?: string; transparent?: boolean; sizeLabel?: boolean; hoverPlay?: boolean; className?: string;
}) {
  const kind = mediaKind(file.mime_type);
  const [measured, setMeasured] = useState<{ width: number; height: number } | null>(null);
  const [hovered, setHovered] = useState(false);
  const stream = useStream(file.document_id, hoverPlay && hovered && kind === "video");
  const moving = useOriginal(file.document_id, hovered && kind === "image" && mayAnimate(file.mime_type));
  const size = sizeLabel ? pixelSizeLabel(file.width, file.height) : null;
  const classes = ["workspace-app-media__frame", aspect ? "is-framed" : "", transparent ? "is-transparent" : "", className].filter(Boolean).join(" ");
  return <span className={classes} style={frameStyle(aspect, file, measured)} data-kind={kind}
    onPointerEnter={() => setHovered(true)} onPointerLeave={() => setHovered(false)}>
    {kind === "image" || kind === "video"
      ? <PackCover documentId={file.document_id} label={label} video={kind === "video"} thumbnailUrl={file.thumbnail_url}
        onMeasure={(width, height) => setMeasured((current) => current || { width, height })} />
      : <span className="workspace-app-media__file" aria-hidden="true">{kind === "audio" ? <IconMusicNote size={22} /> : <IconDocument size={22} />}</span>}
    {moving && <img className="workspace-app-media__overlay" src={moving} alt="" />}
    {stream.data && hovered && <video className="workspace-app-media__overlay" src={stream.data} muted autoPlay loop playsInline aria-hidden="true" />}
    {kind === "video" && !hovered && <span className="workspace-app-data__gallery-play" aria-hidden="true"><IconPlay size={12} /></span>}
    {size && <span className="workspace-app-media__badge mono">{size}</span>}
  </span>;
}

/** A recording that plays here, and tells where it is: a transcript, chapters and a picked span follow it. */
export function MediaPlayer({ file, label, kind, seek, onTime, cues, autoStart = false }: {
  file: { document_id: string }; label: string; kind: "video" | "audio";
  seek?: { at: number; n: number } | null; onTime?: (seconds: number) => void;
  /** Captions shown over the video as it plays. */
  cues?: MediaCue[];
  /** Load and play at once: the person already chose this one. */
  autoStart?: boolean;
}) {
  // A track or a clip streams only once someone presses play (or picks a moment of it).
  const [requested, setRequested] = useState(autoStart || Boolean(seek));
  const stream = useStream(file.document_id, requested);
  const video = useRef<HTMLVideoElement>(null);
  const [time, setTime] = useState(0);
  useEffect(() => {
    if (seek) setRequested(true);
    const player = video.current;
    if (!seek || !player) return;
    player.currentTime = Math.max(0, seek.at);
    void player.play().catch(() => undefined);
  }, [seek, stream.data]);
  if (kind === "audio") {
    if (stream.isError) return <p role="alert" className="workspace-app-media__note">{t("component.workflow_review.play_failed")}</p>;
    if (stream.data) return <AudioWaveform src={stream.data} label={label} seek={seek} onTime={onTime} autoPlay={requested} />;
    return <button type="button" className="workspace-app-media__listen" onClick={() => setRequested(true)} disabled={stream.isFetching}
      aria-label={t("component.workflow_review.play", { name: label })} aria-busy={stream.isFetching}>
      <span className="audio-waveform__play" aria-hidden="true"><IconPlay size={16} /></span>
      <span>{stream.isFetching ? t("component.workflow_review.loading") : t("component.workflow_review.listen")}</span>
    </button>;
  }
  if (!stream.data) {
    return <button type="button" className="workspace-app-media__start" onClick={() => setRequested(true)} disabled={stream.isFetching}
      aria-label={t("component.workflow_review.play", { name: label })} aria-busy={stream.isFetching}>
      <MediaFrame file={{ document_id: file.document_id, title: label, mime_type: "video/mp4" }} label={label} aspect="auto" />
      {stream.isError && <span role="alert">{t("component.workflow_review.play_failed")}</span>}
    </button>;
  }
  const cue = cues?.length ? cues[activeCueIndex(cues, time)] : undefined;
  return <span className="workspace-app-media__video">
    <video ref={video} src={stream.data} controls autoPlay={requested} playsInline aria-label={label}
      onTimeUpdate={(event) => { setTime(event.currentTarget.currentTime); onTime?.(event.currentTarget.currentTime); }} />
    {cue && <span className="workspace-app-media__caption" aria-live="off">{cue.text}</span>}
  </span>;
}

/** What is said, line by line; the line being said is marked and any line goes there. */
function TranscriptLines({ cues, time, onSeek }: { cues: MediaCue[]; time: number; onSeek: (at: number) => void }) {
  const active = activeCueIndex(cues, time);
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const element = list.current?.children[active] as HTMLElement | undefined;
    if (element && list.current) list.current.scrollTop = element.offsetTop - list.current.offsetTop - 24;
  }, [active]);
  return <ol ref={list} className="workspace-app-media__transcript" aria-label={t("component.workspace_app_view.media_transcript")}>
    {cues.map((cue, index) => <li key={`${cue.start}:${index}`} aria-current={index === active || undefined}>
      <button type="button" onClick={() => onSeek(cue.start)}>
        <span className="mono">{clockLabel(cue.start)}</span>
        <span>{cue.speaker && <b>{cue.speaker} </b>}{cue.text}</span>
      </button>
    </li>)}
  </ol>;
}

function ChapterList({ chapters, time, onSeek }: { chapters: Array<{ at: number; title: string }>; time: number; onSeek: (at: number) => void }) {
  const active = activeCueIndex(chapters.map((chapter) => ({ start: chapter.at })), time);
  return <ol className="workspace-app-media__chapters" aria-label={t("component.workspace_app_view.media_chapters")}>
    {chapters.map((chapter, index) => <li key={chapter.at} aria-current={index === active || undefined}>
      <button type="button" onClick={() => onSeek(chapter.at)}><span className="mono">{clockLabel(chapter.at)}</span><span>{chapter.title}</span></button>
    </li>)}
  </ol>;
}

/** A player with what goes with it: its chapters to jump to, its words as it plays, captions over a video. */
export function MediaFieldPlayer({ field, documentId, kind }: { field: WorkspaceAppPresentedField; documentId: string; kind: "video" | "audio" }) {
  const [time, setTime] = useState(0);
  const [seek, setSeek] = useState<{ at: number; n: number } | null>(null);
  const go = (at: number) => setSeek((current) => ({ at, n: (current?.n || 0) + 1 }));
  const captions = useCaptions(field.captions_document_id);
  const spoken = useCaptions(field.transcript_document_id);
  const transcript = field.transcript?.length ? field.transcript.map((line) => ({ ...line, end: line.end ?? line.start }))
    .map((line, index, lines) => ({ ...line, end: line.end > line.start ? line.end : lines[index + 1]?.start ?? line.start + 8 }))
    : spoken.data || [];
  return <span className={`workspace-app-media__field-player is-${kind}`} style={kind === "video" ? { "--media-aspect": aspectRatioCss(field.aspect) } as CSSProperties : undefined}>
    {field.compare && kind === "video" && field.compare.document_id
      ? <span className="workspace-app-media__side-by-side">
        <span><small>{field.compare.label || t("component.workspace_app_view.media_compare_other")}</small>
          <MediaPlayer file={{ document_id: field.compare.document_id }} label={field.compare.label || field.label} kind="video" seek={seek} /></span>
        <span><small>{field.label}</small><MediaPlayer file={{ document_id: documentId }} label={field.label} kind="video" seek={seek} onTime={setTime} cues={captions.data} /></span>
      </span>
      : <MediaPlayer file={{ document_id: documentId }} label={field.label} kind={kind} seek={seek} onTime={setTime} cues={captions.data} />}
    {Boolean(field.chapters?.length) && <ChapterList chapters={field.chapters!} time={time} onSeek={go} />}
    {transcript.length > 0 && <TranscriptLines cues={transcript} time={time} onSeek={go} />}
  </span>;
}

/** Two pictures over each other, a handle between them: drag to see the one before and the one after. */
export function CompareSlider({ before, after, label, transparent = false }: { before: string; after: string; label: string; transparent?: boolean }) {
  const [share, setShare] = useState(50);
  return <span className={`workspace-app-media__compare${transparent ? " is-transparent" : ""}`} style={{ "--compare": `${share}%` } as CSSProperties}>
    <img src={after} alt={label} />
    <img className="workspace-app-media__compare-before" src={before} alt="" aria-hidden="true" />
    <span className="workspace-app-media__compare-handle" aria-hidden="true" />
    <input type="range" min={0} max={100} value={share} onChange={(event) => setShare(Number(event.currentTarget.value))}
      aria-label={t("component.workspace_app_view.media_compare_slider", { name: label })} />
  </span>;
}

/** A picture field shown against another one: a slider over the two, or the two side by side. */
export function ImageCompareField({ field, documentId, imageUrl }: { field: WorkspaceAppPresentedField; documentId?: string; imageUrl?: string }) {
  const compare = field.compare!;
  const after = useOriginal(documentId, Boolean(documentId)) || imageUrl || "";
  const before = useOriginal(compare.document_id, Boolean(compare.document_id)) || compare.image_url || "";
  if (!after || !before) return <span className="workspace-app-media__note">{t("component.workflow_review.loading")}</span>;
  if (compare.mode === "slider") return <CompareSlider before={before} after={after} label={field.label} transparent={field.transparent} />;
  return <span className="workspace-app-media__side-by-side">
    <span><small>{compare.label || t("component.workspace_app_view.media_compare_other")}</small><img src={before} alt="" /></span>
    <span><small>{field.label}</small><img src={after} alt={field.label} /></span>
  </span>;
}

/** A video's shots, or one line in several voices: a numbered strip, each one played here. */
export function ClipsField({ field }: { field: WorkspaceAppPresentedField }) {
  const clips = field.clips || [];
  const [open, setOpen] = useState<string | null>(clips[0]?.document_id && clips.length === 1 ? clips[0].document_id : null);
  if (!clips.length) return <>—</>;
  const shown = clips.find((clip) => clip.document_id === open);
  return <span className="workspace-app-media__clips">
    <span className="workspace-app-media__clip-strip" role="list" aria-label={t("component.workspace_app_view.data_images", { label: field.label, count: clips.length })}>
      {clips.map((clip, index) => <button key={clip.document_id} type="button" role="listitem" aria-pressed={clip.document_id === open}
        className="workspace-app-media__clip" onClick={() => setOpen(clip.document_id === open ? null : clip.document_id)}
        style={{ "--media-aspect": aspectRatioCss(field.aspect) } as CSSProperties}>
        <span className="mono">{index + 1}</span><span>{clip.label || `${field.label} ${index + 1}`}</span>
      </button>)}
    </span>
    {shown && <ClipPlayer key={shown.document_id} documentId={shown.document_id} label={shown.label || field.label} />}
  </span>;
}

/** A clip of a ``clips`` field: its file says whether it is a video or a recording. */
function ClipPlayer({ documentId, label }: { documentId: string; label: string }) {
  const kind = useQuery({
    queryKey: ["workspace-app-media-kind", documentId],
    queryFn: async () => mediaKind((await api.documents.get(documentId)).mime_type),
    staleTime: Infinity,
    retry: false,
  });
  if (!kind.data) return <span className="workspace-app-media__note">{t("component.workflow_review.loading")}</span>;
  return kind.data === "audio" || kind.data === "video"
    ? <MediaPlayer file={{ document_id: documentId }} label={label} kind={kind.data} autoStart />
    : <a href={`/viewer/${encodeURIComponent(documentId)}`}>{label}</a>;
}

/** An edit read in time: a lane per track, each segment where it starts and as long as it lasts. */
export function TimelineField({ field }: { field: WorkspaceAppPresentedField }) {
  const { total, lanes } = useMemo(() => timelineLanes(field.segments || []), [field.segments]);
  if (!total) return <>—</>;
  return <span className="workspace-app-media__timeline" role="group" aria-label={`${field.label} · ${clockLabel(total)}`}>
    {lanes.map((lane) => <span key={lane.track} className="workspace-app-media__lane">
      {lane.track && <small>{lane.track}</small>}
      <span className="workspace-app-media__lane-track">
        {lane.segments.map((segment, index) => <span key={`${segment.start}:${index}`} className="workspace-app-media__segment"
          style={{ left: `${segment.left * 100}%`, width: `${segment.width * 100}%` }}
          title={`${segment.label || ""} ${clockLabel(segment.start)}–${clockLabel(segment.end)}`.trim()}>
          {segment.label && <span>{segment.label}</span>}
        </span>)}
      </span>
    </span>)}
    <span className="workspace-app-media__timeline-scale mono" aria-hidden="true"><span>0:00</span><span>{clockLabel(total)}</span></span>
  </span>;
}

/** Several pictures one by one: swipe or press the arrows; the dots say where you are. */
export function MediaCarousel({ count, label, children }: { count: number; label: string; children: ReactNode }) {
  const track = useRef<HTMLDivElement>(null);
  const [index, setIndex] = useState(0);
  const go = (next: number) => {
    const element = track.current;
    if (!element) return;
    const target = Math.max(0, Math.min(count - 1, next));
    element.scrollTo({ left: target * element.clientWidth, behavior: "smooth" });
  };
  return <div className="workspace-app-media__carousel" role="region" aria-roledescription="carousel" aria-label={label}>
    <div ref={track} className="workspace-app-media__carousel-track" onScroll={(event) => {
      const element = event.currentTarget;
      setIndex(Math.round(element.scrollLeft / Math.max(1, element.clientWidth)));
    }}>{children}</div>
    {count > 1 && <>
      <Button size="sm" variant="ghost" className="workspace-app-media__carousel-prev" ariaLabel={t("component.workflow_review.zoom_previous")}
        disabled={index === 0} onClick={() => go(index - 1)}><IconChevronLeft size={16} /></Button>
      <Button size="sm" variant="ghost" className="workspace-app-media__carousel-next" ariaLabel={t("component.workflow_review.zoom_next")}
        disabled={index >= count - 1} onClick={() => go(index + 1)}><IconChevronRight size={16} /></Button>
      <span className="workspace-app-media__dots" aria-live="polite">
        <span className="workspace-app-media__sr">{t("component.workflow_review.zoom_counter", { number: index + 1, total: count })}</span>
        {Array.from({ length: count }, (_, dot) => <i key={dot} data-active={dot === index || undefined} aria-hidden="true" />)}
      </span>
    </>}
  </div>;
}

/** Saves one file to the device. */
export async function downloadFile(file: { document_id: string; title?: string | null; label?: string | null }): Promise<void> {
  const url = await api.documents.download(file.document_id, { cache: true });
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.title || file.label || "file";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** Every file of a gallery saved one after another; there is no archive download, so each comes as its own file. */
export function DownloadAllButton({ files }: { files: WorkspaceAppMediaFile[] }) {
  const [done, setDone] = useState<number | null>(null);
  if (files.length < 2) return null;
  return <Button size="sm" variant="ghost" disabled={done !== null} ariaBusy={done !== null} onClick={async () => {
    setDone(0);
    try {
      for (const [index, file] of files.entries()) {
        await downloadFile(file);
        setDone(index + 1);
      }
    } catch {
      useToastStore.getState().error(t("component.workspace_app_view.media_download_failed"));
    } finally {
      setDone(null);
    }
  }}>
    <IconDownload size={14} />
    <span>{done === null ? t("component.workspace_app_view.media_download_all", { count: files.length })
      : t("component.workspace_app_view.media_downloading", { done, count: files.length })}</span>
  </Button>;
}

/** One version the viewer shows: a file of a group (9:16, 1:1) or of a result's history (v2). */
export type MediaViewerEntry = { key: string; label: string; file: WorkspaceAppMediaFile; updatedAt?: string | null };

/** What the viewer's file action opens: an App entry, with the file (and the moment) filled. */
export type MediaViewerAction = { action: WorkspaceAppCardAction; open: (actionKey: string, values: Record<string, unknown>) => void };

/** A version's name: its own label, its shape, or its number. */
export function versionLabel(file: WorkspaceAppMediaFile, fallback: string): string {
  return file.label || shapeLabel(file.width, file.height, file.aspect) || fallback;
}

/** The gallery's viewer, in the detail drawer: the file at full size or playing, its versions, a comparison,
 *  its details, download and link, the page's file action, and comments pinned to a spot or a moment. */
export function MediaViewer({ entries, initial = 0, options, action, title }: {
  entries: MediaViewerEntry[]; initial?: number; options: WorkspaceAppBlockMedia; action?: MediaViewerAction; title: string;
}) {
  const [index, setIndex] = useState(Math.min(initial, entries.length - 1));
  const [compareWith, setCompareWith] = useState<number | null>(null);
  const [time, setTime] = useState(0);
  const [span, setSpan] = useState<{ start?: number; end?: number }>({});
  const [seek, setSeek] = useState<{ at: number; n: number } | null>(null);
  const [natural, setNatural] = useState<{ width: number; height: number } | null>(null);
  const [anchor, setAnchor] = useState<MediaCommentAnchor | null>(null);
  const [marking, setMarking] = useState(false);
  const [pins, setPins] = useState<Comment[]>([]);
  const entry = entries[index];
  const file = entry.file;
  const kind = mediaKind(file.mime_type);
  const original = useOriginal(file.document_id, kind === "image");
  const other = compareWith !== null && compareWith !== index ? entries[compareWith] : null;
  const otherOriginal = useOriginal(other?.file.document_id, Boolean(other) && kind === "image");
  useEffect(() => { setNatural(null); setAnchor(null); setSpan({}); setMarking(false); }, [file.document_id]);
  const onCommentsLoaded = useCallback((loaded: Comment[]) => {
    setPins(loaded);
    // Once the comment on the marked area or moment is posted, its pin replaces the draft one.
    setAnchor((current) => current && loaded.some((comment) => JSON.stringify(mediaAnchor(comment.anchor)) === JSON.stringify(current))
      ? null : current);
  }, []);
  // The picture's own pixels, for its shape and size when the file does not record them.
  useEffect(() => {
    if (!original) return undefined;
    let alive = true;
    const probe = new Image();
    probe.onload = () => { if (alive) setNatural({ width: probe.naturalWidth, height: probe.naturalHeight }); };
    probe.src = original;
    return () => { alive = false; };
  }, [original]);
  const go = (at: number) => setSeek((current) => ({ at, n: (current?.n || 0) + 1 }));
  const width = file.width || natural?.width;
  const height = file.height || natural?.height;
  const regions = pins.map((comment) => ({ comment, anchor: mediaAnchor(comment.anchor) }))
    .filter((pin): pin is { comment: Comment; anchor: Extract<MediaCommentAnchor, { type: "media_region" }> } => pin.anchor?.type === "media_region");
  const moments = pins.map((comment) => mediaAnchor(comment.anchor)).filter((pin): pin is Extract<MediaCommentAnchor, { type: "media_time" }> => pin?.type === "media_time");
  const canCompare = entries.length > 1 && (kind === "image" || kind === "video");

  const picture = original
    ? <span className="workspace-app-media__stage-picture">
      <span className={`workspace-app-media__picture${options.transparent ? " is-transparent" : ""}`}>
        {marking
          ? <RegionMarker src={original} label={entry.label} onMark={(region) => {
            setMarking(false);
            setAnchor({ type: "media_region", ...region, label: t("component.workspace_app_view.media_comment_spot") });
          }} />
          : <ImagePreview src={original} alt={entry.label} onError={() => undefined} />}
        {!marking && regions.map(({ comment, anchor: region }, number) => <span key={comment.id} className="workspace-app-media__pin"
          style={{ left: `${region.x * 100}%`, top: `${region.y * 100}%`, width: `${region.w * 100}%`, height: `${region.h * 100}%` }}
          title={comment.content}><b>{number + 1}</b></span>)}
        {anchor?.type === "media_region" && !marking && <span className="workspace-app-media__pin is-new"
          style={{ left: `${anchor.x * 100}%`, top: `${anchor.y * 100}%`, width: `${anchor.w * 100}%`, height: `${anchor.h * 100}%` }} />}
      </span>
    </span>
    : <p className="workspace-app-media__note">{t("component.workflow_review.loading")}</p>;

  const stage = other && kind === "image"
    ? (original && otherOriginal
      ? <CompareSlider before={otherOriginal} after={original} label={entry.label} transparent={options.transparent} />
      : <p className="workspace-app-media__note">{t("component.workflow_review.loading")}</p>)
    : other && kind === "video"
      ? <span className="workspace-app-media__side-by-side">
        <span><small>{other.label}</small><MediaPlayer file={other.file} label={other.label} kind="video" seek={seek} /></span>
        <span><small>{entry.label}</small><MediaPlayer file={file} label={entry.label} kind="video" seek={seek} onTime={setTime} /></span>
      </span>
      : kind === "image" ? picture
        : kind === "video" || kind === "audio" ? <MediaPlayer key={file.document_id} file={file} label={entry.label} kind={kind} seek={seek} onTime={setTime} />
          : <a href={`/viewer/${encodeURIComponent(file.document_id)}`}>{entry.label}</a>;

  const playable = kind === "video" || kind === "audio";
  const fileAction = action?.action;
  const facts: Array<[string, string]> = [
    [t("component.workspace_app_view.media_detail_shape"), shapeLabel(width, height, file.aspect) || ""],
    [t("component.workspace_app_view.media_detail_pixels"), pixelSizeLabel(width, height) || ""],
    [t("component.workspace_app_view.media_detail_length"), file.duration ? formatDuration(file.duration, getLocale()) : ""],
    [t("component.workspace_app_view.media_detail_file_size"), file.file_size ? formatFileSize(file.file_size, getLocale()) : ""],
    [t("component.workspace_app_view.media_detail_source"), (file.details?.source || "").replace(/_/g, " ")],
    [t("component.workspace_app_view.media_detail_model"), [file.details?.model, file.details?.provider].filter(Boolean).join(" · ")],
    [t("component.workspace_app_view.media_detail_made"), file.details?.created_at ? formatDateLong(file.details.created_at) : ""],
  ].filter(([, value]) => value) as Array<[string, string]>;

  return <div className="workspace-app-media__viewer">
    {entries.length > 1 && <TabSwitcher size="sm" ariaLabel={t("component.workspace_app_view.media_versions", { name: title })}
      tabs={entries.map((item, at) => ({ key: String(at), label: item.label }))} value={String(index)}
      onChange={(key) => { setIndex(Number(key)); setCompareWith(null); }} />}
    <div className="workspace-app-media__stage" style={kind === "video" ? { "--media-aspect": aspectRatioCss(options.aspect) } as CSSProperties : undefined}>{stage}</div>
    {options.size_label && kind === "image" && pixelSizeLabel(width, height) && <p className="workspace-app-media__note mono">{pixelSizeLabel(width, height)}</p>}
    {canCompare && <div className="workspace-app-media__toolbar">
      <span>{t("component.workspace_app_view.media_compare")}</span>
      {entries.map((item, at) => at !== index && <Button key={item.key} size="sm" variant={compareWith === at ? "primary" : "outline"}
        ariaPressed={compareWith === at} onClick={() => setCompareWith(compareWith === at ? null : at)}>{item.label}</Button>)}
    </div>}
    {playable && (fileAction?.time_input || fileAction?.start_input || options.comments) && <div className="workspace-app-media__toolbar">
      <span className="mono" aria-live="off">{clockLabel(time)}</span>
      {fileAction?.start_input && <>
        <Button size="sm" variant="outline" onClick={() => setSpan((current) => ({ ...current, start: time }))}>
          {t("component.workspace_app_view.media_mark_start")}{span.start !== undefined ? ` · ${clockLabel(span.start)}` : ""}</Button>
        <Button size="sm" variant="outline" onClick={() => setSpan((current) => ({ ...current, end: time }))}>
          {t("component.workspace_app_view.media_mark_end")}{span.end !== undefined ? ` · ${clockLabel(span.end)}` : ""}</Button>
        {span.start !== undefined && <Button size="sm" variant="ghost" onClick={() => go(span.start!)}>{t("component.workspace_app_view.media_play_span")}</Button>}
      </>}
      {options.comments && <Button size="sm" variant="ghost" onClick={() => setAnchor({ type: "media_time", time: Math.round(time * 10) / 10, label: clockLabel(time) })}>
        <IconComment size={14} /><span>{t("component.workspace_app_view.media_comment_at", { time: clockLabel(time) })}</span></Button>}
    </div>}
    {moments.length > 0 && <ol className="workspace-app-media__chapters" aria-label={t("component.workspace_app_view.media_comment_moments")}>
      {moments.map((moment, at) => <li key={`${moment.time}:${at}`}><button type="button" onClick={() => go(moment.time)}>
        <span className="mono">{clockLabel(moment.time)}</span><span>{t("component.workspace_app_view.media_comment_moment")}</span></button></li>)}
    </ol>}
    <div className="workspace-app-media__actions">
      {fileAction && action && <Button size="sm" disabled={Boolean(fileAction.start_input) && !(span.start !== undefined && span.end !== undefined && span.end > span.start)}
        onClick={() => action.open(fileAction.action, fileActionValues(fileAction, { document_id: file.document_id, label: file.label || file.title },
          { time, start: span.start, end: span.end }))}>
        {fileAction.label}{fileAction.time_input && playable ? ` · ${clockLabel(time)}` : ""}</Button>}
      {options.download && <Button size="sm" variant="outline" onClick={() => void downloadFile(file).catch(() =>
        useToastStore.getState().error(t("component.workspace_app_view.media_download_failed")))}>
        <IconDownload size={14} /><span>{t("component.workspace_app_view.media_download")}</span></Button>}
      <Button size="sm" variant="ghost" onClick={async () => {
        const link = `${window.location.origin}/viewer/${encodeURIComponent(file.document_id)}`;
        const toast = useToastStore.getState();
        if (await copyText(link)) toast.success(t("component.workspace_app_view.media_link_copied"));
        else toast.error(t("component.workspace_app_view.data_copy_failed"));
      }}><IconLink size={14} /><span>{t("component.workspace_app_view.media_copy_link")}</span></Button>
      <a className="workspace-app-media__open" href={`/viewer/${encodeURIComponent(file.document_id)}`}>{t("component.workspace_app_view.media_open_to_share")}</a>
    </div>
    {options.details && (facts.length > 0 || file.details?.prompt) && <dl className="workspace-app-media__details">
      {facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      {file.details?.prompt && <div className="is-wide"><dt>{t("component.workspace_app_view.media_detail_prompt")}</dt><dd>{file.details.prompt}</dd></div>}
    </dl>}
    {options.comments && <div className="workspace-app-media__comments">
      {kind === "image" && !marking && <Button size="sm" variant="outline" onClick={() => setMarking(true)}>
        <IconComment size={14} /><span>{t("component.workspace_app_view.media_mark_spot")}</span></Button>}
      {marking && <p className="workspace-app-media__note">{t("component.workspace_app_view.media_mark_hint")}</p>}
      <CommentThread key={file.document_id} resourceType="document" resourceId={file.document_id} canComment
        anchor={anchor} onCommentsLoaded={onCommentsLoaded}
        onSelectComment={(comment) => { const at = mediaAnchor(comment.anchor); if (at?.type === "media_time") go(at.time); }} />
    </div>}
  </div>;
}

/** Draw a box on a picture (press, drag, let go): where the comment is about. */
function RegionMarker({ src, label, onMark }: { src: string; label: string; onMark: (region: { x: number; y: number; w: number; h: number }) => void }) {
  const [from, setFrom] = useState<{ x: number; y: number } | null>(null);
  const [to, setTo] = useState<{ x: number; y: number } | null>(null);
  const share = (event: ReactPointerEvent<HTMLSpanElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    return { x: (event.clientX - box.left) / box.width, y: (event.clientY - box.top) / box.height };
  };
  const drawn = from && to ? regionFromDrag(from, to) : null;
  return <span className="workspace-app-media__marker" role="application" aria-label={t("component.workspace_app_view.media_mark_on", { name: label })}
    onPointerDown={(event) => { event.currentTarget.setPointerCapture(event.pointerId); setFrom(share(event)); setTo(share(event)); }}
    onPointerMove={(event) => { if (from) setTo(share(event)); }}
    onPointerUp={(event) => {
      const region = from ? regionFromDrag(from, share(event)) : null;
      setFrom(null); setTo(null);
      if (region) onMark(region);
    }}>
    <img src={src} alt={label} draggable={false} />
    {drawn && <span className="workspace-app-media__pin is-new" style={{ left: `${drawn.x * 100}%`, top: `${drawn.y * 100}%`, width: `${drawn.w * 100}%`, height: `${drawn.h * 100}%` }} />}
  </span>;
}
