# Slide layout contract

`layout_plan.json` is a version-1 object with a positive pixel `canvas`, a
`quality_tier` (`standard` or `premium`), and a `slides` object keyed `P01`,
`P02`, … without gaps. Each slide declares a kebab-case `role` and
`layout_family`, `background` (`light`, `dark`, `split`, `image`, or `gradient`),
`dominant_visual` (`chart`, `diagram`, `framework`, `image`, `media`, `metric`,
`process`, `roadmap`, `table`, or `typography`), and `density` (`sparse`,
`medium`, or `dense`).

Each slide also declares `text_roles` from
`templates/layouts/text_role_registry.json`, a `content_budget` with
`title_lines` (1–2), `max_words` (1–120), and `max_items` (1–9), plus at least
one bounded `regions` entry. A region needs a unique kebab-case `id` and
numeric `x`, `y`, `width`, and `height` inside the canvas.

The corresponding SVG root must repeat the slide's `layout_family`,
`background`, `dominant_visual`, `density`, and comma-separated `text_roles`
in `data-layout-family`, `data-background`, `data-dominant-visual`,
`data-density`, and `data-text-roles`. Validate before export with:

```bash
python3 /skill/scripts/layout_plan.py <project>/layout_plan.json \
  --svg-dir <project>/svg_output --slide-count <count>
```

The validator is authoritative for premium text budgets, layout variety,
SVG metadata, and any other detailed constraints. Correct every error before
export; do not invent a passing result.
