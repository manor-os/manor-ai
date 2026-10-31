# Music video style pictures

Sixteen AI-generated stills, one per suggested music-video style template
(`x-example-images` on a music-video Flow's `visual_direction` run input, same
order as its `examples`), and fifteen lyric-style previews (`lyrics-*.webp`,
`x-enum-images` on its `lyrics_style` input). They show a look; they are not
outputs of a Workspace run and are not customer content.

The stills were generated on 2026-10-29 through the Vercel AI Gateway with
`google/gemini-3-pro-image-preview`, one shared prompt ("music video still frame,
cinematic and vivid … a single unmistakable hero subject") plus a scene per
style. No text, logos, third-party marks or visible faces. Each was trimmed of
borders, center-cropped to 16:9, LANCZOS-resized to 640 × 360 and encoded as
WebP quality 82.

The lyric previews are short looping animated WebPs: the sample line "We light
the night, we don't look back" (key word "night") written by
`_render_animated_lyrics_ass` with the same animation each lyric style passes
to `align_subtitles`, burned onto a style still with libass at 1920 × 1080 and
the Debian display fonts the API image installs, then sampled at 12 fps to
640 × 360 (WebP quality 72, the last frame held for one second).
`lyrics-none.webp` is the plain still.
