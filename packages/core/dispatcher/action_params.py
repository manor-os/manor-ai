"""Canonical provider-argument projection for planned action steps.

``ExecutionStep.params`` also carries server-owned runtime coordination state.
That state is useful while leasing/retrying a step, but it is never part of the
provider call.  Every boundary that validates, dispatches, or fingerprints an
action must therefore use this one projection.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def executable_action_params(
    params: object,
    *,
    human_input_response: Any = None,
) -> dict[str, Any]:
    """Return the exact JSON object an action provider is allowed to see.

    Underscore-prefixed keys are Runtime control fields. ``source_contract``
    retains the immutable coordinator checklist contract and is likewise not a
    provider argument.  Values are copied so DTO construction or adapters
    cannot mutate the durable step contract through a shared JSON object.
    """

    raw = params if isinstance(params, dict) else {}
    projected = {
        str(key): deepcopy(value)
        for key, value in raw.items()
        if not str(key).startswith("_") and str(key) != "source_contract"
    }
    if human_input_response is not None:
        projected["human_input_response"] = deepcopy(human_input_response)
    return projected


__all__ = ["executable_action_params"]
