"""Import workflows exported from ComfyUI / n8n / Dify into manor's canonical
workflow graph.

Usage::

    from packages.core.ai.workflow_import import import_workflow

    result = import_workflow(raw_bytes_or_str_or_dict, name="My flow")
    result.report.source_tool      # "dify" | "n8n" | "comfyui"
    result.report.coverage         # fraction of nodes that mapped
    result.definition              # WorkflowDefinition-shaped dict (name+steps)

The importer auto-detects the format, maps known node types to manor node
types, and keeps unmapped nodes as ``unsupported`` placeholders (recorded in
``result.report``) so an import never fails wholesale.

See docs/design/workflow-engine.md.
"""
from __future__ import annotations

import json

from .comfyui import ComfyUIImporter
from .dify import DifyImporter
from .model import (
    GraphNode,
    ImportReport,
    ImportResult,
    WorkflowGraph,
)
from .n8n import N8nImporter

__all__ = [
    "import_workflow",
    "detect_format",
    "ImportResult",
    "ImportReport",
    "WorkflowGraph",
    "GraphNode",
    "UnknownWorkflowFormat",
]

# Order matters: most specific signatures first.
_IMPORTERS = [DifyImporter, N8nImporter, ComfyUIImporter]


class UnknownWorkflowFormat(ValueError):
    """Raised when a payload matches no known export format."""


def _ensure_explicit_entry(graph: WorkflowGraph, report: ImportReport) -> None:
    """Add a visible trigger when the source format has no entry node.

    ComfyUI graphs and some scrubbed n8n exports describe data dependencies but
    do not contain a trigger.  Manor no longer guesses an execution entry, so
    imports make that conversion explicit on the canvas instead of relying on
    hidden runner behaviour.
    """
    if any(node.type in {"trigger", "webhook"} for node in graph.nodes):
        return

    existing_ids = {node.id for node in graph.nodes}
    trigger_id = "manor_trigger"
    suffix = 2
    while trigger_id in existing_ids:
        trigger_id = f"manor_trigger_{suffix}"
        suffix += 1

    targets: set[str] = set()
    for node in graph.nodes:
        targets.update(node.next or [])
        targets.update(node.true_next or [])
        targets.update(node.false_next or [])
        if node.type == "switch":
            targets.update(node.config.get("default_next") or [])
            for case in node.config.get("cases") or []:
                if isinstance(case, dict):
                    targets.update(case.get("next") or [])

    roots = [node.id for node in graph.nodes if node.id not in targets]
    if not roots and graph.nodes:
        roots = [graph.nodes[0].id]
    graph.nodes.insert(
        0,
        GraphNode(
            id=trigger_id,
            type="trigger",
            name="Start",
            config={},
            next=roots,
            meta={"source_tool": graph.source_tool, "synthesized": True},
        ),
    )
    report.warnings.append(
        "The source had no trigger, so Manor added an explicit Start node."
    )


def _coerce(raw) -> dict:
    """Accept dict / JSON / YAML and return a parsed dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise UnknownWorkflowFormat(f"Unsupported input type: {type(raw)!r}")

    text = raw.strip()
    # JSON first (n8n / ComfyUI)
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        pass
    # YAML (Dify DSL)
    try:
        import yaml  # lazy: only needed for Dify
    except ImportError as exc:  # pragma: no cover
        raise UnknownWorkflowFormat(
            "Payload is not JSON and PyYAML is not installed to parse Dify DSL."
        ) from exc
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise UnknownWorkflowFormat(f"Could not parse payload as JSON or YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise UnknownWorkflowFormat("Parsed payload is not a mapping/object.")
    return parsed


def detect_format(raw) -> str | None:
    """Return the source tool name, or None if unrecognised."""
    try:
        data = _coerce(raw)
    except UnknownWorkflowFormat:
        return None
    for importer in _IMPORTERS:
        if importer.detect(data):
            return importer.source_tool
    return None


def import_workflow(raw, name: str | None = None) -> ImportResult:
    """Parse an exported workflow into a canonical graph + import report.

    Raises :class:`UnknownWorkflowFormat` if the format can't be identified.
    """
    data = _coerce(raw)
    for importer_cls in _IMPORTERS:
        if importer_cls.detect(data):
            graph, report = importer_cls().parse(data, name=name)
            _ensure_explicit_entry(graph, report)
            return ImportResult(graph=graph, report=report)
    raise UnknownWorkflowFormat(
        "Payload does not match ComfyUI, n8n, or Dify export formats."
    )
