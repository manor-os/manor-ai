"""Portable Knowledge template configuration, separate from mutable file bytes."""
from __future__ import annotations

from typing import Any

INITIAL_BODY_KEY = "blueprint_initial_body_md"


def normalize_knowledge_template(value: Any) -> dict[str, Any] | None:
    """Validate and normalize the two explicit starter template contracts."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Knowledge template must be an object")
    template_id = value.get("id")
    mode = value.get("mode")
    version = value.get("version")
    # Older portable live-projection payloads coerce and trim these fields.
    # Installation persists this normalized form; initial-file authorship is
    # deliberately stricter and never interprets another mode as a template.
    if str(mode or "").strip() == "live_projection":
        template_id = str(template_id or "").strip()
        mode = "live_projection"
    if not isinstance(template_id, str) or not template_id.strip():
        raise ValueError("Knowledge template requires a non-empty id")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Knowledge template version must be a positive integer")
    template = {"id": template_id.strip(), "mode": mode, "version": version}
    if mode == "initial_file":
        if "renderer" in value:
            raise ValueError("Initial-file Knowledge templates must omit renderer")
    elif mode == "live_projection":
        renderer = str(value.get("renderer") or "").strip()
        if not renderer:
            raise ValueError("Live Knowledge template requires renderer")
        template["renderer"] = renderer.strip()
    else:
        raise ValueError("Knowledge template mode must be initial_file or live_projection")
    return template


def initial_file_body(metadata: Any) -> str | None:
    """Return only explicitly configured initial content, never the live body."""
    if not isinstance(metadata, dict):
        return None
    try:
        template = normalize_knowledge_template(metadata.get("blueprint_template"))
    except ValueError:
        return None
    if template is None or template["mode"] != "initial_file":
        return None
    body = metadata.get(INITIAL_BODY_KEY)
    return body if isinstance(body, str) and body.strip() else None
