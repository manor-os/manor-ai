---
name: file_engine
description: Create new Knowledge files and patch existing Knowledge files through one file-engine workflow.
---

# File Engine

Use this skill when the user asks to create, update, or AI-edit a Knowledge file and no more specific file-type skill has already taken over.

## Workflow

1. Use `inspect_file_engine` or `mcp__manor_mcp_file_engine__inspect` when you are unsure whether a file type supports patching.
2. Use `search_documents` or `list_documents` to resolve the user-visible Knowledge file.
3. Use `read_file` with the resolved `fs_path` before editing. Keep its `source_sha256`.
4. Use `generate_file` or `mcp__manor_mcp_file_engine__generate` only when creating a new file.
5. Use `patch_file` or `mcp__manor_mcp_file_engine__patch` when editing an existing file. Pass `expected_sha256` from `read_file`.

Do not regenerate an existing Office file just to make a small edit. Patch the existing file so the original package structure, editable source, and Knowledge document identity are preserved.

## Current patch coverage

- Plain text-like files (`txt`, `md`, `json`, `html`, `csv`, `tsv`, `xml`, source files): `replace_text`.
- Word (`docx`): `replace_text` across paragraphs, tables, headers, and footers.
- PowerPoint (`pptx`): `replace_text` across slide text and table text.
- Excel (`xlsx`, `xlsm`): `set_cell`, `update_row`, `append_row`, and `add_sheet`.

Legacy Office formats (`doc`, `xls`, `ppt`, `wps`, `et`, `dps`) must be converted to OOXML before patching. PDF needs a PDF-specific editing workflow; do not treat it as a generic text-patch target.

## Patch examples

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
