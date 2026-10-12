"""Portable identity projection for Blueprint subworkflow nodes."""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Mapping

from packages.core.services.reusable_resource_locks import is_reusable_resource_local_id


class WorkflowSkillDependencyFactory:
    """Preserve nested Skill arguments through portable, exact identities."""

    FIELDS = frozenset({"skill", "skill_id"})

    @classmethod
    def has_portable_refs(cls, node: Any) -> bool:
        if isinstance(node, list):
            return any(cls.has_portable_refs(item) for item in node)
        return isinstance(node, dict) and (
            "skill_refs" in node or any(cls.has_portable_refs(value) for value in node.values())
        )

    @classmethod
    def local_ids(cls, node: Any) -> set[str]:
        if isinstance(node, list):
            return set().union(*(cls.local_ids(item) for item in node))
        if not isinstance(node, dict):
            return set()
        return {
            str(value).strip()
            for key, value in node.items()
            if key in cls.FIELDS and is_reusable_resource_local_id(value)
        } | set().union(*(cls.local_ids(value) for value in node.values()))

    @classmethod
    def to_portable(cls, node: Any, *, skill_ref_by_id: Mapping[str, dict]) -> Any:
        if isinstance(node, list):
            return [cls.to_portable(item, skill_ref_by_id=skill_ref_by_id) for item in node]
        if not isinstance(node, dict):
            return deepcopy(node)
        projected = {}
        refs = []
        for key, value in node.items():
            if key in cls.FIELDS and is_reusable_resource_local_id(value):
                ref = skill_ref_by_id.get(str(value).strip())
                if not ref:
                    raise WorkflowDependencyError("Workflow Skill has no portable dependency")
                refs.append({"field": key, "target": dict(ref)})
            else:
                projected[key] = cls.to_portable(value, skill_ref_by_id=skill_ref_by_id)
        if refs:
            if "skill_refs" in projected:
                raise WorkflowDependencyError("Workflow Skill references conflict with runtime arguments")
            projected["skill_refs"] = refs
        return projected


class WorkflowDocumentDependencyFactory:
    """Map exact Knowledge IDs to included starter keys without copying row IDs."""

    @classmethod
    def local_ids(cls, node: Any) -> set[str]:
        if isinstance(node, list):
            return set().union(*(cls.local_ids(item) for item in node))
        if not isinstance(node, dict):
            return set()
        own = {node["document_id"]} if is_reusable_resource_local_id(node.get("document_id")) else set()
        return own | set().union(*(cls.local_ids(value) for value in node.values()))

    @classmethod
    def portable_keys(cls, node: Any) -> set[str]:
        if isinstance(node, list):
            return set().union(*(cls.portable_keys(item) for item in node))
        if not isinstance(node, dict):
            return set()
        keys = set()
        if "knowledge_document_key" in node:
            key = node["knowledge_document_key"]
            if not isinstance(key, str) or not key.strip() or "document_id" in node or "path" in node:
                raise WorkflowDependencyError("Workflow Knowledge reference is invalid or conflicts with a file locator")
            keys.add(key)
        return keys | set().union(*(cls.portable_keys(value) for value in node.values()))

    @classmethod
    def to_portable(cls, node: Any, *, document_key_by_id: Mapping[str, str]) -> Any:
        if isinstance(node, list):
            return [cls.to_portable(item, document_key_by_id=document_key_by_id) for item in node]
        if not isinstance(node, dict):
            return deepcopy(node)
        result = {key: cls.to_portable(value, document_key_by_id=document_key_by_id) for key, value in node.items()}
        document_id = node.get("document_id")
        if (
            isinstance(document_id, str)
            and re.search(r"[0-9A-HJKMNP-TV-Z]{26}", document_id)
            and not is_reusable_resource_local_id(document_id)
        ):
            raise WorkflowDependencyError("Workflow Knowledge locator mixes a local ID with runtime text")
        if is_reusable_resource_local_id(document_id):
            key = document_key_by_id.get(document_id)
            if not key:
                raise WorkflowDependencyError("Workflow reads a Knowledge document not included in this Blueprint. Include its safe starter content or remove the fixed file dependency before exporting.")
            if "knowledge_document_key" in node:
                raise WorkflowDependencyError("Workflow Knowledge reference conflicts with a runtime document")
            if "path" in node:
                raise WorkflowDependencyError("Workflow Knowledge dependency must use an exact document_id without a deployment-local path")
            result.pop("document_id")
            result["knowledge_document_key"] = key
        return result

    @classmethod
    def to_runtime(cls, node: Any, *, document_id_by_key: Mapping[str, str]) -> Any:
        cls.portable_keys(node)
        if isinstance(node, list):
            return [cls.to_runtime(item, document_id_by_key=document_id_by_key) for item in node]
        if not isinstance(node, dict):
            return deepcopy(node)
        result = {key: cls.to_runtime(value, document_id_by_key=document_id_by_key) for key, value in node.items()}
        if "knowledge_document_key" in result:
            key = result.pop("knowledge_document_key")
            if key not in document_id_by_key:
                raise WorkflowDependencyError("Workflow Knowledge starter is missing from this installed Workspace")
            result["document_id"] = document_id_by_key[key]
        return result


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
