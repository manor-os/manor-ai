# PPT Master Toolset

This directory contains the scripts used by the built-in PPTX generation path: project setup, SVG processing, export, and system-image prompt/resource handling.

## Directory Layout

- Top-level `scripts/`: runnable entry scripts
- `scripts/template_import/`: internal PPTX reference-preparation helpers used by `pptx_template_import.py`
- `scripts/svg_finalize/`: internal post-processing helpers used by `finalize_svg.py`
- `scripts/docs/`: topic-focused script documentation

## Quick Start

Typical end-to-end workflow:

```bash
python3 scripts/project_manager.py init <project_name> --format ppt169
python3 scripts/total_md_split.py <project_path>
python3 scripts/finalize_svg.py <project_path>
python3 scripts/svg_to_pptx.py <project_path>
```

Repository update:

```bash
python3 scripts/update_repo.py
```

## Script Index

| Area | Primary scripts | Documentation |
|------|-----------------|---------------|
| Project management | `project_manager.py`, `batch_validate.py`, `generate_examples_index.py`, `error_helper.py`, `pptx_template_import.py` | [docs/project.md](./docs/project.md) |
| SVG pipeline | `finalize_svg.py`, `svg_to_pptx.py`, `total_md_split.py`, `svg_quality_checker.py`, `animation_config.py` | [docs/svg-pipeline.md](./docs/svg-pipeline.md) |
| Spec maintenance | `update_spec.py` | [docs/update_spec.md](./docs/update_spec.md) |
| Image tools | `image_prompts.py`, `import_system_image.py`, `analyze_images.py` | [docs/image.md](./docs/image.md) |
| Repo maintenance | `update_repo.py` | README install/update section |
| Troubleshooting | validation, preview, export, dependency issues | [docs/troubleshooting.md](./docs/troubleshooting.md) |

## High-Frequency Commands

Project setup:

```bash
python3 scripts/project_manager.py init <project_name> --format ppt169
python3 scripts/project_manager.py validate <project_path>
```

Template source import:

```bash
python3 scripts/pptx_template_import.py <template.pptx>
python3 scripts/pptx_template_import.py <template.pptx> --manifest-only
python3 scripts/pptx_template_import.py <template.pptx> --inheritance-mode both
```

Post-processing and export:

```bash
python3 scripts/total_md_split.py <project_path>
python3 scripts/finalize_svg.py <project_path>
python3 scripts/svg_to_pptx.py <project_path>
```

Image prompts and assets:

```bash
python3 scripts/image_prompts.py --validate projects/demo/images/image_prompts.json
python3 scripts/image_prompts.py --render-md projects/demo/images/image_prompts.json
python3 scripts/analyze_images.py <project_path>/images
```

Repository update:

```bash
python3 scripts/update_repo.py
python3 scripts/update_repo.py --skip-pip
```

## Recommendations

- Keep one user-facing entry point per workflow at the top level of `scripts/`
- Move provider-specific or helper internals into subdirectories
- Prefer the unified entry points `project_manager.py`, `finalize_svg.py`, `svg_to_pptx.py`, and `image_prompts.py`
- Prefer `svg_final/` over `svg_output/` when exporting

## Related Docs

- [Project Tools](./docs/project.md)
- [SVG Pipeline Tools](./docs/svg-pipeline.md)
- [Image Tools](./docs/image.md)
- [Troubleshooting](./docs/troubleshooting.md)
- [Skill Entry](../SKILL.md)

_Last updated: 2026-04-09_
