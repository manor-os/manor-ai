from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_sandbox_create_returns_retry_message_when_capacity_is_full(monkeypatch) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    class CapacityError(Exception):
        status_code = 429

    class FakeSandboxClient:
        async def create_from_files(self, **_kwargs):
            raise CapacityError("Max active sandbox limit reached (2)")

        async def close(self):
            return None

    monkeypatch.setattr(
        sandbox_tools,
        "_collect_skill_files_from_minio",
        lambda _skill_id, _entity_id: {"SKILL.md": "# Capacity Skill\n"},
    )
    monkeypatch.setattr(sandbox_tools, "_load_skill_credentials", lambda _skill_id, _entity_id: {})
    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())

    result = await sandbox_tools._sandbox_create(
        entity_id="entity-1",
        skill_id="capacity-skill",
        conversation_id="conversation-1",
    )

    assert "Sandbox capacity is full" in result
    assert "Please retry later" in result
    assert "Max active sandbox limit reached" in result


@pytest.mark.asyncio
async def test_sandbox_create_does_not_bind_entity_fs_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    calls: dict[str, object] = {}

    class FakeSandboxClient:
        async def create_from_files(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                sandbox_id="sandbox_1",
                container_name="skill-sbx-sandbox_1",
                status="running",
                workdir="/skill",
                env_blocked=[],
                skill=SimpleNamespace(
                    entry_hint=None,
                    scripts=[],
                    requirements_txt=None,
                ),
            )

        async def close(self):
            return None

    entity_root = tmp_path / "entity_1"
    entity_root.mkdir()
    monkeypatch.setenv("MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setenv("MANOR_FS_ENABLED", "true")
    monkeypatch.setattr(
        sandbox_tools,
        "_collect_skill_files_from_minio",
        lambda _skill_id, _entity_id: {"SKILL.md": "# Stateless Skill\n"},
    )
    monkeypatch.setattr(sandbox_tools, "_load_skill_credentials", lambda _skill_id, _entity_id: {})
    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())

    result = await sandbox_tools._sandbox_create(
        entity_id="entity_1",
        skill_id="stateless-skill",
    )

    assert calls["config"] is None
    assert "workspace_mount: (none" in result
    assert "read-only entity filesystem" not in result


@pytest.mark.asyncio
async def test_sandbox_write_file_injects_entity_fs_bytes_with_base64(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.ai.tools.sandbox_tools as sandbox_tools
    from packages.core.config import get_settings

    settings = get_settings()
    old_enabled = settings.MANOR_FS_ENABLED
    old_root = settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    entity_dir = tmp_path / "entity_1" / "uploads" / "chat"
    entity_dir.mkdir(parents=True)
    (entity_dir / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n")
    calls: list[dict[str, object]] = []

    class FakeSandboxClient:
        async def write_file_base64(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(path="/skill/input.png", written=True)

        async def close(self):
            return None

    monkeypatch.setattr(sandbox_tools, "_get_client", lambda: FakeSandboxClient())
    try:
        result = await sandbox_tools._sandbox_write_file(
            entity_id="entity_1",
            sandbox_id="sandbox_1",
            path="/skill/input.png",
            workspace_path="/workspace/uploads/chat/image.bin",
        )
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root

    assert "Written to /skill/input.png" in result
    assert calls == [
        {
            "sandbox_id": "sandbox_1",
            "path": "/skill/input.png",
            "content_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii"),
            "mkdir": True,
        }
    ]
