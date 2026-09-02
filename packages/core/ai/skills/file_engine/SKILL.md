---
name: file_engine
description: Create new Knowledge files and patch existing Knowledge files through one file-engine workflow.
---

# File Engine

Use this skill when the user asks to create, update, or AI-edit a Knowledge file and no more specific file-type skill has already taken over.

## Workflow

1. Use `inspect_file_engine` when you are unsure whether a file type supports patching.
2. Use `search_documents` or `list_documents` to resolve the user-visible Knowledge file.
3. Use `read_file` with the resolved `fs_path` before editing. Keep its `source_sha256` (the exact file-byte hash, including formatting). Pass `include_structure=true` to obtain DOCX paragraph/table indices, PPTX slide/shape IDs and first-master layout indices, or Excel sheet/table/column names. Never guess selectors; if the bounded structure result is truncated, do not assume omitted items are absent.
4. Use `generate_file` only when creating a new file.
5. Use `patch_file` when editing an existing file. Pass `expected_sha256` from `read_file`.

Do not regenerate an existing Office file just to make a small edit. Patch the existing file so the original package structure, editable source, and Knowledge document identity are preserved.
When a change needs several edits to the same file, send them as multiple ordered operations in one `patch_file` call so they are saved through one Knowledge projection.

## Current patch coverage

Template layout operations use the same executor in generation and patching:

- `paragraph.format` targets the same paragraph as `text.set`, without replacing text. Its non-empty `format` accepts `bold`, `italic`, `underline`, `strike` (booleans), `font_name`, `font_size`, `font_color` (six RGB hex digits), `alignment` (`left`, `center`, `right`, `justify`), `space_before`, `space_after`, `indent_left`, `indent_right`, `first_line_indent` (points), and `line_spacing` (a line-height multiple). Negative first-line indents create hanging indents. Word also accepts `keep_with_next`, `keep_together`, `page_break_before`, `widow_control` booleans and an existing paragraph `style` name (a style alone is allowed). PPT accepts `level` 0–8; changing level selects the existing list level, not a new bullet definition. Other text, links and properties remain unchanged. All existing runs and empty-paragraph typing styles are formatted; mixed-run range styling is not yet supported. The same plain-paragraph restrictions as `text.set` apply.
- `page.setup` changes page geometry through a non-empty `format` in points. Word requires zero-based `section_index` and supports `width`, `height`, `margin_top`, `margin_bottom`, `margin_left`, `margin_right`, `header_distance`, `footer_distance`. Set explicit dimensions to change orientation; width greater than height is landscape. Margins must leave positive content space. PPT supports whole-deck `width`/`height` (72–4032 points); this changes the canvas only, never scales or moves existing objects. Use `shape.transform` for an intentional object-layout adjustment. Word distances require twentieth-point precision, PPT distances hundredth-point precision; font sizes keep their existing half-/hundredth-point limits.

`read_file(include_structure=true)` reports Word `sections`, `paragraph_styles`, `paragraph_style_definitions`, native `text_boxes` and body/header/footer inline or floating `pictures`, PPT `page_size`, explicit shape/picture format and bounded native chart data/format, and Excel sheet `layout`/`page_setup` plus bounded native table/merge/data-validation/conditional-format/picture/chart selectors, metadata, references and data, so template selectors and dimensions can be inspected before modification. PPT inherited theme styles are not materialized in this discovery result. These are runtime document operations, not a new tool or MCP. Do not claim full template parity for unsupported inherited or vendor-extension features.

Word/PPT `text.set` replaces exactly one existing plain-text paragraph, even if empty or duplicated elsewhere. DOCX uses a zero-based story-local `index`; omit `story` for the body, pass a header/footer `story` plus `section_index`, or pass package-wide `text_box_index` plus that text box's local paragraph `index`. PPTX uses one-based `slide`, slide-unique text-frame `shape_id` (top-level or inside a group), and zero-based paragraph `index`. Discover these with `read_file(include_structure=true)`; Word reports `story_structures` and `text_boxes`, while PPT shapes report `group_path`, `has_text_frame`, paragraph count and bounded indexed text samples. The required `text` string may be empty to clear content. Newlines become soft line breaks, not extra paragraphs; CRLF/CR normalize to LF. Paragraph layout, list properties, Word section properties, PPT geometry, and other paragraphs are retained. Replacement inherits the first text run's style/link, or an empty paragraph's saved typing style; it does not redistribute new text across old mixed styles. Text is limited to 32,767 characters with no invalid control characters. Fields, embedded content, revisions, annotations/bookmarks and explicit page/column breaks are rejected atomically; `text.replace` remains available for supported text around them. Table cells continue to use `cell.set`. This operation works identically during generation and patching.

Color formatting uses opaque RGB: previous theme/tint/brightness/alpha modifiers are cleared only when the corresponding color is explicitly set. PPT shape formatting can additionally specify fill/line opacity.

- Word/PPT table `cell.format` uses the same selectors as `cell.set`, with a non-empty `format` object instead of `value`. Shared Office keys: `bold`, `italic` (booleans), `font_size` (1–409 points), `font_name`, `font_color`, `fill_color` (six RGB hex digits without `#`). Word sizes use half-point increments; PPT sizes use hundredth-point increments. Only specified properties change; text, links, paragraph layout, table geometry, borders, and other cells are retained. Formatting applies to all plain-text runs and the typing format of empty cells, including Latin/East Asian/complex-script font slots. It has the same plain-cell and merged-anchor restrictions as `cell.set`; `wrap_text` and `number_format` remain Excel-only. These operations also work during operation-based generation.
- Word/PPT `table.format` uses the same table selector as `table.delete` and a non-empty `format`. Both support partial `column_widths` maps keyed by column letters and `row_heights` maps keyed by 1-based row strings; values are points and unspecified dimensions remain unchanged. Word also supports `style_name` (an existing table style or null), `alignment` (`left`, `center`, `right`), `autofit`, `width`, `indent`, `row_height_rules` (`auto`, `at_least`, `exact`), and `repeat_header_rows`. PowerPoint also supports `first_row`, `last_row`, `first_column`, `last_column`, `banded_rows`, and `banded_columns` booleans. `read_file(include_structure=true)` reports the native table layout. Generation, template generation, and patching execute this identical operation.
- UTF-8 text/source files (including YAML, TOML, SVG, Draw.io, Mermaid and subtitles): `text.replace`. This preserves the BOM and line endings; other encodings must be converted explicitly first.
- JSON and Diagram JSON: `json.add`, `json.replace`, `json.remove` using `pointer` (JSON Pointer) and `value`. Object/array values are supported. Changes validate JSON syntax, not the complete diagram/application schema: preserve its required structure.
- CSV/TSV: `cell.set` with an existing A1 `cell` and scalar `value`; `row.append` with `values` matching the column count. Delimiters, quoting, and embedded newlines are handled by a CSV parser; byte-identical quoting is not guaranteed.
- Word (`docx`): `text.replace`; `section.insert`; native `paragraph_style.insert/format/delete`; story- or text-box-local `paragraph.insert`, `paragraph.delete`, `text.set`, `paragraph.format`, `table.insert`, `table.format`, `table.delete`, `cell.set`, and `cell.format`; plus native text-box and picture operations documented below. `section.insert` appends at `index=section_count`, accepts `start_type` (`continuous`, `new_page`, `even_page`, `odd_page`), optional page `format`, and `inherit_headers_footers` (default true). Set inheritance false to create independent empty header/footer stories for the new section. Omit `story` for body, use a header/footer story plus `section_index`, or use `text_box_index` without story selectors. Paragraph insertion takes `index`, `text`, and optional existing `style`; table insertion takes the target-local paragraph `index`, rectangular scalar `rows` (1–200 rows, 1–50 columns), and optional existing table `style`. An index equal to that target's `paragraph_count` appends. Table/cell operations use its zero-based `table_index`; `cell` is A1 and `value` is scalar (null clears). Body section-break paragraphs and the sole required paragraph in a header/footer/text box cannot be deleted. A linked later-section header/footer must target its defining prior section.
- PowerPoint (`pptx`): `text.replace`; `shape.transform` with one-based `slide`, slide-unique `shape_id`, and `transform` containing `x`, `y`, `width`, `height` in points and/or `rotation` in degrees. Top-level objects use slide coordinates; nested group members use the parent-local coordinates reported by `coordinate_space` and `group_path`. `shape.reorder` moves that object to zero-based `z_index` among its current siblings (`0` back, `sibling_count - 1` front) without changing group membership or object content. `shape.group` takes 2–100 unique contiguous sibling `shape_ids`, preserves z-order and returns the new group `shape_id`; siblings can be top-level or members of the same parent group. `shape.ungroup` bakes group translation/scaling into its members and lifts them back into that parent; it rejects rotated or flipped groups rather than silently changing their appearance. `slide.insert` requires zero-based insertion `index` and `layout_index` from the first master's layouts; `index=slide_count` appends. `slide.duplicate` uses a one-based source `slide` and optional zero-based insertion `index` (default immediately after the source), cloning its background, shapes, pictures, tables, hyperlinks, notes, charts and embedded workbooks so editable slide-owned parts remain independent. `slide.reorder` moves a one-based source slide to a zero-based index without recreating it; `slide.format` sets a native editable RGB or linear-gradient background, or restores master-background inheritance; `slide.delete` uses a zero-based `index`. `shape.delete` removes a top-level or grouped object by `slide` + `shape_id`; deleting a group's only member is rejected so the group itself can be deleted explicitly. `picture.delete`, `chart.delete` and `table.delete` use the same selector but require the matching native object type. `paragraph.insert` uses `slide`, text-frame `shape_id`, `text`, optional zero-based `index` (default append) and optional `format`; it inherits the nearest paragraph's paragraph/typing style before explicit overrides. `paragraph.delete` uses the same selector and cannot remove a text frame's only paragraph. `textbox.insert` takes `text`; `table.insert` takes rectangular scalar `rows` (max 200×50, native template table style, no Word `style` name), while `table.format` adjusts its native layout/style flags without recreating it. Picture and chart operations are documented below. Insertions require one-based `slide` and all four geometry fields `x/y/width/height`, with optional rotation; their result returns the new `shape_id`. `cell.set` requires one-based `slide`, table `shape_id`, A1 `cell`, and scalar `value`. Discover slide backgrounds/tables/charts through `read_file(include_structure=true)`. Existing slides, text and styling are retained.
- Excel (`xlsx`, `xlsm`): `cell.set`, `row.update`, `row.append`, `sheet.add`, `sheet.delete`, `sheet.reorder`, `sheet.rename`, `table.insert`, `table.format`, `table.delete`, `validation.insert`, `validation.format`, `validation.delete`, `conditional_format.insert`, `conditional_format.format`, `conditional_format.delete`, and the native picture/chart operations documented below; `cell.format` with `cell`, optional `sheet`, and `format` containing `bold`, `italic`, `wrap_text`, `font_size`, `font_name`, `number_format`, `font_color`, or `fill_color`. `sheet.add` accepts an optional zero-based `index` (otherwise appends). `sheet.delete` requires an explicit name and rejects the final visible worksheet, managed chart-data sheets, and detected formula/chart/defined-name references. `sheet.reorder` requires an explicit name and moves the same native worksheet to a zero-based `index`, preserving the active worksheet and all sheet contents/relationships. `sheet.rename` requires `sheet` + `new_sheet` and updates local formula/chart/validation/defined-name/internal-link references while leaving external-workbook references and string literals unchanged; calculation is forced on the next open. `table.insert` creates a named native table over prepared cells, `table.format` changes its built-in style/stripe flags, and `table.delete` preserves its cells. Data-validation and conditional-format operations create, adjust or remove discovered native rules. Also supports `underline`, `strike`, `shrink_to_fit` booleans; `horizontal` (general/left/center/right/fill/justify/centerContinuous/distributed), `vertical` (top/center/bottom/justify/distributed), integer `indent` (0–250) and `rotation` (0–180). `borders` accepts left/right/top/bottom objects with an Excel border `style` (e.g. thin/medium/double/dashed) and RGB `color`; null clears only that side. Colors are six RGB hex digits without `#`. Unspecified styles and cell values are retained.
- Excel `sheet.format` uses optional `sheet` and a non-empty `format`: `column_widths` maps individual column letters to Excel character units (0.1–255); `row_heights` maps row-number strings to points (0.1–409). Each map accepts at most 200 entries. Existing grouped dimensions are split to change only the addressed column. `freeze_panes` is the first unfrozen cell (e.g. B4 freezes the first column and first three rows; null clears); `show_gridlines` is boolean. These change workbook layout, not cell contents.
- Excel `merge.set` and `merge.clear` use optional `sheet` plus a rectangular A1 `range` such as `A1:C2`. `merge.set` is idempotent for an exact existing merge, rejects overlaps with other merges or tables, and refuses to discard a non-anchor cell's value, comment, hyperlink or explicit style. Set the intended anchor value/style separately with `cell.set`/`cell.format`. `merge.clear` requires the exact existing range reported in `read_file` as `merged_ranges`; it does not invent values or styles for newly exposed cells. Both operations are shared by generation and patching and preserve XLSM macros.
- Excel `validation.insert` uses optional `sheet`, one A1 `range`, and a `validation` object with required `type`: `whole`, `decimal`, `list`, `date`, `time`, `textLength`, or `custom`. Numeric/date/time/text-length rules use `operator`, `formula1`, and `formula2` for `between`/`notBetween`; list rules accept either `formula1` or inline `values` (1–100 strings, total Excel limit 255 characters); custom rules require `formula1`. Optional UI fields are `allow_blank`, `show_dropdown` (list only), `show_error_message`, `error_style` (`stop`, `warning`, `information`), `error_title`, `error`, `show_input_message`, `prompt_title`, and `prompt`. New rules cannot overlap an existing data-validation range. `validation.format` uses the zero-based `validation_index` reported by `read_file`, a non-empty partial `validation`, and optional replacement `range`; unspecified native properties are retained. `validation.delete` uses that index. These are native OOXML rules shared by generation and patching, not browser-only validation.
- Excel `conditional_format.insert` uses optional `sheet`, one A1 `range`, and a `conditional_format` object with required `type`. `cell` rules require an Excel comparison `operator` and one formula (two for `between`/`notBetween`); `formula` rules require one formula. Both accept `stop_if_true` and a differential `format` with bold/italic/underline/strike, font name/size/color, fill color, and left/right/top/bottom borders. `color_scale` uses two or three `thresholds` plus the same number of RGB `colors`; `data_bar` uses two thresholds, `color`, optional `show_value`, and 0–100 `min_length`/`max_length`; `icon_set` uses a native 3/4/5-icon `icon_style`, one threshold per icon, and optional `show_value`, `reverse`, and `percent`. Threshold types are `min`, `max`, `num`, `percent`, `percentile`, or `formula`; non-min/max thresholds require `value`, and optional `gte` controls equality. Formula strings may include a leading `=`, which is normalized to native OOXML form. Multiple rules may overlap and share the same range.
- `conditional_format.format` targets the selected sheet's zero-based `conditional_format_index` reported by `read_file`. Supply a partial `conditional_format`, a replacement `range`, or both. Unspecified rule fields, priorities, differential-style properties, and unsupported template attributes are retained. Moving one rule out of a shared range splits only that rule; sibling rules remain at the original range. A range-only move also preserves an unsupported native template rule. `conditional_format.delete` removes only the selected rule and removes its range container only when empty. All three operations are shared by blank generation, template generation and patching, preserve XLSM VBA, and remain native editable OOXML after browser cell saves.
- Excel `page.setup` uses optional `sheet` and `format`: `orientation` portrait/landscape; `paper_size` A3/A4/A5/letter/legal; `margin_left/right/top/bottom` and `header_distance/footer_distance` in points (0–720). `fit_width`/`fit_height` are page counts (0–32767, 0 automatic); setting either switches from percent scaling to fit-to-page. `print_area` is a single local range (A1:C8), `repeat_rows` a row range (1:3), `repeat_columns` a column range (A:A); null clears these ranges. Read the sheet's `layout` and `page_setup` first. Grouped dimension keys such as A:D in discovery are descriptive; write individual column letters. Generation and patch use these same operations. Browser editing must preserve these settings; print pagination is a workbook property, not a claim of browser print-preview support.
- PDF: `page.rotate` with one-based `page` and `degrees` (a multiple of 90 from -360 to 360). Encrypted or digitally signed PDFs are rejected.

Office `text.replace` matches the original text across formatting runs without rebuilding the paragraph. Replacement text inherits the first matched run's formatting and hyperlink; untouched text, inline images, and run properties are retained. `replace_all=false` edits the first match in body order (including Word tables), then existing headers/footers. Word merged cells and shared headers are processed once. Dynamic fields and opaque objects are not plain text: their cached values are not edited, and a match cannot cross them. Use indexed `text.set` rather than `text.replace` for content inside a Word text box.

Word/PPT `cell.set` changes exactly the selected cell, including when it is empty or other cells contain the same text. Word table indices are story-local and accept the same optional header/footer `story + section_index` selector. It preserves table geometry, cell properties, and existing paragraph properties; replacement text inherits the first text run's style, or the saved typing style for an empty paragraph. Newline-separated paragraphs map by position; added paragraphs inherit the last paragraph's properties. Reducing the paragraph count removes the replaced trailing paragraphs. Target only a merged range's top-left anchor. Merged continuations and omitted Word grid positions are rejected rather than redirected to another cell. Fields, embedded objects, nested tables, tracked revisions, annotations/bookmarks, and explicit page/column breaks are not plain cell values and are rejected atomically. Use `text.replace` for supported text-only changes around such content. Cell values must be finite scalars; text is bounded to 32,767 characters and cannot contain invalid control characters. Non-target cells are unchanged.

Excel edits preserve untouched rich-text cells. `cell.set` requires an explicit scalar `value`; use `null` to clear a cell. Non-finite numbers, overlong text, and invalid control characters are rejected instead of being silently changed. `cell.set` and `cell.format` require a single coordinate within the Excel grid; for a merged range, target its top-left anchor. Omit `sheet` to use the active worksheet, or provide its exact name (case-insensitive lookup is also supported). `row.update` requires `match_value` and unambiguous header names; `header_row` must be a positive integer.

`row.append` takes exactly one non-empty `row` object or `values` array. Without new selectors it appends after the worksheet's last used row (row 1 when empty), without copying formulas or extending tables. Two opt-in selectors are available for XLSX/XLSM:

- `table`: a table name from `read_file(include_structure=true)` on the selected sheet. Appends immediately below that table and expands its table/filter and row-sort ranges. `row` keys use its column names; `values` starts at its first column and may omit trailing formula columns. Copies the last data row's cell styles and translates its formulas; declared calculated-column formulas take precedence, including in header-only tables. Explicit values, including `null`, override inherited formulas. Do not also pass `header_row` or `inherit_from_row`.
- `inherit_from_row`: an existing row number for ordinary worksheet append. Copies that row's cell styles and translates its formulas, not literal values. Explicit values override inherited formulas.

Inheritance does not copy comments, links, row heights, merges, conditional formatting or validation rules, and does not calculate formula results. Table append does not move existing worksheet cells: totals rows, externally connected tables, overlapping tables/merges, occupied destination cells, and unsafe formula inheritance are rejected atomically. Ask for an explicit alternative when these limits apply; do not silently fall back to worksheet append.

XLSM patching and template generation preserve existing VBA bytes; they do not execute macros or fabricate a VBA project. XLSM generation therefore requires an existing readable `.xlsm` template with its exact `source_sha256`; blank XLSM generation is rejected. This does not change upload policy, so do not promise that native editing makes otherwise-rejected uploads available.

Only these operations are implemented; this is not full Office/PDF editing. Legacy Office formats (`doc`, `xls`, `ppt`, `wps`, `et`, `dps`) must be converted to OOXML before patching. Arbitrary PDF text/layout changes still need a PDF-specific workflow. Standalone binary image/audio/video editing is separate; Word/PPT/Excel picture operations edit native package pictures. Unsupported types must never be decoded as text.

`generate_file(kind="document", content=...)` accepts text sources, DOCX, PPTX and PDF. For deterministic Office creation, supply `operations` instead: DOCX, PPTX and XLSX blank generation, plus DOCX/PPTX/XLSX/XLSM template generation, use the **same ordered operations, validator and executor as `patch_file`**. `kind="word_document"`, `"presentation"` and `"spreadsheet"` also accept operations for their respective formats. XLSM is template-only because the engine preserves a real VBA project instead of inventing one. Do not mix operations with content, prompt, files, options or expected_sha256; operation generation only creates new files, and refuses an existing path. A blank DOCX starts with no body paragraphs/tables; a blank PPTX has no slides (default-template layout 6 is Blank); a blank XLSX has one sheet named `Sheet`. Every operation must succeed before the source and Knowledge document are committed together. No specialist/model call is needed for supplied operations. Content/prompt specialist generation remains available without operations.

Use the appropriate media kind for image/audio/video. `kind="code"` bundles accept text files only. Unknown extensions are not assumed generatable. Query `inspect_file_engine(file_type=...)` for the authoritative operation list and `can_generate_from_operations`; this does not claim that every specialist-generation feature is patchable.

## Generate with the same operations

```json
{
  "kind": "presentation",
  "name": "Reports/proposal.pptx",
  "operations": [
    {"op": "slide.insert", "index": 0, "layout_index": 6},
    {"op": "textbox.insert", "slide": 1, "text": "Proposal", "transform": {"x": 72, "y": 72, "width": 480, "height": 72}}
  ]
}
```

Read its returned `fs_path` and `source_sha256`, then use `patch_file` for subsequent edits to the same document. Existing PPTX files may have different layouts; discover them before inserting slides instead of assuming layout 6.

## Create and restyle native PPT shapes

PPT also supports `shape.insert` with one-based `slide`, a native DrawingML `preset`, all four point-based `transform` fields, optional rotation, optional `text`, and optional `format`. Presets cover basic geometry (`rect`, `roundRect`, `ellipse`, `triangle`, `rtTriangle`, `diamond`, `pentagon`, `hexagon`, `octagon`, `parallelogram`, `trapezoid`), arrows (`chevron`, `rightArrow`, `leftArrow`, `upArrow`, `downArrow`, `leftRightArrow`), symbols (`star4`, `star5`, `star6`, `plus`), flowchart shapes (`flowChartProcess`, `flowChartDecision`, `flowChartTerminator`, `flowChartInputOutput`), callouts (`wedgeRectCallout`, `wedgeRoundRectCallout`, `wedgeEllipseCallout`), and `line`. Use `inspect_file_engine` as the canonical enum. Lines have no text and may have one zero extent, but not both. Creation returns the native `shape_id`; use `shape.format` with `slide`, that ID and a non-empty `format` to restyle it. These same operations work in generation, template clones and patching. Existing top-level or grouped textboxes/shapes/connectors can be formatted; pictures, tables and group containers use their dedicated operations or transform/delete only.

Shape format keys: `fill_color`/`line_color` (six RGB digits; null removes fill/line), `fill_opacity`/`line_opacity` (0–1, requires solid fill), `line_width` (0–72 points), `line_dash` (solid/dash/dot/dashDot/lgDash/lgDashDot/lgDashDotDot/sysDash/sysDot/sysDashDot/sysDashDotDot), `corner_radius` (roundRect only, points up to half the shorter side), `vertical_alignment` (top/middle/bottom), boolean `word_wrap`, and `margin_left/right/top/bottom` in points leaving positive text space. `gradient_fill={angle,stops:[{position,color,opacity?}]}` takes a 0–360-degree angle and 2–16 ordered 0–1 stops; it cannot be mixed with fill_color/fill_opacity. `shadow={color,opacity,blur,distance,angle}` uses RGB, 0–1 opacity, point distances, and degrees; null clears only outer shadow. Shape style distances use hundredth-point precision. Unspecified properties, existing text and native objects remain unchanged; use paragraph.format/text.set separately for text. Complex effectDag shadows require a separate workflow.

```json
{"op":"shape.insert","slide":1,"preset":"roundRect","transform":{"x":72,"y":90,"width":480,"height":180},"text":"Client proposal","format":{"fill_color":"174C46","line_color":null,"corner_radius":18,"margin_left":18,"vertical_alignment":"middle"}}
```

## Insert and edit native PPT pictures

Read an existing Knowledge image first. Use its exact `fs_path` and `source_sha256` as `source`; the runtime checks read permission, opens a non-symlink snapshot and binds that path/hash to approval. The executor never reads an arbitrary local path. PNG/JPEG/GIF/BMP/TIFF remain native media; readable WebP/AVIF/ICO sources are embedded as PNG.

`picture.insert` requires one-based `slide`, `source` and all four point-based transform dimensions. `fit` is `stretch` (exact box), `cover` (aspect-preserving center crop), or `contain` (aspect-preserving centered smaller box). Optional picture `format` accepts `crop={left,top,right,bottom}` fractions, `opacity` from 0–1 and `alt_text` up to 1000 characters. Opposite crop fractions must total less than 1. The returned `shape_id` addresses later edits.

`picture.replace` uses `slide`, picture `shape_id` and a new source; it preserves geometry, rotation, flips, crop, opacity and alt text. `picture.format` changes only specified picture properties. The same operations target pictures inside groups. `picture.delete` removes that picture and prunes its media only when no remaining object shares the relationship. `shape.transform` moves/resizes/rotates any top-level object or group member and additionally accepts boolean `flip_horizontal` and `flip_vertical`; grouped geometry uses the discovered parent-local coordinate space. These operations run identically during blank generation, template generation and patching.

```json
{"op":"picture.insert","slide":1,"source":{"path":"Assets/hero.webp","expected_sha256":"64 hex SHA-256 from read_file"},"transform":{"x":72,"y":120,"width":576,"height":300},"fit":"cover","format":{"opacity":0.92,"alt_text":"Product dashboard"}}
```

## Insert and edit native Word pictures

Word uses the same authorized `source={path,expected_sha256}` snapshots and `picture.insert`, `picture.delete`, `picture.replace`, `picture.format` names. `picture.insert` creates a native picture in its own paragraph before zero-based story-local `index`; omit `story` for `body`, or select `header`, `first_page_header`, `even_page_header`, `footer`, `first_page_footer` or `even_page_footer` plus optional `section_index` (default 0). A non-first section linked to the previous page-furniture definition is rejected rather than silently changing the shared prior section. `picture.delete` uses a discovered package-wide zero-based `picture_index` and preserves shared media still used elsewhere. Optional `transform` accepts `width` and/or `height` in points; one dimension preserves the image's source aspect ratio. Optional `format` accepts `alt_text`, `layout` (`inline` or `floating`) and inline paragraph `alignment` (`left`, `center`, `right`). The output stays a real DOCX image relationship, not a rasterized page.

Inspect first: Word structure reports `pictures` with package-wide `picture_index`, `story`, shared `section_indices`, `layout`, dimensions, alt text, alignment and a story-local `paragraph_index` when the picture is in a direct paragraph. Body pictures come first, followed by unique header/footer parts in first-reference order. `picture.replace` uses that `picture_index` and a new source while retaining native layout and metadata. `picture.format` changes explicit `width`, `height`, `alt_text`, `layout` or alignment; setting only one dimension preserves the current aspect ratio. Floating format additionally accepts twentieth-point `position_x`/`position_y`, `relative_from_horizontal`, `relative_from_vertical`, `wrap` (`none`, `square`, `top_bottom`), `behind_text`, `allow_overlap`, `layout_in_cell`, and `distance_top`/`bottom`/`left`/`right`. Unspecified anchor and wrap properties remain unchanged.

```json
{"op":"picture.insert","index":2,"source":{"path":"Assets/logo.png","expected_sha256":"64 hex SHA-256 from read_file"},"transform":{"width":180},"format":{"alt_text":"Company logo","alignment":"center"}}
{"op":"picture.insert","story":"header","section_index":0,"index":0,"source":{"path":"Assets/logo.png","expected_sha256":"64 hex SHA-256 from read_file"},"transform":{"width":72},"format":{"layout":"floating","position_x":432,"position_y":12,"relative_from_horizontal":"page","relative_from_vertical":"page","wrap":"none","behind_text":true,"alt_text":"Brand mark"}}
```

## Create and edit native Word text boxes

Word exposes package-wide `text_boxes` from modern DrawingML/WPS and legacy VML content in body, header and footer stories. Inspect first, then use `text_box_index` without `story`/`section_index` for `text.set`, `paragraph.insert/delete/format`, `table.insert/delete`, and `cell.set/format`; paragraph and table indices are local to that box. The last required paragraph cannot be deleted. This edits native OOXML content and retains the surrounding shape rather than rasterizing or rebuilding it.

`textbox.insert` creates a floating editable DrawingML text box in its own paragraph. It accepts optional story selectors and story-local insertion `index`, string `text` (default empty), a supported non-line `preset` (default `rect`), and required `transform={x,y,width,height}` in points with optional rotation. It returns `text_box_index`. `shape.transform` changes selected x/y/width/height/rotation; explicit DrawingML and VML group members use the `group_local` point coordinates reported by discovery and retain the outer anchor and siblings. Ambiguous shared drawings without an explicit group fail atomically. `shape.delete` removes the selected DrawingML or VML group member without deleting siblings, prunes unused relationships and removes the empty group/drawing after its last visual member; ambiguous multi-content shapes or shared drawings without an explicit group fail atomically. `shape.format` accepts `preset`, fill/line RGB or null, fill/line opacity, line width, text margins, `vertical_alignment`, `word_wrap`, `alt_text`, wrap (`none`, `square`, `top_bottom`), layering/overlap/table-layout booleans and anchor edge distances; grouped members support content/member-local formatting but reject shared-frame anchor properties. VML templates support native geometry plus fill/line/margins/alt-text formatting; unsupported VML-only requests fail atomically.

```json
{"op":"textbox.insert","index":1,"text":"Editable callout","preset":"roundRect","transform":{"x":72,"y":90,"width":360,"height":108},"format":{"fill_color":"EAF5F0","line_color":"174C46","line_width":2,"margin_left":12,"vertical_alignment":"middle","alt_text":"Proposal callout"}}
{"op":"text.set","text_box_index":0,"index":0,"text":"Revised callout"}
{"op":"shape.transform","text_box_index":0,"transform":{"x":90,"width":380}}
```

## Create and maintain native Word paragraph styles

`paragraph_style.insert` creates a named editable paragraph style using exact `style`, optional `base_style`, optional `next_style`, and optional direct `format`. Omitted `base_style` defaults to `Normal`; null clears it. A null/self `next_style` means the same style continues after Enter. The direct format keys are the Word subset of `paragraph.format`: font family/size/RGB, bold/italic/underline/strike, alignment, spacing, indents, line-spacing multiple, and pagination flags. `paragraph_style.format` selectively changes those same properties on an existing custom or built-in style. Unspecified style XML and inherited properties remain intact.

Inspect `paragraph_style_definitions` before changing a template; each entry reports exact name, style ID, built-in status, base/next style and direct formatting. `paragraph_style.delete` removes only an unused custom paragraph style. It rejects built-ins, styles still assigned anywhere in the Word package, and styles referenced by another style. These operations run through the same executor during blank generation, template generation and patching, so a style can be defined and immediately used by a later `paragraph.insert` in one atomic batch.

```json
{"op":"paragraph_style.insert","style":"Client Heading","base_style":"Title","next_style":"Normal","format":{"font_name":"Arial","font_size":24,"font_color":"174C46","bold":true,"space_after":8,"keep_with_next":true}}
{"op":"paragraph.insert","index":0,"text":"Service proposal","style":"Client Heading"}
```

## Insert and edit native Excel pictures

Excel uses the same authorized image snapshots and `picture.insert`, `picture.delete`, `picture.replace`, `picture.format` names. `picture.insert` targets optional `sheet`, requires `source`, accepts a top-left A1 `anchor` (default `A1`), optional `transform={width,height}` in points, and optional `format={alt_text}`. `picture.delete` uses `sheet` + zero-based `picture_index`. Setting one dimension preserves the source aspect ratio. It creates a native SpreadsheetML drawing relationship, not a cell value or screenshot layer.

Inspect first: each worksheet reports `pictures` with zero-based `picture_index`, start/end anchors, anchor type, displayed point dimensions when fixed, intrinsic pixel dimensions, name, alt text and media format. `picture.replace` uses `sheet`, `picture_index` and a new source while retaining its native anchor, displayed size, object name and alt text. `picture.format` changes explicit `anchor`, `width`, `height` or `alt_text`; a single dimension preserves the current displayed aspect ratio for fixed one-cell pictures. Moving a two-cell template picture retains both markers and offsets. Resizing a two-cell picture requires both dimensions and converts only that picture to a fixed one-cell extent. XLSM macro bytes are preserved.

The editor, Knowledge viewer and chat preview resolve the native drawing relationship and place the image at its worksheet anchor. Ordinary browser cell edits preserve the drawing XML, drawing relationships and media bytes.

```json
{"op":"picture.insert","sheet":"Dashboard","source":{"path":"Assets/logo.png","expected_sha256":"64 hex SHA-256 from read_file"},"anchor":"F4","transform":{"width":180},"format":{"alt_text":"Company logo"}}
```

## Create and edit native PPT charts

`chart.insert` creates an editable PowerPoint chart, including its native embedded workbook. It requires one-based `slide`, a supported `chart_type`, `categories`, `series=[{name,values}]`, and all four point-based transform fields. Supported types are `column`, `column_stacked`, `column_stacked_100`, `bar`, `bar_stacked`, `bar_stacked_100`, `line`, `line_markers`, `area`, `area_stacked`, `area_stacked_100`, `pie`, `doughnut`, `scatter`, `scatter_lines`, `scatter_lines_markers`, `scatter_smooth`, `scatter_smooth_markers`, `combo_column_line`, `stock_hlc`, `stock_ohlc`, `stock_vhlc`, and `stock_vohlc`. Categories contain 1–1000 string/finite-number labels; scatter categories are finite numeric x values. There are 1–50 named series and at most 10,000 total numeric/null values. Every values array must match the category count. Pie/doughnut charts require one series. A combo chart requires at least two series: its final series is the line plot and all preceding series are clustered columns. Stock series are exactly High/Low/Close, Open/High/Low/Close, Volume/High/Low/Close, or Volume/Open/High/Low/Close. The volume variants keep volume on the primary axis and prices on a native secondary axis.

`chart.data` targets a discovered chart `shape_id` and replaces only `categories`/`series`, retaining chart type, geometry, title, axes, colors and native styling. For `combo_column_line`, replacement also rebalances plot membership so only the final series is a line; stock replacements retain their native high-low lines and OHLC up/down bars. `chart.format` changes only explicit chart presentation properties: `title` (null clears), `style` 1–48, legend visibility/position/layout, `vary_colors`, data-label visibility/content/position, category/value axis titles and visibility, value-axis min/max/major unit, major gridlines, `series_colors`, and pie/doughnut `category_colors`. Scatter charts additionally accept `category_axis_min`, `category_axis_max`, and `category_axis_major_unit` for their numeric x axis. Non-stacked column/bar/line/scatter/combo charts accept `trendlines`, an array of unique `{series_index,type,...}` entries. Types are `linear`, `exponential`, `logarithmic`, `polynomial`, `power`, and `moving_average`; optional fields are `name`, `forward`, `backward`, `intercept`, `display_equation`, and `display_r_squared`, plus `order` 2–6 for polynomial or `period` 2–255 for moving average. Set `type:null` to clear that series' trendline. The same charts accept `error_bars` entries with `series_index`, `type` (`fixed`, `percentage`, `standard_deviation`, `standard_error`), optional `direction` (`y`, or `x` for scatter), `side` (`both`, `plus`, `minus`), `end_style` (`cap`, `no_cap`), and the required `value` for fixed/percentage/standard-deviation types. Set `type:null` to clear that series' error bars. Stock charts reject `vary_colors`, trendlines and error bars. `chart.delete` removes the selected native chart and its unused embedded workbook/chart parts. Colors are six-digit RGB without `#`; null axis scales restore automatic scaling. `shape.transform` moves/resizes/rotates/flips the chart. Unrelated charts, masters, notes and template parts remain intact.

```json
{"op":"chart.insert","slide":1,"chart_type":"column","categories":["Jan","Feb","Mar"],"series":[{"name":"Revenue","values":[12,15,19]},{"name":"Cost","values":[8,9,11]}],"transform":{"x":72,"y":96,"width":620,"height":310},"format":{"title":"Quarterly performance","legend_position":"bottom","category_axis_title":"Month","value_axis_title":"USD millions","show_data_labels":true,"show_value":true,"series_colors":["336699","CC6600"]}}
```

## Create and edit native Excel charts

Excel uses the same `chart.insert`, `chart.delete`, `chart.data` and `chart.format` names, chart types, inline `categories`/`series` contract, formatting keys and validation as PowerPoint, including all marker/line/smooth scatter variants, `combo_column_line`, and all four stock variants. `secondary_value_axis_title`, `show_secondary_value_axis`, `secondary_value_axis_min`, `secondary_value_axis_max`, `secondary_value_axis_major_unit`, and `show_secondary_major_gridlines` edit the native right-side price axis on volume stock charts. Scatter categories are the finite numeric x values shared by every series. `chart.insert` targets optional `sheet`, accepts a top-left A1 `anchor` (default `A1`), and accepts optional `transform={width,height}` in points. `chart.delete` uses `sheet` + zero-based `chart_index`; deleting the final engine-managed chart removes its now-unused very-hidden data sheet. It creates a real OOXML chart linked to a very-hidden managed worksheet, so Excel and compatible editors can continue editing the series; it is not a screenshot or browser-only metadata chart.

Use `read_file(include_structure=true)` before changing a template. Each worksheet reports `charts` with its stable zero-based `index`, type, anchor, native point dimensions, source references, bounded categories/series and explicit formatting. `chart.data` uses `sheet`, `chart_index`, `categories` and `series`; it replaces the linked data while retaining chart type, placement and supported series styling. `chart.format` uses `sheet`, `chart_index` and `format`; Excel additionally accepts `anchor`, `width` and `height`. Moving a loaded chart preserves its exact native dimensions. A two-cell anchored template chart can move without conversion; resizing it requires both width and height and converts only that anchor to a fixed one-cell extent.

```json
{"op":"chart.insert","sheet":"Dashboard","chart_type":"line_markers","categories":["Jan","Feb","Mar"],"series":[{"name":"Revenue","values":[12,15,19]}],"anchor":"F4","transform":{"width":520,"height":280},"format":{"title":"Revenue trend","legend_position":"bottom","show_data_labels":true,"show_value":true,"series_colors":["174C46"]}}
```

## Generate from an existing template

To create a **new** Word, PowerPoint or Excel file using an existing Knowledge file's layout, read that template with `include_structure=true`, then pass `template: {path, expected_sha256}` alongside a non-empty `operations` list. `path` is its exact entity-relative `fs_path`; `expected_sha256` is `source_sha256` from that read. The output must have the same DOCX/PPTX/XLSX/XLSM extension and a new name; XLSM is supported only through this same-type template path. Use actual template paragraph/shape/sheet selectors, not blank-file assumptions.

```json
{
  "kind": "word_document",
  "name": "Reports/client-proposal.docx",
  "template": {"path": "Templates/service-proposal.docx", "expected_sha256": "64 hex SHA-256 from read_file"},
  "operations": [{"op": "text.set", "index": 0, "text": "Client proposal"}]
}
```

This runs the same patch executor on a read-authorized byte snapshot and commits a separate Knowledge Document. It never changes the template file, its Document identity or its permissions. Approval binds the template path/version and the exact operations. A changed, unreadable or symlinked template fails before creation; any operation/projection failure rolls back the new output. No model call, extra MCP, format conversion or rasterization is involved. Subsequent changes to the new output use `patch_file` and its own returned hash. Existing unsupported OOXML features have the same preservation/editing limits as patching; this is not arbitrary DOTX/POTX/XLTX import or a full Office fidelity guarantee.

## Patch examples

Change just the second paragraph of a discovered PPT text frame (including an empty paragraph):

```json
{"op": "text.set", "slide": 1, "shape_id": 7, "index": 1, "text": "Revised clause\nSecond line"}
```

For a Word body paragraph, omit `slide` and `shape_id` and use its discovered `index`. Never use a table index or assume repeated text uniquely identifies the target.

Change only one Word table cell (including an empty cell):

```json
{
  "path": "Reports/proposal.docx",
  "expected_sha256": "source sha from read_file",
  "operations": [{"op": "cell.set", "table_index": 0, "cell": "B2", "value": "Revised price"}]
}
```

For a PPT table, use `slide` and its discovered `shape_id` instead of `table_index`, with the same `cell` and `value`. These operations work identically in Office operation-based generation and subsequent patching.

Format that same Word cell without changing its text:

```json
{"op": "cell.format", "table_index": 0, "cell": "B2", "format": {"bold": true, "font_size": 18, "font_color": "112233", "fill_color": "F3D9AA"}}
```

Text/Word/PowerPoint:

```json
{
  "path": "Reports/proposal.docx",
  "expected_sha256": "source sha from read_file",
  "operations": [
    {
      "op": "text.replace",
      "old_text": "Old clause",
      "new_text": "New clause",
      "replace_all": false
    }
  ]
}
```

Excel:

```json
{
  "path": "Reports/model.xlsx",
  "expected_sha256": "source sha from read_file",
  "operations": [
    {
      "op": "cell.set",
      "sheet": "Forecast",
      "cell": "B12",
      "value": 120000
    }
  ]
}
```

Add a sheet in the same workbook:

```json
{
  "path": "Reports/model.xlsx",
  "expected_sha256": "source sha from read_file",
  "operations": [
    {
      "op": "sheet.add",
      "sheet": "Actuals"
    }
  ]
}
```

Create or restyle a native Excel table with the same operation in generation, template generation, or patching. Write the header/data cells first, then use an explicit rectangular `range`. Header cells must be non-empty strings and unique ignoring case. `table` is optional for insertion (the engine returns a generated globally unique name); supply it when later operations need a stable selector. `format.style_name` accepts built-in `TableStyleLight1`–`21`, `TableStyleMedium1`–`28`, or `TableStyleDark1`–`11`. The boolean flags are `show_first_column`, `show_last_column`, `show_row_stripes`, and `show_column_stripes`. Use `{"style_name":null}` alone to clear the table style. The operation rejects merged ranges and overlaps with an existing table.

```json
{
  "path": "Reports/model.xlsx",
  "expected_sha256": "source sha from read_file",
  "operations": [
    {"op":"table.insert","sheet":"Forecast","range":"A1:D12","table":"ForecastTable","format":{"style_name":"TableStyleMedium2","show_row_stripes":true}},
    {"op":"table.format","sheet":"Forecast","table":"ForecastTable","format":{"show_last_column":true}}
  ]
}
```

Append to an existing named table, preserving its calculated columns:

```json
{
  "path": "Reports/orders.xlsx",
  "expected_sha256": "source sha from read_file",
  "operations": [
    {
      "op": "row.append",
      "sheet": "Sales",
      "table": "Orders",
      "row": {"Item": "Service", "Qty": 3, "Price": 120}
    }
  ]
}
```
