from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_sandbox_skill_reports_capacity_as_retryable_error(monkeypatch, tmp_path) -> None:
    import packages.core.services.sandbox_sdk as sandbox_sdk
    import packages.core.services.skill_service as skill_service
    from packages.core.config import get_settings
    from packages.core.services.sandbox_sdk.exceptions import SandboxCapacityError

    skill_dir = tmp_path / "capacity_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Capacity skill", encoding="utf-8")
    (skill_dir / "run.py").write_text("print('ok')", encoding="utf-8")

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {
                "status": "ok",
                "sandbox_image": "sandbox-skill:test",
                "sandbox_image_available": True,
            }

        async def create_from_builtin(self, **_kwargs):
            raise SandboxCapacityError("Max active sandbox limit reached (2)", status_code=429)

        async def close(self):
            return None

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)

    settings = get_settings()
    old_url = settings.SANDBOX_SERVICE_URL
    settings.SANDBOX_SERVICE_URL = "http://sandbox-service"
    try:
        result = await skill_service._invoke_sandbox_skill(
            SimpleNamespace(
                id="skill_capacity",
                entity_id=None,
                name="capacity",
                slug="capacity",
                system_prompt="# Capacity skill",
                config={"source": "builtin", "skill_dir": str(skill_dir)},
            ),
            entity_id="ent_1",
            user_id="user_1",
            input_text="run",
        )
    finally:
        settings.SANDBOX_SERVICE_URL = old_url

    assert result["stop_reason"] == "error"
    assert "Sandbox resources are busy" in result["error"]
    assert "Please retry later" in result["error"]
    assert "Max active sandbox limit reached" in result["error"]
