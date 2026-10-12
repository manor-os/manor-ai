# Text roles

Use `templates/layouts/text_role_registry.json` as the authoritative list of
semantic text roles. Declare the roles used by each slide in
`layout_plan.json` and on its SVG root as `data-text-roles`; mark each text
element with its own `data-text-role`. The role describes purpose, not a fixed
font size. A title and its supporting copy must have visibly distinct scale
and weight.

Start with a readable body size (normally 22–28 px at 1280×720), then apply
the registry's `ratio_to_body`, `weight`, and `max_lines`. Respect the stricter
minimums and title-safe region in `references/shared-standards.md`. If copy
does not fit at the minimum size, shorten it or split the slide. Do not shrink
text until it is unreadable or allow a title to wrap accidentally.

Choose roles for the actual content: for example `deck-title` + `dek` on a
cover; `slide-title` + `body` + `source` on an evidence slide; `metric` +
`metric-context` for a number; and `decision` for an explicit final action.
`scripts/layout_plan.py` validates role names and premium role variety.
