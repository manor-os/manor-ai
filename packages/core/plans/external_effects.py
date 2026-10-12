"""Which plan steps have an external side effect, and what governance subject they carry.

One detector, three consumers: the partial-upstream gate (an external effect
must not run on half a deliverable), the plan pre-flight (an external effect
that the workspace forbids or cannot route fails before any step burns
credits), and evidence checks. Deterministic; nothing here calls a model.

A step has an external effect when any of these say so:

* the plan declares it — ``expects: ["publish"]`` / ``["send"]`` or
  ``output_shape: PublishResult``;
* the step's own ``capability_id`` is an ``external.*`` capability;
* the step's ``action_key`` maps to an ``external.*`` capability through the
  same table the governance layer uses (``runtime_capability_id_for_action_key``).
"""
from __future__ import annotations

from typing import Any

#: effect → (action_key, capability_id) used for governance evaluation when
#: the step itself does not declare them (a subagent step with a PublishResult
#: shape has no action_key of its own). These are the keys the runtime approval
#: classifier assigns to the same operations at execution time.
EXTERNAL_EFFECT_SUBJECTS: dict[str, tuple[str, str]] = {
    "external.social": ("social_post.publish", "external.social"),
    "external.email": ("email.send", "external.email"),
    "external.message": ("external_message.send", "external.message"),
}

#: Substrings of an MCP server key that mark a provider as a publishing route
#: for the effect. Mirrors the markers in ``approval_classifier`` (social /
#: email branches) so pre-flight and runtime agree on what counts.
EXTERNAL_EFFECT_PROVIDER_MARKERS: dict[str, tuple[str, ...]] = {
    "external.social": (
        "twitter", "linkedin", "facebook", "instagram", "tiktok",
        "youtube", "xiaohongshu", "rednote",
    ),
    "external.email": ("gmail", "outlook", "email", "mail", "smtp", "imap"),
}

_EXPECTS_EFFECTS: dict[str, str] = {"publish": "external.social", "send": "external.message"}


def plan_dag_step(plan_dag: Any, step_key: str) -> dict[str, Any]:
    """The planner's declaration for ``step_key`` (``expects``, ``output_shape``…)."""
    steps = plan_dag.get("steps") if isinstance(plan_dag, dict) else None
    for declared in steps or []:
        if isinstance(declared, dict) and str(declared.get("key") or "") == step_key:
            return declared
    return {}


def step_external_effect(step: Any, plan_dag: Any) -> str | None:
    """The ``external.*`` capability class this step commits to, or None."""
    declared = plan_dag_step(plan_dag, str(getattr(step, "step_key", "") or ""))
    for expect in declared.get("expects") or []:
        effect = _EXPECTS_EFFECTS.get(str(expect or "").strip())
        if effect:
            return effect
    if str(declared.get("output_shape") or "").strip() == "PublishResult":
        return "external.social"

    capability = str(getattr(step, "capability_id", None) or "").strip()
    if capability.startswith("external."):
        return capability

    action_key = str(getattr(step, "action_key", None) or "").strip()
    if action_key:
        from packages.core.ai.runtime.approvals import runtime_capability_id_for_action_key

        mapped = str(
            runtime_capability_id_for_action_key(
                action_key, provider=getattr(step, "provider", None),
            ) or ""
        ).strip()
        if mapped.startswith("external."):
            return mapped
    return None


def external_effect_subject(step: Any, effect: str) -> tuple[str, str]:
    """``(action_key, capability_id)`` to evaluate governance against."""
    action_key = str(getattr(step, "action_key", None) or "").strip()
    capability = str(getattr(step, "capability_id", None) or "").strip()
    fallback_action, fallback_capability = EXTERNAL_EFFECT_SUBJECTS.get(
        effect, ("external.action", effect),
    )
    return (action_key or fallback_action, capability or fallback_capability)
