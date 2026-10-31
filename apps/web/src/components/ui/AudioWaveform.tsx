/**
 * A track as its waveform: play, pause and click anywhere to go there. The
 * peaks are read from the audio itself; until they are (or when the browser
 * cannot decode it) the bar is flat but still plays and seeks. While it plays
 * the bars move with the music's live levels, unless motion is reduced.
 */
import { useEffect, useRef, useState } from "react";
import { t } from "../../lib/i18n";
import { IconPause, IconPlay } from "../icons";

const BARS = 96;

export function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** A track's loudness in ``bars`` buckets (0.06–1), read from its decoded audio. */
export async function readPeaks(url: string, bytes?: Blob, bars = BARS): Promise<number[]> {
  const Context = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Context) return [];
  const context = new Context();
  try {
    // A blob: URL cannot be fetched under the app's connect-src; the caller hands over the bytes instead.
    const buffer = await context.decodeAudioData(await (bytes ?? await fetch(url)).arrayBuffer());
    const data = buffer.getChannelData(0);
    const step = Math.max(1, Math.floor(data.length / bars));
    const peaks = Array.from({ length: bars }, (_, bar) => {
      let peak = 0;
      for (let index = bar * step; index < Math.min(data.length, (bar + 1) * step); index += 16) {
        peak = Math.max(peak, Math.abs(data[index]));
      }
      return peak;
    });
    const loudest = Math.max(...peaks, 0.01);
    return peaks.map((peak) => Math.max(0.06, peak / loudest));
  } finally {
    void context.close();
  }
}

export default function AudioWaveform({ src, data, label, seek, onError, autoPlay = false }: {
  src: string;
  /** The track's bytes when ``src`` is a blob: URL made from them, to read its waveform. */
  data?: Blob;
  label: string;
  /** Start as soon as it can: the person already pressed play to load it. */
  autoPlay?: boolean;
  /** Go to this second and play (a section picked elsewhere); ``n`` makes a repeat pick count. */
  seek?: { at: number; n: number } | null;
  onError?: () => void;
}) {
  const audio = useRef<HTMLAudioElement>(null);
  const [peaks, setPeaks] = useState<number[]>([]);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  // Live levels, one per bar, while it plays; null shows the still waveform.
  const [levels, setLevels] = useState<number[] | null>(null);
  const live = useRef<{ context: AudioContext; analyser: AnalyserNode } | null>(null);

  useEffect(() => () => { void live.current?.context.close().catch(() => undefined); }, []);
  useEffect(() => {
    const player = audio.current;
    const reduced = typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!playing || !player || reduced) {
      setLevels(null);
      return;
    }
    if (!live.current) {
      const Context = window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Context) return;
      try {
        const context = new Context();
        const analyser = context.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.7;
        // The element now plays through the analyser, so it must reach the speakers from there.
        context.createMediaElementSource(player).connect(analyser);
        analyser.connect(context.destination);
        live.current = { context, analyser };
      } catch {
        return;
      }
    }
    const { context, analyser } = live.current;
    void context.resume().catch(() => undefined);
    const bins = new Uint8Array(analyser.frequencyBinCount);
    let frame = 0;
    const tick = () => {
      analyser.getByteFrequencyData(bins);
      // Spread the bass-to-presence bands (about 0.3–5.5 kHz, where a song has energy) across the
      // bars, so neighbours move differently and none sits still on a silent treble band.
      setLevels(Array.from({ length: BARS }, (_, bar) => bins[2 + ((bar * 7) % 31)] / 255));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing]);

  useEffect(() => {
    let alive = true;
    setPeaks([]);
    readPeaks(src, data).then((values) => { if (alive) setPeaks(values); }).catch(() => undefined);
    return () => { alive = false; };
  }, [src, data]);

  useEffect(() => {
    const player = audio.current;
    if (!seek || !player) return;
    player.currentTime = Math.max(0, seek.at);
    void player.play().catch(() => undefined);
  }, [seek]);

  const toggle = () => {
    const player = audio.current;
    if (!player) return;
    if (player.paused) void player.play().catch(() => undefined);
    else player.pause();
  };
  const goTo = (fraction: number) => {
    const player = audio.current;
    if (!player || !duration) return;
    player.currentTime = Math.min(duration, Math.max(0, fraction * duration));
    setTime(player.currentTime);
  };
  const played = duration ? time / duration : 0;
  const shape = peaks.length ? peaks : Array.from({ length: BARS }, () => 0.12);
  const bars = levels ? shape.map((peak, index) => Math.min(1, Math.max(0.08, peak * 0.45 + (levels[index] ?? 0) * 0.7))) : shape;

  return <div className="audio-waveform">
    <audio ref={audio} src={src} preload="metadata" aria-label={label}
      onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)}
      onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
      onLoadedMetadata={(event) => {
        setDuration(event.currentTarget.duration || 0);
        if (autoPlay) void event.currentTarget.play().catch(() => undefined);
      }}
      onError={onError} />
    <button type="button" className="audio-waveform__play" onClick={toggle}
      aria-label={t(playing ? "component.audio_waveform.pause" : "component.audio_waveform.play", { name: label })}>
      {playing ? <IconPause size={16} /> : <IconPlay size={16} />}
    </button>
    <div className="audio-waveform__bars" data-live={levels ? "true" : undefined} role="slider" tabIndex={0}
      aria-label={t("component.audio_waveform.position", { name: label })}
      aria-valuemin={0} aria-valuemax={Math.round(duration)} aria-valuenow={Math.round(time)} aria-valuetext={clock(time)}
      onClick={(event) => {
        const box = event.currentTarget.getBoundingClientRect();
        goTo((event.clientX - box.left) / box.width);
      }}
      onKeyDown={(event) => {
        if (event.key === "ArrowRight") goTo((time + 5) / (duration || 1));
        else if (event.key === "ArrowLeft") goTo((time - 5) / (duration || 1));
        else if (event.key === " " || event.key === "Enter") { event.preventDefault(); toggle(); }
      }}>
      {bars.map((peak, index) => <i key={index} data-played={index / bars.length < played || undefined}
        style={{ height: `${Math.round(peak * 100)}%` }} />)}
    </div>
    <span className="audio-waveform__time mono">{clock(time)} / {clock(duration)}</span>
  </div>;
}
