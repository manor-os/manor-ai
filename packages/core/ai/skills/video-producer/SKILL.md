---
name: video-producer
description: Produce one finished, publish-ready video of any requested length from a brief, with narration, music, and accurate subtitles and titles. Starts from the given reference or start images (or generates an anchor frame when there are none), plans provider-sized clips, generates them in order with each clip starting from the previous clip's last frame so people and sets stay the same, then trims and merges them into a single MP4. Use for any video length, from a few seconds to several minutes.
version: 2.1.1
---

# Video Producer

Produce exactly one finished video file of the requested length. A single
provider clip is only a few seconds long, so every video — short or long —
goes through the same steps: anchor frame, plan, generate clip by clip, merge
a clean picture master, then post-production (narration, music, subtitles,
titles) into the final MP4.

**Video models never draw text.** They misspell it. Every image and clip
prompt describes picture only and ends with "No on-screen text, captions,
titles, or lettering." All words in the video come from post-production,
where they are exact.

## Inputs

- **brief** — what the video shows (subject, story, style).
- **scenes** — optional ordered scene notes. Spread them across the clips.
- **duration_seconds** — the finished length. Required.
- **aspect_ratio** — for example `9:16` (vertical) or `16:9`. Default `16:9`.
- **resolution** — `480p`, `720p`, or `1080p`. Default `1080p`.
- **output_name** — the file name for the finished video.
- **reference_images** — optional images that show what must appear:
  product screenshots, a person or character, a logo, an earlier campaign
  image. They may be URLs, Knowledge paths, or picked-file objects; pass them
  to `generate_image` as `reference_urls` as given. Use them; never
  contradict them (real interface screenshots mean the screens in the video
  show that interface, not an invented one).
- **start_image** — optional image the video must open on.
- **narration** — optional voice-over script. Spoken verbatim.
- **on_screen_text** — optional title lines (hook, key message, closing line).
- **music** — optional music direction (mood, genre). Default: a calm,
  modern instrumental bed that fits the brief.
- **finish** — `full` (default: narration, music, subtitles, titles) or
  `picture` (clean picture only).

## Steps

Never pass `model` or `expected_model` to `generate_image` or
`generate_video`: the account's selected models are used. Guessing a model
name makes the call fail. If a call is still refused because "the
Account-selected ... model changed", make the same call once more with
`expected_model` set to the `model` named in that refusal.

When you call `render_frame_samples`, use the frame whose timestamp is the
one you asked for (the last frame in the returned list), never the 0-second
frame.

**Frames that start a clip must not show a realistic human face.** Video
models refuse a start image that "may contain a real person". When people
belong in the video, show them from behind, over the shoulder, as hands at
work, in silhouette, or in a clearly illustrated or 3D style; otherwise keep
the frame on the product, screen, or setting.

0. **Anchor frame.** Every video opens on a real image, so the look is fixed
   before any clip is made.
   - If **start_image** is given, it is the anchor.
   - Otherwise call `generate_image` once: a `prompt` describing the opening
     moment (subject, wardrobe, set, palette, lighting, camera), the
     requested `aspect_ratio`, and `reference_urls` = **reference_images**
     when there are any. If it returns a job, wait for it with
     `wait_media_jobs`. Its image URL is the anchor.
   - If the anchor image fails, stop and report why.

1. **Plan.** Call `plan_video_segments` with
   `target_duration_seconds = duration_seconds` and
   `generation_mode = image_to_video` (every clip after the first starts from
   a frame). If it returns `blocked`, stop and report its error. Otherwise
   keep `generation_durations_seconds` (what to request from the provider)
   and `segment_durations_seconds` (how long each clip is in the finished
   video).

2. **Generate clip by clip, each starting where the last one ended.** Clips
   are made in order, not all at once, so the same people, clothes, set, and
   light carry through every cut.
   - Every `generate_video` call uses: the requested `aspect_ratio` and
     `resolution`; `duration` = that segment's generation duration;
     `generate_audio = false`; no `name` (each clip gets its own file). Leave
     out any text, caption, title, or slogan the brief asks to show (it is
     added in post-production) and end the prompt with "No on-screen text,
     captions, titles, or lettering." Do not pass `reference_urls` to
     `generate_video`: the reference images already shaped the anchor, and
     every clip starts from a frame that carries them.
   - **Clip 1:** `first_frame_url` = the anchor, and a `prompt` that fully
     describes the subject (who they are, what they wear), the set, palette,
     lighting, and camera, plus what happens in this segment.
   - **Wait** for the clip with `wait_media_jobs`. If the clip was refused
     because its start image may show a real person, make that clip once more
     without `first_frame_url`, with a prompt that fully describes the
     subject and look, and carry on. If it failed for any other reason, or
     fails again, stop and report which clip failed and why.
   - **Hand-off frame:** call `render_frame_samples` on the finished clip with
     `input_path` = its video URL, `output_dir = video-producer/frames`, and
     `timestamps = [<that clip's segment duration minus 0.1>]` (the moment the
     finished video cuts away). Use the returned frame's `result_url`.
   - **Next clip:** `first_frame_url` = that hand-off frame, and a `prompt`
     that continues the action from that exact moment while repeating the same
     subject and look. Repeat wait → hand-off frame → next clip until every
     segment is done.

3. **Clean picture master.** Call `merge_videos` once with `clips` in
   segment order, each
   `{"job_id": ..., "start_seconds": 0, "end_seconds": <segment duration>}`,
   plus `aspect_ratio` and `resolution` as requested. When **finish** is
   `picture`, use `output_name` here and you are done. Otherwise name it
   `video-producer/<short-slug>/picture-master.mp4`; it is the input to
   post-production. Always merge, even for a single clip: it trims to the
   exact length.

4. **Post-production** (skip when **finish** is `picture`). All files go in
   the same `video-producer/<short-slug>/` folder. Use concrete `result_url`
   or `fs_path` values from tool results, never guessed paths.
   - **Narration.** When there is a narration script, call `generate_file`
     with `kind="audio"`, `prompt` = the script verbatim,
     `params={"purpose": "narration", "voice_instructions": "warm, clear,
     unhurried"}`. Do not pass `model`. If it is longer than the video,
     shorten nothing: report it as a blocker instead. Then call
     `normalize_audio_loudness` on it and use its result.
   - **Music.** Call `generate_file` with `kind="audio"`,
     `params={"purpose": "music", "duration_seconds": <video length>}` and an
     instrumental prompt from **music** (no vocals, no lyrics) that names its
     mood (for example calm, upbeat, cinematic, tech, warm, inspiring). When
     live music is unavailable the tool returns a track from Manor's own
     library instead (`source: "library"`); use it like generated music, with
     `loop` true. If no music comes back at all, continue without music and
     say so in the result.
   - **Subtitles and titles (one ASS file, two styles).** Call
     `transcribe_video` on the normalized narration audio to get its timed
     segments. Caption cues are the narration's own words in order, split into
     short phrases (at most two lines, about 32 characters a line), timed from
     those segments; captions never overlap each other. Each
     **on_screen_text** line is a Title event: the first at 0.3s to about
     2.5s, the last over the final 2-3 seconds, others where the story fits.
     Titles sit near the top, captions at the bottom, so a title may share
     time with a caption. Write it as ASS with `PlayResX`/`PlayResY` = the
     video size, Caption font size = 6% of the width (bottom-centre,
     alignment 2, MarginV = 9% of the height) and Title font size = 8% of the
     width, bold (top-centre, alignment 8, MarginV = 13% of the height), both
     white with a black outline. Every event time is `H:MM:SS.cc` (for
     example `0:00:17.50`):
     ```
     [Script Info]
     ScriptType: v4.00+
     PlayResX: 1080
     PlayResY: 1920
     WrapStyle: 0
     ScaledBorderAndShadow: yes

     [V4+ Styles]
     Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
     Style: Caption,Inter,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,0,2,80,80,173,1
     Style: Title,Inter,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,0,8,80,80,250,1

     [Events]
     Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
     Dialogue: 0,0:00:00.30,0:00:02.50,Title,,0,0,0,,One workspace for\Nyour solo company
     Dialogue: 0,0:00:00.20,0:00:01.90,Caption,,0,0,0,,Your solo company has a lot to run.
     ```
     Save it with `generate_file(kind="code",
     name="video-producer/<short-slug>", params={"files": [{"path":
     "subtitles.ass", "content": "<ass>"}]})`.
   - **Timeline.** Save `timeline.json` the same way:
     `{"delivery": {"clean_picture_master": "<picture master path>"},
     "audio_tracks": [{"id": "narration", "type": "narration", "path":
     "<normalized narration path>", "start": 0, "end_source":
     "probe_duration", "volume_db": 0},
     {"id": "music", "type": "music", "path": "<music path>", "start": 0,
     "end": <video length>, "loop": true, "volume_db": -18, "fade_in": 0.5,
     "fade_out": 1.5}],
     "subtitles": {"subtitle_path": "<subtitles.ass path>"}}`. Leave out a
     track you do not have (music is often skipped).
   - **Final mix.** Call `compose_video_timeline` with that `timeline_path`,
     `output_name` as requested, `burn_subtitles=true`, `include_audio=true`,
     `require_audio` true when there is narration or music, and
     `ducking={"enabled": true, "amount_db": -9}` so music sits under the
     voice. Do not pass `subtitle_style`: the ASS file carries the Caption
     and Title styles. Its `result_url` is the finished video.

When a tool rejects its input and says what is wrong (a missing field, a
wrong file type, a value out of range), correct that input and call it once
more before giving up. A skipped optional step (music) is not a failure.

## Result

Return JSON only:

```json
{"video_url": "<result_url of the finished video>", "duration_seconds": 30, "clip_count": 3, "narration": true, "music": true, "subtitles": true}
```

`video_url` must be exactly the `result_url` of the last step that produced
the finished video (`compose_video_timeline`, or `merge_videos` when
**finish** is `picture`). Also return `narration` (true/false), `music`
(true/false), and `subtitles` (true/false).
If any step fails, report the error instead. Never return a partial video or
a URL you did not get from a tool.
