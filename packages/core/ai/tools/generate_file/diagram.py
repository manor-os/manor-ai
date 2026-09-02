from __future__ import annotations

import html
import json
import os
import re
from typing import Any


MAX_DIAGRAM_ELEMENTS = 10_000
MAX_DIAGRAM_CANVAS_DIMENSION = 100_000
MIN_DIAGRAM_CANVAS_WIDTH = 320
MIN_DIAGRAM_CANVAS_HEIGHT = 180


def _diagram_canvas_dimension(
    params: dict[str, Any] | None,
    key: str,
    minimum: int,
) -> int | None:
    raw_value = (params or {}).get(key)
    if raw_value is None:
        return None

    if isinstance(raw_value, bool):
        raise ValueError(f"{key} must be an integer")
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, float) and raw_value.is_integer():
        value = int(raw_value)
    elif isinstance(raw_value, str) and re.fullmatch(r"[+-]?\d+", raw_value.strip()):
        value = int(raw_value.strip())
    else:
        raise ValueError(f"{key} must be an integer")

    if value < minimum or value > MAX_DIAGRAM_CANVAS_DIMENSION:
        raise ValueError(
            f"{key} must be between {minimum} and "
            f"{MAX_DIAGRAM_CANVAS_DIMENSION}"
        )
    return value


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


def _looks_like_mermaid_source(prompt: str) -> bool:
    cleaned = "\n".join(
        line.split("%%", 1)[0]
        for line in (prompt or "").splitlines()
    )
    return bool(re.match(r"^\s*(?:flowchart|graph)\b", cleaned, re.IGNORECASE))


def _clean_mermaid_label(value: str) -> str:
    label = html.unescape(value or "")
    label = re.sub(r"<br\s*/?>", " ", label, flags=re.IGNORECASE)
    label = label.strip()
    if len(label) >= 2 and label[0] == label[-1] and label[0] in '"\'`':
        label = label[1:-1]
    return re.sub(r"\s+", " ", label).strip()


def _split_mermaid_statements(value: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    depth = 0

    def push_current() -> None:
        statement = "".join(current).strip()
        if statement:
            statements.append(statement)
        current.clear()

    for character in value:
        if quote:
            current.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        previous = current[-1] if current else ""
        if character in '"\'`' and (character != "'" or not previous.isalnum()):
            quote = character
            current.append(character)
            continue
        if character in "[{(":
            depth += 1
        elif character in "]})":
            depth = max(0, depth - 1)
        if depth == 0 and character in ";\n\r":
            push_current()
            continue
        current.append(character)
    push_current()
    return statements


def _parse_mermaid_flow(prompt: str) -> dict[str, Any] | None:
    cleaned_source = "\n".join(
        line.split("%%", 1)[0]
        for line in (prompt or "").splitlines()
    )
    header = re.match(
        r"^\s*(?:flowchart|graph)\b(?:\s+(TD|TB|BT|LR|RL))?",
        cleaned_source,
        re.IGNORECASE,
    )
    if not header:
        return None
    raw_direction = (header.group(1) or "TD").upper()
    direction = "TD" if raw_direction == "TB" else raw_direction

    nodes: dict[str, dict[str, str]] = {}
    edges: list[dict[str, Any]] = []
    edge_keys: set[tuple[Any, ...]] = set()

    def ensure_element_budget(additional_edges: int = 0) -> None:
        # The generated document adds one title element to the parsed graph.
        if len(nodes) + len(edges) + additional_edges + 1 > MAX_DIAGRAM_ELEMENTS:
            raise ValueError(
                f"Diagram has too many elements; maximum is {MAX_DIAGRAM_ELEMENTS}"
            )

    def remember_node(
        node_id: str,
        label: str | None = None,
        shape: str = "rect",
    ) -> None:
        clean_id = (node_id or "").strip()
        if not clean_id:
            return
        clean_label = _clean_mermaid_label(label or clean_id) or clean_id
        if clean_id not in nodes:
            nodes[clean_id] = {"label": clean_label, "shape": shape}
            ensure_element_budget()
        elif label:
            nodes[clean_id] = {"label": clean_label, "shape": shape}

    node_patterns = [
        (
            re.compile(
                r"\b([A-Za-z][A-Za-z0-9_-]*)\s*\[\s*\[\s*(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^\]\n]+?))\s*\]\s*\]"
            ),
            "rect",
        ),
        (
            re.compile(
                r"\b([A-Za-z][A-Za-z0-9_-]*)\s*\[\s*(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^\]\n]+?))\s*\]"
            ),
            "rect",
        ),
        (
            re.compile(
                r"\b([A-Za-z][A-Za-z0-9_-]*)\s*\{\s*(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^}\n]+))\s*\}"
            ),
            "diamond",
        ),
        (
            re.compile(
                r"\b([A-Za-z][A-Za-z0-9_-]*)\s*\(\s*\(\s*(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^)\n]+?))\s*\)\s*\)"
            ),
            "ellipse",
        ),
        (
            re.compile(
                r"\b([A-Za-z][A-Za-z0-9_-]*)\s*\(\s*(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^)\n]+?))\s*\)"
            ),
            "roundRect",
        ),
    ]
    arrow_pattern = re.compile(
        r"--\s+(.+?)\s+-->|-\.\s+(.+?)\s+\.->|==\s+(.+?)\s+==>|"
        r"<-->|<==>|<-\.->|o--o|x--x|o-->|x-->|<--o|<--x|--o|--x|<--|"
        r"-->|---|==>|-\.->"
    )
    identifier_pattern = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]*\b")

    for line in _split_mermaid_statements(cleaned_source[header.end():]):
        subgraph = re.match(
            r"^subgraph\s+([A-Za-z][A-Za-z0-9_-]*)(?:\s*\[\s*(.+?)\s*\])?\s*$",
            line,
            re.IGNORECASE,
        )
        if subgraph:
            remember_node(subgraph.group(1), subgraph.group(2))
            continue
        if re.match(r"^end\b", line, re.IGNORECASE):
            continue
        if re.match(
            r"^(?:style|classDef|class|linkStyle|direction)\b",
            line,
            re.IGNORECASE,
        ):
            continue

        simplified = line
        for node_pattern, node_shape in node_patterns:
            def replace_node(match: re.Match[str], shape: str = node_shape) -> str:
                label = next(
                    (value for value in match.groups()[1:] if value is not None),
                    None,
                )
                remember_node(match.group(1), label, shape)
                return match.group(1)

            simplified = node_pattern.sub(replace_node, simplified)
        arrows = list(arrow_pattern.finditer(simplified))
        if not arrows:
            standalone_nodes = simplified.strip()
            if re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]*(?:\s*&\s*[A-Za-z][A-Za-z0-9_-]*)*",
                standalone_nodes,
            ):
                for node_id in re.split(r"\s*&\s*", standalone_nodes):
                    remember_node(node_id)
            continue
        def endpoint_ids(value: str) -> list[str]:
            return [
                identifiers[0]
                for part in value.split("&")
                if (identifiers := identifier_pattern.findall(part))
            ]

        from_ids = endpoint_ids(simplified[: arrows[0].start()])
        for index, arrow in enumerate(arrows):
            end = arrows[index + 1].start() if index + 1 < len(arrows) else len(simplified)
            segment = simplified[arrow.end() : end]
            label_match = re.match(r'^\s*\|\s*"?([^|"]+)"?\s*\|', segment)
            endpoint_text = segment[label_match.end() :] if label_match else segment
            to_ids = endpoint_ids(endpoint_text)
            inline_label = next(
                (value for value in arrow.groups() if value is not None),
                None,
            )
            arrow_token = re.sub(r"\s+", "", arrow.group(0))

            def endpoint_marker(character: str) -> str | None:
                if character in "<>":
                    return "arrow"
                if character == "o":
                    return "circle"
                if character == "x":
                    return "cross"
                return None

            marker_start = endpoint_marker(arrow_token[0])
            marker_end = endpoint_marker(arrow_token[-1])
            for from_id in from_ids:
                for to_id in to_ids:
                    remember_node(from_id)
                    remember_node(to_id)
                    edge = {
                        "from": from_id,
                        "to": to_id,
                        # v1 clients only know these booleans. Preserve a
                        # visible endpoint while current clients use markers.
                        "arrow_start": marker_start is not None,
                        "arrow_end": marker_end is not None,
                        "marker_start": marker_start,
                        "marker_end": marker_end,
                    }
                    if label_match:
                        edge["label"] = _clean_mermaid_label(label_match.group(1))
                    elif inline_label:
                        edge["label"] = _clean_mermaid_label(inline_label)
                    edge_key = (
                        edge["from"],
                        edge["to"],
                        edge.get("label"),
                        edge.get("marker_start"),
                        edge.get("marker_end"),
                    )
                    if edge_key not in edge_keys:
                        ensure_element_budget(additional_edges=1)
                        edge_keys.add(edge_key)
                        edges.append(edge)
            from_ids = to_ids

    rendered_nodes = [
        {"id": node_id, **node}
        for node_id, node in nodes.items()
    ]
    return {
        "direction": direction,
        "nodes": rendered_nodes,
        "edges": edges,
    } if rendered_nodes else None


def _diagram_title(prompt: str, name: str) -> str:
    if not _looks_like_mermaid_source(prompt):
        return _concise_diagram_title(prompt)
    filename = os.path.basename((name or "").strip())
    stem = re.sub(r"(?:\.diagram\.json|\.diagram)$", "", filename, flags=re.IGNORECASE)
    readable = re.sub(r"[-_]+", " ", stem).strip()
    return _concise_diagram_title(readable or "Editable flowchart")


def _diagram_label_width(value: str) -> int:
    return sum(2 if ord(character) > 0xFF else 1 for character in value)


def _wrap_diagram_label(label: str, limit: int = 24) -> str:
    lines: list[str] = []
    line = ""
    for word in label.strip().split():
        candidate = f"{line} {word}".strip()
        if _diagram_label_width(candidate) <= limit:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        for character in word:
            if line and _diagram_label_width(f"{line}{character}") > limit:
                lines.append(line)
                line = character
            else:
                line += character
    if line:
        lines.append(line)
    return "\n".join(lines)


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
    label: str | None = None,
    arrow_start: bool = False,
    arrow_end: bool = True,
    marker_start: str | None = None,
    marker_end: str | None = None,
) -> dict[str, Any]:
    connector: dict[str, Any] = {
        "id": next_id("conn"),
        "kind": "connector",
        "from": {"bind": {"elementId": from_id, "anchor": from_anchor}},
        "to": {"bind": {"elementId": to_id, "anchor": to_anchor}},
        "routing": routing,
        "stroke": stroke,
        "strokeWidth": stroke_width,
        "arrowStart": arrow_start,
        "arrowEnd": arrow_end,
    }
    if marker_start:
        connector["markerStart"] = marker_start
    if marker_end:
        connector["markerEnd"] = marker_end
    if label:
        connector["label"] = label
    return connector


def _extract_diagram_labels(prompt: str) -> list[str]:
    mermaid = _parse_mermaid_flow(prompt)
    if mermaid and len(mermaid["nodes"]) >= 2:
        return [node["label"] for node in mermaid["nodes"][:6]]
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
    name: str = "",
) -> dict[str, Any]:
    next_id = _diagram_id_factory()
    mermaid = _parse_mermaid_flow(prompt)
    if (
        mermaid
        and len(mermaid["nodes"]) + len(mermaid["edges"]) + 1
        > MAX_DIAGRAM_ELEMENTS
    ):
        raise ValueError(
            f"Diagram has too many elements; maximum is {MAX_DIAGRAM_ELEMENTS}"
        )
    labels = _extract_diagram_labels(prompt)
    lower_prompt = (prompt or "").lower()
    scientific = bool(re.search(
        r"paper|fuzzy|kalman|neural|layer|system|architecture|network|diagram|workflow",
        lower_prompt,
    ))
    requested_canvas_width = _diagram_canvas_dimension(
        params,
        "canvas_width",
        MIN_DIAGRAM_CANVAS_WIDTH,
    )
    requested_canvas_height = _diagram_canvas_dimension(
        params,
        "canvas_height",
        MIN_DIAGRAM_CANVAS_HEIGHT,
    )
    canvas_width = requested_canvas_width or 2400
    canvas_height = requested_canvas_height or 1600
    title = _diagram_title(prompt, name)
    elements: list[dict[str, Any]] = [
        _diagram_text(next_id, 80, 28, 1040, 42, title, prefix="title", size=28),
    ]

    if mermaid:
        graph_nodes = mermaid["nodes"]
        graph_edges = mermaid["edges"]
        graph_direction = mermaid["direction"]
        layout_traversal: dict[str, list[str]] = {}
        back_edge_indexes: set[int] = set()

        def has_layout_path(start: str, target: str) -> bool:
            pending = [start]
            seen: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current in seen:
                    continue
                seen.add(current)
                pending.extend(
                    next_node
                    for next_node in layout_traversal.get(current, [])
                    if next_node not in seen
                )
            return False

        for index, edge in enumerate(graph_edges):
            if has_layout_path(edge["to"], edge["from"]):
                back_edge_indexes.add(index)
            else:
                layout_traversal.setdefault(edge["from"], []).append(edge["to"])

        outgoing: dict[str, list[str]] = {node["id"]: [] for node in graph_nodes}
        indegree = {node["id"]: 0 for node in graph_nodes}
        for index, edge in enumerate(graph_edges):
            if index in back_edge_indexes:
                continue
            outgoing.setdefault(edge["from"], []).append(edge["to"])
            indegree[edge["to"]] = indegree.get(edge["to"], 0) + 1
        remaining_indegree = dict(indegree)
        queue = [
            node["id"]
            for node in graph_nodes
            if not remaining_indegree.get(node["id"])
        ]
        levels = {node_id: 0 for node_id in queue}
        cursor = 0
        while cursor < len(queue):
            current = queue[cursor]
            cursor += 1
            for target in outgoing.get(current, []):
                levels[target] = max(
                    levels.get(target, 0),
                    levels[current] + 1,
                )
                remaining_indegree[target] = remaining_indegree.get(target, 0) - 1
                if remaining_indegree[target] == 0:
                    queue.append(target)
        for node in graph_nodes:
            levels.setdefault(node["id"], 0)

        rows: dict[int, list[dict[str, str]]] = {}
        for node in graph_nodes:
            rows.setdefault(levels[node["id"]], []).append(node)
        node_width = 260
        node_height = 112
        cross_gap = 56
        level_gap = 112
        stage_start_x = 80
        stage_start_y = 150
        widest_row = max((len(row) for row in rows.values()), default=1)
        max_level = max(levels.values(), default=0)
        is_vertical = graph_direction in {"TD", "BT"}
        is_reverse = graph_direction in {"BT", "RL"}
        max_cross_size = (
            widest_row * node_width + (widest_row - 1) * cross_gap
            if is_vertical
            else widest_row * node_height + (widest_row - 1) * cross_gap
        )
        canvas_width = max(
            requested_canvas_width or 0,
            800 if is_vertical else 160 + (max_level + 1) * node_width + max_level * level_gap,
            160 + max_cross_size if is_vertical else 800,
        )
        canvas_height = max(
            requested_canvas_height or 0,
            stage_start_y + (max_level + 1) * node_height + max_level * level_gap + 80
            if is_vertical
            else stage_start_y + max_cross_size + 80,
            600,
        )
        title_width = min(1040, canvas_width - 160)
        elements[0]["x"] = (canvas_width - title_width) // 2
        elements[0]["w"] = title_width
        element_ids: dict[str, str] = {}
        for level, row in sorted(rows.items()):
            cross_size = (
                len(row) * node_width + (len(row) - 1) * cross_gap
                if is_vertical
                else len(row) * node_height + (len(row) - 1) * cross_gap
            )
            visual_level = max_level - level if is_reverse else level
            for index, node in enumerate(row):
                x = (
                    (canvas_width - cross_size) // 2 + index * (node_width + cross_gap)
                    if is_vertical
                    else stage_start_x + visual_level * (node_width + level_gap)
                )
                y = (
                    stage_start_y + visual_level * (node_height + level_gap)
                    if is_vertical
                    else stage_start_y + (max_cross_size - cross_size) // 2 + index * (node_height + cross_gap)
                )
                item = _diagram_shape(
                    next_id,
                    "node",
                    x,
                    y,
                    node_width,
                    node_height,
                    _wrap_diagram_label(node["label"]),
                    shape=node.get("shape", "rect"),
                    fill="#e0f2fe" if level % 2 == 0 else "#fef3c7",
                    stroke="#0f172a",
                    stroke_dash=None,
                    size=18,
                )
                element_ids[node["id"]] = item["id"]
                elements.append(item)
        for edge in graph_edges:
            from_id = element_ids.get(edge["from"])
            to_id = element_ids.get(edge["to"])
            if not from_id or not to_id:
                continue
            anchors = {
                "TD": ("bottom", "top"),
                "BT": ("top", "bottom"),
                "LR": ("right", "left"),
                "RL": ("left", "right"),
            }[graph_direction]
            elements.append(_diagram_connector(
                next_id,
                from_id,
                to_id,
                from_anchor=anchors[0],
                to_anchor=anchors[1],
                routing="straight" if levels[edge["to"]] > levels[edge["from"]] else "curve",
                label=(
                    _wrap_diagram_label(edge["label"], 22)
                    if edge.get("label")
                    else None
                ),
                arrow_start=bool(edge.get("arrow_start", False)),
                arrow_end=bool(edge.get("arrow_end", True)),
                marker_start=edge.get("marker_start"),
                marker_end=edge.get("marker_end"),
            ))
        groups = [{
            "id": next_id("group"),
            "label": "Mermaid flow",
            "elementIds": list(element_ids.values()),
        }]
        constraints = []
    elif scientific:
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

    if (
        canvas_width > MAX_DIAGRAM_CANVAS_DIMENSION
        or canvas_height > MAX_DIAGRAM_CANVAS_DIMENSION
    ):
        raise ValueError(
            "Diagram canvas is too large; maximum dimension is "
            f"{MAX_DIAGRAM_CANVAS_DIMENSION}"
        )

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
    runtime_envelope: Any | None = None,
    storage_scope: str = "task",
) -> str:
    from packages.core.ai.runtime import runtime_generate_document_file

    diagram = _diagram_document_from_prompt(prompt, params, name)
    return await runtime_generate_document_file(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        name=_diagram_filename(name, prompt),
        content=json.dumps(diagram, ensure_ascii=False, indent=2),
        file_type="diagram.json",
        approval_token=approval_token,
        expected_sha256=expected_sha256,
        workspace_id=workspace_id,
        task_id=task_id,
        agent_id=agent_id,
        runtime_envelope=runtime_envelope,
        storage_scope=storage_scope,
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
    from packages.core.ai.runtime.tool_context import runtime_tool_call_context_from_kwargs

    runtime_context = runtime_tool_call_context_from_kwargs(kwargs)
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
        runtime_envelope=runtime_context.runtime_envelope,
    )
