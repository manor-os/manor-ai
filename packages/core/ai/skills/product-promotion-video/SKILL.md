---
name: product-promotion-video
description: Product-specific shot production guidance for a user brief or script depicting a real product, interface, feature, or customer result. Use inside video-production to preserve identity and accurate evidence while Manor generates assets and assembles an editable film; do not impose a launch campaign.
---

# Product Promotion Video

This is a domain-specific production supplement for built-in `video-production`. Accept a product-related brief or script, including a demonstration, fictional scene, training video, documentary interview, or promotion. When a script is supplied, preserve its exact scene order, dialogue, product claims, and ending. For an open brief, help the parent develop only what the requested video needs. Do not impose a launch arc, CTA, fixed runtime, or marketing message absent from the user's request.

This specialist runs as `instructions_only` and supplies constraints to the calling agent. The parent `video-production` skill owns the canvas, Manor media calls, and footage-led native timeline assembly; built-in `video-edit` handles edits that need its layered motion workflow. This specialist alone does not create footage or a final video.

## Adapt to the product brief

Read what the user wants the viewer to see, understand, feel, or do and what product evidence is actually available. Help the parent plan shots that serve that specific goal: the video may show form, operation, a working setting, people, a result, or a comparison when supported by the request and sources. It may use any script structure, style, duration, or platform format. Do not select a promotional subtype as a prerequisite, force a product reveal or CTA, or repeat the same shot recipe across projects. If the user supplies a complete script, preserve its order and wording; if the brief is open, propose visual beats based on the stated goal and mark invented or illustrative elements clearly.

Return the target buyer, platform constraints when supplied, and proof basis as production context in the parent canvas. For a restaurant buyer, pair this specialist with `restaurant-promotion-video` when service or venue details matter. The parent production and editing workflow owns output variants and any requested publication handoff.

## Make product shots executable

For each relevant script shot, give the parent a note keyed to its script anchor and stable canvas shot ID:

- Define the exact first frame, visible product/UI state, user or actor action, camera/framing, and exact last frame. Show how that last state hands off to the next scripted shot.
- Choose supplied/captured source for exact UI, product geometry, labels, packaging, physical controls, and actual outcomes when factual proof is required. When the user explicitly requests entirely new AI footage, use the supplied product media only as generation/identity reference, keep it out of the final timeline, and mark uncertain interface or mechanical details as illustrative rather than demonstrated fact. Use editable graphics for captions, callouts, charts, and text.
- Use `generate_image` keyframes or `generate_video` motion only where visual invention is allowed. Pass actual reference files into model-supported controls and keep generated clips replaceable in the edit.
- Record source asset and in/out, master timeline in/out, transition, track, subtitle/dialogue cue, and any required proof or fallback take. Keep the same shot ID across canvas, receipt, and edit decision list.

Lock a compact identity sheet: product version, silhouette, color/material, labels, packaging, screen layout and state, workflow order, and user/actor identity. A generated shot must not change a verified control, shipped UI, displayed output, or product result. Where the reference cannot prove a detail, keep it out of factual claims and label the visualization as illustrative rather than evidence of a shipped feature.

For a promotional brief, make the sequence reveal more than repeated hero angles: move through distinct, supportable facets such as product form, a real or clearly illustrative interaction, material/detail, output or service context, and a human use moment. Pick only facets supported by the source and script. Vary shot scale, focal subject, depth, and action while retaining the same product identity; record the new information in each canvas card. If a claim or physical detail cannot be verified, add texture and context around the known device rather than inventing a feature or performance result.

For performance, compatibility, pricing, release, customer, or result claims, attach the authorized source to the exact spoken line or visible text. Mark an unverified claim as a blocker or placeholder; do not create visual proof from model output. Capture real product action with start, intermediate, and result states so the chosen editor can place the cut on observable evidence. Preserve dialogue, music, sound effects, and subtitles as separately editable assets when the script needs them.

Return only the domain-specific continuity locks, source/proof choices, per-shot first/action/last details, and edit risks to `video-production`. That parent handles the actual Manor `generate_image`/`generate_video`/audio calls and their completed receipts, then assembles a timecoded editable timeline through the selected Manor edit route. Review the final rendered product and UI frames against approved sources before acceptance.
