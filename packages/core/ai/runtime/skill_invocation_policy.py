from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal


REQUIRED_BEFORE_ANSWER = "required_before_answer"
PRIMARY_SOURCE = "primary_source"
_MAX_SEMANTIC_TRIGGER_CHARS = 600
_POLICY_FIELDS = frozenset({"mode", "semantic_trigger", "result_authority"})

InvocationMode = Literal["required_before_answer"]
ResultAuthority = Literal["primary_source"]


@dataclass(frozen=True)
class SkillInvocationPolicy:
    """Trusted, declarative policy for presenting a Skill to the parent model."""

    mode: InvocationMode
    semantic_trigger: str
    result_authority: ResultAuthority

    @classmethod
    def from_config(cls, raw: Any) -> "SkillInvocationPolicy":
        if not isinstance(raw, Mapping):
            raise ValueError("invocation_policy must be an object")

        unknown_fields = set(raw).difference(_POLICY_FIELDS)
        if unknown_fields:
            names = ", ".join(sorted(str(field) for field in unknown_fields))
            raise ValueError(f"invocation_policy has unknown fields: {names}")

        mode = raw.get("mode")
        if mode != REQUIRED_BEFORE_ANSWER:
            raise ValueError(
                f"invocation_policy.mode must be {REQUIRED_BEFORE_ANSWER!r}"
            )

        semantic_trigger_raw = raw.get("semantic_trigger")
        if not isinstance(semantic_trigger_raw, str):
            raise ValueError("invocation_policy.semantic_trigger must be a string")
        semantic_trigger = " ".join(semantic_trigger_raw.split())
        if not semantic_trigger:
            raise ValueError("invocation_policy.semantic_trigger is required")
        if len(semantic_trigger) > _MAX_SEMANTIC_TRIGGER_CHARS:
            raise ValueError(
                "invocation_policy.semantic_trigger exceeds "
                f"{_MAX_SEMANTIC_TRIGGER_CHARS} characters"
            )

        result_authority = raw.get("result_authority")
        if result_authority != PRIMARY_SOURCE:
            raise ValueError(
                f"invocation_policy.result_authority must be {PRIMARY_SOURCE!r}"
            )

        return cls(
            mode=mode,
            semantic_trigger=semantic_trigger,
            result_authority=result_authority,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "semantic_trigger": self.semantic_trigger,
            "result_authority": self.result_authority,
        }


def trusted_skill_invocation_policy(
    skill: Any,
    *,
    source: str | None = None,
) -> SkillInvocationPolicy | None:
    """Return a policy only for repository-controlled built-in Skills.

    Invocation policies become system-prompt instructions. Entity-imported or
    marketplace Skills must never be able to inject them through config data.
    """

    config = getattr(skill, "config", None)
    metadata = getattr(skill, "metadata", None)
    declared_source = str(source or getattr(skill, "source", None) or "").strip()

    raw = metadata.get("invocation_policy") if isinstance(metadata, dict) else None
    if raw is not None:
        # Runtime descriptors are immutable, internal projections. Their source
        # is assigned by the resolver, not by Skill authors.
        if declared_source != "builtin":
            return None
    elif isinstance(config, dict):
        raw = config.get("invocation_policy")
        # Persisted policy config is trusted only when both the row provenance
        # and the runtime classification identify a repository built-in.
        if (
            raw is not None
            and (
                getattr(skill, "entity_id", "missing") is not None
                or config.get("source") != "builtin"
                or (declared_source and declared_source != "builtin")
            )
        ):
            return None
    if raw is None:
        return None

    try:
        return SkillInvocationPolicy.from_config(raw)
    except ValueError:
        return None


def retain_required_skill_invocation_policies(
    all_skills: Iterable[Any],
    selected_skills: Iterable[Any],
) -> list[Any]:
    """Keep trusted required policies visible after intent-specific narrowing."""

    required = [
        skill
        for skill in all_skills
        if trusted_skill_invocation_policy(skill) is not None
    ]
    selected = list(selected_skills)
    required_object_ids = {id(skill) for skill in required}
    return required + [skill for skill in selected if id(skill) not in required_object_ids]


def render_skill_invocation_policy(
    *,
    skill_slug: str,
    policy: SkillInvocationPolicy,
) -> str:
    """Render a validated policy without embedding product-specific logic."""

    trigger = policy.semantic_trigger.rstrip(". ")
    return (
        f'- When {trigger}, you must call `invoke_skill(skill="{skill_slug}", '
        "input=<current user request>)` before answering. Determine whether the "
        "policy applies from semantic meaning, not literal keyword matching. "
        "Treat the Skill result and its references as the primary source of "
        "truth; do not answer from memory when the Skill is available."
    )
