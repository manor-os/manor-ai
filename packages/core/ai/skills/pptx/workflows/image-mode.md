# Workflow: Full-Page Image Mode

> Optional generation mode. Each slide is a **single AI-generated full-page
> image** instead of hand-written editable SVG. Still page-by-page, one page at
> a time — only the per-page artifact changes (a raster image, not an SVG).

## When this mode runs

⛔ **Explicit opt-in only.** Enter this mode ONLY when the user explicitly asks
for it, e.g. "用整页图片模式 / image mode", "每页直接用图片生成", "做成图片
PPT，不用可编辑", "whole-slide image per page". The default pipeline (editable
SVG) stays in force for every other request. Do NOT switch to image mode on your
own judgement, and do NOT suggest it unless the user's intent clearly calls for
a non-editable, image-rendered deck.

Manor may also enter this mode when `invoke_skill(skill="pptx")` provides
structured tool params whose `params.render == "full_page_image"`. Treat that
parameter exactly like an explicit user opt-in from the composer UI; do not look
for keywords in the prompt to decide.

## Tradeoffs the user has accepted by choosing this mode

State these once, briefly, when entering the mode so the user is not surprised:

- **Not editable.** Every slide is one flat picture. Text, charts, and shapes
  cannot be edited in PowerPoint afterward.
- **Text can be imperfect.** Image models routinely misspell or garble text,
  especially long passages, small labels, and non-Latin scripts. Review each
  page; regenerate pages with broken text.

## Pipeline

Steps 1–4 are unchanged from `SKILL.md` (Request Intake → Project Init →
Template Option → Strategist). The Strategist still runs the Eight Confirmations
and produces `design_spec.md` + `spec_lock.md`, but frame each page entry as a
**full-slide image brief** (overall composition, the exact text to render,
palette, mood, illustration style) rather than an SVG layout spec. Carry the
confirmed color scheme / typography mood / motif into every page brief so the
deck stays visually consistent.

Then replace the Executor (Step 6) and Export (Step 7) with the steps below.

### Step I — Per-page image generation (sequential, one page at a time)

🚧 GATE: Step 4 complete; `design_spec.md` + `spec_lock.md` exist with a page
roster.

For each page, in order, in the current main agent (do NOT delegate to
sub-agents, do NOT batch):

1. `read_file <project_path>/spec_lock.md` and look up this page's brief
   (content, palette, motif). Same per-page re-read discipline as the SVG
   Executor — resists style drift on long decks.
2. Write one image prompt describing the **entire slide** at the deck's aspect
   ratio (match the canvas format: ppt169 → 16:9, story → 9:16, etc.). Include
   the exact on-slide text in quotes, the palette, and the recurring motif.
3. Generate the image **delivered straight into this project's `images/`**, as
   a zero-padded page file:
   ```
   generate_file(kind="image", params={
     "save_to_knowledge": false,
     "sandbox_path": "<project_path>/images/page_<NN>.png"
   })
   ```
   - **`sandbox_path`** writes the image bytes directly into the sandbox at that
     path — no `/workspace` mount round-trip, no propagation lag. Pass the
     project-relative path (e.g. `projects/<name>/images/page_01.png`); it is
     resolved against the skill working directory.
   - **`save_to_knowledge: false`** keeps the throwaway page image out of the
     user's Knowledge. Only the final `.pptx` is a deliverable.
   - Use `page_01.png`, `page_02.png`, … so slide order is unambiguous.
4. Confirm the page landed: `read_file`/`ls` `<project_path>/images/page_<NN>.png`.
   It should exist immediately (delivered in-band). **Fallback only if missing:**
   ```bash
   python3 ${SKILL_DIR}/scripts/import_system_image.py \
     --image-url <returned image_url> --project <project_path> \
     --filename page_<NN>.png
   ```
   (`import_system_image.py` polls the read-only `/workspace` mount with backoff
   for older deployments that lack in-band delivery.)
5. Glance at the result. If the text is broken or the composition is wrong,
   rewrite the prompt and regenerate that page before moving on.

> ⛔ **A missing page image is a hard stop, not a detour.** If the page file is
> still absent after the in-band delivery AND one `import_system_image.py`
> fallback, **stop the page loop, tell the user exactly which `page_<NN>.png` is
> missing and where it should land (`<project_path>/images/`), and wait.** Do
> NOT keep regenerating, do NOT switch to a different artifact (e.g. a video),
> and do NOT assemble a partial deck.

> Aspect ratio matters: request the image at the canvas aspect ratio so the
> export can use the full slide cleanly. The export default is `--fit contain`,
> which preserves every pixel and may letterbox mismatched images instead of
> clipping edge text. Use `--fit cover` only when the user explicitly wants a
> full-bleed crop.

### Step II — Assemble the PPTX

🚧 GATE: every `page_<NN>.png` exists in `<project_path>/images/`.

```bash
python3 ${SKILL_DIR}/scripts/images_to_pptx.py <project_path> --cleanup
# Output: exports/<project_name>_<timestamp>.pptx
```

> **`--cleanup` is the default for this mode.** The page images are
> intermediates; once the deck is assembled they are deleted so the project
> keeps only the `.pptx`. Omit `--cleanup` only if you explicitly need to keep
> the raw page images for debugging.

Options:

- `--cleanup` — delete the source page images after the deck is written, so the
  only artifact left is the `.pptx`.
- `--fit cover|contain|stretch` — how each image fills the slide. Default
  `contain` (show the whole image, letterboxing if needed). Use `cover` only
  for intentional full-bleed center-cropping, and `stretch` only if images are
  already the exact slide aspect ratio.
- `--format <key>` — override the canvas format (otherwise parsed from the
  project directory name).
- `--glob <pattern>` — change the page-image filename pattern (default
  `page_*`).
- `-o <path>` — custom output path.

The script reads `images/page_*.png` in natural order (page_2 before page_10),
sets the slide size to the canvas format, and places one full-bleed image per
slide. It does NOT run SVG quality checks, `finalize_svg.py`, or `svg_to_pptx.py`
— those are SVG-pipeline steps and do not apply here.

**✅ Checkpoint**:
```markdown
## ✅ Full-Page Image Mode Complete
- [x] All page_<NN>.png generated and reviewed (text legible)
- [x] images_to_pptx.py produced exports/<project>_<timestamp>.pptx
- [x] User reminded the deck is image-only (not editable)
```
