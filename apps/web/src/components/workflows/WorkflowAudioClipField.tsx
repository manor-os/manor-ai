/**
 * A part of a track picked by sliding a window over its waveform: drag the
 * window to move it, drag an edge to make it longer or shorter, and play to
 * hear just that part. The track is the audio chosen in another input.
 */
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { clock, readPeaks } from "../ui/AudioWaveform";
import { IconPause, IconPlay } from "../icons";

const BARS = 160;
const AUDIO_NAME = /\.(mp3|wav|m4a|aac|flac|ogg|oga|opus|aif|aiff|weba)$/i;

export type AudioClipSource = { document_id: string; name?: string };

/** The first audio file among an input's documents (a song may come with covers and notes). */
export function audioClipSource(value: unknown): AudioClipSource | null {
  // A JSON input's draft can still be its text ("[]" before anything is added).
  if (typeof value === "string" && /^\s*[[{]/.test(value)) {
    try {
      return audioClipSource(JSON.parse(value));
    } catch {
      return null;
    }
  }
  // A single Knowledge file input holds the document id itself.
  if (typeof value === "string") return value.trim() ? { document_id: value.trim() } : null;
  const items = Array.isArray(value) ? value : value && typeof value === "object" ? [value] : [];
  const documents = items.filter((item): item is AudioClipSource => Boolean(
    item && typeof item === "object" && typeof (item as AudioClipSource).document_id === "string",
  ));
  return documents.find((item) => AUDIO_NAME.test(item.name || "")) || null;
}

function seconds(value: unknown): number | null {
  const number = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(number) ? number : null;
}

export default function WorkflowAudioClipField({
  id, source, start, end, clipSeconds, minSeconds = 5, maxSeconds = 60, disabled, onChange, describedBy,
  allowAuto = false, auto = false, onAutoChange,
}: {
  id: string;
  source: AudioClipSource | null;
  start: unknown;
  end: unknown;
  clipSeconds: number;
  /** The shortest and longest part that may be picked, in seconds. */
  minSeconds?: number;
  maxSeconds?: number;
  disabled?: boolean;
  onChange: (start: number, end: number) => void;
  describedBy?: string;
  /** Offer "let the AI pick" beside picking by hand; ``auto`` is that choice. */
  allowAuto?: boolean;
  auto?: boolean;
  onAutoChange?: (auto: boolean) => void;
}) {
  const [blob, setBlob] = useState<{ id: string; url: string; data: Blob } | null>(null);
  const [failed, setFailed] = useState(false);
  const [peaks, setPeaks] = useState<number[]>([]);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const audio = useRef<HTMLAudioElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<{ mode: "move" | "start" | "end"; x: number; start: number; end: number; moved: boolean } | null>(null);
  const longest = Math.max(minSeconds, maxSeconds);

  useEffect(() => {
    if (!source) return;
    let alive = true;
    let url = "";
    setFailed(false);
    setPeaks([]);
    setDuration(0);
    api.documents.previewBlob(source.document_id, { cache: true }).then((data) => {
      if (!alive) return;
      url = URL.createObjectURL(data);
      setBlob({ id: source.document_id, url, data });
      readPeaks(url, data, BARS).then((values) => { if (alive) setPeaks(values); }).catch(() => undefined);
    }).catch(() => { if (alive) setFailed(true); });
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [source?.document_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Nothing picked yet: the first clipSeconds of the song.
  const picked = useMemo(() => {
    const from = seconds(start);
    const to = seconds(end);
    if (from === null || to === null || to <= from) return { start: 0, end: Math.min(clipSeconds, longest, duration || clipSeconds) };
    // A part picked before the limit existed (or typed in) is kept to the longest allowed.
    const capped = Math.min(to, from + longest);
    return { start: from, end: duration ? Math.min(capped, duration) : capped };
  }, [start, end, clipSeconds, duration, longest]);
  useEffect(() => {
    // Only a hand-picked part is filled in; with the AI picking there is nothing to set.
    if (allowAuto && auto) return;
    if (duration && (seconds(start) === null || seconds(end) === null)) onChange(picked.start, picked.end);
  }, [duration, auto]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const player = audio.current;
    if (player && playing && time >= picked.end) {
      player.pause();
      player.currentTime = picked.start;
    }
  }, [time, playing, picked]);

  const modes = allowAuto ? <div className="workflow-starter-input-segments workflow-audio-clip__modes" role="radiogroup"
    aria-label={t("component.workflow_audio_clip.mode")}>
    {([true, false] as const).map((value) => <button key={String(value)} type="button" role="radio"
      className="workflow-starter-input-segment" aria-checked={auto === value} disabled={disabled}
      onClick={() => onAutoChange?.(value)}>
      {t(value ? "component.workflow_audio_clip.mode_auto" : "component.workflow_audio_clip.mode_manual")}
    </button>)}
  </div> : null;
  if (allowAuto && auto) {
    return <div className="workflow-audio-clip__picker">
      {modes}
      <small id={id} className="workflow-starter-input-help workflow-audio-clip__empty" aria-describedby={describedBy}>
        {t("component.workflow_audio_clip.auto_help")}
      </small>
    </div>;
  }
  if (!source) {
    if (modes) return <div className="workflow-audio-clip__picker">{modes}<small id={id} className="workflow-starter-input-help workflow-audio-clip__empty" aria-describedby={describedBy}>{t("component.workflow_audio_clip.choose_audio")}</small></div>;
    return <small id={id} className="workflow-starter-input-help workflow-audio-clip__empty" aria-describedby={describedBy}>{t("component.workflow_audio_clip.choose_audio")}</small>;
  }
  if (failed) {
    return <small id={id} className="workflow-starter-input-help workflow-audio-clip__empty" role="status">{t("component.workflow_audio_clip.unavailable")}</small>;
  }

  const span = duration || Math.max(picked.end, clipSeconds);
  const left = (picked.start / span) * 100;
  const width = ((picked.end - picked.start) / span) * 100;
  const shape = peaks.length ? peaks : Array.from({ length: BARS }, () => 0.12);
  const play = (fromStart = false) => {
    const player = audio.current;
    if (!player) return;
    if (!player.paused && !fromStart) {
      player.pause();
      return;
    }
    if (fromStart || player.currentTime < picked.start || player.currentTime >= picked.end) player.currentTime = picked.start;
    void player.play().catch(() => undefined);
  };
  const place = (from: number, to: number) => {
    const length = Math.min(longest, Math.max(minSeconds, to - from));
    const clampedStart = Math.min(Math.max(0, from), Math.max(0, span - length));
    onChange(Math.round(clampedStart * 10) / 10, Math.round(Math.min(span, clampedStart + length) * 10) / 10);
  };
  const grab = (mode: "move" | "start" | "end") => (event: ReactPointerEvent<HTMLElement>) => {
    if (disabled) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { mode, x: event.clientX, start: picked.start, end: picked.end, moved: false };
  };
  const move = (event: ReactPointerEvent<HTMLElement>) => {
    const box = track.current?.getBoundingClientRect();
    const held = drag.current;
    if (!held || !box) return;
    if (Math.abs(event.clientX - held.x) < 3 && !held.moved) return;
    held.moved = true;
    const delta = ((event.clientX - held.x) / box.width) * span;
    if (held.mode === "move") place(held.start + delta, held.end + delta);
    else if (held.mode === "start") {
      const from = Math.min(Math.max(held.start + delta, held.end - longest, 0), held.end - minSeconds);
      onChange(Math.round(from * 10) / 10, held.end);
    } else {
      const to = Math.max(Math.min(held.end + delta, held.start + longest, span), held.start + minSeconds);
      onChange(held.start, Math.round(to * 10) / 10);
    }
  };
  // A press on the window that does not move it plays the part.
  const release = () => {
    const held = drag.current;
    drag.current = null;
    if (held && held.mode === "move" && !held.moved) play(true);
  };
  // A click outside the window moves it there, centred on the click.
  const jump = (event: ReactPointerEvent<HTMLDivElement>) => {
    const box = track.current?.getBoundingClientRect();
    if (disabled || !box || event.target !== event.currentTarget) return;
    const length = picked.end - picked.start;
    place(((event.clientX - box.left) / box.width) * span - length / 2, ((event.clientX - box.left) / box.width) * span + length / 2);
  };
  const nudge = (step: number) => place(picked.start + step, picked.end + step);

  const picker = <div className="workflow-audio-clip">
    {blob && <audio ref={audio} src={blob.url} preload="metadata"
      onLoadedMetadata={(event) => setDuration(event.currentTarget.duration || 0)}
      onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
      onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)}
      onError={() => setFailed(true)} />}
    <button type="button" className="audio-waveform__play" onClick={() => play()} disabled={!blob}
      aria-label={t(playing ? "component.workflow_audio_clip.pause" : "component.workflow_audio_clip.play")}>
      {playing ? <IconPause size={16} /> : <IconPlay size={16} />}
    </button>
    <div ref={track} className="workflow-audio-clip__track" onPointerDown={jump}>
      <div className="workflow-audio-clip__bars" aria-hidden="true">
        {shape.map((peak, index) => {
          const at = ((index + 0.5) / shape.length) * span;
          return <i key={index} data-in={at >= picked.start && at <= picked.end || undefined}
            data-played={playing && at >= picked.start && at <= time || undefined}
            style={{ height: `${Math.round(peak * 100)}%` }} />;
        })}
      </div>
      <div
        id={id}
        className="workflow-audio-clip__window"
        style={{ left: `${left}%`, width: `${width}%` }}
        role="slider"
        tabIndex={disabled ? -1 : 0}
        aria-label={t("component.workflow_audio_clip.window")}
        aria-describedby={describedBy}
        aria-valuemin={0}
        aria-valuemax={Math.round(span)}
        aria-valuenow={Math.round(picked.start)}
        aria-valuetext={`${clock(picked.start)} – ${clock(picked.end)}`}
        aria-disabled={disabled || undefined}
        onPointerDown={grab("move")}
        onPointerMove={move}
        onPointerUp={release}
        onPointerCancel={release}
        onKeyDown={(event) => {
          if (event.key === "ArrowRight") { event.preventDefault(); nudge(event.shiftKey ? 5 : 1); }
          else if (event.key === "ArrowLeft") { event.preventDefault(); nudge(event.shiftKey ? -5 : -1); }
          else if (event.key === " " || event.key === "Enter") { event.preventDefault(); play(); }
        }}
      >
        {playing && time >= picked.start && time <= picked.end && <span className="workflow-audio-clip__playhead" aria-hidden="true"
          style={{ left: `${((time - picked.start) / Math.max(0.1, picked.end - picked.start)) * 100}%` }} />}
        <span className="workflow-audio-clip__edge workflow-audio-clip__edge--start" aria-hidden="true"
          onPointerDown={grab("start")} onPointerMove={move} onPointerUp={release} onPointerCancel={release} />
        <span className="workflow-audio-clip__edge workflow-audio-clip__edge--end" aria-hidden="true"
          onPointerDown={grab("end")} onPointerMove={move} onPointerUp={release} onPointerCancel={release} />
      </div>
    </div>
    <span className="workflow-audio-clip__time mono">
      {clock(picked.start)}–{clock(picked.end)} · {Math.round(picked.end - picked.start)}s
    </span>
  </div>;
  return modes ? <div className="workflow-audio-clip__picker">{modes}{picker}</div> : picker;
}
