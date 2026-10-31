"""Concurrent execution plan for one model round of independent tool calls.

A round that contains any call not proven read-only normally runs serially:
approval gates are only discovered at each call's execution boundary, and a
mutating sibling must not overtake a request for a human. This module decides,
before anything runs, when that serialization is unnecessary:

* every call's gate is previewed with the live approval decision
  (``RuntimeApprovalMiddleware.preview_request``) and would pass, or refuse,
  with no human stop;
* every call is a read or a policy-gated action (never a child loop, a turn
  park, browser or draft/sandbox state, see ``runtime_tool_always_serial``);
* each action's effect target is the exact Workspace file set its
  authorization receipt binds. Calls whose targets may overlap, and calls with
  no derivable target, keep their authored order.

Any doubt (a preview that fails, an undecided gate, an unclassified call)
returns no plan, and the round runs serially as before. The live gate still
runs for every call; the plan only decides which calls may overlap.
"""

from __future__ import annotations

import logging
import os
import posixpath
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _concurrent_tool_calls_max() -> int:
    try:
        value = int(os.environ.get("LOOP_CONCURRENT_TOOL_CALLS_MAX", "4") or 4)
    except ValueError:
        value = 4
    return max(1, value)


# Upper bound of mutating calls of one round in flight at once.
LOOP_CONCURRENT_TOOL_CALLS_MAX = _concurrent_tool_calls_max()

# Tools whose effect is a command, not the file targets their arguments name.
_COMMAND_TOOL_NAMES = frozenset({"bash", "code"})

RuntimeToolEffectTargets = frozenset[str] | None
RuntimeToolCallPreviewer = Callable[[dict[str, Any]], Awaitable[Any]]


def _target_key_conflict(left: str, right: str) -> bool:
    left_kind, _, left_path = left.partition(":")
    right_kind, _, right_path = right.partition(":")
    left_path, right_path = left_path.lower(), right_path.lower()
    if left_path == right_path:
        return True
    if left_path.startswith(right_path + "/") or right_path.startswith(left_path + "/"):
        return True
    if left_kind == right_kind == "file":
        # The same leaf reached through a different spelling of its folder
        # (a typed default directory, Workspace vs task scope).
        return posixpath.basename(left_path) == posixpath.basename(right_path)
    # An unscoped leaf may be placed under any tree.
    file_path = left_path if left_kind == "file" else right_path if right_kind == "file" else None
    return file_path is not None and "/" not in file_path


def runtime_tool_targets_conflict(
    left: RuntimeToolEffectTargets,
    right: RuntimeToolEffectTargets,
) -> bool:
    """True when two calls may touch the same resource. Unknown conflicts."""

    if left is None or right is None:
        return True
    return any(
        _target_key_conflict(left_key, right_key)
        for left_key in left
        for right_key in right
    )


def _workspace_file_targets(request: Any) -> RuntimeToolEffectTargets:
    """The exact file targets the call's authorization receipt binds."""

    from packages.core.ai.runtime.authorization_receipts import (
        WorkspaceFileAuthorizationResourceFactory,
        WorkspaceFileResourceMatchKind,
        workspace_file_resource_id,
    )
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory

    action = request.classification.action
    if action is None or action.resource_kind != "file":
        return None
    canonical = RuntimeCompositeToolCallFactory.create(request.tool_name, request.arguments)
    if canonical.tool_name in _COMMAND_TOOL_NAMES:
        return None
    resources = WorkspaceFileAuthorizationResourceFactory.create(
        request=request,
        action_key=action.action_key,
        resource_id=action.resource_id,
    )
    if not resources:
        return None
    scope = request.workspace_file_scope
    base = workspace_file_resource_id(scope.artifact_base_dir if scope is not None else None)
    keys: set[str] = set()
    for resource in resources:
        path = workspace_file_resource_id(resource.resource_id)
        if path is None:
            return None
        if (
            base is not None
            and resource.match_kind is not WorkspaceFileResourceMatchKind.ENTITY_EXACT
            and path != base
            and not path.startswith(base + "/")
            and not path.startswith("Workspaces/")
        ):
            path = f"{base}/{path}"
        if resource.match_kind in {
            WorkspaceFileResourceMatchKind.EXACT,
            WorkspaceFileResourceMatchKind.ENTITY_EXACT,
        }:
            keys.add(f"file:{path}")
        elif resource.match_kind is WorkspaceFileResourceMatchKind.COLLISION_SAFE_FILE:
            # The writer picks a free sibling name in the same folder.
            keys.add(f"tree:{posixpath.dirname(path) or path}")
        else:
            keys.add(f"tree:{path}")
    return frozenset(keys)


def runtime_tool_call_effect_targets(request: Any) -> tuple[bool, RuntimeToolEffectTargets]:
    """``(eligible, targets)`` for one classified approval request.

    A read is eligible with unknown targets (it keeps its order against
    every other call). Only a policy-gated ACTION may overlap with others;
    orchestrators, controls and leaf-only actions are never eligible.
    """

    from packages.core.ai.runtime.approval_classifier import (
        runtime_call_executes_nothing,
        runtime_effect_gate,
    )
    from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
    from packages.core.governance.action_effects import EffectGate

    classification = request.classification
    if classification is None or classification.effect is RuntimeToolEffect.UNKNOWN:
        return False, None
    if runtime_call_executes_nothing(request.tool_name, classification):
        return True, None
    if (
        classification.effect is not RuntimeToolEffect.ACTION
        or runtime_effect_gate(classification) is not EffectGate.POLICY
    ):
        return False, None
    return True, _workspace_file_targets(request)


@dataclass(frozen=True)
class RuntimeToolBatchPlan:
    """Per-call effect targets of one round, in authored order."""

    targets: tuple[RuntimeToolEffectTargets, ...]

    def dependencies(self) -> tuple[frozenset[int], ...]:
        """For each call, the earlier calls it must wait for."""

        return tuple(
            frozenset(
                earlier
                for earlier in range(index)
                if runtime_tool_targets_conflict(self.targets[index], self.targets[earlier])
            )
            for index in range(len(self.targets))
        )

    @property
    def runs_concurrently(self) -> bool:
        """False when the dependencies form one chain, i.e. a serial round."""

        dependencies = self.dependencies()
        return any(index - 1 not in dependencies[index] for index in range(1, len(dependencies)))


def _always_serial(name: str, arguments: dict[str, Any]) -> bool:
    from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
    from packages.core.ai.runtime.human_gate import runtime_tool_always_serial

    if runtime_tool_always_serial(name):
        return True
    try:
        canonical = RuntimeCompositeToolCallFactory.create(name, arguments)
    except Exception:
        return True
    return runtime_tool_always_serial(canonical.tool_name)


async def runtime_plan_tool_batch(
    calls: Sequence[dict[str, Any]],
    *,
    preview: RuntimeToolCallPreviewer,
) -> RuntimeToolBatchPlan | None:
    """A concurrent plan for ``calls``, or ``None`` to keep the round serial.

    ``preview`` returns the call's ``RuntimeApprovalPreview`` (or ``None``
    when the call cannot be previewed). Every call must be clear of human
    gates; the first doubt ends planning.
    """

    if len(calls) < 2:
        return None
    for call in calls:
        arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
        if _always_serial(str(call.get("name") or ""), arguments):
            return None
    targets: list[RuntimeToolEffectTargets] = []
    for call in calls:
        try:
            previewed = await preview(call)
            if previewed is None or not previewed.clear:
                return None
            eligible, call_targets = runtime_tool_call_effect_targets(previewed.request)
        except Exception:
            logger.warning(
                "[tool_batch] gate preview failed for %s; keeping the round serial",
                call.get("name"),
                exc_info=True,
            )
            return None
        if not eligible:
            return None
        targets.append(call_targets)
    plan = RuntimeToolBatchPlan(tuple(targets))
    return plan if plan.runs_concurrently else None


__all__ = [
    "LOOP_CONCURRENT_TOOL_CALLS_MAX",
    "RuntimeToolBatchPlan",
    "runtime_plan_tool_batch",
    "runtime_tool_call_effect_targets",
    "runtime_tool_targets_conflict",
]
