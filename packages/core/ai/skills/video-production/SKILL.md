---
name: video-production
description: Accept a plain-language video request, brief, script, or reference; build a reviewable first/last-frame canvas, generate or collect assets with Manor tools, edit an accurate final, and prepare or publish requested platform versions through connected tools. Use for any style or length; use video-edit directly for a narrow edit of an existing approved timeline.
---

# Video Production

Own the script-to-master path for **any genre or script structure**. Resolve `production_mode` as `reuse`, `adapt`, or `generate` from the caller's current request and any clearly confirmed continuation brief or Canvas, then record that resolution. The caller does not need to name the enum. Ask only when those sources leave the intended change or delivery ambiguous or conflicting; never turn an authorized `generate` or `adapt` request into `reuse` to make a delivery appear complete.

`adapt` and `generate` produce an editable `CANVAS.md` **before** starting video generation or assembly. A canvas-only request stops with that artifact; a finished-video request continues through Manor's media and composition tools. `reuse` verifies and hands off one already finished source without constructing a canvas, timeline, subtitle project, or new master. Use only Manor tools for production: do not move audio generation, subtitle authoring, file registration, or final assembly to a local script or external editor. Do not turn an already supplied script into a new marketing strategy or rewrite its dialogue without the user's request.

### Production mode and source identity

For every mode, retain the caller's authorization and the input identities separately from newly created project artifacts. Prefer an authorized Knowledge `source_document_id` and exact `expected_source_sha256` for every existing media source; use its resolved `fs_path` only where a tool requires a path. An authorized attachment path remains valid when no Document ID is available. Do not guess a run-relative path. A Document ID, hash, filename, or source name by itself is not content-quality evidence. Never copy an already authorized root-level source into a Workflow run directory merely to make a project layout look uniform.

Use `reuse` only when the caller explicitly authorizes delivery of that exact finished source unchanged. When it has a Knowledge Document, first read/probe that Document and verify its returned identity against `expected_source_sha256`; use `probe_media`, then `render_frame_samples(document_id=source_document_id, expected_source_sha256=..., output_dir=<new review-only project path>)` across `0..measured_duration`, read each returned frame, and call `analyze_audio(document_id=source_document_id, expected_source_sha256=...)` when audio analysis is required. When no Document exists, use the exact authorized attachment path only for tools that accept that attachment route; never invent a Document ID or hash. In particular, do not promise `analyze_audio` for `uploads/chat` attachment paths. If a required audio measurement has no supported route, record it as unavailable and report the exact missing evidence. Record line-level observations with timestamp, frame/reference receipt, what is visibly present, each readable subtitle/text line, and uncertainty. Do not claim a line is readable because a file name, matching hash, or source name suggests it; if sampling cannot establish a required line or its timing, report that missing evidence.

For `reuse`, perform the available duration, format, source-identity, visual/text readability, audio, rights, factual-claim, destination-fit, and artifact checks. Preserve the original bytes and whole source range `0..measured_duration`; do not trim, transcode, burn subtitles, recompress, generate a replacement, or compose a new video. A `reuse` review may save a review record and extracted frames under a distinct output path, but it returns the original Document ID and observed SHA-256 when they exist, otherwise the original authorized attachment reference. It never skips independent review, normal platform authorization, or publication approval.

Use `adapt` when a caller-authorized change needs an editable source/timeline, such as a cut, reframing, new captions, audio, graphics, or platform version. Use `generate` when new footage or audio is authorized. Both modes retain the existing CANVAS, editable-source, subtitle, composition, review, and approval requirements below. Do not downgrade an incomplete `adapt` or `generate` request to `reuse`; return its exact blocker instead.

For `adapt` and `generate`, when an invocation includes an existing canvas artifact, read **that exact document**, inventory its project assets with `list_files`, and **start at step 3**. Do not execute the canvas-creation step, make a condensed successor canvas, or treat this as a first-canvas invocation. A `/viewer/<document-id>` link is a document URL, **not** a Knowledge file path: never pass it to `read_file`. For a viewer link, call `search_documents(query="CANVAS", detail="details")`, page as needed, and match its `document_id` to the ID in the link; then pass that result's `fs_path` to `read_file`. If no authorized result matches, stop and report the unresolved document ID. Never substitute a same-named `CANVAS.md`, another version, or a plausible project path. If the earlier `generate_file` receipt already supplies both the exact document ID and Knowledge-relative path, use that path. Keep the selected canvas's project root, shot IDs, exact recorded dialogue, selected music, and edit route; do not regenerate accepted takes **unless the user's latest revision explicitly replaces them**. If execution changes the plan, patch that canvas by shot ID with the actual receipt instead of replacing it. `reuse` never enters this canvas or generation route.

For an `adapt` or `generate` follow-up whose input identifies an existing project but does not contain the canvas's exact document reference, use `list_files` to locate the project's actual `CANVAS.md` and read that returned path. Resume from its existing assets and the user's latest revision request. Treat earlier turn instructions such as “only make a subtitle file” or “do not invoke a skill” as applying to those earlier turns; the current request controls the work now. If the existing project cannot be identified unambiguously, report the missing project reference instead of starting an unrelated canvas.

Before an `adapt` or `generate` canvas resume, apply the platform-overlay rule in step 1 when the latest request changes the destination or specifies a previously unresolved platform. Patch the same canvas with those decisions, then continue its production; keep its accepted assets where they still satisfy the revised brief.

For `adapt` or `generate`, when a requested change concerns setting, product depiction, narration language, or other substantive visual/audio content, compare the actual current clip and stem references against the new requirement. A subtitle or voice change alone cannot satisfy a new setting. If replacement visuals are needed, generate or collect them, update the shot IDs and timeline, and create a new clean picture master before the final compose. Do not reuse an older `clean_video_path` and then claim the new setting is present. If the new assets cannot be obtained, return `stage=production_in_progress` with the completed work and the exact blocker, not a completed-video claim.

For `adapt` or `generate`, if the latest request explicitly says to regenerate **all** visual shots, submit new `generate_file(kind="video")` jobs for every shot that will appear in the new picture master. Earlier project clips may guide prompts and continuity but must not be merged into that master. After `merge_videos`, inspect its `inputs` receipt and confirm every source path belongs to a completed new shot for this revision. If any old clip remains, keep the run in progress; an edit of the narration or subtitles is not a replacement for those shots.

Knowledge may normalize names when saving a requested path. Treat every
tool-returned `fs_path` and `list_files` entry as authoritative over paths
printed in the canvas or conversation. Locate an existing edit directory with
`list_files`, then pass its exact returned directory path to Video Edit. Never
infer that a requested name with hyphens is the path actually stored on disk.

## 1. Read the script and route only what is needed

Read the exact supplied script, attachment, or Knowledge reference with `read_file`; retain its scene order, character identity, spoken lines, and intended ending. For an uploaded reference video, prefer its authorized Document ID with `probe_media` and `render_frame_samples`; use an authorized attachment path only when no Document ID exists and the selected tool supports that attachment route. Sample the opening, scene changes, representative middle points, and ending. For a `reuse` source, use the Document-aware calls when it has a Document; otherwise use only supported attachment-route tools and report a required unavailable measurement. Do not pass an invented or run-relative path. Call `read_file` on returned frame paths so the model actually sees the images; a frame URL or tool receipt alone is not visual analysis. Use `transcribe_video` only when measured dialogue or timed caption evidence is required by the accepted delivery goal and the available route can provide it; split a needed transcription longer than 30 minutes into successive `start_seconds`/`end_seconds` windows. Do not treat music symbols, untimed text, or uncertain transcription as lyric evidence. Align every observed visual and spoken detail to its timestamp, and say which parts were sampled rather than claiming frame-by-frame review. This tool route is required even when the selected chat model advertises native video input, until Manor actually sends video bytes to that model. Mark the reference as **reference only** when the user asks for a completely new video, and never place it among source clips. If there is only a brief or visual reference, derive a working script and label it as such. Resolve format, language, aspect ratio, target runtime, supplied media, visual references, and any factual or rights constraints. Map each script scene and dialogue line to stable chapter/shot IDs; estimate timing from actual speech and action rather than filling a preset duration.

Turn the user's request into this project's production brief: desired result, supplied script/reference/assets, audience, duration, language, visual and sound direction, output formats, editing needs, and publication destination if requested. Fill gaps only where production needs a working decision and label assumptions in the Canvas for `adapt`/`generate` or review record for `reuse`. Do not require a named video type, choose from a fixed list of story structures, or infer the next action from keywords. An approved script remains the creative source; an open brief can be developed into a working script tailored to the user's goal.

### Audio-led and lyric-caption films

When the brief requires lyrics, captions tied to a song, or another audio-led
film, make the source, timing, and editorial meaning explicit in the Canvas for
`adapt`/`generate` or in the review record for `reuse`. Prefer a supplied lyric document or timed lyric file as the canonical
words. Otherwise transcribe the supplied audio with Manor and keep the returned
measured cues; transcription with no timestamps, only music symbols, omitted
lines, or uncertain words is not lyric evidence. A supplied lyric visualizer
can establish that text was visibly present at sampled times, but it is only
approximate visual evidence unless it also supplies readable, measured cue
timing. Never invent, complete, translate, or silently omit required lyric
lines.

When captions come from a reference recording but the soundtrack comes from a
separate audio file, use `measure_media_audio_alignment` on their Knowledge
Document IDs and approximately corresponding 5–60 second spans. It measures a
local constant offset within one second of those anchors, including ordinary
codec delay. Retain both source hashes and the returned equation:
`target_time = reference_time + target_time_offset_seconds`. Apply that offset
to observed reference cues before translating them into the final excerpt.
This measures audio correspondence; it does not turn visible text into verified
vocal onsets or replace final soundtrack verification. If the inputs have no
known approximate correspondence, are silent, drift, or do not match, retain
the limitation rather than assuming zero offset or repeatedly probing the
same unsupported operation.

Keep visual production and caption timing as separate readiness checks. Once
the words and creative meaning are supported, useful picture and poster work
can proceed while cue boundaries are refined. Missing final caption evidence
blocks complete delivery, not all independent asset generation. Preserve and
reuse accepted assets when continuing an existing canvas.

For `adapt` and `generate`, record each accepted cue's source-audio start/end and its output-timeline
start/end. When the edit uses a source-audio offset, translate the cue by that
same offset and reject cues outside the selected span. Use `align_subtitles`
and the native timeline or `video-edit` to render text after picture generation;
do not ask a video model to bake lyrics or captions into generated footage. If
the requested captions cannot be made readable and accurately timed from the
available evidence, keep the completed picture assets and return the precise
caption blocker rather than delivering an uncaptioned result as complete.

For `adapt` and `generate`, treat lyric-inspired scenery, action, and symbolism as creative interpretation,
not documentary evidence or an asserted real event. Plan multiple materially
different visual shots around musical phrases or cue groups, then reserve
on-screen text for restrained, post-production typography. Keep a supplied
song as the exact music track in the final timeline: do not replace it with a
generated bed or automatically duck it as background music beneath narration.

For `adapt` and `generate`, when the user or a Workspace action has **explicitly selected** a Video Studio type id (for example `viral_short`), invoke built-in `video-type-playbooks`, load that type's playbook with `invoke_skill(skill_id="video-type-playbooks", bundle_file="references/<type_id>.md")` (continue with `char_offset` while next_char_offset is not null; your `read_file` reads only video-production's own bundle), and apply its structure, visual rules, route, pitfalls and acceptance checklist to the canvas. The user's brief, script and facts still override the playbook; an explicitly selected type is a direction, not a fixed formula. Never infer a type from keywords when none was selected.

When the user names a destination platform, requests platform-specific versions, or asks you to choose a destination for this video, resolve and invoke built-in `marketing-platform` before creating or revising the canvas. Pass the original brief or approved script, destination/placement when supplied, audience, duration, language, supplied format choices, CTA when requested, and factual constraints. Apply its platform overlay to framing, caption-safe regions, sound, edit, export, and publication checks. Preserve the user's approved scene order and dialogue. If `marketing-campaign` or the existing canvas already supplies a usable overlay for that destination, reuse it; invoke the platform specialist again only when the destination or relevant requirements change or remain unresolved. Pass the overlay as text, not as another skill's bundled reference paths. A single requested video stays under `video-production`; do not invoke `marketing-campaign` merely to obtain platform guidance. With no named destination, use the requested output format and leave platform selection open unless the user asks to choose one.

The parent skill handles all script types. Invoke each specialist whose domain-specific continuity rules help the production; specialists can be combined and never replace the script:

| User need | Specialist |
| --- | --- |
| Script depicts a real product, UI, or product claims | Resolve and invoke built-in `product-promotion-video` for identity and proof constraints |
| Script depicts real food, a restaurant, service, or a venue | Resolve and invoke built-in `restaurant-promotion-video` for food/venue continuity and capture constraints |

A product sold to restaurants, such as a tea machine, may need **both** product identity/proof rules and restaurant service/context rules. `video-production` remains the executor for the canvas, media tools, edit, and verification; do not treat the specialist's `instructions_only` guidance as a completed production. Specialists supply factual and continuity constraints, not a mandatory concept, shot order, runtime, or advertising formula.

For drama, education, documentary, animation, and other scripts, use the common workflow directly. An existing approved timeline that only needs a narrow edit goes directly to `video-edit`.

## 2. Create and present the canvas (`adapt` and `generate` only)

Read `references/canvas-contract.md`. Create a durable `CANVAS.md` with a project-specific path, for example `generate_file(kind="document", name="Videos/<project>/CANVAS.md", content="<canvas Markdown>")`, and keep the returned document/path reference. Use `storage_scope="workspace"` only when this turn has an active Workspace context; in ordinary Chat, omit `storage_scope` so the tool uses its task scope. Every shot needs its source-script anchor, exact time range, asset method, detailed first frame, action, detailed last frame, exact dialogue or intentional silence, visible text, ambient/Foley sound, music cue, and precise edit handoff. The canvas must also contain dedicated, executable sections explaining **how Manor generates the assets** and **how Manor assembles, mixes, reviews, and revises the editable video**; include a visual/index card for every shot, rather than treating the short render contact sheet as the canvas. For a multi-minute film, use chapters whose durations add to the total. The first canvas may show clearly labeled planned-frame placeholders, but a text description must never be presented as an actual image.

Make the canvas visual before presenting it when image assets are available: show a storyboard image or supplied reference beside each shot's planned first and last frames, label its provenance, and keep reference-only footage distinct from material approved for the final edit. If no image exists yet, say `planned frame — no image asset yet` in that exact shot card. Do not imply that these planned images are frames extracted from a completed video.

If saving the canvas returns a Knowledge storage limit, stop with that exact blocker and keep the drafted canvas in the response for review. Do not begin media generation while the durable canvas cannot be saved.

**Only an invocation without an existing canvas ends after saving the first canvas.** If the user requested a finished video, reply with stage `canvas_ready` (final reply contract below) and put the exact complete `markdown_link` supplied by `generate_file` in its text, so the canvas is presented before billable video jobs or assembly. Copy that link verbatim; never retype or shorten its document ID. If the user requested a finished film, the parent Chat **must invoke this skill again with the same canvas reference and continue without waiting for another user prompt** under that authorization; do not treat `canvas_ready` as the finished result. When the user asked only for a canvas or explicitly requested canvas approval before production, reply with stage `delivered` instead: the canvas is then the requested result. Keep shot IDs stable through revisions.

## 3. Generate or collect actual shot assets (`adapt` and `generate` only)

Read `references/ai-routing.md`. Choose a method for each shot: supplied/captured footage for factual content; editable graphics for text and diagrams; `generate_file(kind="image")` for needed stills and first/last keyframes; `generate_file(kind="video")` for short generated motion; `generate_file(kind="audio")` for needed speech, music, ambience, or SFX. Use this public generation gateway even when an older canvas names a low-level generator; its absence from discovery is not a missing generation capability. Before any billable media generation, inspect `generate_file(kind="search", params={"media_kind":<image|video|audio>,...})` for each required media kind (including `purpose` for every audio role), and `plan_video_segments` for the planned video shots. Follow the returned generation schema and put model-specific controls in `params`. Select models through the user's Account settings and Manor's current tool capability, not a fixed provider list. Generate required narration and original AI music first, then probe their completed files, so a missing credential or provider route is known before paid video jobs. A generated clip is one replaceable asset, not a full long film. If a required original-audio route is unconfigured, return the exact setup blocker unless the licensed music-bed path below fits the brief; never silently create a stem outside Manor.

The native generation call enforces permission, admission and credit checks.
Do not invent a separate credit-query or account-wide job-list prerequisite
unless the user explicitly requires it. Reconcile retained project receipts
and known job IDs before resubmitting an uncertain operation; absence of a
separate discovery tool neither proves admission nor blocks the authorized
native call. Respect any actual denial returned by that call.

First check whether a completed, authorized narration file already matches the current script and language. If so, `probe_media` it and reuse it; do not submit a duplicate paid voice job merely because this is a new edit pass. For every new audio generation call, repeat the searched role in the **generation** `params.purpose`; search does not carry that value forward. In particular, English voiceover is `generate_file(kind="audio", prompt=<exact English lines>, name=<new .wav path>, params={"purpose":"narration","language":"en-US","voice":<selected voice>,"response_format":"wav","expected_model":<narration search expected_model>})`. Never omit `purpose` or the tool defaults to `speech`, which is incompatible with narration voice mode. In ordinary Chat without a Workspace ID, never use `narration_voice_mode="fixed_per_workspace"`; use `random_per_conversation` when a stable narrator is needed for that conversation, or omit the mode. Apply the same explicit purpose rule to `music`, `ambience`, `dialogue`, and `sfx` stems.

When the brief needs only a background music bed and the selected original-music model lacks a credential, use Manor's licensed catalog: `search_free_music(query=...)`, choose a commercially reusable track fitting the canvas (prefer CC0 or public domain; preserve CC BY attribution), then call `import_free_music(track_id=..., source_path=<CANVAS.md path>)` before any paid video jobs. This checks Knowledge storage admission as well as licensing before generation costs. Record its track provenance and Knowledge `document.fs_path` in the canvas and timeline. Say clearly that the track is licensed catalog music, not AI generated. If the user explicitly requires original AI-composed music, or the catalog route fails, retain the exact blocker and do not substitute another method.

For each AI-motion shot, call `plan_video_segments(target_duration_seconds=<whole-shot-seconds>, generation_mode="image_to_video" or "text_to_video")` **before** the video prompt. Use its `model`, `generation_durations_seconds`, and `segment_durations_seconds`; generate only that shot's required provider clips. Call `generate_file(kind="video", prompt=..., name=<stable shot-based filename>, params={"expected_model":<plan.model>,"duration":<planned provider duration>,...})`, including exact completed frame/reference URLs when supported and `generate_audio=false` when sound will be edited separately. Keep every returned `job_id`; call `wait_media_jobs(job_ids=[...])` until terminal, then use only completed Knowledge asset references. For every clip, use `probe_media` and `render_frame_samples` to check actual duration, frames, identity, and continuity against the canvas. Record the job and accepted asset reference against its shot ID in the canvas with `patch_file` so a later invocation can resume. Preserve completed assets on retry; an unresolved pending job is not permission to resubmit it.

After accepting a take, save its actual opening and closing frame images with `render_frame_samples`. Patch the **same** `CANVAS.md` shot card with both embedded images, the selected clip's exact Knowledge path, measured source in/out, and any difference from the planned images. Use browser-loadable current-entity `/api/v1/fs/<entity_id>/<encoded Knowledge path>` image URLs returned or verified by Manor; a local Sandbox path is not a canvas image. The overview must show a representative image for every accepted shot. Do not leave that shot's asset receipt as `pending`.

For a real branded device, read the visible wordmark in sampled opening, middle, and ending frames before accepting a shot. Reject a take with misspelled or changing brand text, even if the prompt spelled it correctly; do not call that an OLOSO product hero. Use an approved product reference frame or keep generated lettering out of view and add verified brand text as an editable overlay when the selected edit route supports it. Record the rejected take and replacement against its shot ID.

For a long production that cannot finish in one invocation, reply with stage `production_in_progress` and, in its text, the same canvas reference, completed/pending/failed shot IDs with actual receipts, and the exact next missing IDs. The parent Chat reinvokes this skill with those references until all required assets are completed, then proceeds to the selected Manor edit route. A pending or blocked stage is never a finished video.

## 4. Assemble and refine (`adapt` and `generate` only)

Read `references/production-and-edit.md`. Keep canvas shot IDs on assets, prompts, selected takes, source in/out, and timeline entries. Measure real narration timing and revise the canvas when shot timing changes. **Honor the edit route named in an existing canvas.** For a footage-led film whose canvas selects the native timeline, use Manor's `merge_videos`, `align_subtitles`, and `compose_video_timeline` with a complete Knowledge timeline JSON. Its registered `.video-edit.json` sidecar is the editable source; a flat merge alone is never the final deliverable. For a canvas selecting Video Edit, or for designed motion graphics, independently editable brand typography, and audio automation the native timeline cannot express, resolve and invoke built-in `video-edit` with the real canvas, completed assets, and edit decision list; that skill owns its own technical review and render approval gate. Do not silently render via the native composer when Video Edit review is blocked, and do not claim its render succeeded when the runtime fails.

Apply the review-evidence rule in `references/production-and-edit.md` to all
checks below. Keep measured results, sampled observations, and explicit human
creative acceptance distinct. An unavailable playback tool alone must not
repeatedly block the same unchanged deliverable whose creative result the user
has explicitly accepted; retain the limitation and complete the available
technical checks and the independent review. Known defects and unresolved
mandatory delivery requirements still block completion.

Independent review always has an owner: the reviewer or review step the
request, brief, or Workflow names, otherwise Manor AI. The `video_edit` review
is a technical gate, not independent review. When a later Workflow step owns the
review, leave it to that step without waiting for it. Otherwise, once the master
and its sampled frames exist, call `delegate_to_agent` with agent `Manor AI`
(find it with `search_tools` if it is not listed), giving it the brief, the exact
Canvas path, the master Document ID, and the saved frame paths. It only reads,
so it checks what already exists. Fix the real defects it reports in at most one
revision, then deliver and list any remaining findings. Report its verdict as the
independent-review state. If this invocation cannot call it, end your result with
`independent-review state: pending Manor AI` and that same request, so the calling
Agent asks it next. Its verdict does not replace the user's creative acceptance.

Review in small batches. Immediately save each batch's actual observations,
asset IDs/hashes, sampled times and remaining checks in the same project's
review document or Canvas before reading the next batch. Do not defer all
receipt writing until the end. After compaction or continuation, read that
ledger once and inspect only missing, changed or contradictory evidence;
evicted image attachments do not erase recorded observations. If repeating
inventory and image reads produces no new finding or artifact, save the exact
unresolved check and return a partial handoff instead of looping. This does
not approve unchecked content or replace the independent review.

Treat the measured end of the last spoken phrase as a hard timing constraint. A
delivery duration shorter than the narration file is not acceptable merely
because the MP4 decodes or its loudness passes: it can cut off a word. Shorten
and regenerate the script, or extend the picture and music to let the line
finish naturally and leave a readable brand hold. Update the canvas, captions,
audio automation, and edit duration together. Verify the exported ending by
listening through the final word and inspecting the final branded frames; a
static validation pass alone does not establish either result.

For the native composer, make the saved JSON executable, not merely descriptive. It needs `delivery.clean_picture_master`, a `clips` array with each accepted shot's `path`, `start`, `end`, `sourceStart`, and `sourceDuration`, and an `audio_tracks` array. Each audio track needs `id`, `type`, its **exact tool-returned or `search_documents`-resolved `fs_path`**, `start`, `end`, and `volume_db`; include the English narration and music as separate tracks. To begin a track partway through its measured source, use `source_start` (or `sourceStart`) in seconds alongside its timeline `start` and `end`; for example, `{"id":"MUSIC-01","type":"music","fs_path":"Videos/project/audio/music.wav","start":0,"end":30,"source_start":150,"volume_db":-12}`. The selected timeline span must fit inside the source after that offset, and source offset cannot be combined with `loop`. Put the subtitle file at `subtitles.srt_path`, with `subtitles.style` for font size and lower margin. Reuse and patch a known-good project timeline when one exists; do not replace `clips`/`audio_tracks` with ad hoc `shots`/`audio` keys. Before composing, read the saved JSON back, probe every referenced audio path, and check that the clean picture master and all required tracks resolve. A music title or a guessed path under the project folder is not an `fs_path`.

`merge_videos(clips=[...])` uses a different clip shape from the editable timeline JSON. **Each merge clip must contain exactly one nonempty source locator:** `path` **or** `document_id` **or** `job_id`. For an accepted `list_files` path, submit for example `{"path":"Videos/project/shot-01.mp4","start_seconds":0,"end_seconds":5,"label":"S01"}`; omit the `document_id` and `job_id` keys entirely. Do not copy a timeline clip containing both `path` and `document_id` into `merge_videos`. Keep both references only in the saved editable timeline.

When handing off, give `video-edit` the exact existing `project_path` and a
list of verified Knowledge **files** with their `fs_path`s. Never suggest a
directory or a fabricated asset path for `reference_paths`; an imported music
document must be resolved to its actual file before it can be staged.

Run the secondary edit pass on pacing, continuity, crop, color, captions, dialogue/music/ambient mix, and requested variants. Return the canvas, editable source project, reviewed master/variant receipts, measured runtime, and unresolved blockers. Never claim a complete video from a storyboard, pending job, unverified path, or review-only frames.

Before final delivery, sample the rendered master just inside both boundaries of **every editorial shot** and at the final visible frame. Replace planned thumbnails in the same canvas with these real first/last timeline frames, while keeping the source-asset image and path identified separately. Update narration, music, captions, actual edit route, source trims, transitions, project path, and final MP4 receipt. Open the saved canvas in Manor and confirm its images load; if they do not, report the canvas as incomplete and repair the references. A completed MP4 with a still-pending text-only canvas does not satisfy this workflow.

On a successful native compose, copy the returned `document_id`, `editor_recipe_document_id`, `fs_path`, `editor_recipe_path`, and complete viewer link into the skill's final result. The parent Chat needs these exact receipts; a prose claim that an editable recipe exists, or a filename alone, is insufficient. If final media checks pass but those IDs are absent, report the partial result and its missing evidence instead of asserting finished delivery.

## 5. Package and publish when requested

For `adapt` and `generate`, use the user's requested destination to prepare each platform version from the verified master: export specification, cover or thumbnail, caption/title/description, subtitles, attribution, claims and rights notes, and the exact approved media reference. Record these choices and actual variant receipts in the same canvas; when no destination is specified, deliver the editable master and avoid inventing a posting plan. A platform version is a deliberate edit/export from the master, not a new fixed video template. For `reuse`, hand off the unchanged original Document ID/hash when available, otherwise its authorized attachment reference, with QA receipts and required post metadata; do not require a Canvas or editable master.

If the user asked to publish, use `search_tools` to discover the current actor's connected platform capability and `list_skills`/`invoke_skill` for its platform-specific publication workflow. Check its current media-source, account, metadata, privacy, disclosure, and approval requirements before any upload. A Knowledge file or viewer URL is not automatically a valid provider upload URL; prepare a supported source through Manor or report the exact missing transfer route. Pass the exact final asset and post payload through the connector's normal authorization and review gate. Verify the returned post/video ID and terminal publish status, then add the platform receipt/link to the Canvas for `adapt`/`generate` or the review record for `reuse`. If the provider is disconnected, source transfer is unsupported, approval is pending, or processing has not completed, report that state precisely and preserve the finished video and prepared post package for continuation.

## Final result contract

Return `production_mode`; an authorized `source_document_id`, `expected_source_sha256`, and observed source SHA-256 when known; otherwise the authorized attachment reference. Return Canvas, EDL, review-record, editable-project, master, and variant paths only in their existing document/tool receipts; never substitute one of those output paths for source identity. For `reuse`, the only new receipts may be review artifacts; the delivered media remains the unchanged source over `0..measured_duration`, with measured QA observations and unresolved checks. For `adapt` and `generate`, retain the existing canvas, editable source, subtitle, master, and variant receipts. In every mode separately report passed measurements, sampled observations, human creative acceptance, unavailable measurements, independent-review state, and publication/approval state.
