# Native PowerPoint charts

Use a real PowerPoint chart for quantitative column, bar, line, area, pie,
doughnut, radar, scatter, or bubble data. Never invent missing values. Reserve
the chart's region in the source SVG with a rectangle carrying
`data-native-chart-slot="<id>"`, then declare the same ID in
`<project>/native_charts.json` before running `scripts/svg_to_pptx.py`.

Example for the second slide:

```json
{
  "version": 1,
  "slides": {
    "P02": [{
      "id": "evidence-chart",
      "slot": "evidence-chart",
      "type": "column",
      "position": {"x": 100, "y": 200, "width": 700, "height": 390},
      "categories": ["Before", "After"],
      "series": [{"name": "Hours", "values": [46, 18]}]
    }]
  }
}
```

The SVG uses the same 1280×720 pixel coordinate system as the plan. The
exporter validates slot geometry, chart type and data before writing the deck;
the final quality gate checks that the expected editable chart objects exist.
For scatter/bubble charts use `x_values` and `y_values` (and `sizes` for
bubble) in each series, as required by `scripts/svg_to_pptx/native_charts.py`.
