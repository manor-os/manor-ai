"""Server-owned output targets carried by asynchronous media jobs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.services.generated_media_naming import GeneratedMediaOutputPlan


MEDIA_JOB_OUTPUT_CONTRACT_KEY = "_runtime_media_output"


@dataclass(frozen=True)
class MediaJobOutputContract:
    job_id: str
    entity_id: str
    user_id: str | None
    workspace_id: str | None
    task_id: str | None
    plan: GeneratedMediaOutputPlan

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "job_id": self.job_id,
            "entity_id": self.entity_id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "plan": self.plan.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> MediaJobOutputContract:
        keys = {"version", "job_id", "entity_id", "user_id", "workspace_id", "task_id", "plan"}
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or type(value["version"]) is not int
            or value["version"] != 1
        ):
            raise ValueError("Invalid media job output contract")
        for key in ("job_id", "entity_id"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ValueError("Invalid media job output identity")
        for key in ("user_id", "workspace_id", "task_id"):
            if value[key] is not None and (
                not isinstance(value[key], str) or not value[key].strip()
            ):
                raise ValueError("Invalid media job output scope")
        plan = GeneratedMediaOutputPlan.from_dict(value["plan"])
        if plan.kind != "video":
            raise ValueError("Media job output contract must describe a video")
        return cls(
            job_id=value["job_id"],
            entity_id=value["entity_id"],
            user_id=value["user_id"],
            workspace_id=value["workspace_id"],
            task_id=value["task_id"],
            plan=plan,
        )

    def validate_scope(
        self,
        *,
        job_id: str,
        entity_id: str,
        user_id: str | None,
        workspace_id: str | None,
        task_id: str | None,
        artifact_base_dir: str,
    ) -> None:
        if (
            self.job_id != job_id
            or self.entity_id != entity_id
            or self.user_id != (user_id or None)
            or self.workspace_id != (workspace_id or None)
            or self.task_id != (task_id or None)
            or self.plan.artifact_base_dir != artifact_base_dir
        ):
            raise PermissionError("Media job output target does not match its authorized scope")


def public_media_job_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (params or {}).items()
        if key != MEDIA_JOB_OUTPUT_CONTRACT_KEY
    }


def media_job_output_kwargs(params: dict[str, Any]) -> dict[str, object]:
    # Absence identifies legacy jobs. A present null/malformed value must reach
    # the strict reader instead of silently downgrading to the legacy writer.
    return (
        {"output_contract": params[MEDIA_JOB_OUTPUT_CONTRACT_KEY]}
        if MEDIA_JOB_OUTPUT_CONTRACT_KEY in params
        else {}
    )
