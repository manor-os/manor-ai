# Follow an authorized reference PPTX

Use this route only when the user explicitly provides an accessible reference
deck. Check that its bytes are already available in the current sandbox's
authorized project files; a filename in a prompt is not proof of access.
Never read another group, personal Knowledge, or Workspace path merely because
the user mentions it.

Run `scripts/pptx_template_import.py <reference.pptx> --output
<project>/reference --inheritance-mode both` as specified by `workflows/free-design.md`.
Read the resulting master, layout and slide hierarchy, palette, typography
and recurring elements,
then record the relevant traits in `design_spec.md` and `spec_lock.md`.
Generate new content through the normal layout, quality, export and render
pipeline. Do not copy private content or present the original deck as a new
artifact. If the reference is unavailable in this sandbox, ask for an
authorized upload rather than fabricating its appearance.
