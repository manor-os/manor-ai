"""Importer base class + shared node-mapping helpers."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .model import CANONICAL_NODE_TYPES, GraphNode, ImportReport, WorkflowGraph


class WorkflowImporter(ABC):
    """One adapter per external format (ComfyUI / n8n / Dify)."""

    source_tool: str = ""

    @classmethod
    @abstractmethod
    def detect(cls, raw: dict) -> bool:
        """Cheap structural check: does this payload look like our format?"""

    @abstractmethod
    def parse(self, raw: dict, name: str | None = None) -> tuple[WorkflowGraph, ImportReport]:
        """Parse a payload into a canonical graph + import report."""

    # -- shared helpers ----------------------------------------------------

    def _map_node_type(self, original_type: str, mapping: dict[str, str]) -> str | None:
        """Look up a canonical type; None means unmapped."""
        return mapping.get(original_type)

    def _placeholder(
        self, report: ImportReport, node_id: str, name: str,
        original_type: str, raw: dict, reason: str,
    ) -> GraphNode:
        """Build an ``unsupported`` node that preserves the original payload."""
        report.add_unmapped(node_id, original_type, reason)
        return GraphNode(
            id=node_id,
            type="unsupported",
            name=name or original_type,
            config={},
            meta={
                "source_tool": self.source_tool,
                "original_type": original_type,
                "original_raw": raw,
                "unmapped": True,
            },
        )

    def _finalize(self, report: ImportReport, nodes: list[GraphNode]) -> None:
        report.node_count = len(nodes)
        report.mapped = sum(
            1 for n in nodes if not n.unmapped and n.type in CANONICAL_NODE_TYPES
        )
