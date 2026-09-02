"""Portable identity projection for Blueprint subworkflow nodes."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


class WorkflowDependencyError(ValueError):
    """Raised when a subworkflow target cannot cross the Blueprint boundary."""


class WorkflowDependencyFactory:
    """Translate subworkflow targets between local IDs and component keys."""

    STEP_TYPES = frozenset({"subworkflow", "foreach_subworkflow"})

    @classmethod
    def has_dependencies(cls, steps: list[dict[str, Any]]) -> bool:
        return any(
            isinstance(step, dict)
            and (step.get("type") or step.get("kind")) in cls.STEP_TYPES
            for step in steps
        )

    @classmethod
    def missing_source_keys(
        cls,
        steps: list[dict[str, Any]],
        *,
        workflow_id_by_key: Mapping[str, str],
    ) -> frozenset[str]:
        """Return portable dependency keys that cannot yet be materialized."""

        missing: set[str] = set()
        for step in steps:
            if (
                not isinstance(step, dict)
                or (step.get("type") or step.get("kind")) not in cls.STEP_TYPES
            ):
                continue
            config = cls._config(step)
            source_key = str(
                config.get("source_workflow_key")
                or config.get("workflow_id")
                or ""
            ).strip()
            if not source_key or not str(
                workflow_id_by_key.get(source_key) or ""
            ).strip():
                missing.add(source_key)
        return frozenset(missing)

    @classmethod
    def to_portable(
        cls,
        steps: list[dict[str, Any]],
        *,
        workflow_key_by_id: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        projected = deepcopy(steps)
        for step in projected:
            if (
                not isinstance(step, dict)
                or (step.get("type") or step.get("kind")) not in cls.STEP_TYPES
            ):
                continue
            config = cls._config(step)
            workflow_id = str(config.get("workflow_id") or "").strip()
            source_key = str(config.get("source_workflow_key") or "").strip()
            if workflow_id:
                source_key = str(workflow_key_by_id.get(workflow_id) or "").strip()
                if not source_key:
                    raise WorkflowDependencyError(
                        f"workflow step {step.get('id')!r} references a Flow "
                        "that is not part of this Workspace Blueprint"
                    )
            if not source_key:
                raise WorkflowDependencyError(
                    f"workflow step {step.get('id')!r} has no portable subworkflow target"
                )
            config.pop("workflow_id", None)
            config["source_workflow_key"] = source_key
            step["config"] = config
        return projected

    @classmethod
    def to_runtime(
        cls,
        steps: list[dict[str, Any]],
        *,
        workflow_id_by_key: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        materialized = deepcopy(steps)
        for step in materialized:
            if (
                not isinstance(step, dict)
                or (step.get("type") or step.get("kind")) not in cls.STEP_TYPES
            ):
                continue
            config = cls._config(step)
            source_key = str(
                config.get("source_workflow_key")
                or config.get("workflow_id")
                or ""
            ).strip()
            workflow_id = str(workflow_id_by_key.get(source_key) or "").strip()
            if not source_key or not workflow_id:
                raise WorkflowDependencyError(
                    f"workflow step {step.get('id')!r} references missing "
                    f"Blueprint Flow {source_key!r}"
                )
            config["source_workflow_key"] = source_key
            config["workflow_id"] = workflow_id
            step["config"] = config
        return materialized

    @staticmethod
    def _config(step: dict[str, Any]) -> dict[str, Any]:
        config = step.get("config")
        return dict(config) if isinstance(config, dict) else {}
