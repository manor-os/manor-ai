---
name: restaurant-promotion-video
description: Restaurant-specific shot production guidance for a user brief or script involving real food, drinks, staff, service, or a venue. Use inside video-production to preserve food and location identity while Manor generates media and assembles an editable film; do not impose a marketing campaign.
---

# Restaurant Promotion Video

This is a domain-specific production supplement for the built-in `video-production` skill. Accept a restaurant-related brief or script, including dialogue, documentary, dramatized, instructional, or promotional scenes. When a script is supplied, preserve its scene order, exact dialogue, characters, action, and ending; for an open brief, help the parent develop only what the requested video needs. Do not impose a campaign funnel, narrative archetype, fixed duration, or CTA that the user did not request.

This specialist runs as `instructions_only`: it returns constraints to the calling `video-production` agent and cannot call media tools itself. The parent skill calls Manor's media tools and selects its native editable timeline or `video-edit` for layered motion. Do not claim that this specialist alone generated a clip or finished a video.

## Translate script scenes into shootable shots

For each restaurant scene, give the parent a production note keyed to the script scene/line and stable canvas shot ID:

- **First frame:** exact food or drink state, ingredient and portion visible, plate/glass, hands/utensils, person/wardrobe, room geography, camera size/angle, light, and any visible text.
- **Action:** one clear physical action with start/end states, camera movement, and the timing of dialogue or food sound. A pour has a visible source and target; a cut reveals the real interior.
- **Last frame:** exact final state and visual/audio handoff to the next scripted shot. Provide a clean hard-cut fallback when a generated match is fragile.
- **Source:** supplied clip/still and in/out, new real capture, generated keyframe, generated moving shot, or editable graphic. Distinguish factual evidence from an illustrative shot.
- **Edit:** source in/out, target timeline in/out, transition/handles, crop, dialogue, Foley, music, subtitles, and track placement. Keep shot IDs identical in the canvas, asset ledger, and editable timeline.

Do not substitute a generated interpretation for an exact menu item, real restaurant, actual employee, quoted guest, or documented outcome. When the script explicitly calls for fiction or dramatization, mark that scene as staged and keep character and setting continuity without presenting it as documentary proof.

## Lock food and venue references before generation

Build a concise reference sheet from approved source media: dish name and visible ingredients, portion, doneness, garnish, vessel, table/props, venue layout, time of day, light, staff/actor identity, wardrobe, and screen direction. Track food state across prep → cooked → plated → served → partly eaten. A dish cannot gain an ingredient, change portion or vessel, or reset from eaten to pristine between adjacent shots without an explicit time jump or new take.

When a shot needs AI motion, use actual reference files or accepted `generate_image` keyframes. Ask the parent to pass the returned `first_frame_url`, supported `last_frame_url`, or `reference_urls` into the real `generate_video` call. If a must-have control is unsupported by the Account-selected model, revise the shot method or flag the blocker before a paid job; do not silently drop the reference. Short provider clips are cut together in the editable master, including for several-minute films.

Prioritize real capture for menu, plating, signage, people, and room proof. Capture the finished food before it deteriorates, plus preparation, texture/action details, a room wide, entrance or service geography, clean dialogue, and room tone as the script requires. Record food resets and alternate takes. Use a motion cue only when physically plausible: pour, lift, crisp break, steam, sizzle, slice, plating, or serving must match the actual dish and visible action.

## Sound and accurate edit handoff

Keep exact dialogue or narration from the script. Plan diegetic sound at the source action (pan, ice, utensil, door, service, room) and place Foley only where it matches the picture. Generate or reuse dialogue, ambience, SFX, and music as separate assets when the final mix needs control; ask for silent AI picture rather than depending on baked provider music. Captions, menu names, prices, addresses, and other text belong in editable layers.

For a promotion, build visual range from verified food/drink preparation, texture, service handoff, room atmosphere, and people as the source permits. A sequence of similar plated dishes or device hero shots needs a deliberate reason; otherwise vary distance, focal action, and pace. Map the audible preparation/service sounds to the same actions and let them create some cuts or pauses. Do not invent menu items, prices, crowds, or customer reactions to fill an empty shot.

Return to the parent only the restaurant-specific additions needed for the common `CANVAS.md`: reference/continuity locks, per-shot first/action/last details, real capture or AI-asset method, scene sound, factual/rights gaps, and edit-point risks. The parent uses completed Knowledge asset paths and a timecoded edit decision list in the chosen Manor edit route. Inspect rendered food and venue frames against approved sources before treating a shot as accepted.
