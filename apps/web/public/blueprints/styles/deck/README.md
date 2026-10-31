# Deck style previews

One picture per designed deck style of the built-in pptx skill
(`packages/core/ai/skills/pptx/templates/decks/styles.json`), shown as the
Style choices of the Presentation studio (`x-enum-images` on its `style` run
input). Each is four slides of the same fictional sample deck (cover, icon
grid, stats, process) built by that style's own deck engine
(`scripts/build_deck.py`, quality score 98–100), converted with LibreOffice and
rasterised with pdftoppm, then laid out 2 × 2 and encoded as 960 × 543 WebP
quality 80.

The cover pictures were generated on 2026-10-23 with OpenRouter
`google/gemini-3-pro-image-preview` from each style's own art direction (the
cut-out cover styles from one fictional cafe owner on white). The sample text
and numbers are illustrative, not product claims or customer content. No
logos or third-party marks.

## Per kind of deck

`pitch/`, `report/`, `training/`, `event/` and `proposal/` hold the same kind of
2 × 2 preview for the styles that suit that kind of deck (the `x-enum-cases`
images of the `style` input; the App's deck entries set the hidden `deck_type`
input). Each shows four slides of a fictional deck of that kind — a seed-round
pitch (market size, revenue chart, the ask), a quarterly report, a support
training module, a launch event and a website redesign proposal — built and
rendered as above. A product deck's recommended styles use the general
previews, which are a product introduction. Their cover pictures were generated
on 2026-10-24 with the same model from a scene or a person suited to the kind
of deck. All names, figures and dates are illustrative.

## Rebuilt 2026-10-24 with baked backdrops

Every preview above was rebuilt after the deck engine gained per-style baked
backdrops (blurred light, grain, halftone, starfields, a cover-photo wash) and
new page types. Styles added that day — `keynote`, `stripe`, `velvet`,
`aurora`, `bloom`, `photo`, `swiss`, `periwinkle`, `signal`, `documentary`,
`plain` — show their plain cover (their backdrop) except `photo`, whose cover,
pitch and event pictures were generated with OpenRouter
`google/gemini-3.1-flash-image` (a cafe counter, a clinic reception, a launch
stage). Rendering used LibreOffice with Segoe UI Light mapped to Helvetica Neue
Light, as PowerPoint on Windows shows it.
