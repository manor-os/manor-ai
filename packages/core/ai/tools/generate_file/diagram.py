from __future__ import annotations

import json
import os
import re
from typing import Any


def _diagram_id_factory():
    counter = 0

    def next_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_{counter:03d}"

    return next_id


def _concise_diagram_title(prompt: str) -> str:
    title = re.sub(r"\s+", " ", prompt or "").strip()
    if not title:
        return "Editable diagram"
    return title if len(title) <= 72 else f"{title[:69]}..."


def _diagram_filename(name: str, prompt: str) -> str:
    raw = (name or "").strip()
    if not raw:
        base = re.sub(
            r"[^A-Za-z0-9._\-\u4e00-\u9fff]+",
            "-",
            _concise_diagram_title(prompt),
        ).strip(".-")
        raw = (base[:48] or "diagram").strip(".-")
    if raw.lower().endswith(".diagram.json"):
        return raw
    if raw.lower().endswith(".diagram"):
        return f"{raw}.json"
    root, ext = os.path.splitext(raw)
    if ext:
        return f"{root}.diagram.json"
    return f"{raw}.diagram.json"


def _diagram_theme() -> dict[str, Any]:
    return {
        "fontFamily": "Inter, ui-sans-serif, system-ui, sans-serif",
        "labelFontFamily": "Times New Roman, serif",
        "palette": {
            "line": "#111827",
            "accent": "#008cad",
            "containerStroke": "#55a9e6",
            "cream": "#f5df9b",
            "orange": "#f3a77f",
            "blueFill": "#bfe1f0",
            "paper": "#ffffff",
            "text": "#111827",
            "muted": "#64748b",
        },
    }


def _diagram_text(
    next_id,
    x: int,
    y: int,
    w: int,
    h: int,
    text: str,
    *,
    prefix: str = "text",
    size: int = 24,
    weight: int = 700,
) -> dict[str, Any]:
    return {
        "id": next_id(prefix),
        "kind": "text",
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "text": text,
        "textStyle": {
            "fontFamily": "Times New Roman, serif",
            "fontSize": size,
            "fontWeight": weight,
            "color": "#111827",
            "align": "center",
        },
    }


def _diagram_shape(
    next_id,
    prefix: str,
    x: int,
    y: int,
    w: int,
    h: int,
    text: str,
    *,
    shape: str = "roundRect",
    fill: str = "transparent",
    stroke: str = "#55a9e6",
    stroke_width: int = 2,
    stroke_dash: str | None = "dash",
    radius: int = 16,
    size: int = 24,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": next_id(prefix),
        "name": text,
        "kind": "shape",
        "shape": shape,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "fill": fill,
        "stroke": stroke,
        "strokeWidth": stroke_width,
        "radius": radius,
        "text": text,
        "textStyle": {
            "fontFamily": "Times New Roman, serif",
            "fontSize": size,
            "fontWeight": 700,
            "color": "#111827",
            "align": "center",
        },
    }
    if stroke_dash:
        item["strokeDash"] = stroke_dash
    return item


def _diagram_connector(
    next_id,
    from_id: str,
    to_id: str,
    *,
    from_anchor: str = "bottom",
    to_anchor: str = "top",
    routing: str = "straight",
    stroke: str = "#008cad",
    stroke_width: int = 5,
) -> dict[str, Any]:
    return {
        "id": next_id("conn"),
        "kind": "connector",
        "from": {"bind": {"elementId": from_id, "anchor": from_anchor}},
        "to": {"bind": {"elementId": to_id, "anchor": to_anchor}},
        "routing": routing,
        "stroke": stroke,
        "strokeWidth": stroke_width,
        "arrowEnd": True,
    }


def _extract_diagram_labels(prompt: str) -> list[str]:
    quoted = [
        m.group(1).strip()
        for m in re.finditer(r'["\u201c]([^"\u201d]{2,40})["\u201d]', prompt or "")
    ]
    if len(quoted) >= 2:
        return quoted[:6]
    layer_mentions = [
        m.group(1).strip()
        for m in re.finditer(r"([A-Za-z][A-Za-z\s-]{2,32} Layer)", prompt or "")
    ]
    if len(layer_mentions) >= 2:
        return layer_mentions[:6]
    parts = [p.strip() for p in re.split(r"->|=>|[,;>\n]+", prompt or "") if p.strip()]
    if 2 <= len(parts) <= 6:
        return parts[:6]
    return [
        "Fuzzification Layer",
        "Spatial Firing Layer",
        "Normalize Layer",
        "Defuzzification Layer",
    ]


def _diagram_document_from_prompt(
    prompt: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_id = _diagram_id_factory()
    labels = _extract_diagram_labels(prompt)
    lower_prompt = (prompt or "").lower()
    scientific = bool(re.search(
        r"paper|fuzzy|kalman|neural|layer|system|architecture|network|diagram|workflow",
        lower_prompt,
    ))
    canvas_width = int((params or {}).get("canvas_width") or 2400)
    canvas_height = int((params or {}).get("canvas_height") or 1600)
    title = _concise_diagram_title(prompt)
    elements: list[dict[str, Any]] = [
        _diagram_text(next_id, 80, 28, 1040, 42, title, prefix="title", size=28),
    ]

    if scientific:
        layer_ids: list[str] = []
        left = 245
        y_start = 105
        gap = 132
        widths = [700, 610, 520, 700, 620, 540]
        for index, label in enumerate(labels):
            layer = _diagram_shape(
                next_id,
                "layer",
                left + index * 20,
                y_start + index * gap,
                widths[index] if index < len(widths) else 620,
                82 if index else 92,
                label,
                fill="transparent",
                stroke="#55a9e6",
                stroke_width=2,
                stroke_dash="dash",
            )
            layer_ids.append(layer["id"])
            elements.append(layer)
        for index in range(len(layer_ids) - 1):
            elements.append(_diagram_connector(next_id, layer_ids[index], layer_ids[index + 1]))
        if "kalman" in lower_prompt or "smoothing" in lower_prompt:
            kalman = _diagram_shape(
                next_id,
                "kalman",
                925,
                255,
                180,
                88,
                "Kalman\nSmoothing",
                fill="#bfe1f0",
                stroke="#111827",
                stroke_dash=None,
                size=26,
            )
            elements.append(kalman)
            elements.append(_diagram_connector(
                next_id,
                kalman["id"],
                layer_ids[-1],
                from_anchor="bottom",
                to_anchor="right",
                routing="elbow",
                stroke="#111827",
                stroke_width=3,
            ))
        groups = [{"id": next_id("group"), "label": "Generated layers", "elementIds": layer_ids}]
        constraints = [{"type": "alignX", "elementIds": layer_ids}]
    else:
        node_labels = labels if len(labels) >= 2 else ["Input", "Analyze", "Transform", "Output"]
        node_ids: list[str] = []
        for index, label in enumerate(node_labels):
            node = _diagram_shape(
                next_id,
                "node",
                120 + index * 245,
                300,
                170,
                82,
                label,
                fill="#e0f2fe" if index % 2 == 0 else "#fef3c7",
                stroke="#0f172a",
                stroke_dash=None,
                size=19,
            )
            node_ids.append(node["id"])
            elements.append(node)
            if index > 0:
                elements.append(_diagram_connector(
                    next_id,
                    node_ids[index - 1],
                    node["id"],
                    from_anchor="right",
                    to_anchor="left",
                ))
        groups = [{"id": next_id("group"), "label": "Generated flow", "elementIds": node_ids}]
        constraints = []

    document: dict[str, Any] = {
        "version": "editable_diagram_v1",
        "id": next_id("diagram"),
        "title": title,
        "prompt": prompt,
        "canvas": {
            "width": canvas_width,
            "height": canvas_height,
            "unit": "px",
            "originX": -120,
            "originY": -90,
        },
        "theme": _diagram_theme(),
        "elements": elements,
        "groups": groups,
    }
    if constraints:
        document["constraints"] = constraints
    return document


async def generate_diagram_file(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    workspace_id: str | None,
    task_id: str | None,
    agent_id: str | None,
    approval_token: str | None,
    expected_sha256: str | None,
) -> str:
    from packages.core.ai.runtime import runtime_generate_document_file

    diagram = _diagram_document_from_prompt(prompt, params)
    return await runtime_generate_document_file(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        name=_diagram_filename(name, prompt),
        content=json.dumps(diagram, ensure_ascii=False, indent=2),
        file_type="json",
        approval_token=approval_token,
        expected_sha256=expected_sha256,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
    )


async def handle_diagram(
    *,
    entity_id: str,
    user_id: str,
    conversation_id: str,
    prompt: str,
    name: str,
    params: dict[str, Any],
    kwargs: dict[str, Any],
    agent_id: str | None,
) -> str:
    if not prompt:
        prompt = str(kwargs.get("content") or params.get("content") or "").strip()
    if not prompt:
        return json.dumps({"error": "kind=diagram requires prompt"}, ensure_ascii=False)
    return await generate_diagram_file(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        prompt=prompt,
        name=name,
        params=params,
        workspace_id=kwargs.get("workspace_id"),
        task_id=kwargs.get("task_id"),
        agent_id=agent_id,
        approval_token=kwargs.get("approval_token") or params.get("approval_token"),
        expected_sha256=kwargs.get("expected_sha256") or params.get("expected_sha256"),
    )
