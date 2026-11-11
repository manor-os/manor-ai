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
    added_step_ids=(),
) -> dict:
    """Plan a retry of ``steps`` from ``target`` over prior ``results``.

    ``added_step_ids`` are nodes the definition gained after the failed
    attempt. One on the taken path before ``target`` never ran, so it runs
    first and leads into the checkpoint instead of being skipped.
    """
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
    added = frontier & ancestors & set(added_step_ids)
    # The checkpoint and anything else an added node leads into start from it.
    for sid in added:
        entries -= reachable_ids(declared, [sid]) - {sid}
    if explicit_checkpoint:
        entries -= ancestors - added
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


# Steps that only end a run and report why. Running one again re-renders the
# same failure from the same inputs; nothing is made again.
TERMINAL_STEP_TYPES = frozenset({"stop", "end"})


def error_route_origin(steps: list[dict], results: dict, step_id: str) -> str | None:
    """The handled failure whose error route ended at ``step_id``.

    A ``continue_error`` step that fails is recorded as handled, so the run
    takes its ``error_next`` route. When that route then fails, the work to redo
    is the step that failed, not the handler where the route stopped. A handled
    failure whose normal path also reaches ``step_id`` did not cause it, so the
    run's own completed work stays the source.
    """
    from packages.core.services.workflow_service import _declared_step_targets

    edges = graph_edges(steps, results)
    for step in steps:
        sid = str(step.get("id") or "")
        result = results.get(sid)
        if not sid or sid == step_id or not isinstance(result, dict) or result.get("continued") is not True:
            continue
        error_next = (step.get("config") or {}).get("error_next") or []
        route = [str(item) for item in (error_next if isinstance(error_next, list) else [error_next]) if item]
        normal = [target for target in _declared_step_targets(step) if target not in route]
        if not route or step_id not in reachable_ids({**edges, sid: route}, route):
            continue
        if normal and step_id in reachable_ids({**edges, sid: normal}, normal):
            continue
        return sid
    return None


def retry_target(steps: list[dict], prior, retry_state: dict, requested: str | None) -> str:
    """Resolve repair hints against failed receipts, not a stale progress pointer.

    A requested step is honoured unless it is a terminal step: such a target
    comes from a card drawn before retry learned to redo the work before the
    terminal, and would only reproduce the old error.
    """
    step_map = {step["id"]: step for step in steps if step.get("id")}
    requested = str(requested or "").strip()
    if requested and (step_map.get(requested) or {}).get("type") not in TERMINAL_STEP_TYPES:
        return requested
    results = prior.step_results or {}
    failed = [sid for sid, result in results.items() if result.get("status") == "failed" and not completed(result)]
    current = str(prior.current_step_id or "")
    failed_id = current if current in failed else next(iter(failed), None)
    if failed_id:
        hint = _retry_hint(step_map, results, failed_id)
        if hint:
            return hint
        # An error handler that failed is a consequence of the handled failure that routed to it.
        failed_id = error_route_origin(steps, results, failed_id) or failed_id
        hint = _retry_hint(step_map, results, failed_id)
        if hint:
            return hint
        if (step_map.get(failed_id) or {}).get("type") in TERMINAL_STEP_TYPES:
            return work_step_before(steps, results, failed_id) or failed_id
        return failed_id
    return str(retry_state.get("retry_from_step_id") or current).strip()


def _retry_hint(step_map: dict, results: dict, step_id: str) -> str:
    """A recovery point the Flow or its receipt names for ``step_id``."""
    config = (step_map.get(step_id) or {}).get("config") or {}
    return str(results[step_id].get("retry_from_step_id") or config.get("retry_from_step_id") or "").strip()


# Steps that do no work of their own: they start the run, route it, or
# recompute from what they are given, so again with the same inputs they give
# the same answer.
_DETERMINISTIC_STEP_TYPES = frozenset({"trigger", "condition", "switch", "transform", "code", "stop", "end", "note"})


def work_step_before(steps: list[dict], results: dict, stop_id: str) -> str | None:
    """The nearest step on the path a run took into ``stop_id`` that did real work.

    A stop node only reports why the run ended; retrying it alone ends the same
    way. The work that came up short is upstream of it, past the conditions and
    code that routed there.
    """
    step_map = {step["id"]: step for step in steps if step.get("id")}
    reverse: dict[str, list[str]] = {}
    for sid, targets in graph_edges(steps, results, strict=True).items():
        for child in targets:
            reverse.setdefault(child, []).append(sid)
    seen = {stop_id}
    frontier = [stop_id]
    while frontier:
        parents = [
            parent for sid in frontier for parent in reverse.get(sid, [])
            if parent not in seen and completed(results.get(parent))
        ]
        work = [parent for parent in parents if (step_map.get(parent) or {}).get("type") not in _DETERMINISTIC_STEP_TYPES]
        if work:
            return work[0]
        seen.update(parents)
        frontier = parents
    return None


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
