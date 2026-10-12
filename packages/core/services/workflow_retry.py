"""Pure recovery planning for persisted Workflow graph receipts.

The checkpoint identifies work to redo; it is not the entire active graph.
Successful sibling receipts and unfinished sibling frontiers survive retries.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

RETRY_CONTEXT_KEY = "_workflow_retry_context"


def completed(result: dict | None) -> bool:
    return bool(result and (result.get("status") == "completed" or result.get("continued") is True))


def graph_edges(steps: list[dict], results: dict | None = None, *, strict: bool = False) -> dict[str, list[str]]:
    from packages.core.services.workflow_service import _declared_step_targets, _selected_step_targets

    children: dict[str, list[str]] = {}
    for step in steps:
        for dependency in step.get("depends_on") or []:
            children.setdefault(str(dependency), []).append(step["id"])
    edges = {}
    for step in steps:
        sid = step["id"]
        declared = _declared_step_targets(step)
        result = (results or {}).get(sid)
        if not declared:
            edges[sid] = children.get(sid, [])
        elif results is None or not completed(result):
            edges[sid] = declared
        elif strict and step.get("type") in {"condition", "switch"} and "next_override" not in result and not (
            step.get("type") == "condition" and isinstance(result.get("condition_result", result.get("output")), bool)
        ):
            # Old conditional receipts can be ambiguous. They never authorize
            # exploring both arms again; an explicit checkpoint can bypass them.
            edges[sid] = []
        else:
            edges[sid] = _selected_step_targets(step, result)
        if not strict:
            # Data dependencies invalidate consumers even when an explicit
            # control route owns activation of that consumer elsewhere.
            edges[sid] = list(dict.fromkeys([*edges[sid], *children.get(sid, [])]))
    return edges


def reachable_ids(edges: dict[str, list[str]], entries) -> set[str]:
    reached: set[str] = set()
    pending = list(entries)
    while pending:
        sid = pending.pop()
        if sid in reached or sid not in edges:
            continue
        reached.add(sid)
        pending.extend(edges[sid])
    return reached


def build_retry_context(
    steps: list[dict],
    results: dict[str, dict],
    target: str,
    *,
    explicit_checkpoint: bool = False,
) -> dict:
    from packages.core.services.workflow_service import entry_step_id

    steps = [step for step in steps if step.get("id") and step.get("type") != "note"]
    declared = graph_edges(steps)
    if target not in declared:
        raise ValueError("Retry step is not part of the workflow")
    selected = graph_edges(steps, results, strict=True)
    reverse: dict[str, list[str]] = {sid: [] for sid in declared}
    # Legacy ancestor receipts may be valid even when a conditional route was
    # not saved. Use them as data, never as permission to replay that condition.
    for sid, targets in graph_edges(steps, results).items():
        for child in targets:
            if child in reverse:
                reverse[child].append(sid)
    ancestors = reachable_ids(reverse, [target]) - {target}
    has_deps = any(step.get("depends_on") for step in steps)
    roots = [step["id"] for step in steps if not step.get("depends_on")] if has_deps else [entry_step_id(steps)]
    # In hybrid graphs, nodes with an explicit incoming edge are not roots.
    incoming = {child for targets in declared.values() for child in targets}
    if has_deps:
        roots = [sid for sid in roots if sid not in incoming]
    visited: set[str] = set()
    frontier: set[str] = set()
    pending = list(roots)
    while pending:
        sid = pending.pop()
        if sid in visited or sid not in declared:
            continue
        visited.add(sid)
        if completed(results.get(sid)):
            pending.extend(selected[sid])
        else:
            frontier.add(sid)

    invalidated = reachable_ids(declared, [target])
    entries = {target} | (frontier - invalidated)
    if explicit_checkpoint:
        entries -= ancestors
        scope = reachable_ids(declared, entries)
        missing_dependencies = {
            str(dep) for step in steps if step["id"] in scope
            for dep in step.get("depends_on") or []
            if str(dep) not in scope and not completed(results.get(str(dep)))
        }
        if missing_dependencies:
            raise ValueError(
                "Retry checkpoint requires completed dependencies: " + ", ".join(sorted(missing_dependencies))
            )
    # Recovered upstream failures can make an old downstream success stale.
    # Include every active unfinished branch in invalidation, not only target.
    invalidated |= reachable_ids(declared, entries)
    inherited = {
        sid for sid in (visited | ancestors) - invalidated
        if completed(results.get(sid))
    }
    order = [step["id"] for step in steps]
    return {
        "version": 1,
        "target_step_id": target,
        "entry_step_ids": [sid for sid in order if sid in entries],
        "inherited_step_ids": [sid for sid in order if sid in inherited],
        "invalidated_step_ids": [sid for sid in order if sid in invalidated],
    }


def retry_entries(run, step_ids) -> list[str] | None:
    """Read server-owned recovery roots; old attempts retain checkpoint semantics."""
    context = (getattr(run, "trigger_data", None) or {}).get(RETRY_CONTEXT_KEY)
    if context is None:
        return None
    if not isinstance(context, dict) or context.get("version") != 1:
        raise ValueError("Invalid workflow retry context")
    entries = context.get("entry_step_ids")
    if not isinstance(entries, list) or not entries or any(not isinstance(sid, str) or sid not in step_ids for sid in entries):
        raise ValueError("Invalid workflow retry entries")
    snapshot = getattr(run, "execution_snapshot", None) or {}
    if context.get("fingerprint") and context["fingerprint"] != snapshot.get("fingerprint"):
        raise ValueError("Workflow retry snapshot does not match its recovery context")
    return entries


def retry_target(steps: list[dict], prior, retry_state: dict, requested: str | None) -> str:
    """Resolve repair hints against failed receipts, not a stale progress pointer."""
    if requested:
        return str(requested).strip()
    results = prior.step_results or {}
    failed = [sid for sid, result in results.items() if result.get("status") == "failed" and not completed(result)]
    current = str(prior.current_step_id or "")
    failed_id = current if current in failed else next(iter(failed), None)
    step_map = {step["id"]: step for step in steps}
    if failed_id:
        result = results[failed_id]
        config = (step_map.get(failed_id) or {}).get("config") or {}
        return str(result.get("retry_from_step_id") or config.get("retry_from_step_id") or failed_id).strip()
    return str(retry_state.get("retry_from_step_id") or current).strip()


def merge_retry_evidence(steps: list[dict], attempts: list) -> SimpleNamespace:
    """Fold an already scope/snapshot-validated chain, oldest first.

    Keep tombstones for invalidated receipts so variable cleanup cannot revive
    their output aliases. Never infer an old entry from its mutable failure
    pointer: the execution trace or server recovery context is authoritative.
    """
    edges = graph_edges(steps)
    results: dict = {}
    executed_results: dict = {}
    variables: dict = {}
    sources: dict = {}
    for index, attempt in enumerate(attempts):
        trigger = attempt.trigger_data or {}
        context = trigger.get(RETRY_CONTEXT_KEY) or {}
        if index:
            entry = context.get("target_step_id") or next((
                item.get("node_id") for item in (attempt.execution_trace or [])
                if item.get("status") == "running" and item.get("node_id") in edges
            ), None)
            if not entry:
                if any(completed(result) and sid not in (attempt.step_results or {}) for sid, result in results.items()):
                    raise ValueError("Cannot recover historical workflow results without an execution checkpoint")
                results = {}
                sources = {}
            invalidated = set(context.get("invalidated_step_ids") or reachable_ids(edges, [entry]))
            if trigger.get("input_patch"):
                # An input change is a barrier to historical resurrection.
                invalidated.update(results)
            for sid in invalidated:
                if sid in results:
                    results[sid] = {"status": "invalidated"}
                    sources.pop(sid, None)
        variables.update(deepcopy(attempt.variables or {}))
        for sid, result in (attempt.step_results or {}).items():
            results[sid] = deepcopy(result)
            if result.get("status") != "invalidated":
                executed_results[sid] = deepcopy(result)
            sources[sid] = attempt.id
    return SimpleNamespace(step_results=results, variables=variables, source_run_ids=sources,
                           executed_results=executed_results)
