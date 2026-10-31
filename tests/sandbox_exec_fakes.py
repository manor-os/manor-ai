"""Adapt a fake sandbox client that only implements ``exec`` to the exec protocol.

The sandbox tool starts every command with ``start_exec`` (so a full shared
sandbox can queue it) and collects it by execution id. A fake whose ``exec``
returns the finished result (or raises, or blocks) keeps working: its
``start_exec`` runs ``exec`` and answers the terminal record.
"""

from __future__ import annotations

from types import SimpleNamespace

from packages.core.services.sandbox_sdk.capabilities import capability_flags

#: /health of a current runner: every capability (``sandbox_sdk.capabilities``).
CURRENT_RUNNER_HEALTH = {
    "status": "ok",
    "sandbox_image_available": True,
    "skill_layout": "slots",
    "env_injection": "exec",
    "baked_skills": {},
    **capability_flags,
}
#: /health of the runner image staging ran on 2026-10-18 (built 2026-08-17):
#: no /exec/start, no work areas, no per-command Skill view or env.
AUGUST_RUNNER_HEALTH = {
    "status": "ok",
    "build_version": "2026-08-17",
    "sandbox_image": "manor-sandbox:latest",
    "sandbox_image_available": True,
    "active_sandboxes": 0,
    "max_active": 20,
}
#: A September runner: /exec/start and per-exec Skill env, still no work areas.
SEPTEMBER_RUNNER_HEALTH = {**AUGUST_RUNNER_HEALTH, "build_version": "2026-09-18", "env_injection": "exec"}


class ExecViaStart:
    async def start_exec(self, **kwargs):
        result = await self.exec(**kwargs)
        return SimpleNamespace(
            sandbox_id=kwargs.get("sandbox_id"),
            execution_id=kwargs.get("execution_id"),
            status="completed" if getattr(result, "exit_code", 0) == 0 else "failed",
            terminal=True,
            created_at=0.0,
            stdout=getattr(result, "stdout", ""),
            stderr=getattr(result, "stderr", ""),
            exit_code=getattr(result, "exit_code", None),
            error=None,
            outcome=getattr(result, "outcome", None),
            outcome_detail=getattr(result, "outcome_detail", None),
            queue_position=None,
            events=[],
        )

    async def execution_status(self, sandbox_id, execution_id, **_kwargs):  # pragma: no cover - never polled
        raise AssertionError("a finished execution is never polled")
