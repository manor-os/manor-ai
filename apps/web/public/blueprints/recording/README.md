# Recording Workspace example media

These are actual outputs and unmodified browser screenshots from the installed
live Workspaces on an isolated local deployment on 2026-09-11. They are listing
media only; installing a Blueprint does not install demo files or run history.

The non-Manor example is **Cedar Cup**, a fictional cafe's pickup-order desk.
A manager turns a narrated lesson into two employee microtraining clips with
Shorts, or one complete new-hire tutorial with Tutorial. Neither Blueprint
operates orders or integrates with a POS system.

The 56-second input combines four captured states of an interactive fictional
app with AI-generated speech (Gemini TTS, Zephyr, through the configured Manor
voice route) and supplied SRT. The source was captured separately at 720×1280
for Shorts and 1280×720 for Tutorial; this is not evidence of smart reframing.
It is not continuous screen recording, customer footage or proof of automatic
speech transcription. All names and transactions are invented.

The real Workspace pipeline selected source cuts with the configured model,
rendered MP4s with FFmpeg, registered Knowledge documents and stopped for browser
playback acceptance. The three shipped MP4s are byte-identical output copies:

| File | Actual result | SHA-256 |
| --- | --- | --- |
| `coffee-shorts-intake.mp4` | 26.033 s, 720×1280, check and accept an order | `43139c3c4b8563e6fdc26ab008156b824b5651f6e890ddfa1f64798d6bd9e5af` |
| `coffee-shorts-pickup.mp4` | 30.033 s, 720×1280, ready and hand off a pickup | `9e759bc2d3ff8f2b2a33649fb514659c30db8695718c41bd8ae4b3ad2c63f058` |
| `coffee-tutorial.mp4` | 56.033 s, 1280×720, full onboarding tutorial | `d84159c088994abfbff6d9122d180f9964657ff4e99270925bd925ccd1419441` |

`coffee-shorts-preview.png` is the pipeline's frame preview from the final MP4,
not a browser screenshot. The three `coffee-*.jpg` files are browser captures
of final playback, two-file review and the editable tutorial with 14 captions
and two chapters. No screenshot or output video was retouched. No videos were
published to external accounts; the listing media is bundled locally for review.

See the verification report in `docs/recording-workspace-browser-verification.md`.
