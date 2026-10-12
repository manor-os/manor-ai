# Captions and audio

## Where timing comes from

- **Generated narration (TTS):** one audio file per caption-sized segment.
  Measure each with `probe_media`, then normalize it with
  `normalize_audio_loudness(input_path=..., output_name=..., target_lufs=-16, target_duration_seconds=<positive seconds>)`.
  `target_duration_seconds` time-stretches the audio: pass the measured
  duration unless you are deliberately retiming a line to fit a block, and then
  keep the tempo change small (a few percent; large stretches sound unnatural
  and are rejected). Pass the normalized outputs to `build_narration_timeline`;
  it keeps `require_normalized_segments` true by default and rejects a raw or
  duration-less segment with the error code narration_segment_not_normalized.
  Its segment times and cues are the caption source of truth.
- **Recorded speech** (an interview, a voice memo, any audio without TTS
  generation provenance): `build_narration_timeline` rejects it
  (narration_segment_provenance_unverified), so do not route it there. Call
  `transcribe_video` with `start_seconds` / `end_seconds` windows for segment
  timestamps. When it reports timestamps_available=false, take the pauses from
  the silence intervals `analyze_audio` reports, allocate the script's phrases
  between them by hand, and mark that timing "estimated" in `STORYBOARD.md`.

## Word timing by interpolation

Inside a segment, spread words by character weight:

```js
function wordTimes(segment) {           // {start, end, words: ["Your", "phone", ...]}
  const weights = segment.words.map((w) => Math.max(1, Array.from(w).length));
  const total = weights.reduce((a, b) => a + b, 0);
  const span = segment.end - segment.start;
  let acc = 0;
  return segment.words.map((word, i) => {
    const start = segment.start + (span * acc) / total;
    acc += weights[i];
    return { word, start, end: segment.start + (span * acc) / total };
  });
}
```

For Chinese, each character is a word. Expect 0.3–0.5 s error; never claim measured word timing.

## Grouping and highlight

- Latin scripts: 2–4 words per group; CJK: 4–8 characters. Break at
  punctuation and phrase boundaries, never across a sentence end.
- Show a group for at least 0.5 s; avoid one-word groups except a phrase's last
  word. At most two lines.
- Current word: accent colour or heavier weight. Spoken words: full opacity.
  Upcoming words in the group: about 40% opacity, so the eye can read ahead.

```js
words.forEach((w) => {
  tl.set(`#${w.id}`, { opacity: 1, color: "var(--accent)" }, snap(w.start));
  tl.set(`#${w.id}`, { color: "var(--ink)" }, snap(w.end));
});
```

- Clamp the last cue to the end of its audio on the composition timeline:
  `end = Math.min(cue.end, narrationStart + narrationDuration, compositionDuration)`,
  where `narrationStart` is the audio clip's `data-start` and
  `narrationDuration` comes from `probe_media`. A cue that outlives its audio
  flashes text over silence.
- Captions are live HTML text in an installed or staged font, inside the safe zone.

## The last word

The final spoken word must finish before the picture ends. Set the composition
duration to at least the narration end plus a brand hold (typically 1–2 s), and
let music fade under that hold. Review a snapshot inside the hold and check the
MP4 tail with `analyze_audio`.

## Stems and mix

1. Normalize every stem with `normalize_audio_loudness` **before**
   `video_edit(operation="push_assets", reference_paths=[...])`. Typical
   targets: narration −16 LUFS, music bed −20 to −24 LUFS, ambience −24 LUFS,
   effects as needed.
2. Place each stem at unity gain. Every timed `<audio>` is a direct root child
   with `id`, `class="clip"`, `data-start`, `data-duration`,
   `data-track-index` and `data-volume="1"`.
3. Duck music under narration by 8–12 dB with direct `volume` tweens in the
   inline master timeline, written as `tl.to("#music", {volume: ...})` on the
   master timeline variable `tl` (the renderer looks for that pattern and
   samples element volume while seeking; callbacks do not run there). Gain is
   linear amplitude:
   −8 dB = 0.40, −10 dB = 0.32, −12 dB = 0.25.

```js
narrationSpans.forEach(({ start, end }) => {
  tl.to("#music", { volume: 0.32, duration: 0.25, ease: "none" }, snap(start - 0.2));
  tl.to("#music", { volume: 1, duration: 0.4, ease: "none" }, snap(end + 0.1));
});
```

4. Delivery default: −16 to −14 LUFS integrated, true peak ≤ −1 dBTP, checked
   on the final MP4 with `analyze_audio` (`target_lufs_min`, `target_lufs_max`,
   `max_true_peak_dbfs`). A selected playbook's range replaces this default.
5. Beat-driven sound: there is no beat detection; place hits and whooshes on
   `beat(n)` from `references/gsap-timelines.md` and list the cues in `STORYBOARD.md`.
