"""Dify DSL (YAML) importer.

Dify exports an app DSL with ``app:`` / ``kind: app`` / ``workflow.graph`` of
``nodes`` (``data.type``) + ``edges`` (source/target/sourceHandle). Semantic fit
with manor is high — most node types map 1:1.
"""
from __future__ import annotations

import re

from .base import WorkflowImporter
from .connectors import resolve_connector
from .model import GraphNode, ImportReport, WorkflowGraph

# Dify variable references look like {{#nodeId.field#}}; manor uses {{field}}.
_DIFY_REF = re.compile(r"\{\{#[^#}]*?\.([A-Za-z0-9_]+)#\}\}")
_DIFY_REF_BARE = re.compile(r"\{\{#([A-Za-z0-9_]+)#\}\}")


def _norm_refs(text: str) -> str:
    """Normalise Dify ``{{#node.field#}}`` refs to manor ``{{field}}``."""
    if not isinstance(text, str):
        return text
    text = _DIFY_REF.sub(r"{{\1}}", text)
    return _DIFY_REF_BARE.sub(r"{{\1}}", text)


def _param_value(param):
    """Unwrap a Dify ``{type, value}`` parameter wrapper to its value."""
    if isinstance(param, dict) and "value" in param:
        return param["value"]
    return param

# Dify data.type -> canonical manor node type
DIFY_NODE_MAP = {
    "start": "trigger",
    "llm": "llm",
    "knowledge-retrieval": "rag",
    "if-else": "condition",
    "code": "code",
    "http-request": "http",
    "tool": "connector",
    "agent": "agent",
    "question-classifier": "classifier",
    "iteration": "loop",
    "loop": "loop",
    "variable-aggregator": "merge",
    "variable-assigner": "transform",
    "assigner": "transform",
    "template-transform": "transform",
    "parameter-extractor": "llm",
    "list-operator": "transform",
    "document-extractor": "tool",
    "answer": "notify",
    "end": "end",
}

# Dify comparison operators -> the operators manor's condition evaluator supports.
# Operators with no native equivalent (contains / empty / start with / in ...) are
# intentionally absent: those conditions are left untranslated (best-effort).
DIFY_OP_MAP = {
    "=": "==", "is": "==", "==": "==", "equals": "==",
    "≠": "!=", "!=": "!=", "is not": "!=", "not equals": "!=",
    ">": ">", "<": "<",
    "≥": ">=", ">=": ">=", "≤": "<=", "<=": "<=",
}


class DifyImporter(WorkflowImporter):
    source_tool = "dify"

    @classmethod
    def detect(cls, raw: dict) -> bool:
        if not isinstance(raw, dict):
            return False
        if raw.get("kind") == "app":
            return True
        return "app" in raw and "workflow" in raw

    def parse(self, raw: dict, name: str | None = None) -> tuple[WorkflowGraph, ImportReport]:
        report = ImportReport(source_tool=self.source_tool)
        app = raw.get("app") or {}
        wf_name = name or app.get("name") or "Imported Dify workflow"

        graph_obj = (raw.get("workflow") or {}).get("graph") or {}
        raw_nodes = graph_obj.get("nodes") or []
        raw_edges = graph_obj.get("edges") or []

        # successors keyed by source node id; branch handles tracked separately
        succ: dict[str, list[str]] = {}
        branch: dict[str, dict[str, list[str]]] = {}  # node_id -> {"true":[], "false":[]}
        for e in raw_edges:
            src, tgt = e.get("source"), e.get("target")
            if not src or not tgt:
                continue
            handle = (e.get("sourceHandle") or "").lower()
            if handle in ("true", "false"):
                branch.setdefault(src, {}).setdefault(handle, []).append(tgt)
            else:
                succ.setdefault(src, []).append(tgt)

        nodes: list[GraphNode] = []
        for rn in raw_nodes:
            nid = str(rn.get("id"))
            data = rn.get("data") or {}
            otype = data.get("type") or "unknown"
            title = data.get("title") or otype
            canonical = DIFY_NODE_MAP.get(otype)

            if canonical is None:
                node = self._placeholder(
                    report, nid, title, otype, rn,
                    reason=f"Dify node type '{otype}' has no manor equivalent",
                )
                node.next = succ.get(nid, [])
                nodes.append(node)
                continue

            raw_cfg = {k: v for k, v in data.items() if k not in ("type", "title")}
            native = self._translate(otype, data)
            node = GraphNode(
                id=nid, type=canonical, name=title,
                config={**native, "dify": raw_cfg},
                next=succ.get(nid, []),
                meta={"source_tool": self.source_tool, "original_type": otype},
            )
            if canonical == "condition" and nid in branch:
                node.true_next = branch[nid].get("true", [])
                node.false_next = branch[nid].get("false", [])
            nodes.append(node)

        self._finalize(report, nodes)
        return WorkflowGraph(name=wf_name, source_tool=self.source_tool, nodes=nodes), report

    # ── Config translation: Dify node data -> manor-native config ────────

    def _translate(self, otype: str, data: dict) -> dict:
        """Best-effort translation of a Dify node's data into native config
        keys the runner understands. Unknown shapes return ``{}`` (the raw
        payload is still preserved under ``config.dify``)."""
        try:
            translator = getattr(self, f"_t_{otype.replace('-', '_')}", None)
            return translator(data) if translator else {}
        except Exception:  # never let translation break the import
            return {}

    def _t_llm(self, data: dict) -> dict:
        """Dify llm node -> native {model, system_prompt, prompt, temperature}."""
        cfg: dict = {}
        model = data.get("model") or {}
        if model.get("name"):
            cfg["model"] = model["name"]
        params = model.get("completion_params") or {}
        if "temperature" in params:
            cfg["temperature"] = params["temperature"]

        systems, users = [], []
        template = data.get("prompt_template")
        if isinstance(template, list):
            for msg in template:
                role = (msg.get("role") or "").lower()
                text = _norm_refs(msg.get("text") or "")
                if role == "system":
                    systems.append(text)
                elif text:
                    users.append(text)
        elif isinstance(template, dict):  # completion-mode single template
            users.append(_norm_refs(template.get("text") or ""))

        if systems:
            cfg["system_prompt"] = "\n".join(s for s in systems if s)
        if users:
            cfg["prompt"] = "\n".join(u for u in users if u)
        return cfg

    def _t_http_request(self, data: dict) -> dict:
        cfg: dict = {}
        if data.get("method"):
            cfg["method"] = str(data["method"]).upper()
        if data.get("url"):
            cfg["url"] = _norm_refs(data["url"])
        headers = data.get("headers")
        if isinstance(headers, dict):
            cfg["headers"] = {k: _norm_refs(str(v)) for k, v in headers.items()}
        body = data.get("body") or {}
        if isinstance(body, dict) and body.get("data") is not None:
            cfg["body"] = body["data"]
        return cfg

    def _t_knowledge_retrieval(self, data: dict) -> dict:
        cfg: dict = {}
        selector = data.get("query_variable_selector")
        if isinstance(selector, list) and selector:
            cfg["query"] = "{{" + str(selector[-1]) + "}}"
        if data.get("dataset_ids"):
            cfg["dataset_ids"] = data["dataset_ids"]
        return cfg

    def _t_code(self, data: dict) -> dict:
        cfg: dict = {}
        if data.get("code") is not None:
            cfg["code"] = data["code"]
        if data.get("code_language"):
            cfg["language"] = data["code_language"]
        return cfg

    def _t_tool(self, data: dict) -> dict:
        """Dify tool node -> shared connector resolution (same layer n8n uses)."""
        provider = (
            data.get("provider_name") or data.get("provider_id")
            or data.get("provider_type") or ""
        )
        return resolve_connector(
            provider,
            operation=data.get("tool_name"),
            args=data.get("tool_parameters") or {},
        )

    def _t_agent(self, data: dict) -> dict:
        """Dify agent node -> native agent-step config.

        Maps onto manor's agent step: instruction->system_prompt, query->prompt,
        model, tools (names), maximum_iterations->max_rounds, and the Dify agent
        strategy (react / function_calling) -> ``strategy`` (manor's pluggable
        reasoning hook).
        """
        cfg: dict = {}
        params = data.get("agent_parameters") or {}

        strategy = data.get("agent_strategy_name") or data.get("strategy")
        if strategy:
            cfg["strategy"] = strategy

        instruction = _param_value(params.get("instruction"))
        if instruction:
            cfg["system_prompt"] = _norm_refs(str(instruction))
        query = _param_value(params.get("query"))
        if query:
            cfg["prompt"] = _norm_refs(str(query))

        model = _param_value(params.get("model"))
        if isinstance(model, dict):
            name = model.get("model") or model.get("name")
            if name:
                cfg["model"] = name

        max_iter = _param_value(params.get("maximum_iterations"))
        if max_iter:
            cfg["max_rounds"] = max_iter

        tools = _param_value(params.get("tools"))
        if isinstance(tools, list):
            names = []
            for t in tools:
                if isinstance(t, dict):
                    names.append(t.get("tool_name") or t.get("tool") or t.get("name"))
                elif isinstance(t, str):
                    names.append(t)
            names = [n for n in names if n]
            if names:
                cfg["tools"] = names
        return cfg

    def _t_if_else(self, data: dict) -> dict:
        """Dify if-else -> native ``expression``.

        Translates all conditions of the first case, joined by the case's
        ``logical_operator`` (and/or) — the runner's evaluator supports compound
        boolean expressions. Conditions with unsupported operators are dropped.
        """
        conditions, logic = None, "and"
        cases = data.get("cases")
        if isinstance(cases, list) and cases:
            conditions = cases[0].get("conditions")
            logic = cases[0].get("logical_operator", "and")
        if conditions is None:
            conditions = data.get("conditions")
            logic = data.get("logical_operator", "and")
        if not isinstance(conditions, list) or not conditions:
            return {}
        exprs = [e for e in (self._condition_to_expr(c) for c in conditions) if e]
        if not exprs:
            return {}
        joiner = " or " if str(logic).lower() == "or" else " and "
        return {"expression": joiner.join(exprs)}

    def _condition_to_expr(self, cond: dict) -> str | None:
        selector = cond.get("variable_selector")
        if isinstance(selector, list) and selector:
            var = str(selector[-1])
        else:
            var = cond.get("variable")
        op = DIFY_OP_MAP.get(str(cond.get("comparison_operator", "")).strip())
        if not var or not op:
            return None
        value = cond.get("value", "")
        val_str = str(value)
        try:
            float(val_str)
            rhs = val_str  # numeric — leave unquoted
        except ValueError:
            rhs = f'"{val_str}"'  # string — quote it
        return f"{var} {op} {rhs}"
