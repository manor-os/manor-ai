# Canvas contract

This Canvas contract applies only to caller-authorized `production_mode=adapt` or `production_mode=generate`. Write `CANVAS.md` as the first durable, reviewable production artifact. Use the user's content language. Give it a version and keep stable IDs across later assets and edits. Treat an attached or pasted script as the source of truth: preserve its scene order, exact spoken text, character names, and ending. If a visual beat is inferred rather than scripted, mark that inference. A brief-only request may use a labeled working script.

For `production_mode=reuse`, do not manufacture a canvas, edit decision list, subtitle project, or replacement media. Keep a concise review record instead: caller authorization; source `Document` ID and SHA-256 (or an authorized attachment path when no Document exists); measured `0..duration` range; timestamped frame/text and audio observations; rights/fact/destination checks; independent-review and approval state; and only review artifact paths. The original source identity remains separate from those output paths.

## Header and chapters

When a destination is requested, record the platform overlay supplied by `marketing-platform` or the calling campaign: destination/placement, format and runtime choices, framing/crop, caption-safe regions, sound constraints, requested CTA, and current-rule verification state. Carry the same decisions into shot cards and delivery specifications; update them when the destination changes. A canvas without a requested platform keeps that choice open.

Record source-script reference or pasted-script identity, title, the user's creative goal or script genre in their own terms, output format, language, target runtime, visual identity, supplied assets, and unresolved placeholders. For a product video, record product identity and evidence sources, the intended viewer, and any supplied destination or format constraints; do not require a promotional subtype. Include audience, CTA, and claims only when the script or brief needs them. State the creative premise, what the viewer learns or feels in each chapter, and the visual change from opening to ending; avoid repeating one attractive product angle as a substitute for new information. Give each chapter or scripted scene an ID, start/end time, script line/page/scene anchors, narrative event, required media, and transition. Chapter durations must add to the target runtime. Multi-minute films require distinct scenes or chapters; do not lengthen a short montage to fill time.

## One card for every editorial shot

```markdown
### C02-S04 — Working title — 00:42.0–00:49.5 (7.5 s)
- Script anchor: scene/line/page or user-provided timecode; mark inferred beat
- Purpose / story event:
- New information or feeling delivered to the viewer; explain why this shot is distinct from its neighbors
- Source: existing asset ID and source in/out; capture plan; or generated asset method
- Asset receipt: pending; later patch with actual job ID, completed Knowledge path/document ID, and selected take
- First frame: exact subject identity, camera angle and size, subject position, background, light/color, visible text, and continuity anchor
- Composition: foreground/midground/background, focal detail, material/texture, negative space, depth of field, and screen/caption-safe region where relevant
- Action: visible subject/camera movement with timecoded internal beats and a clear change of state; describe what happens rather than only saying "slow push-in"
- Last frame: exact final subject position and pose, camera frame, background, light/color, visible text, and visual handoff
- Dialogue/VO: exact words, speaker, and cue; or intentional silence
- On-screen text/captions: exact copy, placement, and safe area; or none
- Ambient/Foley: named sound, source, entry/exit, and mix intention; or deliberate silence
- Music: actual track/candidate or sourcing brief, cue in/out, phrase or beat relationship to the visible action, energy change, and speech ducking; or deliberate silence
- Edit: source in/out, timeline in/out, trim, speed, cut/transition, overlap/handles, track, and reason for the cut
- Timing fit: measured or estimated speech end, breathing room, and whether any sound or caption intentionally crosses the cut
- Dependencies: source, rights, factual claim, likeness, product/dish/venue continuity
```

“First frame” and “last frame” are the opening and closing visual states of **each shot**, even for a filmed take. Descriptions must be specific enough to draw or generate. Put the corresponding image immediately beside each description when a supplied frame, planning keyframe, accepted source take, or rendered master exists. Label each image `planned`, `source take`, or `rendered timeline` and record its timestamp; never use one kind as evidence for another. A text card for uncaptured footage is a visible placeholder, not proof that an image exists.

## Production blueprint inside the canvas

Give the canvas a navigable opening with a section list and a compact visual overview of **every editorial shot**. A six-frame render contact sheet is only a review sample: never present it as the complete canvas for a 13-shot film. When assets exist, embed a representative image in the overview and an actual first/last-frame pair beside **each** shot card using browser-loadable, durable Knowledge references. For a finished video, extract the pair from the final timeline just inside that shot's in/out points and keep a separately labeled source-asset visual and path; this shows both what was generated and what reached the edit. If a planned image does not exist yet, label the frame as a textual placeholder. Make the amount of canvas content follow the number of shots and chapters, not a preset page count.

Add a dedicated **How Manor generates this video** section that a different agent could execute without reconstructing decisions from prose. Include:

1. An asset inventory separating supplied reference-only media, accepted reusable takes, missing footage, narration, music, ambience, captions, and editable graphics. Give each one a stable shot or track ID, actual Knowledge path/document ID when available, and status.
2. The ordered Manor tool route and gates: `generate_file(kind="search")` for required image/video/audio models, `plan_video_segments` for each new motion shot, `generate_file(kind="image"|"video"|"audio")` with model-specific controls in `params`, `wait_media_jobs`, `probe_media`, and `render_frame_samples`. Show the selected Account model and provider duration only after the tool returns them; label preflight values as pending beforehand.
3. For each generated shot, a paste-ready generation brief containing subject identity, first state, visible action and camera path, last state, negative constraints, aspect ratio, requested provider duration, reference asset roles, and expected audio policy. Keep exact prompts/parameters or a linked prompt record after submission, plus returned job ID and accepted take. Explain how to reject a failed take and reuse completed jobs without resubmitting blindly.
4. A long-form chapter plan when runtime exceeds a short clip: which shots are provider segments, how chapters are staged, and where narration/music continuity crosses chapter boundaries.

Add a dedicated **How Manor assembles and finishes this video** section. Name the chosen editable route (`compose_video_timeline` with its `.video-edit.json` recipe, or `video_edit` for layered motion), the project path, output spec and timebase. Provide an ordered assembly table with **one row per actual timeline clip**: shot ID, completed source path, source in/out, master in/out, speed, crop or reframing, track/layer, transition/handle, spoken or graphic cue, and reason for the cut. Explain the exact Manor call sequence for source delivery, writing the editable project, captions, music/Foley placement, dynamic ducking, review, approval when required, render, probe, and secondary edit. State which treatments remain editable and how a reviewer can replace one bad shot without flattening the whole film.

End with an acceptance checklist tied to observable evidence: chapter/shot count and duration sums, actual first/last and transition frames, dialogue timing across cuts, audio playback and peaks, captions/safe area, claims/rights, editable-source receipt, review hash when applicable, and final media receipt. Distinguish planned steps from completed tool receipts. Update both blueprint sections whenever a take, music cue, source trim, or timeline time changes.

When a destination or publication is requested, add a **Delivery and publication** section: requested platform/account, version/export spec, cover, title/caption/description, subtitle and attribution assets, verified master/variant reference, destination-specific media-transfer requirements, connected tool route, required review state, and final post ID/URL/status once known. Separate `planned`, `rendered`, `ready for platform review`, `processing`, and `published`; the finished MP4 alone is not publication evidence. Leave this section out when the user asked only for a canvas or editable video without a distribution destination.

## Cross-shot plan

Include a continuity sheet for characters, locations, props, product/UI, food, wardrobe, palette, and typography as relevant. Add a visual progression table listing each shot's scale, angle, focal subject, visible action, and new information; adjacent repetitions need an intentional reason or a different shot. Include a separate audio map for exact dialogue/VO, environment/Foley, music, pauses, and scene handoffs. Its music brief must specify mood, instrumentation or texture, pulse/tempo only when known or measured, phrase changes, desired accents, and unwanted qualities. Record actual candidate tracks and license/provenance as they are auditioned; do not label a procedural tone bed as a selected creative score. Include an asset ledger with each shot ID, source status, actual tool receipt/path, rights, and relevant verified/unknown claims. Add an edit decision list mapping shot ID → source asset and source in/out → master timeline in/out, plus video/audio track, transition, subtitle or graphic cue, and fallback. The editable project must follow that map; update the map when measured media changes timing.

Before showing the canvas, check that shot intervals have no unexplained gaps or overlaps, chapter time sums are correct, spoken text plausibly fits its interval with room for the intended delivery, opening and ending are intentional, each cut has a visual or sound reason, and unresolved factual or rights items are marked. Identify unintentional voice or caption spill across cuts. A detailed card still needs actual visual variety: for a short promotion, reject a run of near-identical hero framings unless repetition is the concept. Return a real Knowledge/Workspace reference to the saved canvas; a proposed filename is not an artifact.
