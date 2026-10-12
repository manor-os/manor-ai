---
name: pptx
description: "Use this skill to generate high-quality editable PowerPoint/PPTX decks from user requirements, topic prompts, or provided text/Markdown. Backed by PPT Master: plans the deck, writes SVG pages, quality-checks them, post-processes them, and exports to natively editable PPTX."
---

# PPT Master Skill

> AI-driven SVG presentation generation system. Turns user requirements, topic prompts, or provided text/Markdown into high-quality editable PPTX decks.

**Core Pipeline**: `Request → Project → Strategist + layout_plan.json → [Images/Video] → SVG composition + [native chart/media data] → Quality Check → Export → Render/Repair`

## Presentation Quality Contract (Mandatory)

The deck is an audience-facing communication artifact, not a formatted outline.
Every generation route MUST apply this contract before selecting layouts:

1. **Communication job** — record one sentence in `design_spec.md`: “By the end,
   [audience] should [outcome] because [central takeaway].” Infer sensible
   defaults from the request; ask only when a missing choice would materially
   change the result.
2. **Narrative arc** — use a cumulative arc appropriate to the job, such as
   context → stakes → evidence → implications → action; problem → options →
   recommendation; or current state → change → future state. An agenda alone is
   not a narrative.
3. **One claim per slide** — each page has one narrative job and a concise,
   takeaway-style title. Adjacent slides should answer or advance the question
   raised by the prior slide. Open deliberately and close with a decision,
   synthesis, implication, or next action rather than a generic “Thank you.”
4. **Audience-facing copy** — never expose production notes, prompt scaffolds,
   design deliberation, or model instructions on the canvas. Put presenter-only
   material in speaker notes.
5. **Density and typography** — shorten or split content before reducing type.
   Use at least 67 px for deck titles (≈50 pt), 47 px for slide titles (≈35 pt),
   32 px for subheads (≈24 pt), and 22 px for every audience-facing body or
   label (exports safely above 16 pt). Only footer/legal text in the bottom 12%
   of the canvas may be smaller. A reference template may preserve its style,
   but its non-footer text must still meet these delivery minimums. A title
   intended as one line must not wrap. Read `references/text-role-system.md` and
   design a page-specific role grammar rather than repeating only title, body,
   and boxed labels. Read `references/typography-profiles.md` and select one
   profile from `templates/layouts/typography_profiles.json`; ordinary
   unbranded business decks default to `office-modern`, not the more web-like
   `product-modern`. Premium slides declare at least three text roles in
   `layout_plan.json` and on the SVG root.
6. **Visual composition** — use one coherent composition, not a dashboard of
   repeated cards, pills, tabs, or UI panels. Vary adjacent silhouettes by
   content role. Use diagrams only when they materially clarify a relationship
   or sequence.
7. **Provenance** — every externally sourced non-trivial claim and asset must be
   traceable. Add a `[Sources]` block with exact URLs to that slide's speaker
   notes; never invent citations.
8. **Final-output QA** — SVG validation is necessary but not sufficient. After
   exporting the native PPTX, render every final slide through LibreOffice,
   inspect each slide at full size, and fix unintended overlap, clipping,
   wrapping, font substitution, broken connectors, unresolved placeholders,
   inconsistent page markers, and chart/data mismatches before delivery.

> **Generation methods** — the default is the editable-SVG pipeline below. An
> optional **Full-Page Image mode** (`workflows/image-mode.md`) keeps the same
> page-by-page SVG composition and validation discipline, then rasterizes every
> validated page into one full-slide image. AI generation may supply text-free
> visual assets, but exact text, charts, tables, and diagrams are authored in
> SVG before rasterization. The final deck is **not editable**, but image-model
> typography is forbidden.
> Image mode is **explicit opt-in only** — enter it only when the user asks for
> it (e.g. "整页图片模式 / image mode / 每页直接用图片生成") or when Manor invokes this
> built-in skill with structured `params.render == "full_page_image"`.

## Manor Built-In Skill Compatibility

This package is the built-in `pptx` skill, mounted in the sandbox at `/skill/`.

- Treat `/skill` as `SKILL_DIR`. Before running commands, use `cd /skill` and set `SKILL_DIR=/skill` when needed.
- Use `/skill/projects/...` for generated project folders unless the user asks for a different writable path.
- Final PPTX files are expected under `/skill/projects/<project>/exports/`; save the final artifact from there.
- Do not require external image-generation credentials inside the built-in skill. For `Acquire Via: ai` rows, create `images/image_prompts.json`, render `images/image_prompts.md` with `${SKILL_DIR}/scripts/image_prompts.py --render-md`, call Manor's system image tool with `generate_file(kind="image")`, then call outer sandbox action `write_file` with the returned `workspace_path` and an absolute destination under `<project_path>/images/`. Use `${SKILL_DIR}/scripts/import_system_image.py` only when the generated asset is actually mounted under `/workspace`.
- Do not use local image-provider backends from this skill. The built-in package has no local AI image generation entry point; use `${SKILL_DIR}/scripts/image_prompts.py` only for prompt manifest bookkeeping.
- User-provided video attachments exposed under `/workspace` are supported as
  embedded presentation assets. Copy each selected clip into
  `<project_path>/media/`, probe it with ffprobe, and follow
  `workflows/embedded-media.md`. Do not reference `/workspace` directly from
  `embedded_media.json`, because delivery verification must be reproducible
  from the project folder.

## Mandatory Pipeline Evidence

> Scope: the serial authoring, layout-plan, SVG, and quality rules in this
> section govern both output modes. The opt-in Full-Page Image mode
> (`workflows/image-mode.md`) replaces only the editable `finalize_svg.py` /
> `svg_to_pptx.py` export path with validated SVG → PNG → `images_to_pptx.py`.
> Follow that workflow's evidence checklist when it is selected.

- This skill has no direct-PPTX helper path. Do not write a custom Python, Node, or shell script that constructs a presentation directly.
- Final PPTX output must be produced from hand-written SVG pages through the PPT Master pipeline.
- Required pipeline evidence before saving the final PPTX:
  1. Project initialized with `${SKILL_DIR}/scripts/project_manager.py init <project_name>`.
  2. Strategist outputs exist: `<project_path>/design_spec.md` and `<project_path>/spec_lock.md`.
  3. `<project_path>/layout_plan.json` exists, follows
     `references/layout-contract.md`, and passes `scripts/layout_plan.py`.
  4. Executor SVG pages exist under `<project_path>/svg_output/`, written sequentially by the main agent. Quantitative chart pages also declare real PowerPoint charts in `native_charts.json`; read `workflows/native-charts.md`. Slides with playable video reserve `data-video-slot` regions and declare real PowerPoint media objects in `embedded_media.json`; read `workflows/embedded-media.md`.
  5. Quality checks have run with `layout_plan.py` and `${SKILL_DIR}/scripts/svg_quality_checker.py <project_path>` and have 0 errors.
  6. Post-processing/export commands have run in order: `total_md_split.py`, `finalize_svg.py`, then `svg_to_pptx.py`.
  7. The saved `.pptx` comes from `<project_path>/exports/` and was generated by `svg_to_pptx.py`.
  8. The final native PPTX was rendered with `render_pptx.py`; every rendered
     slide was inspected at full size and the QA result recorded in
     `<project_path>/qa/final-render.txt`.
  9. `pptx_quality_gate.py` passed with score 90 or higher and wrote
     `<project_path>/qa/pptx-quality.json`. A failed machine gate is a hard
     stop: fix, re-export, re-render, and rerun the gate before delivery.

Sandbox action `save_result` reruns this gate server-side for every PPTX in
`/skill/projects/<project>/exports/`. It verifies all-slide render evidence and
binds the report to the exact PPTX bytes by SHA-256. Missing, failing, stale, or
manually copied evidence cannot be delivered; renaming the output does not
bypass the check.

> [!CAUTION]
> ## 🚨 Global Execution Discipline (MANDATORY)
>
> **This workflow is a strict serial pipeline. The following rules have the highest priority — violating any one of them constitutes execution failure:**
>
> 1. **SERIAL EXECUTION** — Steps MUST be executed in order; the output of each step is the input for the next. Non-BLOCKING adjacent steps may proceed continuously once prerequisites are met, without waiting for the user to say "continue"
> 2. **BLOCKING = MATERIAL CHOICE ONLY** — Pause only when missing information would materially change scope, brand identity, source fidelity, or the requested output mode. Ordinary topic-only requests use recommended defaults and continue.
> 3. **NO CROSS-PHASE BUNDLING** — Cross-phase bundling is FORBIDDEN. Once the design contract is resolved—automatically for ordinary requests or explicitly for a material choice—all subsequent non-BLOCKING steps may proceed without further confirmation.
> 4. **GATE BEFORE ENTRY** — Each Step has prerequisites (🚧 GATE) listed at the top; these MUST be verified before starting that Step
> 5. **NO SPECULATIVE EXECUTION** — "Pre-preparing" content for subsequent Steps is FORBIDDEN (e.g., writing SVG code during the Strategist phase)
> 6. **NO SUB-AGENT SVG GENERATION** — Executor Step 6 SVG generation is context-dependent and MUST be completed by the current main agent end-to-end. Delegating page SVG generation to sub-agents is FORBIDDEN
> 7. **SEQUENTIAL PAGE GENERATION ONLY** — In Executor Step 6, after the global design context is confirmed, SVG pages MUST be generated sequentially page by page in one continuous pass. Grouped page batches (for example, 5 pages at a time) are FORBIDDEN
> 8. **SPEC_LOCK RE-READ PER PAGE** — Before generating each SVG page, Executor MUST `read_file <project_path>/spec_lock.md`. All colors / fonts / icons / images MUST come from this file — no values from memory or invented on the fly. Executor MUST also look up the current page's `page_rhythm` (`anchor` / `dense` / `breathing`), `page_layouts` (which template SVG to inherit, if any), and `page_charts` (which chart template to adapt, if any). Empty / absent entries are intentional Strategist signals — see executor-base.md §2.1. This rule exists to resist context-compression drift on long decks and to break the uniform "every page is a card grid" default
> 9. **SVG MUST BE HAND-WRITTEN, NOT SCRIPT-GENERATED** — Every SVG page is written by the main agent directly, one page at a time (see rules 6 and 7). Writing or running a Python / Node / shell script that produces the SVG files in batch — looping over pages, templating from data, or emitting them via a generator — is FORBIDDEN, including under "save tokens", "quick draft", or "user is in a hurry" pretexts. The script-generation path was tried on a feature branch and abandoned: cross-page visual consistency depends on per-page authoring with full upstream context, which a generator script cannot reproduce

> [!IMPORTANT]
> ## 🌐 Language & Communication Rule
>
> - **Response language**: match the user's input and source materials. Explicit user override (e.g., "请用英文回答") takes precedence.
> - **Template format**: `design_spec.md` MUST follow its original English template structure (section headings, field names) regardless of conversation language. Content values may be in the user's language.

> [!IMPORTANT]
> ## 🔌 Compatibility With Generic Coding Skills
>
> - `ppt-master` is a repository-specific workflow, not a general application scaffold
> - In this built-in package, invoke it through the `pptx` skill name.
> - Do NOT create `.worktrees/`, `tests/`, branch workflows, or generic engineering structure by default
> - On conflict with a generic coding skill, follow this skill unless the user explicitly says otherwise

## Main Pipeline Scripts

| Script | Purpose |
|--------|---------|
| `${SKILL_DIR}/scripts/project_manager.py` | Project init / validate / manage |
| `${SKILL_DIR}/scripts/analyze_images.py` | Image analysis |
| `${SKILL_DIR}/scripts/image_prompts.py` | AI image prompt manifest validation / Markdown sidecar / status updates |
| `${SKILL_DIR}/scripts/import_system_image.py` | Compatibility fallback for generated images already mounted under `/workspace` |
| `${SKILL_DIR}/scripts/icon_composer.py` | Build standalone vector icons with gradients, containers, duotone layers, and status badges |
| `${SKILL_DIR}/scripts/svg_quality_checker.py` | SVG quality check |
| `${SKILL_DIR}/scripts/layout_plan.py` | Validate semantic layout families, capacity budgets, SVG metadata, and deck rhythm |
| `${SKILL_DIR}/scripts/total_md_split.py` | Speaker notes splitting |
| `${SKILL_DIR}/scripts/finalize_svg.py` | SVG post-processing (unified entry) |
| `${SKILL_DIR}/scripts/svg_to_pptx.py` | Export to PPTX |
| `${SKILL_DIR}/scripts/render_pptx.py` | Render the final native PPTX through LibreOffice for per-slide visual QA |
| `${SKILL_DIR}/scripts/pptx_quality_gate.py` | Validate final OOXML, editability, typography, provenance, media quality, and rendered-slide evidence |
| `${SKILL_DIR}/scripts/render_page_images.py` | Full-Page Image mode composition — rasterize validated SVG pages to exact-size PNGs |
| `${SKILL_DIR}/scripts/images_to_pptx.py` | Full-Page Image mode export — assemble `page_images/page_*.png` into a one-image-per-slide PPTX |
| `${SKILL_DIR}/scripts/update_spec.py` | Propagate a `spec_lock.md` color / font_family change across all generated SVGs |

For complete tool documentation, see `${SKILL_DIR}/scripts/README.md`.

## Template Index

| Index | Path | Purpose |
|-------|------|---------|
| Layout templates | `${SKILL_DIR}/templates/layouts/layouts_index.json` | Query available page layout templates |
| Brand presets | `${SKILL_DIR}/templates/brands/brands_index.json` | Query available brand identity presets (color / typography / logo / voice) |
| Visualization templates | `${SKILL_DIR}/templates/charts/charts_index.json` | Query available visualization SVG templates (charts, infographics, diagrams, frameworks) |
| Composition registry | `${SKILL_DIR}/templates/layouts/composition_registry.json` | Semantic layout families with use/avoid rules, density, and bounded regions |
| Text-role registry | `${SKILL_DIR}/templates/layouts/text_role_registry.json` | Semantic typography roles, scale bands, family roles, and line limits |
| Typography profiles | `${SKILL_DIR}/templates/layouts/typography_profiles.json` | Office-modern, Microsoft 365, editorial, product, and CJK font systems with deterministic fallbacks |
| Icon library | `${SKILL_DIR}/templates/icons/` | See `${SKILL_DIR}/templates/icons/README.md`; search icons on demand with `ls templates/icons/<library>/ \| grep <keyword>` |

## Diagram Guidance

When a slide needs a process map, architecture diagram, relationship diagram,
matrix, funnel, timeline, or custom infographic, load
`references/blocks/editable-diagram.md`. Diagram output must remain editable
with native PPTX shapes/connectors/text after export, not a flattened raster
image. Existing diagram block guidance lives at `references/blocks/diagram.md`
and defers to the editable-diagram rules. Logical SVG edges must use
`data-connector="true"`; diagram nodes and panels must use
`data-connector-obstacle="true"`. The SVG gate rejects edges that cross text,
enter protected shapes, cross other connectors, render above nodes, or fail to
terminate on a visible shape boundary.

Before choosing a diagram, write the relationship as one sentence and select a
single topology. A hub-and-spoke is valid only for genuinely symmetric peer
relationships around one hub. Intake → controls → execution with audit or
evidence feedback is a governed pipeline, not a radial diagram.

## Standalone Workflows

| Workflow | Path | Purpose |
|----------|------|---------|
| `create-template` | `workflows/create-template.md` | Standalone layout template creation workflow |
| `create-brand` | `workflows/create-brand.md` | Standalone brand-only template creation (identity preset; no SVG page roster) |
| `image-mode` | `workflows/image-mode.md` | Full-Page Image mode — compose exact SVG pages with optional AI imagery, then flatten one image per slide; explicit opt-in only |
| `follow-reference-pptx` | `workflows/follow-reference-pptx.md` | Generate from a user-provided PPTX while preserving its hierarchy and visual system |
| `icon-composer` | `workflows/icon-composer.md` | Create or reuse refined native-PPT vector icons with layer palettes, official brand colors, optical alignment, quality checks, containers, gradients, and badges |
| `resume-execute` | `workflows/resume-execute.md` | Phase B entry — resume execution in a fresh chat after Phase A (Step 1–5) completed in another session (split mode) |
| `verify-charts` | `workflows/verify-charts.md` | Chart coordinate calibration — run after SVG generation if the deck contains data charts |
| `embedded-media` | `workflows/embedded-media.md` | Editable image placement plus true embedded PowerPoint video objects with posters and playback settings |
| `customize-animations` | `workflows/customize-animations.md` | Object-level PPTX animation customization — run only when the user explicitly asks to tune animation order/effects/timing |

---

## Workflow

### Step 1: Request Intake

🚧 **GATE**: User has provided a topic, requirements, outline, text, or Markdown content.

Use the invocation prompt and any provided text/Markdown directly as the source
material. A user-provided `.pptx` is a supported visual reference: carry its
path into Step 3 and follow `workflows/follow-reference-pptx.md`. This built-in
skill does not install or run PDF, DOC, Excel, URL, or legacy `.ppt` conversion
pipelines. If the user supplies only a topic, create the deck from that topic
and the model's general knowledge unless the user explicitly provides source
text.
>
> Browser-based live preview cannot render EMF (will show blank) — this is expected;
> the PPTX output is the source of truth.

**✅ Checkpoint — Confirm source content is ready, proceed to Step 2.**

---

### Step 2: Project Initialization

🚧 **GATE**: Step 1 complete; source content is ready (Markdown file, user-provided text, or requirements described in conversation are all valid).

```bash
python3 ${SKILL_DIR}/scripts/project_manager.py init <project_name> --format <format>
```

The canonical project directory includes the canvas format and date. Capture
the exact path printed after `Project created:` and use that path for every
later command, file write, quality check, and sandbox action `save_result` call. Never
reconstruct or guess it from `<project_name>`. Initialization also creates a
`projects/<project_name>` compatibility symlink, but the printed canonical path
remains the source of truth for delivery evidence.

Format options: `ppt169` (default), `ppt43`, `xhs`, `story`, etc. For the full format list, see `references/canvas-formats.md`.

Use the user's prompt and any provided text/Markdown directly as the source context. Do not run source import or conversion commands in the built-in generation path.

**✅ Checkpoint — Confirm project structure created successfully and the source context is available from the invocation prompt/conversation. Proceed to Step 3.**

---

### Step 3: Template Option

🚧 **GATE**: Step 2 complete; project directory structure is ready.

Choose exactly one visual route. The first matching route wins:

1. **User-provided PPTX or explicit template directory** — treat it as the
   visual source of truth. Reuse its page hierarchy, layouts, typography, and
   media frames; do not mix in an unrelated built-in template.
2. **Explicit custom direction without a reference deck** — create a custom
   system from the requested brand, theme, mood, or formatting brief.
3. **No visual direction** — query `layouts_index.json` and automatically
   shortlist unbranded layouts by semantic role: cover, statement, comparison,
   process, timeline, evidence, chart, table, and closing action. Preserve the
   selected layout's hierarchy and vary adjacent page silhouettes. Do not wait
   for the user to choose a filesystem path.

A bare **unbranded** template name may be resolved through
`layouts_index.json`. Never automatically apply a branded layout or logo from a
brand mention alone. Branded templates and brand bundles require either an
explicit directory path or supplied brand assets.

When the reference is a `.pptx`, load
`workflows/follow-reference-pptx.md`, then prepare the hierarchy-aware reference
workspace before Strategist begins:

```bash
python3 ${SKILL_DIR}/scripts/pptx_template_import.py \
  <reference.pptx> --output <project_path>/reference \
  --inheritance-mode both
```

Read `reference/manifest.json`, every master/layout/slide SVG, the inheritance
graph, and every flattened slide at full size. Record the selected reference
family for every new page in `spec_lock.page_layouts`. If the import exposes an
unsupported source object, report and reconstruct it deliberately; never
silently replace the reference with a generic built-in layout.

For an explicit template directory, copy the exact bundle into the project:

```bash
TEMPLATE_DIR=<user-supplied path>
cp ${TEMPLATE_DIR}/*.svg <project_path>/templates/
cp ${TEMPLATE_DIR}/design_spec.md <project_path>/templates/
cp ${TEMPLATE_DIR}/*.png <project_path>/images/ 2>/dev/null || true
cp ${TEMPLATE_DIR}/*.jpg <project_path>/images/ 2>/dev/null || true
```

> To create a new template, read `workflows/create-template.md`.

An explicit brand directory contributes color, typography, logo, voice, and
icon treatment. Copy it without flattening its assets:

```bash
BRAND_DIR=<user-supplied brand path>
cp ${BRAND_DIR}/design_spec.md <project_path>/templates/
cp ${BRAND_DIR}/*.svg <project_path>/templates/ 2>/dev/null || true     # brand logo SVG files
cp ${BRAND_DIR}/*.png <project_path>/templates/ 2>/dev/null || true     # brand logo raster files
[ -d ${BRAND_DIR}/images ] && cp -r ${BRAND_DIR}/images <project_path>/templates/
[ -d ${BRAND_DIR}/illustrations ] && cp -r ${BRAND_DIR}/illustrations <project_path>/templates/
[ -d ${BRAND_DIR}/icons ] && cp -r ${BRAND_DIR}/icons <project_path>/templates/
```

> To create a new brand, read `workflows/create-brand.md`.

#### Brand + layout combined input

A brand path and a layout template path may both be supplied in the same message. When both are present, Step 3 **fuses them into a single `design_spec.md`** inside `<project_path>/templates/` instead of leaving two specs side by side. Field-level precedence is fixed (no per-deck prompting):

| Field group | Source |
|---|---|
| Color (primary / secondary / accents / text / bg) | **brand** |
| Typography (font family) | **brand** |
| Logo | **brand** (if absent, fall back to layout's logo) |
| Voice & tone | **brand** |
| Icon style preference | **brand** |
| Canvas (size / viewBox / margins) | **layout** |
| Page roster + signature visual elements (top bar / underline / decorative motifs) | **layout** |
| Font-size hierarchy (H1 / H2 / body / data / label) | **layout** |
| Spacing, grid, layout patterns | **layout** |
| SVG technical constraints | **layout** |
| Placeholder set | **layout** |

Action: AI reads `${LAYOUT_DIR}/design_spec.md` and `${BRAND_DIR}/design_spec.md`, composes one fused `design_spec.md` using the table above, writes it to `<project_path>/templates/design_spec.md`. SVG page files come from `${LAYOUT_DIR}`; brand logos and asset subdirectories from `${BRAND_DIR}`. The fused spec carries a one-line `> Fused from: layout=<layout_id>, brand=<brand_id>` provenance note under its H1.

**Conflict gates** — clarify with the user only in these two cases:

1. **Brand has no logo, layout has one.** Ask: "your brand has no bundled logo; use the layout's logo, or leave the deck logo-less?"
2. **Layout is itself a branded template (e.g. `招商银行`, `重庆大学`, `中汽研_*`, `中国电建_*`) and the supplied brand is different.** Ask: "this layout carries `<layout's own brand>` identity, which conflicts with the `<supplied brand>` you provided — confirm you want brand identity from `<supplied brand>` and only the page structure from `<layout>`?"

If neither gate trips, fusion proceeds silently and Step 3 advances.

**✅ Checkpoint — Visual route selected and its constraints recorded. Ordinary
topic-only requests now have a semantic layout shortlist; explicit reference or
brand bundles are copied or fused before advancing.**

---

### Step 4: Strategist Phase (MANDATORY — cannot be skipped)

🚧 **GATE**: Step 3 complete; default free-design path taken, or (if triggered) template files copied into the project.

First, read the role definition:
```
Read references/strategist.md
```

> ⚠️ **Mandatory gate**: before writing `design_spec.md`, Strategist MUST `read_file templates/design_spec_reference.md` and follow its full I–XI section structure. See `strategist.md` Section 1.

**Eight Design Decisions** (full template: `templates/design_spec_reference.md`):

Resolve these as a single recommended design contract. For an ordinary request,
infer sensible defaults, record them in the spec, briefly tell the user what was
chosen, and continue without stopping. Ask for confirmation only when a missing
choice would materially alter scope, brand identity, reference fidelity, or the
editable versus full-page-image output mode.

1. Canvas format
2. Page count range
3. Target audience
4. Style objective
5. Color scheme
6. Icon usage approach
7. Typography plan
8. Image and video usage approach

**Split-mode note** (not a ninth decision): include one short line, rendered in
the user's language and prefixed with 💡, only for a heavy deck or when a context
window switch would materially improve execution quality.

| Signal read | Line content |
|---|---|
| Heavy (long page count / bulky source text) | State estimated page count and large source size; recommend switching to [split mode](workflows/resume-execute.md) after Step 5 — stop this chat, open a fresh window and input `继续生成 projects/<project_name>` to enter Phase B (SVG generation + export); no response or "continue" = default continuous mode. |
| Normal (default) | Omit the split-mode note and continue in one pass. |

If the user provided images, run analysis **before outputting the design spec**:
```bash
python3 ${SKILL_DIR}/scripts/analyze_images.py <project_path>/images
```

> ⚠️ **Image handling**: run `analyze_images.py` for objective dimensions and
> metadata, then inspect every selected image and its final crop at slide size
> with the available image-viewing tool. Replace blurry, distorted, badly
> framed, text-corrupted, or visually inconsistent assets before export.

If the user provided video, inspect its filename, duration, dimensions, and
codec with `ffprobe`, then list it in the design spec's media resource section.
Do not fabricate or fetch video implicitly. A missing user-supplied clip is
recorded as `Needs-Manual`, not silently replaced with a screenshot.

**Output**:
- `<project_path>/design_spec.md` — human-readable design narrative
- `<project_path>/spec_lock.md` — machine-readable execution contract (skeleton: `templates/spec_lock_reference.md`); Executor re-reads before every page
- `<project_path>/layout_plan.json` — one structured page composition per slide; read `references/layout-contract.md`, shortlist from `templates/layouts/composition_registry.json`, and validate before Executor begins

**✅ Checkpoint — Phase deliverables complete, auto-proceed to next step**:
```markdown
## ✅ Strategist Phase Complete
- [x] Eight design decisions resolved (recommended defaults or explicit confirmation)
- [x] Communication job and narrative arc recorded
- [x] Split-mode note included only when materially useful
- [x] Design Specification & Content Outline generated
- [x] Execution lock (spec_lock.md) generated
- [x] Structured layout plan generated and validated
- [ ] **Next**: Auto-proceed to [Image_Generator / Executor] phase
```

---

### Step 5: Image Acquisition Phase (Conditional)

🚧 **GATE**: Step 4 complete; Design Specification & Content Outline generated and the design contract resolved through recommended defaults or explicit confirmation.

> **Trigger**: At least one row in the resource list has `Acquire Via: ai`. If every row is `user` or `placeholder`, skip to Step 6.

**Always load the common framework**:

```
Read references/image-base.md
```

Then load the AI image generation reference only when at least one row needs generated imagery:

| Acquire Via | Load reference (only if any such row exists) | Run |
|---|---|---|
| `ai` | `references/image-generator.md` | System image generation tool; deliver the returned workspace file to `<project_path>/images/<filename>` with outer sandbox action `write_file`; use `import_system_image.py` only for `/workspace` compatibility and `image_prompts.py` for manifest state |
| `user` / `placeholder` | (skip) | (skip) |

Do not use web image search in this built-in skill. If an external visual is needed, generate it through the system image tool or mark the row `Needs-Manual`.

> ⚠️ **In-pipeline ai path MUST use manifest mode for planning** — even when only 1 ai row exists. Write `images/image_prompts.json` first and render `image_prompts.md` as the audit sidecar via `image_prompts.py --render-md`. In built-in Manor mode, use `generate_file(kind="image")` to create each file from the manifest prompts, then deliver it with outer `sandbox(action="write_file", params={"workspace_path": ..., "path": "<project_path>/images/<filename>"})`; use `import_system_image.py` only for an actual `/workspace` mount and do not ask for image API keys.

Workflow:

1. Extract all rows with `Status: Pending` and `Acquire Via: ai` from the design spec
2. Generate prompts per [image-base.md](references/image-base.md) §2 dispatch table
3. Verify every row reaches a terminal status: `Generated` or `Needs-Manual`

**✅ Checkpoint — Confirm acquisition attempted for every row**:
```markdown
## ✅ Image Acquisition Phase Complete
- [x] image_prompts.json created (when any ai rows processed)
- [x] image_prompts.md sidecar rendered (when any ai rows processed)
- [x] image_sources.json created (when any web rows processed)
- [x] Each row: status is `Generated` / `Needs-Manual` (no `Pending` remaining)
```

**Default — auto-proceed to Step 6.** Only when the user's Step 4 response explicitly opted into split mode (in reply to the optional hint), output the Phase A hand-off below and stop this conversation:

  ```markdown
  ## ✅ Phase A Complete
  - [x] Spec: `design_spec.md`, `spec_lock.md`
  - [x] Resources: `sources/`, `images/`, `templates/`
  - [ ] **Next**: open a fresh chat window and input `继续生成 projects/<project_name>` to enter Phase B via the [`resume-execute`](workflows/resume-execute.md) workflow.
  ```

> On acquisition failure, do NOT halt — follow the Failure Handling rule in [image-base.md](references/image-base.md) §5: retry once, then mark the row `Needs-Manual`, report to user, and continue to the checkpoint above.

---

### Step 6: Executor Phase

🚧 **GATE**: Step 4 (and Step 5 if triggered) complete; all prerequisite deliverables are ready.

Read the role definition based on the selected style:
```
Read references/executor-base.md          # REQUIRED: common guidelines
Read references/shared-standards.md       # REQUIRED: SVG/PPT technical constraints
Read references/layout-contract.md        # REQUIRED: per-slide composition contract
Read references/executor-general.md       # General flexible style
Read references/executor-consultant.md    # Consulting style
Read references/executor-consultant-top.md # Top consulting style (MBB level)
```

> Read executor-base + shared-standards + layout-contract + exactly one style file.

**Design Parameter Confirmation (Mandatory)**: before the first SVG, output key design parameters from the spec (canvas dimensions, color scheme, font plan, body font size). See executor-base.md §2.

**Pre-generation Batch Read (Mandatory)**: before the first SVG, batch-read every distinct layout SVG referenced in `spec_lock.page_layouts` and every distinct chart SVG referenced in `spec_lock.page_charts` (plus any §VII backup charts). One read per file, up front — do not re-read these during page generation. See executor-base.md §1.0.

**Per-page spec_lock re-read (Mandatory)**: before **each** SVG page, `read_file <project_path>/spec_lock.md` and use only its colors / fonts / icons / images, plus the per-page `page_rhythm` / `page_layouts` / `page_charts` lookups (resolves to template SVGs already loaded in the batch read above). Resists context-compression drift on long decks. See executor-base.md §2.1.

When the design spec contains a video resource, also read
`workflows/embedded-media.md`. Reserve its SVG region with `data-video-slot`
and write `<project_path>/embedded_media.json`; do not rasterize the clip into
the page or substitute a hyperlink.

**Title-safe layout contract (Mandatory)**: reserve the complete header stack
before placing body content. Keep titles inside the 5%–95% horizontal safe area.
Use one line when it fits; otherwise use exactly one explicit semantic line
break (`<tspan>`) and no more than two title lines. Never reduce a page title
below 47px to force fit. A subtitle or eyebrow must start below the title's
actual line box with at least 24px clear space, and body content must start at
least 32px below the final header line. Shorten the claim or split the slide if
those clearances cannot be met.

> ⚠️ **Main-agent only**: SVG generation MUST stay in the current main agent — page design depends on full upstream context. Do NOT delegate to sub-agents.
> ⚠️ **Generation rhythm**: generate pages sequentially, one at a time, in the same continuous context. Do NOT batch (e.g., 5 per group).

**Visual Construction Phase**: generate SVG pages sequentially, one at a time, in one continuous pass → `<project_path>/svg_output/`

For every quantitative data chart, read `workflows/native-charts.md`, reserve an
SVG slot, and write the structured series/axis configuration to
`<project_path>/native_charts.json`. Do not hand-draw ordinary bar, column,
line, area, pie, doughnut, radar, scatter, or bubble charts as SVG geometry.

**Quality Check Gate (Mandatory)** — after all SVGs, before post-processing/export:
```bash
python3 ${SKILL_DIR}/scripts/layout_plan.py \
  <project_path>/layout_plan.json \
  --svg-dir <project_path>/svg_output \
  --slide-count <count>
python3 ${SKILL_DIR}/scripts/svg_quality_checker.py <project_path>
```
- Any `error` (banned SVG features, viewBox mismatch, spec_lock drift, etc.) MUST be fixed before proceeding — return to Visual Construction, regenerate that page, re-run check.
- `warning` entries (low-res image, non-PPT-safe font tail, etc.): fix when straightforward, otherwise acknowledge and release.
- Run against `svg_output/` (not after `finalize_svg.py` — finalize rewrites SVG and masks violations).

**Logic Construction Phase**: generate speaker notes → `<project_path>/notes/total.md`.
For every externally sourced non-trivial claim or asset, append a literal
`[Sources]` block with the exact supporting URLs to that page's notes. Do not
invent sources and do not place production-only citations on the visible slide
unless the audience needs them.

**✅ Checkpoint — Confirm all SVGs and notes are fully generated and quality-checked. Proceed directly to Step 7 post-processing**:
```markdown
## ✅ Executor Phase Complete
- [x] Live preview started and kept available at the reported URL
- [x] All SVGs generated to svg_output/
- [x] svg_quality_checker.py passed (0 errors)
- [x] Speaker notes generated at notes/total.md
```

> **Chart pages?** If this deck contains data charts (bar / line / pie / radar / etc.), run the standalone [`verify-charts`](workflows/verify-charts.md) workflow before Step 7 to calibrate coordinates. AI models routinely introduce 10–50 px errors when mapping data to pixel positions; verify-charts eliminates that class of error. Skip if no chart pages.

---

## Design Ideas

**Don't create boring slides.** Plain bullets on a white background won't impress anyone. Consider ideas from this list for each slide.

- **Pick a bold, content-informed color palette**: The palette should feel designed for THIS topic. If swapping your colors into a completely different presentation would still "work," you haven't made specific enough choices.
- **Dominance over equality**: One color should dominate (60-70% visual weight), with 1-2 supporting tones and one sharp accent. Never give all colors equal weight.
- **Dark/light contrast**: Dark backgrounds for title + conclusion slides, light for content ("sandwich" structure). Or commit to dark throughout for a premium feel.
- **Commit to a visual motif**: Pick ONE distinctive element and repeat it — rounded image frames, icons in colored circles, thick single-side borders. Carry it across every slide.

### Step 7: Post-processing & Export

🚧 **GATE**: Step 6 complete; all SVGs generated to `svg_output/`; speaker notes `notes/total.md` generated.

🚧 **Image readiness GATE** (when Step 5 left ai rows in `Needs-Manual`): every expected file must exist at `project/images/<filename>` before running 7.1.

> If files are missing: PAUSE, list the missing filenames, point the user to `images/image_prompts.md` (each `### Image N:` block is paste-ready; auto-generated from `image_prompts.json`) and the required placement `project/images/<filename>`. Resume Step 7.1 only after all expected files are in place. `finalize_svg.py` and `svg_to_pptx.py` do not detect missing files at this layer — proceeding with gaps produces a deck with broken image references.

> ⚠️ Run the six sub-steps **one at a time** — each must complete successfully before the next.
> ❌ **NEVER** combine them into a single code block or shell invocation.

Canonical export-and-QA pipeline (mirrors `references/shared-standards.md` §5):

**Step 7.1** — Split speaker notes:
```bash
python3 ${SKILL_DIR}/scripts/total_md_split.py <project_path>
```

**Step 7.2** — SVG post-processing (icon embedding / image crop & embed / text flattening / rounded rect to path):
```bash
python3 ${SKILL_DIR}/scripts/finalize_svg.py <project_path>
```

**Step 7.3** — Export PPTX (embeds speaker notes by default):
```bash
python3 ${SKILL_DIR}/scripts/svg_to_pptx.py <project_path>
# Output (default-flow mode):
#   exports/<project_name>_<timestamp>.pptx           ← native pptx (canonical output, reads svg_output/)
#   backup/<timestamp>/svg_output/                    ← Executor SVG source backup (always written)
#
# Add --svg-snapshot to additionally emit the SVG-image preview pptx alongside the native pptx:
#   exports/<project_name>_<timestamp>_svg.pptx      ← SVG preview pptx (reads svg_final/)
```

When `<project_path>/native_charts.json` exists, the exporter automatically
adds those charts as editable PowerPoint Chart objects at their reserved SVG
slots. A manifest error is a hard export failure.

When `<project_path>/embedded_media.json` exists, the exporter automatically
embeds each local clip as a real PowerPoint media object at its reserved SVG
slot, using a supplied poster or an ffmpeg-generated frame. A manifest, media,
poster, or slot error is a hard export failure.

**Step 7.4 — Render the final native PPTX (mandatory)**:

```bash
python3 ${SKILL_DIR}/scripts/render_pptx.py \
  <project_path>/exports/<final_native_deck>.pptx \
  --output-dir <project_path>/qa/final-render
```

**Step 7.5 — Run the final machine quality gate (mandatory)**:

```bash
python3 ${SKILL_DIR}/scripts/pptx_quality_gate.py \
  <project_path>/exports/<final_native_deck>.pptx \
  --mode auto \
  --project <project_path> \
  --render-dir <project_path>/qa/final-render \
  --min-score 90 \
  --report <project_path>/qa/pptx-quality.json
```

The gate auto-detects editable versus full-page-image delivery and checks OOXML
relationship integrity, portable table typefaces/alignment, slide bounds,
title fit/header collisions, editable
content, the 16pt audience-text minimum, unresolved placeholders, internal
process copy, repeated layout silhouettes, overlap risk, source-note
formatting, `layout_plan.json` fidelity, declared native-chart and embedded-video counts, raster
DPI, exact rendered-slide count, and a SHA-256 fingerprint of the final file.
Its `repair_actions` are the mandatory next-pass instructions. Exit code 1 or
a score below 90 is a hard failure.

When the report contains `metrics.layout_defects`, use those records as the
authoring targets for the next pass. Each record identifies the slide, defect
kind, involved shape IDs, visible text, normalized bounds, and (for overlaps)
the measured intersection ratio. Repair the named elements, regenerate the
whole final PPTX, render every slide again, and rerun the gate; never discard
this evidence and retry an unrelated layout.

**Step 7.6 — Inspect every rendered slide at full size (mandatory)**:

Inspect every `qa/final-render/slide-*.png` individually at full size. Use a
contact sheet only to judge deck-level rhythm. Record the inspection in
`qa/final-render.txt`, including title wrapping, overflow/overlap, font
substitution, image crops, connector routing, chart/data fidelity, sources in
speaker notes, and adjacent-layout repetition. Any unintended defect returns to
the appropriate authoring step, followed by re-export and a fresh full render.
For Full-Page Image mode, also follow `workflows/image-mode.md` and write the
required `qa/visual-inspection.json` receipt bound to the exact PPTX and every
rendered-slide SHA-256. Its checks explicitly include nested slide screenshots,
duplicate/stale layers, and duplicate footer/callout text. Do not deliver a deck
that has only passed SVG validation or an automated numeric score. The receipt
must also record `connector_routing: pass` and `text_containment: pass`. Write
`qa/text-containment.json` version 2 with exact final-PPTX SHA-256, every slide
number, every final-render SHA-256, and slide-scoped review records. Any slide
whose text was changed or is placed in a panel, node, bubble, badge, or chart
mark must use `method: pixel-bbox` and include measured text/container padding
records. Every measured record must prove the whole containment chain: `bbox`
inside the actual `containing_region_bbox`, that region inside its actual
`parent_region_bbox`, and the parent inside the final render canvas. Use the
slide canvas as parent only for a true top-level region; a nested card or panel
must name its visual owner. Record `minimum_required_padding`, recomputed
`actual_minimum_padding`, `font_size`, and `minimum_font_size`; the gate rejects
inflated padding, undersized text, and a card that extends outside its parent.
For every `pixel-bbox` slide, also record `source_pixel_dimensions` from the
actual full-page image embedded in that slide. Premium raster typography must
use at least 192 effective DPI (2560×1440 for a 13.333×7.5 inch 16:9 slide);
never upscale a 1280×720 page image. Use real Regular/Medium/Semibold font files,
avoid faux bold and default DejaVu Bold unless the reference deck requires it,
and reserve the heaviest weight for rare emphasis rather than whole labels.
For generic business decks, use the `office-modern` profile: sentence-case
Carlito with regular body and bold headings. Lato is an explicit product/digital
choice, not the default PowerPoint look.
The corresponding `svg_output` page must retain real SVG `<text>` primitives;
a wrapper containing only a full-page raster is not authoring evidence and is
rejected. Never satisfy the DPI gate by resampling a smaller flattened page.
A single-slide evidence file never proves a multi-slide deck. Inspect every diagram at full size
and fail the deck if an edge crosses text, a node/panel interior, or another
connector, or if an endpoint floats away from its target boundary. For every
panel or circular-node label, bind the SVG `<text>` to an explicit
`data-text-container-id`; verify the measured text bbox remains inside the
container's declared padding. Shorten copy before reducing audience text below
the minimum size. A chart bubble or data mark is not automatically a label
container: keep only a short number inside the mark and place the audience label
in a measured external text region unless the full label passes the circle's
declared padding.

> The native pptx consumes `svg_output/` directly so the converter can preserve
> high-fidelity primitives (icon `<use>` placeholders, image `preserveAspectRatio`
> → `srcRect`, rounded rect `rx/ry` → `prstGeom roundRect`). The `svg_output/`
> snapshot in `backup/<timestamp>/` is always written so the project can be
> re-exported from frozen SVG sources without re-running the LLM. The SVG-rendered
> preview pptx is opt-in via `--svg-snapshot` — live preview already provides the
> SVG visual reference, so it's only needed when you want a self-contained file
> to share. Pass `-s output` or `-s final` to force a single source if you need it.

> **Paragraph editability vs line fidelity** — by default every dy-stacked line is
> its own PowerPoint text frame, preserving exact SVG layout. Add `--merge-paragraphs`
> only when the user explicitly asks for an editable / wrap-friendly export (e.g.
> "I want to edit the abstract as one block", "make text boxes resizable / reflow"):
> mergeable paragraph blocks collapse into one editable text frame with multiple
> `<a:p>`, at the cost of PowerPoint re-wrapping inside each box. Default off keeps
> pixel-fidelity; turn it on per the user's request, not on your own judgement.

**Optional animation flags** (the defaults already enable rich entrance animations — adjust only when the user asks for something different):
- `-t <effect>` — page transition. Default `fade`. Options: `fade` / `push` / `wipe` / `split` / `strips` / `cover` / `random` / `none`.
- `-a <effect>` — per-element entrance animation. Default `mixed` (auto-vary across the deck). Pass `none` to disable, or pick a specific effect like `fade`. Requires top-level `<g id="...">` groups (already required by Executor).
- `--animation-trigger {on-click,with-previous,after-previous}` — Start mode (matches PowerPoint's animation-pane Start dropdown). Default `after-previous` (click-free cascade; pace via `--animation-stagger`). Use `on-click` for presenter-paced reveals, or `with-previous` for all-at-once.
- `--animation-config <path>` — optional object-level sidecar. Default: `<project_path>/animations.json` when present.
- `--auto-advance <seconds>` — kiosk-style auto-play.

**Optional custom animations** (only when the user asks to tune animation order/effects/timing for specific objects):

Run the standalone [`customize-animations`](workflows/customize-animations.md) workflow. Default export already has global entrance animation; do not create `animations.json` unless object-level customization was requested.

Full effect list, anchor logic, and limits: [`references/animations.md`](references/animations.md).

> ❌ **NEVER** substitute `cp` for `finalize_svg.py` — finalize performs multiple critical processing steps
> ❌ **NEVER** force `-s output` for the legacy/preview pptx (PowerPoint's internal SVG parser drops icons and rounded corners). The default auto-split already gives native the high-fidelity source it needs without touching legacy.
> ❌ **NEVER** use `--only` (it suppresses one of the two output files)

## Role Switching Protocol

Before switching roles, **MUST first read** the corresponding reference file. Output marker:

```markdown
## [Role Switch: <Role Name>]
📖 Reading role definition: references/<filename>.md
📋 Current task: <brief description>
```

---

## Reference Resources

| Resource | Path |
|----------|------|
| Shared technical constraints | `references/shared-standards.md` |
| Canvas format specification | `references/canvas-formats.md` |
| Image-text layout patterns (Primary structures + Modifier layers — combine freely) | `references/image-layout-patterns.md` |
| Image layout sizing (math for side-by-side container dimensions) | `references/image-layout-spec.md` |
| SVG image embedding | `references/svg-image-embedding.md` |
| Structured layout contract | `references/layout-contract.md` |
| Native PowerPoint charts | `workflows/native-charts.md` |
| Icon library | `templates/icons/README.md` |

---

## Notes

- Local preview: `python3 -m http.server -d <project_path>/svg_final 8000`
- **Troubleshooting**: on generation issues (layout overflow, export errors, blank images, etc.), check `docs/faq.md` for known solutions
