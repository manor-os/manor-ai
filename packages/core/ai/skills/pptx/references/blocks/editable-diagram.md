# Editable Diagram Block

Use this guidance whenever a deck needs a process, architecture, hierarchy,
matrix, funnel, timeline, or custom conceptual diagram.

## Hard Requirements

- Do not flatten diagrams into a single screenshot-like image.
- Build diagrams from native PPTX shapes/connectors/text through the SVG to PPTX
  pipeline whenever possible.
- Use text boxes for all labels so the user can edit wording after export.
- Use native connectors or editable lines for arrows, links, callouts, and flow
  paths instead of baking them into one bitmap.
- Keep each node, lane, icon, connector, and label as a separate editable object
  unless a visual asset truly must remain an image.

## QA

- Object count should show many shapes/connectors/text runs for a real diagram,
  not one large picture plus a title.
- Text remains selectable in PowerPoint.
- Connectors, arrows, and section boundaries can be moved independently.
- Raster images are allowed only for photos, screenshots, or visual references,
  not for the diagram structure itself.
