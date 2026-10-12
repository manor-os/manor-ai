# Composing vector icons

Search `templates/icons/` for a semantically appropriate bundled SVG. Use an
existing icon only when its meaning fits the slide. To make a compact icon
tile, run:

```bash
python3 /skill/scripts/icon_composer.py chart-scatter \
  --color '#17324D' --background '#EAF2F4' \
  --output <project>/images/chart-scatter.svg
```

The output remains vector and may be embedded by the normal SVG finalize and
PPTX export pipeline. Use one palette and consistent optical size across a
deck. Avoid decorative icons that compete with titles or quantitative
evidence; verify the rendered slide after export.
