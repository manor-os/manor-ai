"""Canonical workflow graph — the import target for ComfyUI / n8n / Dify.

Every external workflow format is parsed into a ``WorkflowGraph`` of canonical
``GraphNode`` objects, then serialised to the manor ``WorkflowDefinition.steps``
shape via :meth:`WorkflowGraph.to_steps`. Nodes that have no manor equivalent
are kept as ``unsupported`` placeholders (original payload preserved in
``meta``) and recorded in the :class:`ImportReport`, so an import never fails
wholesale — it degrades gracefully and tells you exactly what didn't map.

Canonical node types (the seed of the future NodeSpec vocabulary):

    trigger | llm | rag | agent | tool | connector | code | http
    condition | switch | loop | parallel | merge | transform | stage
    classifier | wait | notify | end | unsupported

See docs/design/workflow-engine.md (§5–6).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Canonical node types that downstream (builder/runner) understand or will grow into.
CANONICAL_NODE_TYPES = frozenset({
    "trigger", "llm", "rag", "agent", "tool", "connector", "code", "http",
    "condition", "switch", "loop", "parallel", "merge", "transform",
    "classifier", "wait", "notify", "end", "unsupported",
    # restartable inline operation graph presented as one business-level node
    "stage",
    # media generation (image / video / audio) — backed by generate_file
    "media", "image", "video", "audio",
    # sub-workflow: run one child, or durably map child runs over typed items
    "subworkflow", "foreach_subworkflow",
    # durable cross-run project state, scoped grants, and browser effects
    "workflow_project", "workflow_action_grant", "browser_effect",
    # data-plumbing building blocks (n8n parity)
    "extract", "filter", "aggregate", "datetime",
    # list ops + sync webhook response (n8n parity)
    "split", "limit", "respond",
    # more list ops + control + file parse (n8n parity)
    "sort", "dedupe", "stop", "extractfromfile",
})


@dataclass
class GraphNode:
    """One node in the canonical graph."""
    id: str
    type: str
    name: str
    config: dict = field(default_factory=dict)
    next: list[str] = field(default_factory=list)
    true_next: list[str] | None = None
    false_next: list[str] | None = None
    meta: dict = field(default_factory=dict)  # {source_tool, original_type, original_raw, unmapped}

    @property
    def unmapped(self) -> bool:
        return bool(self.meta.get("unmapped")) or self.type == "unsupported"

    def to_step(self) -> dict:
        """Serialise to the WorkflowDefinition.steps shape."""
        step: dict = {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "config": self.config,
            "next": self.next,
        }
        if self.true_next is not None:
            step["true_next"] = self.true_next
        if self.false_next is not None:
            step["false_next"] = self.false_next
        if self.meta:
            step["meta"] = self.meta
        return step


@dataclass
class WorkflowGraph:
    """A parsed, format-agnostic workflow."""
    name: str
    source_tool: str  # "comfyui" | "n8n" | "dify"
    nodes: list[GraphNode] = field(default_factory=list)
    variables: dict = field(default_factory=dict)

    def to_steps(self) -> list[dict]:
        return [n.to_step() for n in self.nodes]


@dataclass
class ImportReport:
    """What mapped, what didn't, and why — surfaced to the user after import."""
    source_tool: str
    node_count: int = 0
    mapped: int = 0
    unmapped: list[dict] = field(default_factory=list)  # [{id, original_type, reason}]
    warnings: list[str] = field(default_factory=list)

    def add_unmapped(self, node_id: str, original_type: str, reason: str) -> None:
        self.unmapped.append({"id": node_id, "original_type": original_type, "reason": reason})

    @property
    def unmapped_count(self) -> int:
        return len(self.unmapped)

    @property
    def coverage(self) -> float:
        """Fraction of nodes that mapped to a real manor node type (0.0–1.0)."""
        if self.node_count == 0:
            return 1.0
        return round(self.mapped / self.node_count, 3)

    def to_dict(self) -> dict:
        return {
            "source_tool": self.source_tool,
            "node_count": self.node_count,
            "mapped": self.mapped,
            "unmapped_count": self.unmapped_count,
            "unmapped": self.unmapped,
            "warnings": self.warnings,
            "coverage": self.coverage,
        }


@dataclass
class ImportResult:
    """Return value of :func:`import_workflow`."""
    graph: WorkflowGraph
    report: ImportReport

    @property
    def definition(self) -> dict:
        """A WorkflowDefinition-shaped dict (name + steps + variables)."""
        return {
            "name": self.graph.name,
            "steps": self.graph.to_steps(),
            "variables": self.graph.variables,
            "source_tool": self.graph.source_tool,
        }
