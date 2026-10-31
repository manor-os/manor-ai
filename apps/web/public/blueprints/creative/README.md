# Creative Studio showcase provenance

All public demos use English copy, visible text, subtitles and AI narration.
A translation demo may use a clearly identified non-English input, but its public
result is English. This showcase policy does not restrict Workspace languages.

All businesses/clubs are fictional. JPG files are unmodified browser captures of
actual generated files in the local Manor application, not interface mockups.
Illustrations and narration were generated through normal configured media tools.

| File | Evidence |
| --- | --- |
| presentation-editable.jpg | Native editable four-slide Clay House workshop deck |
| presentation-slide-01.jpg, presentation-slide-03.jpg | Rendered pages from an earlier retained Clay House production version |
| podcast-playback.jpg | Native player for the actual 45-second Lantern Book Club MP3 |
| carousel-page-01.jpg through carousel-page-03.jpg | Three actual Window Garden cards, with visually checked lettering |
| handbook-pdf-cover.jpg | Latest Frame Club PDF in the browser PDF viewer |
| handbook-page-04.jpg | Latest handbook page with visible venue and portrait-consent cautions |
| dubbing-english-playback.jpg | Fresh English Cedar Cup video playing in the browser on 2026-09-14 |
| cedar-cup-english.mp4 | Byte-for-byte copy of the fresh 56.033-second English Workspace output |
| podcast-episode.mp3 | Byte-for-byte copy of the actual AI-narrated Workspace output |
| video-series-app.jpg | Unmodified 1280×720 browser capture of the Video Series Studio App with the Oak Street Baking Habits series opened: episode cards, the series' pinned narrator and its host and key art |
| oak-street-bakery-episode.mp4 | Size-reduced re-encode of the actual E01 vertical episode (see below) |
| series-showcase.mp4 | A 15-second edit of what two series made (see below), shown in the Video Series Studio and Stickman listings |

Cedar Cup's English demo uses captured fictional cafe-app states with existing AI
Mandarin narration and its matching Chinese SRT. The internal input was cropped
from 1280×720 to 1280×600 to remove prior burned captions, then padded below with a
200-pixel dark caption band. The crop retains the primary order actions, but loses
part of the bottom progress row and the repeated pickup-code area in the final
state. It is a prepared prototype-state input, not a full continuous UI recording
or customer footage.

The installed Dubbing Flow generated a new English AI voiceover and English SRT,
and rendered the actual 1280×800 MP4 while preserving this prepared picture. All
19 subtitle cues fit inside the caption band without covering app buttons; timing
is proportional, not word-aligned. The final translation notes contain both full
scripts, with Chinese only in the source-provenance appendix. No voice cloning,
lip sync or music separation is claimed. The old Mandarin video and screenshot
were removed from public assets; historical tests were not rewritten as English.

Original output SHA-256:

- cedar-cup-english.mp4: `f5c4654d6d1aa7bd902f02cda62cd18ab9b9271d0f54489654c95c78758eab72`
- dubbing-english-playback.jpg: `0739c4e2b2caf999b4ce416eb3a559e0c44ef9bb1fc134c1124b4cba47079283`
- podcast-episode.mp3: `e3749ce6ce220e7751d1905e8d872c50dc72628347419f3c937b628ea039d342`

Oak Street Bakery is fictional. The Video Series Studio made two episodes of one
series on the local stack on 2026-10-25. E01 is run `01M5V2RMDW39YQADVZ27FCBB74`, with
episode Document `01M5V33JBREZ45MD4EJH46ADJK`. E02 reused the series' narrator, host
picture and key art without generating them again, and is still at the review gate.
The public MP4 was re-encoded from that episode (H.264 CRF 31, AAC) to keep the
bundle small. Picture, narration and burned captions are otherwise unchanged:
720×1280, 44.437 seconds. Caption timing is estimated from the script, not
word-aligned.

- Original E01 episode.mp4: `37025568f46d11e4f7a805052c5c51afc13a092dd50cded89716b49d0b0e5f4a`
- oak-street-bakery-episode.mp4: `44d2f7630053e345380b9b4de0c8225fad5c12f3c239bb4a9362b85730ea25aa`
- video-series-app.jpg: `02707cb67e59e31a3575371509787f396832623542233ef09442501d16ff6b58`

series-showcase.mp4 is an edited 1920×1080, 15.000-second cut made on 2026-10-26
from actual local outputs of two fictional series: Tiny Habits, Calmer Mornings
(Stickman studio; series intro, five episode thumbnails, two seconds of the E06
episode) and Oak Street Baking Habits (Video Series Studio; series intro, three
episode covers, two seconds of the E03 episode, the series host and key art). The
vertical Oak Street material is centred over a blurred copy of itself; stills move
in slowly; the closing frame tiles eight of these pictures. The cut adds no text
of its own. The music is Mixkit "Motivating Mornings" under the Mixkit free
license, from its 28.18-second phrase, slowed 0.8% so every cut lands on a beat,
and loudness-normalized; the episodes' own narration is not used. It is an edit
of generated outputs, not a continuous recording of a run.

- series-showcase.mp4: `9091b4b1f301e1b6f396af1cd45a1fbe738942d8f3cf98acba0e89aaa87a1ca9`

## Verification scope and limits

Local browser checks on 2026-09-11 exercised all five installed production Flows,
real model-generated files, durable Knowledge storage, native viewers/editors,
and accept/revise branches. The podcast and dubbed video played to the end without
player errors. Final configurations also passed service-level export/install and
upgrade/revert checks. On 2026-09-14, all five 1.0.6 candidates were freshly
installed Live through the browser in an isolated Cloud test stack and generated
actual outputs. This uncovered additional quality/integration issues: narrow PDF
display distortion, Chat attachment path/preview handling, podcast loudness below
target, and incomplete notes/overlapping captions in an internal Mandarin run.
These findings are not an all-green acceptance claim or a production deployment.

The replacement English dubbing run is `01M2FD6JF0CFY5PCDN70PX0YR7`, with final
video Document `01M2FDGT8M9WY6F3MQB1WKTW4F`. Its native browser player completed a
fresh uninterrupted playback from the beginning to 56.033008 seconds with
`ended=true` and no player error. Full decoding and final-file hashes passed.
The prepared caption space and explicit full-script brief address this example;
they do not represent fixes to the general composer or other defects above.
The run remains at the operator review gate; no human listening approval occurred.

Playback and decoding do not certify speech intelligibility, pronunciation or
translation fidelity; no human listening approval was given. Independent
no-reference STT was unavailable. The English demo used supplied matching Chinese SRT;
the no-SRT transcription route was not browser-verified. Subtitle timing is
proportional, not word-aligned. Technical checks establish readable files,
media streams and matching page counts, not factual accuracy, visual quality or
independent PPTX/PDF content equivalence. Review every output before accepting.
These assets are listing examples, not installed Workspace files or history.
