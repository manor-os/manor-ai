---
name: pptx
description: "Generate editable PowerPoint/PPTX decks from requirements, topics, or text/Markdown. Pass params.mode='free_design' when the person asks, in any words or language, for no template, a different design on every page, a bespoke or magazine-like look, or a reference deck to imitate; otherwise leave params.mode unset. Template mode (default) writes one deck.json and builds a native editable PPTX from designed slot templates in seconds; free-design mode (PPT Master) designs every page as hand-written SVG for bespoke, reference-driven, or image-mode decks."
---

# PPTX Skill

Two ways to make a deck. Both produce a native, fully editable PPTX and both
are verified by the same final quality gate.

| Mode | How | Typical time | Use when |
|---|---|---|---|
| **Template** (default) | You write `deck.json`; `scripts/build_deck.py` lays it out on a designed theme | 3 tool calls | Every ordinary request: reports, proposals, updates, training, pitches, any page count |
| **Free design** | PPT Master: strategist spec → one hand-written SVG per page → export | many tool calls | Only on the structured signals below |

## Choose the mode

The invocation prompt is written by the calling assistant, not the user, so
its wording never selects a mode: "高端", "发布会级", "premium", "每页独立设计",
color and mood descriptions are style hints for template mode. Use **free
design** — read `/skill/workflows/free-design.md` (long; page through it with
`read_file` offset/limit) and follow it exactly — only on one of these
structured signals:

1. `params.mode == "free_design"` (the caller sets it when the user explicitly
   asked for bespoke per-page design).
2. A reference `.pptx` to imitate or an explicit template or brand directory
   path is provided (`workflows/follow-reference-pptx.md`, brand/layout
   templates).
3. Full-page image mode: `params.render == "full_page_image"`.

Otherwise use **template mode**, including when the prompt asks for a specific
style or brand color: pick the closest theme, and set `theme_overrides` only
for a brand color or company font the user actually named. Color descriptions
in the invocation prompt ("white background, navy titles, teal accents") are
style hints — map them to the closest theme instead of overriding. Only
`primary` and `accent` can be overridden, and they must stay legible. Never
switch modes because content does not fit or a build fails; fix `deck.json`.
Do not mix modes within one deck.

## Template mode

1. **Plan privately** (no tool call): the communication job — by the end,
   [audience] should [outcome] because [takeaway]; a narrative arc; one claim
   per slide with a takeaway-style title; the style; a layout per slide that
   matches its content shape. Slide 1 is always `cover`.
2. **Images** (default: the cover plus 2–4 image layouts such as `feature`,
   `image_cards`, `split_image`; skip only when the user wants text-only or a
   fast draft). Images carry most of a deck's polish — budget for them. Follow `generate_file`'s own image
   contract (one model search, then the generation calls). Issue every
   `generate_file(kind="image")` call **in the same round, in parallel**, with
   prompt `"<concrete subject for that slide>. Art direction: <the style's art
   direction below>. No text, no letters, no logos."` (cover/split/full images
   `aspect_ratio` 3:2, card and feature images 4:3). For **cutout-cover styles**
   (`cobalt`, `pop`, `editorial`) the cover image is one subject — a person
   (waist-up) or a product — `"... isolated on a plain white seamless studio
   background, soft shadowless light"`, aspect 3:4; the build removes the
   background and stands the subject over the style's painted shape. Then, again in one parallel round,
   copy each result into `/skill/projects/<slug>/images/<name>.png` with
   sandbox `write_file` using the returned `workspace_path`. If image
   generation is unavailable or fails, continue without images — never retry
   it more than once; image layouts are then replaced by text layouts.
3. **Write** `/skill/projects/<slug>/deck.json` with sandbox action
   `write_file` (`<slug>`: lowercase ASCII letters, digits, `_`).
4. **Build**:
   `sandbox(action="exec", params={"sandbox_id": ..., "command": "cd /skill && python3 scripts/build_deck.py projects/<slug>/deck.json"})`
   - `"status": "ready"` → step 5.
   - `"status": "fix_deck_json"` → every problem is listed with its JSON path.
     Fix all of them in one `write_file`, then rerun the same command.
   - `"status": "gate_failed"` → fix content errors it names (draft words such
     as TODO/TBD, production language on slides, URLs outside sources) and
     rebuild; layout/overflow errors are engine defects — report them, do not
     loop.
5. **Deliver**: `sandbox(action="save_result", params={"sandbox_id": ...,
   "file_path": "<pptx path printed by the build>", "filename": "<deck title>.pptx",
   "artifact_role": "final"})`, then reply with the returned file link.

The build fits every text slot, keeps all text ≥14 pt (titles ≥32 pt, cover
≥48 pt), prevents overlap and overflow by construction, draws the style's
backgrounds, motifs and native editable icons, writes
`projects/<slug>/exports/<slug>.pptx`, and runs the final gate (layout evidence
is verified by re-planning `deck.json`, so no LibreOffice render is needed).
Rebuilding overwrites the same file. Images must be PNG/JPEG/WebP; layouts
without an image fall back to the style's motifs, so a missing image never
blocks a deck.

### deck.json

```json
{
  "title": "文件引用统一测试",
  "theme": "mono",
  "footer": "内部测试演示",
  "slides": [
    {"layout": "cover", "kicker": "内部测试", "title": "文件引用统一测试",
     "subtitle": "统一引用格式、验证访问链路、提升协作效率", "meta": "2026 年 10 月 · 平台团队",
     "image": "images/cover.png"},
    {"layout": "icon_grid", "title": "测试目标：一套引用，处处可用",
     "items": [{"icon": "link", "title": "格式统一", "text": "所有入口输出同一种引用结构"},
               {"icon": "shield", "title": "访问可达", "text": "聊天、知识库与沙箱解析到同一文件"},
               {"icon": "history", "title": "可追溯", "text": "每次读写可追溯到版本与来源"}],
     "notes": "介绍三个测试目标。"},
    {"layout": "process", "title": "执行与验收：四步闭环",
     "steps": [{"title": "准备样本", "icon": "files"}, {"title": "跨入口引用", "icon": "link"},
               {"title": "校验结果", "icon": "check"}, {"title": "记录问题", "icon": "bug"}]}
  ]
}
```

Top level: `title` (required), `theme` (a style id below, default `mono`),
`brand` (≤60 chars, short name shown in every page's top chrome, e.g.
`"Manor · Q3 Review"`), `footer` (≤60 chars, defaults to the title),
`page_numbers` (default `true`),
`theme_overrides` (brand only: `{"colors": {"primary": "C8152D", "accent":
"..."}, "fonts": {"latin": "...", "ea": "..."}}`), `slides`.

**Title emphasis.** Wrap the one or two words that carry a title in braces —
`"让交付 {快三倍}"`, `"Grow {faster} with less"` — and the build sets them in
the style's emphasis treatment (accent color; serif italic for Latin words in
`mono`, `gradient`, `starlit`, `editorial`). Use it on the cover and on a few
key slides, at most one group per title. Limits count the text without braces.

**Styles** (`templates/decks/styles.json`). Each is a complete visual language:
page backgrounds and rhythm, motifs, card treatment, icon set, image framing
and type. Pick by audience and mood, not by color alone.

| style | look | use for | image art direction |
|---|---|---|---|
| `mono` | warm white, giant light type, serif-italic emphasis, wide rounded photo band, acid-yellow cards, tall numeral cards | default; proposals, strategy, product, executive | monochrome studio photography of obsidian and glass sculpture, soft grey backdrop, dramatic single light |
| `cobalt` | cobalt cover with a cut-out subject on a lemon brush stroke, ivory content, bold caps | brand, identity, product strategy, polished corporate | clean studio stationery and product photography, cobalt and lemon props on ivory paper, crisp daylight |
| `consulting` | navy and white, restrained red, hairline rules, outlined cards, numbers on rules | business review, board, consulting, finance | corporate architecture photography, glass and steel, muted navy tones, overcast daylight |
| `ink` | rice paper, ink-wash hills, vermilion seal, serif type, ghost characters | Chinese culture, tea, heritage, lifestyle, Eastern aesthetics | traditional Chinese ink wash painting, misty mountains, rice paper texture, one touch of vermilion |
| `gradient` | full-bleed photo cover with a frosted glass card, pastel gradients, glass cards | product launch, SaaS, AI, consumer tech, startup pitch | soft 3D abstract shapes, glossy glass spheres and ribbons, pastel gradient lighting |
| `starlit` | cinematic full-bleed cover, deep navy starfield, warm gold serif italics | launch events, premium brands, awards, galas | cinematic night sky over a quiet landscape, stars, warm golden light, deep navy |
| `pop` | cut-out subject on a yellow blob, saturated color cards, confetti, photo chips | marketing, campaigns, events, consumer, training | saturated studio photography of cheerful people and props on bright solid backdrops |
| `editorial` | cream paper, cut-out portrait on a mustard brush stroke, hand-drawn underline, column rules | research, culture, publishing, reports that read like a magazine | warm editorial portrait and still-life photography, film grain, mustard and vermilion tones |
| `neon` | near-black grid, neon lime and violet, condensed caps, mono chrome, ghost words | technology, AI, engineering, developer, data | futuristic data center and abstract circuitry, neon lime and violet light trails, cinematic |
| `aurora` | violet-to-magenta mesh field, one light centred title, mono captions, dark glass cards | AI-native startups, seed and Series A pitches, developer and AI products | luminous violet and magenta mesh-gradient light, soft depth, no objects |
| `documentary` | full-bleed reportage photo, giant condensed caps headline set low, mono captions, paper content pages | mission-driven, climate, health, marketplace pitches told through real people | documentary reportage of real people at work, natural light, candid, slight grain |
| `periwinkle` | periwinkle field, heavy white headline, product screen on a phone with floating UI cards | consumer apps, SaaS, fintech pitches where the product screen is the hero | a clean mobile app screen with cards, charts and buttons, flat periwinkle and white UI |
| `signal` | black space, planet horizon with a yellow rim, signal-yellow headline, phone mockup and KPI ring | growth-stage and consumer pitches led by one bold metric, dark keynote rooms | a phone showing a dark app dashboard with one bright yellow chart, black background |
| `plain` | white, black sans, one claim per slide, no decoration, one restrained orange accent | seed pitches and demo days where the argument carries the deck | simple honest product photography on a plain white background |
| `keynote` | black stage, centred giant type, product floating on black, bento tiles, huge thin step numerals | product launches, keynotes, announcements | studio product photography on pure black, dramatic rim light |
| `stripe` | white with a diagonal gradient wave, metrics divided by hairlines, raised cards with gradient icon tiles, pill pipeline | platform and fintech launches, developer products, metrics updates | clean payments dashboard UI, soft shadows, white and blurple |
| `bloom` | soft-focus photo under a print-dot screen, thin centred white title, sky and petal bokeh, airy cream content pages | lifestyle, beauty, hospitality and consumer brand stories, launches, proposals | soft-focus nature and lifestyle photo, shallow depth of field, airy pastel daylight |
| `velvet` | violet-to-magenta silk light with feathered glows and a soft light band, thin white type, mono captions | AI products, creative tech, launch events, cinematic pitches | cinematic violet and magenta light, silky gradients, soft glow, no objects |
| `photo` | every page carried by photography: full-bleed photo covers, statements, quotes and closings under a soft shadow, content pages over the cover photo thrown out of focus, serif titles with gold italics | brand stories, travel, hospitality, real estate, culture | cinematic editorial photography, natural light, shallow depth of field, calm grade, negative space for text |
| `swiss` | charcoal 12-column grid with registration marks, white modular cards, orange-red accent | data-heavy investor updates, fintech and B2B decks with many numbers | precise architectural photography, concrete and glass, one orange accent |

Legacy ids still work: `minimal`→`mono`, `business`→`consulting`,
`tech`→`neon`, `academic`→`editorial`, `vibrant`→`pop`, `elegant`→`starlit`.

**Layouts.** Every slide may also carry `notes` (speaker notes) and `sources`
(list of URLs/citations; rendered as a `[Sources]` block in the notes — required
for externally sourced claims). Content slides accept optional `kicker` (≤24
chars) and `lead` (≤90 chars, one-sentence subheading). Wherever an item
accepts `icon`, give a plain English keyword (`"shield"`, `"users"`,
`"growth"`, `"rocket"`, `"database"`); the build picks the matching icon from
the style's library.

| layout | required | optional | use for |
|---|---|---|---|
| `cover` | `title` | `subtitle`, `kicker`, `meta`, `image` | slide 1 only; with `image` it becomes a photo cover |
| `section` | `title` | `subtitle`, `number`, `kicker`, `image` | chapter divider |
| `statement` | `title` (≤40 chars, the one big message) | `subtitle`, `kicker`, `image` | a single key claim on a contrast page |
| `agenda` | `title`, `items` (3–7 strings) | | agenda / contents |
| `bullets` | `title`, `points` (2–6) | | one claim + supporting points; a point is a string or `{"title","text","icon"?}` |
| `cards` | `title`, `items` (2–4 `{title, text?, label?, icon?}`) | `variant` (`filled`/`numeral`/`icon`/`outline`) | parallel ideas, pillars; treatment rotates automatically |
| `numbered` | `title`, `items` (2–4 `{title, text?}`) | | numbered steps or principles with big numerals |
| `icon_grid` | `title`, `items` (3–6 `{icon, title, text?}`) | | capabilities, features, benefits |
| `process` | `title`, `steps` (3–5 `{title, text?, icon?}`) | | sequential steps |
| `timeline` | `title`, `events` (3–6 `{date, title, text?}`) | | milestones, roadmap |
| `stats` | `title`, `stats` (2–4 `{value, label, note?, icon?, trend?}`) | | headline numbers (`value` ≤8 chars, e.g. `42%`, `¥1.2亿`); `trend`: 3–16 real numbers drawn as a sparkline |
| `comparison` | `title`, `columns` (2–3 `{heading, points: 1–5 strings}`) | `highlight` (index of the recommended column) | options, before/after, pros/cons |
| `matrix` | `title`, `quadrants` (exactly 4 `{title, text?}`, row by row) | `axes` (`{"x": "...", "y": "..."}`) | 2×2: SWOT, priority, positioning |
| `quote` | `title`, `quote` (≤120 chars) | `attribution`, `image` (the quote then sits over the photo) | testimonial, key statement |
| `chart` | `title`, `chart` | `takeaways` (1–4 strings, shown beside the chart) | native editable chart |
| `progress` | `title`, `items` (2–6 `{label, value: 0–100, note?}`) | | completion, shares, survey results (bars or rings); values above 100% (NRR 118%) belong in `stats`, never clip them |
| `funnel` | `title`, `stages` (3–5 `{title, value?, text?}`, widest first) | | conversion, pipeline, filtering |
| `pyramid` | `title`, `levels` (3–5 `{title, text?}`, top level first) | | hierarchy, maturity, priorities |
| `cycle` | `title`, `steps` (3–6 `{title, text?, icon?}`) | `kicker` (shown in the centre) | a repeating loop (PDCA, seasons, flywheel) |
| `venn` | `title`, `sets` (2–3 `{title, text?}`) | `center` (≤16 chars, what they share) | overlap of capabilities or audiences |
| `team` | `title`, `people` (2–4 `{name, role, image?, text?}`) | | people, owners, speakers, award winners |
| `gallery` | `title`, `images` (3–5 `{image, caption?}`) | | a photo mosaic: places, products, moments |
| `table` | `title`, `columns` (2–6), `rows` (1–8, one cell per column) | | compact data table |
| `image_text` | `title`, `image`, `points` (1–5) | `image_side` (`left`/`right`), `caption` | a framed picture beside points |
| `split_image` | `title`, `image` | `points` (1–5), `image_side`, `kicker`, `lead` | half-page bleed photo with a story |
| `image_cards` | `title`, `items` (2–4 `{image, title, text?}`) | | scenarios, products, people, places |
| `feature` | `title`, `image` | `kicker`, `text` (≤160 chars), `points` (1–3), `tags` (1–3, ≤20 chars), `image_side` (default `left`) | one product, person, place or offer told with a large photo tile |
| `metric` | `title` (the claim, one line), `value` (≤8 chars) | `kicker`, `subtitle`, `image` (the number then sits over the photo) | one number that carries the slide: "-62%" + "fewer no-shows in 90 days" |
| `logos` | `title`, `logos` (3–16 company names, ≤24 chars) | `value` + `label` (a headline number beside the wall) | customers, partners, investors, press |
| `pricing` | `title`, `plans` (2–4 `{name, price ≤10 chars, period?, text?, badge? ≤14 chars, features: 1–6 strings}`) | `highlight` (index of the recommended plan) | plans, packages, quote options |
| `device` | `title`, `image` (a real screenshot) | `device` (`browser`/`laptop`/`phone`, default `browser`), `points` (1–4 call-outs `{title, text?}`), `image_side` | showing the product itself |
| `market` | `title`, `tiers` (exactly 3 `{label, value ≤8 chars, text?}`, largest first) | | TAM / SAM / SOM, total → reachable |
| `launches` | `title`, `items` (3–10 strings, ≤40 chars) | `tag` (≤16 chars pill, e.g. `New`), `highlight` (index to light up), `icon` | what's new, everything included, feature lists |
| `case_study` | `title`, `image`, `sections` (2–3 `{label, text ≤160 chars}`) | `value` + `value_label` (the result number) | customer story: challenge → approach → result |
| `testimonials` | `title`, `quotes` (2–3 `{quote ≤160 chars, name, role?, image?}`) | | several customer voices at once (one voice → `quote`) |
| `roadmap` | `title`, `periods` (3–8 labels ≤12 chars), `tasks` (2–7 `{label, start, end, note?}`; `start`/`end` are period indexes) | | Gantt plan by workstream (dated milestones → `timeline`) |
| `closing` | `title` | `subtitle`, `points` (1–4), `contact`, `kicker`, `image` | final takeaway / next steps |

`chart`: `{"type": "...", "categories": [2–12 labels], "series": [{"name":
"...", "values": [one number per category]}], "number_format": "0%" (optional
Excel format; values 0–100 with `0.0%` are shown as written), "unit": "百万元" (optional axis title), "highlight": 2 (optional,
column/bar only: the category to emphasise)}` — up to 4 series, exactly 1 for
pie/doughnut. Types: `column`, `bar`, `stacked_column`, `stacked_bar`,
`percent_column`, `percent_bar` (shares adding to 100%), `line`, `area`,
`stacked_area`, `pie`, `doughnut`, `radar` (3+ categories, comparing profiles).
Pick the type by the question: change over time → `line`/`area`; ranking →
`bar` + `highlight`; part of a whole → `doughnut`/`percent_*`; composition by
group → `stacked_*`; profile against a benchmark → `radar`. Use real data only.

**Compositions.** Each style lays out every layout its own way (Mono's
modular cards, Cobalt's rising bubbles, Starlit's glowing timeline…), and a
layout used twice in one deck automatically switches composition. To force
one, set `"variant"` on the slide: `agenda` list/typographic/cards ·
`statement` plain/ghost/centered/boxed/giant · `numbered` numeral/tall ·
`icon_grid` grid/columns/bento/cards · `stats` panels/rules/steps/hero/bento/metrics ·
`process` circles/zigzag/growing/numerals/pipeline · `timeline` axis/glow/vertical ·
`image_cards` cards/chips/overlay · `quote` bar/centered · `bullets`
rows/lines/checklist/keynote · `closing` plain/centered · `chart` panel/split ·
`progress` bars/rings · `team` circles/portraits · `gallery` mosaic/strip ·
`comparison` columns/spec · `metric` centered/side · `logos` grid/open ·
`pricing` cards/columns · `device` side/stage · `market` circles/bars ·
`launches` list/icon/chips · `case_study` split/band · `testimonials`
cards/columns. Usually leave it to the style.

### Writing rules

- Titles state the takeaway ("收入连续四个季度增长"), not the topic ("收入情况");
  keep them to about 20 CJK characters or 10 English words. Content shorter
  than a layout's maximum is better than content that barely fits.
- Choose layouts by content shape and vary them; three consecutive slides with
  the same layout read as a list. A 10-page deck typically mixes a `statement`,
  an icon or card page, one or two data pages (`chart`, `stats`, `progress`),
  a diagram (`process`, `funnel`, `pyramid`, `cycle`, `venn`, `matrix`), and
  two or three image pages (`feature`, `image_cards`, `gallery`, `team`). Close with a
  decision, synthesis, or next step, not "谢谢".
- Pitch and sales decks earn their credibility with proof pages: `metric` for the
  one number, `logos` for who already buys, `market` for the size of the prize,
  `device` for the real product, `case_study`/`testimonials` for outcomes,
  `pricing` and `roadmap` for what happens next.
- Slide text is for the audience: no prompt scaffolding, layout commentary, or
  "speaker notes" wording on slides; presenter detail goes in `notes`.
- Never invent statistics or sources. Without real data, prefer `cards`,
  `process`, or `bullets` over `stats`/`chart`.
- Match the user's language.

## Shared sandbox rules

- Skill files live at `/skill` (`SKILL_DIR`); work under `/skill/projects/`.
- Final PPTX files must be saved from `/skill/projects/<project>/exports/`.
  `save_result` reruns the final quality gate server-side and binds it to the
  exact PPTX bytes; editing the PPTX after the build, or copying it elsewhere,
  is rejected.
- Do not write ad-hoc scripts that construct a presentation. In template mode
  `scripts/build_deck.py` is the builder; in free-design mode the PPT Master
  pipeline in `workflows/free-design.md` is.
- A failing command caused by the environment (killed process, exit 137,
  missing binary, timeout) is not a content problem: report it instead of
  redesigning the deck.
