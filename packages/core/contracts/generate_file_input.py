"""Pure input checks shared by generate_file dispatch and Runtime preflight."""
from __future__ import annotations

import json
from pathlib import PurePath
from typing import Any, Mapping


def coerce_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def generate_file_input_error(arguments: Mapping[str, Any]) -> str | None:
    """Reject provably invalid file inputs before approval or a write intent.

    Other routes retain their authoritative checks; a later handler failure
    never proves that no effect occurred.
    """
    kind = str(arguments.get("kind") or "").strip().lower().replace("-", "_")
    params = coerce_params(arguments.get("params"))
    if "source" in arguments or "source" in params:
        if any(source.get(key) is not None for source in (arguments, params) for key in (
            "content", "prompt", "files", "options", "operations", "template", "expected_sha256",
        )):
            return (
                "source conversion cannot be combined with "
                "content/prompt/files/options/operations/template/expected_sha256. "
                "To edit a PDF, use the PDF specialist with prompt referencing "
                "the current file path, without source, template or operations. "
                "Use source only for unchanged format conversion."
            )
    template = arguments.get("template") or params.get("template")
    if isinstance(template, Mapping):
        template_type = PurePath(str(template.get("path") or "")).suffix.lower().lstrip(".")
        if template_type == "pdf":
            return (
                "A PDF cannot be used as an Office template. Read and verify the "
                "complete PDF, then use the word_document specialist to create "
                "an edited DOCX (or the PDF specialist for PDF output), without "
                "template or operations. Do not reconstruct from a partial excerpt."
            )
    if kind != "document":
        return None
    if arguments.get("files") is not None or params.get("files") is not None:
        return None
    if any(key in source for source in (arguments, params) for key in ("source", "operations", "template")):
        return None
    file_type = str(arguments.get("file_type") or params.get("file_type") or "").strip().lower().lstrip(".")
    name = str(arguments.get("name") or params.get("name") or params.get("output_name") or params.get("filename") or "").strip()
    if file_type in {"diagram", "diagram.json"} or name.lower().endswith((".diagram", ".diagram.json")):
        return None
    title = str(arguments.get("title") or params.get("title") or "").strip()
    if title and not name:
        return (
            "kind=document does not use title as a filename. Use name (including "
            "the extension) and keep the complete content."
        )
    content = arguments.get("content")
    if content is None:
        content = params.get("content")
    if content is None:
        return "kind=document requires complete content. Use prompt for skill/media kinds."
    return None
