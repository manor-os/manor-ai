# Troubleshooting

## Validation Failed

1. Run:

```bash
python3 scripts/project_manager.py validate <project_path>
```

2. Fix missing files or invalid directories reported by the validator.
3. Re-run validation before post-processing or export.

## SVG Preview Looks Wrong

1. Check the file path and filename.
2. Confirm naming conventions are consistent.
3. Preview via a local server if browser file loading is inconsistent:

```bash
python3 -m http.server --directory <svg_output_path> 8000
```

## Speaker Notes Do Not Split

Check `total.md`:
- headings must start with `# `
- heading text must match SVG filenames
- sections must be separated by `---`

Then rerun:

```bash
python3 scripts/total_md_split.py <project_path>
```

## PPT Export Quality Issues

Preferred sequence:

```bash
python3 scripts/total_md_split.py <project_path>
python3 scripts/finalize_svg.py <project_path>
python3 scripts/svg_to_pptx.py <project_path>
```

Do not export directly from `svg_output/` when `svg_final/` exists.

## Dependency Checklist

The sandbox auto-installs the minimal `requirements.txt` needed for the main PPT generation/export path:

```bash
pip install -r requirements.txt
```

The built-in skill intentionally does not auto-install PDF/DOC/Excel converters, TTS packages, WeChat/TLS tooling, or local image provider SDKs. Those workflows are outside the current PPTX generation path.
