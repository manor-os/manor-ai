# Production and precise edit handoff

Keep the source script, first/last-frame canvas, and actual edit aligned for `production_mode=adapt` and `production_mode=generate`. Preserve originals and assign every real or generated asset the canvas shot ID. Record the actual Knowledge path/document ID, measured duration, dimensions, source in/out, generation job/model or camera source, and rights/claim status. Measure narration before locking timings; revise the canvas and edit decision list when real takes differ.

## Reuse an unchanged finished source

`production_mode=reuse` is a review-and-handoff route, not a minimal edit. Prefer the caller-authorized source `Document` ID plus expected SHA-256; an authorized attachment path remains valid if no Document exists. Probe the chosen source, sample the full measured `0..duration` interval with Document-aware `render_frame_samples` when a Document exists, and read the returned frames. With attachment-only input, use only tools that support its exact authorized attachment path; do not invent Document identity or hash. `analyze_audio` does not support `uploads/chat` attachment paths, so do not promise that measurement there; report a required unavailable audio check explicitly. Keep timestamped, line-level visual/text observations, including every readable subtitle/text line that the brief requires and the evidence limitation where a line or timing cannot be established. Select transcription only when the accepted QA target needs measured dialogue/cue evidence; untimed transcript text or music symbols are not lyric evidence. Check format, duration, source identity, rights, factual claims, and destination fit from actual evidence. Never re-encode, trim, compose, burn subtitles, create a CANVAS, or require an editable timeline solely to reuse the source.

The returned master is the original `source_document_id` and observed source SHA-256 when available, otherwise the original authorized attachment reference, never a new file under a project directory. A review record and frame samples can live under distinct output paths; keep those paths out of source identity. Explicit creative acceptance may resolve only the accepted subjective concern. It does not replace technical checks, rights or factual checks, an independently assigned reviewer's decision, platform authorization, or any pending approval.

## Audio-led captions (`adapt` and `generate` only)

When a brief requires lyric captions or other captions tied to supplied audio,
the canvas and EDL must name a canonical text source and durable cue evidence.
Use a supplied lyric document or timed lyric file first; otherwise retain only
the measured result of Manor transcription. Text-only transcription with no
timestamps, musical symbols, uncertain words, or omitted lines cannot prove
timed lyrics. A lyric visualizer may prove readable text at sampled timestamps,
but does not prove word-level alignment unless it supplies measured cue data.
Never fill missing words from inference, translate them without instruction, or
drop a required line quietly.

For a reference video's captions and a separately supplied soundtrack, call
`measure_media_audio_alignment` with the reference and target Knowledge
Documents and approximately corresponding source spans. Retain the measured
hashes, bounded search range, correlations and `target_time_offset_seconds`.
Map reference cues with `target_time = reference_time + offset`, then subtract
the final excerpt's source start. The local match does not certify vocal cue
boundaries, a whole recording, or the final rendered soundtrack. Failed or
ambiguous alignment must remain explicit; do not fabricate an offset. Continue
independent picture/poster assets where their creative inputs are already
supported, and hold the final captioned delivery until timing is reviewed.

For every accepted cue, retain source-audio `start`/`end`, output-timeline
`start`/`end`, text, source document, and timing provenance. If an excerpt
begins at `source_start`, map a cue into the output by subtracting that offset;
reject it when any part falls outside the selected source span. Generate clean
picture clips without lettering, build the ASS/VTT/SRT with `align_subtitles`,
and burn it through `compose_video_timeline` or place it in `video-edit`.
Use source-led or generated multi-shot imagery to interpret the audio's mood,
phrases, and imagery, but record that it is creative interpretation rather
than a factual depiction. Keep typography restrained and readable in the safe
area. If required captions cannot be proven accurate and readable, preserve the
picture master and return the caption evidence blocker; an uncaptioned final is
not a substitute.

When the supplied song is the intended soundtrack, keep its verified file and
selected span as the music track. Do not substitute generated music, turn it
into narration, or automatically duck it beneath an unrelated voice track.

## Assembly (`adapt` and `generate` only)

1. Produce or collect the shot assets. Make only the needed AI clips; save prompts, job IDs, selected takes, and **completed** Knowledge paths/document IDs. Verify recurring character, location, product, dish, and prop identity at cut points. Use `probe_media` and sampled frames to reject incorrect or truncated takes.
2. Prepare a timecoded edit decision list (EDL). For every shot include `shot_id`, exact source path and document ID, source in/out, timeline in/out, speed, track/layer, transition and overlap, caption/graphic cues, dialogue/music/Foley start/end, and fallback take. Source and timeline times are different; do not substitute one for the other. The ordered timeline must have no unexplained gaps or collisions. Reconcile each cut with measured speech, visible action, Foley, and the selected music's real phrasing. A cut may lead or trail a beat when the image calls for it; do not impose a uniform interval or claim that arbitrary cuts are beat-synced.
3. For a footage-led edit, call `merge_videos(clips=[...], include_source_audio=false)` with the accepted source clips, exact source trims, format, and output Knowledge path. Each `merge_videos` clip uses **exactly one** of `path`, `document_id`, or `job_id`; omit the other keys, including empty strings. The saved editable timeline in step 4 uses a different shape and may retain both path and document ID. This creates the clean picture master only. Create the soundtrack through Manor: inspect the Account model for each required narration, music, ambience, or SFX purpose with `generate_file(kind="search")`; call `generate_file(kind="audio", params={"purpose":<searched role>,"expected_model":<search result model>,...})`, wait for pending jobs, and `probe_media` each completed stem. If the music model is unconfigured and the brief permits a licensed background bed, use the Manor `search_free_music`/`import_free_music` route and its completed Knowledge audio path; retain license/attribution metadata. **If the canvas already selected and imported a particular music track, use that track and its documented cue points; do not revert to an older project stem merely because its filename is convenient.** For a music-led revision or when the user dislikes the current score, compare more than one relevant, playable candidate against the canvas and spoken track before committing the replacement. Record the selected segment, entry/exit, loop or phrase join, and attribution. A simple synthetic pulse or chord loop is a planning placeholder, not a default finished commercial score. Use `build_narration_timeline` when speech consists of measured segments. First normalize **each** segment with `normalize_audio_loudness(target_duration_seconds=<positive measured or planned duration>)` (with `target_lufs`, typically -16) and pass those normalized outputs as the segments; a raw or duration-less segment is rejected with `narration_segment_not_normalized`. A model/credential/credit failure for required original audio is a resumable blocker, not permission to invent a local substitute.
4. Save a complete `Videos/<project>/timeline.json` with `generate_file(kind="document", file_type="json", content=...)`. Include `spec`, `duration`, `delivery.clean_picture_master`, an ordered `clips` array, exact `audio_tracks`, subtitle cues, and markers. Every clip must carry its original completed `path` and `document_id`, timeline `start`/`end`, and source-relative `sourceStart`/`sourceDuration`; never put master-timeline time into a source trim. This lets the composer register a `.video-edit.json` with replaceable clips rather than one flattened final. Each active `audio_tracks` item must use the completed Knowledge-relative **`path` or `fs_path`** (not `source`), `start`, and `end` (not just `duration`); for a measured dialogue/SFX file, `end_source="probe_duration"` is also accepted. Set `source_start` or `sourceStart` in seconds only when a track must start partway through its source, for example `{"id":"MUSIC-01","type":"music","fs_path":"Videos/project/audio/music.wav","start":0,"end":30,"source_start":150,"volume_db":-12}`. The selected `end - start` window must remain within the source after that offset, and a source offset cannot be combined with `loop`. Keep the imported music's exact Knowledge `fs_path`, even when it is at the root rather than inside `Videos/<project>`. Check that each audio path exists and its `probe_media` result has an audio stream before calling `compose_video_timeline(require_audio=true)`. Read the saved JSON back and verify its top-level key is `audio_tracks`, with both narration and music entries; do not use a top-level `audio` planning list. An `audio_tracks` array with only `source` keys resolves to zero usable tracks. Put spoken captions in their own cues instead of asking the video model to paint text. If the requested branding needs a separately positioned title or graphic, use `video-edit`; the native subtitle burn-in applies one shared subtitle style.
5. Call `align_subtitles(timeline_path=..., format="ass", output_name=...)` using the **exact spoken lines** in the source script/canvas and measured dialogue cues; do not paraphrase existing narration in captions. Validate the returned subtitle file with `validate_subtitles`, then call `compose_video_timeline(timeline_path=..., clean_video_path=..., subtitle_path=..., output_name=..., include_source_audio=false, require_audio=true, ducking={...}, loudness_normalization={...})` only when the canvas selected this route. Keep narration, music, ambience, and effects as separate timeline tracks. Require the returned final `document_id`, Knowledge path, and `editor_recipe_document_id`; if a needed track is unavailable, keep completed assets and return `stage=production_in_progress` with the exact blocker. A required brand title/CTA must appear as a real editable layer and in the reviewed image, not only in the storyboard or narration.
6. Check the final MP4 with `probe_media`, `render_frame_samples`, `analyze_audio`, and `validate_subtitles` against the requested duration, aspect ratio, first/last frames, transitions, captions, and sound. Review the actual opening, each cut, and the ending at playback speed; inspect dialogue/music balance and listen to the complete mix where playback is available. Verify that planned music ducking, cue changes, fades, and Foley accents exist in the editable timeline and are audible in the render; a written mix intention or a static low volume is not proof. Read the registered Video Editor recipe through `read_file` and verify its source references, trims, caption intervals and intended clip/caption/audio counts. For the native compositor, retain the executable source timeline, subtitle file and actual compose settings (including call-time overrides in `generation.render_settings` when available). These are the source for reproducing the native render. Browser-editor caption styling uses a different renderer; do not claim pixel-identical browser export from a source read, or require an unrequested edit/save/reopen cycle as evidence that sources are editable. The `video-edit` HTML authoring tool is not an importer for `.video-edit.json`. If an actual content check fails, revise only the affected timeline or source asset and compose again under a new output name; do not rerender unchanged media to compensate for unavailable review tools. Preserve and report any unresolved playback or audible-phrase concern honestly.

When the EDL requires designed motion, layered graphics, or transitions the native compositor cannot express, resolve `video-edit` with `list_skills` and call `invoke_skill` with the real `CANVAS.md`, EDL, and completed assets. That skill owns the `video_edit` Sandbox, strict review, and exact-source approval before render; a request for the finished video is that approval, so pass the user's request through unchanged and never answer an approval prompt yourself. Do not switch from its failed review to a lower-fidelity render without telling the user. Do not use a local script, shell FFmpeg, or an external editor for any required production step.

## Review evidence and accepted deliverables

Use the review capabilities actually exposed by the current runtime. If
continuous audiovisual playback is unavailable, say so; frame samples, audio
analysis and transcription do not prove continuous listening or exact vocal
alignment. Do not keep searching for a nonexistent player or rerender unchanged
media to resolve that limitation.

When the user explicitly accepts an existing deliverable's creative result,
record the original feedback, its scope, and the exact Document ID and hash when
available in the Canvas for `adapt`/`generate` or review record for `reuse`.
For attachment-only reuse, retain the authorized attachment reference instead;
never invent a Document ID or hash. This is human creative acceptance of that
version, not an automated quality measurement. It can resolve a subjective
review concern for an unchanged product film, lesson, music video, or other
deliverable; do not demand a second automated subjective review solely because
the agent lacks playback. Generic permission to continue is not evidence that
the user accepted the creative result. Do not infer that a particular phrase,
caption timing, factual claim, or technical constraint was verified unless the
feedback actually addresses it.

Still perform available file, duration, format, source identity, caption and
artifact checks. Preserve identity-bound observations for unchanged assets and
inspect only missing or changed evidence. An independently assigned reviewer
must make its own decision from those observations, applicable requirements,
and clearly attributed acceptance; do not write its approval for it. Actual
defects, unfulfilled explicit requirements, rights, publication authorization,
and downstream verification remain binding. In the final result distinguish
passed checks, human acceptance, and unavailable measurements; an unavailable
measurement alone is not an observed defect in the accepted creative result.

## Secondary edit pass (`adapt` and `generate` only)

- **Story and pacing:** preserve the script's intended scene order and dialogue, remove accidental repeats and near-identical adjacent framings, check that each shot adds information or emotion, and verify actual runtime. Compare intended narration breath and musical phrase boundaries with the rendered cut points.
- **Picture:** reframe for each aspect ratio, stabilize where needed, match exposure/color, inspect first/last frames and generated continuity, and keep on-screen text legible.
- **Sound:** clean dialogue, balance ambience and Foley, implement actual ducking or volume automation under speech, smooth cue and phrase changes, and listen through the whole master. If the music is disliked, replace or re-edit the bed rather than merely lowering its gain.
- **Captions and text:** align subtitles verbatim to measured speech, check safe areas, and verify every visible name, quote, factual claim, offer, price, or CTA where present.
- **Variants:** make channel cutdowns and alternate aspect ratios from the approved timeline with deliberate openings and reframes, not a blind crop.

For `adapt` and `generate`, return the real canvas reference, EDL, editable project/source, reviewed master, requested variants, measured duration, and unresolved blockers. Check source-frame samples, every chapter boundary, audio peaks/silence, subtitles, and intentional ending before handoff. Follow the output destination's current export requirements; do not hard-code one platform setting into all projects.
