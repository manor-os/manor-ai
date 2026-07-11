from __future__ import annotations

from types import SimpleNamespace

import pytest


def _fake_create_result(
    sandbox_id: str,
    *,
    scripts: list[str] | None = None,
    requirements_txt: str | None = None,
    entry_hint: str | None = None,
    env_blocked: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        container_name=f"skill-sbx-{sandbox_id}",
        status="running",
        workdir="/skill",
        env_blocked=env_blocked or [],
        skill=SimpleNamespace(
            entry_hint=entry_hint,
            scripts=scripts or [],
            requirements_txt=requirements_txt,
        ),
    )


@pytest.mark.asyncio
async def test_builtin_sandbox_skill_contract_embeds_complete_external_skill_md(
    monkeypatch,
    tmp_path,
) -> None:
    """Regression guard for imported/built-in skills with arbitrary SKILL.md shape."""

    import packages.core.services.sandbox_sdk as sandbox_sdk
    import packages.core.services.skill_service as skill_service
    from packages.core.config import get_settings

    skill_dir = tmp_path / "external_style_skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    skill_md = "\n".join(
        [
            "---",
            "name: external-style-skill",
            "description: imported skill without Manor-specific headings",
            "---",
            "",
            "# External Style Skill",
            "",
            "This SKILL.md intentionally does not contain a Manor-specific contract heading.",
            "",
            "### Operating Notes",
            "- Run the package workflow as written.",
            "- Do not replace this with an ad-hoc one-file generator.",
            "",
            "### Evidence",
            "- UNIQUE-BEGIN-3d2e1a",
            "- Preserve every line in this section.",
            "- UNIQUE-END-9f8c7b",
        ]
    )
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    (skill_dir / "references" / "checklist.md").write_text("checklist", encoding="utf-8")

    calls: list[dict[str, object]] = []

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float):
            calls.append({"handler": "init", "base_url": base_url, "timeout": timeout})

        async def health(self):
            calls.append({"handler": "health"})
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_builtin(self, **kwargs):
            calls.append({"handler": "create_from_builtin", **kwargs})
            return _fake_create_result("sb_builtin", scripts=["scripts/run.py"])

        async def create_from_files(self, **kwargs):
            raise AssertionError("built-in skills must use create_from_builtin")

        async def close(self):
            calls.append({"handler": "close"})

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)

    settings = get_settings()
    old_url = settings.SANDBOX_SERVICE_URL
    settings.SANDBOX_SERVICE_URL = "http://sandbox-service"
    try:
        result = await skill_service._invoke_sandbox_skill(
            SimpleNamespace(
                id="skill_external",
                entity_id=None,
                name="external-style-skill",
                slug="external-style-skill",
                system_prompt="stale DB copy should be replaced by disk SKILL.md",
                config={"source": "builtin", "skill_dir": str(skill_dir)},
            ),
            entity_id="ent_1",
            user_id="user_1",
            input_text="Run the imported skill.",
        )
    finally:
        settings.SANDBOX_SERVICE_URL = old_url

    create_call = next(call for call in calls if call["handler"] == "create_from_builtin")
    content = result["content"]

    assert result["stop_reason"] == "sandbox_ready"
    assert create_call["files"]["SKILL.md"] == skill_md
    assert create_call["files"]["scripts/run.py"] == "print('ok')"
    assert create_call["files"]["references/checklist.md"] == "checklist"
    assert "## Runtime Skill Execution Contract" in content
    assert "## Skill Instructions" in content
    assert skill_md in content
    assert "UNIQUE-BEGIN-3d2e1a" in content
    assert "UNIQUE-END-9f8c7b" in content
    assert "stale DB copy should be replaced" not in content
    assert "## Manor Runtime Harness Contract" not in content
    assert "PPTX" not in content
    assert "PowerPoint" not in content
    assert "SVG" not in content
    assert "ad-hoc generator" in content


@pytest.mark.asyncio
async def test_entity_sandbox_skill_contract_uses_minio_bundle_and_credentials(
    monkeypatch,
) -> None:
    """Entity skills must use their stored bundle, not a DB-only prompt fallback."""

    import packages.core.services.sandbox_sdk as sandbox_sdk
    import packages.core.services.skill_file_storage as skill_file_storage
    import packages.core.services.skill_service as skill_service
    from packages.core.config import get_settings

    skill_md = "\n".join(
        [
            "# Entity Runtime Smoke",
            "",
            "Follow this skill's own files and scripts.",
            "",
            "TAIL-MARKER-48c87d",
        ]
    )
    script_body = "from pathlib import Path\nPath('/tmp/entity-smoke.txt').write_text('ok')\n"

    monkeypatch.setattr(
        skill_file_storage,
        "load_skill_prompt",
        lambda entity_id, skill_id, *, skill_dir=None, config=None: skill_md,
    )
    monkeypatch.setattr(
        skill_file_storage,
        "load_skill_scripts",
        lambda entity_id, skill_id, *, skill_dir=None, config=None: {"scripts/main.py": script_body},
    )
    monkeypatch.setattr(
        skill_file_storage,
        "load_skill_requirements",
        lambda entity_id, skill_id, *, skill_dir=None, config=None: "requests==2.32.3\n",
    )
    monkeypatch.setattr(
        skill_file_storage,
        "load_skill_extra_files",
        lambda entity_id, skill_id, *, skill_dir=None, config=None: {
            "references/checklist.md": "check the output file",
        },
    )
    monkeypatch.setattr(
        skill_file_storage,
        "load_skill_credentials",
        lambda entity_id, skill_id, *, skill_dir=None, config=None: {"SMOKE_TOKEN": "secret"},
    )

    calls: list[dict[str, object]] = []

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float):
            calls.append({"handler": "init", "base_url": base_url, "timeout": timeout})

        async def health(self):
            calls.append({"handler": "health"})
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_builtin(self, **kwargs):
            raise AssertionError("entity-owned skills must use create_from_files")

        async def create_from_files(self, **kwargs):
            calls.append({"handler": "create_from_files", **kwargs})
            return _fake_create_result(
                "sb_entity",
                scripts=["scripts/main.py"],
                requirements_txt="requests==2.32.3\n",
            )

        async def close(self):
            calls.append({"handler": "close"})

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)

    settings = get_settings()
    old_url = settings.SANDBOX_SERVICE_URL
    settings.SANDBOX_SERVICE_URL = "http://sandbox-service"
    try:
        result = await skill_service._invoke_sandbox_skill(
            SimpleNamespace(
                id="skill_entity",
                entity_id="ent_owner",
                name="entity-runtime-smoke",
                slug="entity-runtime-smoke",
                system_prompt="stale db prompt",
                config={"type": "sandbox", "minio_dir": "skills/entity-runtime-smoke"},
            ),
            entity_id="ent_owner",
            user_id="user_1",
            input_text="Use the entity skill.",
        )
    finally:
        settings.SANDBOX_SERVICE_URL = old_url

    create_call = next(call for call in calls if call["handler"] == "create_from_files")
    files = create_call["files"]
    content = result["content"]

    assert result["stop_reason"] == "sandbox_ready"
    assert create_call["skill_name"] == "entity-runtime-smoke"
    assert files["SKILL.md"] == skill_md
    assert files["scripts/main.py"] == script_body
    assert files["requirements.txt"] == "requests==2.32.3\n"
    assert files["references/checklist.md"] == "check the output file"
    assert create_call["env"] == {"SMOKE_TOKEN": "secret"}
    assert create_call["allowed_sensitive_keys"] == ["SMOKE_TOKEN"]
    assert "## Skill Instructions" in content
    assert skill_md in content
    assert "TAIL-MARKER-48c87d" in content
    assert "credentials_injected (1): SMOKE_TOKEN" in content
    assert "dependencies: installed from requirements.txt" in content
