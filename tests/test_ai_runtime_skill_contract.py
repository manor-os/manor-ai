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
    contexts: list[dict[str, object]] = []

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            calls.append({"handler": "init", "base_url": base_url, "timeout": timeout})

        async def health(self):
            calls.append({"handler": "health"})
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_builtin(self, **kwargs):
            calls.append({"handler": "create_from_builtin", **kwargs})
            return _fake_create_result("sb_builtin", scripts=["scripts/run.py"])

        async def load_skill(self, **_kwargs):
            raise AssertionError("a foreign owner's sandbox must not be reused")

        async def create_from_files(self, **kwargs):
            raise AssertionError("built-in skills must use create_from_builtin")

        async def close(self):
            calls.append({"handler": "close"})

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)

    async def load_foreign_context(_conversation_id):
        return {
            "sandbox_id": "sb_foreign",
            "skill_id": "external-style-skill",
            "entity_id": "ent_1",
            "user_id": "other-user",
        }

    monkeypatch.setattr(
        skill_service,
        "runtime_load_sandbox_context",
        load_foreign_context,
    )

    async def record_sandbox_context(
        conversation_id,
        sandbox_id,
        skill_id,
        **kwargs,
    ):
        contexts.append(
            {
                "conversation_id": conversation_id,
                "sandbox_id": sandbox_id,
                "skill_id": skill_id,
                **kwargs,
            }
        )

    monkeypatch.setattr(
        skill_service,
        "runtime_init_sandbox_context",
        record_sandbox_context,
    )

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
            conversation_id="conversation_1",
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
    assert '`artifact_role="final"` so Chat receives a clickable file card' in content
    assert '`artifact_role="intermediate"` for supporting files' in content
    assert content.index("## Next Tool Guidance") < content.index("## Skill Instructions")
    assert "continue from `next_offset`" in content
    assert contexts == [
        {
            "conversation_id": "conversation_1",
            "sandbox_id": "sb_builtin",
            "skill_id": "skill_external",
            "entity_id": "ent_1",
            "user_id": "user_1",
            "agent_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_sandbox_skill_destroys_new_instance_when_context_admission_fails(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.services.sandbox_sdk as sandbox_sdk
    import packages.core.services.skill_service as skill_service
    from packages.core.config import get_settings

    skill_dir = tmp_path / "context_failure_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Context failure", encoding="utf-8")
    (skill_dir / "run.py").write_text("print('ok')", encoding="utf-8")
    destroyed: list[str] = []

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            pass

        async def health(self):
            return {"status": "ok", "sandbox_image_available": True}

        async def create_from_builtin(self, **_kwargs):
            return _fake_create_result("sb_unadmitted", scripts=["run.py"])

        async def destroy(self, sandbox_id):
            destroyed.append(sandbox_id)

        async def close(self):
            return None

    async def fail_context(*_args, **_kwargs):
        raise RuntimeError("Sandbox owner context could not be persisted.")

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(skill_service, "runtime_init_sandbox_context", fail_context)

    settings = get_settings()
    old_url = settings.SANDBOX_SERVICE_URL
    settings.SANDBOX_SERVICE_URL = "http://sandbox-service"
    try:
        result = await skill_service._invoke_sandbox_skill(
            SimpleNamespace(
                id="skill_context_failure",
                entity_id=None,
                name="context-failure",
                slug="context-failure",
                system_prompt="# Context failure",
                config={"source": "builtin", "skill_dir": str(skill_dir)},
            ),
            entity_id="ent_1",
            user_id="user_1",
            agent_id="agent_1",
            input_text="run",
            conversation_id="conversation_1",
        )
    finally:
        settings.SANDBOX_SERVICE_URL = old_url

    assert result["stop_reason"] == "error"
    assert "owner context" in result["error"]
    assert destroyed == ["sb_unadmitted"]


@pytest.mark.asyncio
async def test_consumed_sandbox_is_not_destroyed_when_context_refresh_fails(
    monkeypatch,
    tmp_path,
) -> None:
    import packages.core.services.sandbox_queue_service as sandbox_queue_service
    import packages.core.services.sandbox_sdk as sandbox_sdk
    import packages.core.services.skill_service as skill_service
    from packages.core.config import get_settings

    skill_dir = tmp_path / "consumed_context_failure_skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Context failure", encoding="utf-8")
    (skill_dir / "run.py").write_text("print('ok')", encoding="utf-8")

    reservation = SimpleNamespace(
        status="consumed",
        sandbox_id="sb_consumed",
    )
    commits: list[bool] = []

    class FakeResult:
        def scalar_one_or_none(self):
            return reservation

    class FakeDatabase:
        async def execute(self, _statement):
            return FakeResult()

        async def commit(self):
            commits.append(True)

    async def resolve_runner(_db, _sandbox_id):
        return SimpleNamespace(base_url="http://sandbox-runner")

    async def fail_context(*_args, **_kwargs):
        raise RuntimeError("Sandbox owner context could not be persisted.")

    class UnexpectedSandboxClient:
        def __init__(self, **_kwargs):
            raise AssertionError("a consumed sandbox must not be destroyed on refresh failure")

    monkeypatch.setattr(sandbox_queue_service, "resolve_sandbox_runner", resolve_runner)
    monkeypatch.setattr(skill_service, "runtime_init_sandbox_context", fail_context)
    monkeypatch.setattr(sandbox_sdk, "SandboxClient", UnexpectedSandboxClient)

    settings = get_settings()
    previous = (
        settings.MANOR_RUNTIME_EXECUTION_MODE,
        settings.SANDBOX_COORDINATION_MODE,
        settings.SANDBOX_RUNNERS_JSON,
    )
    settings.MANOR_RUNTIME_EXECUTION_MODE = "durable"
    settings.SANDBOX_COORDINATION_MODE = "external-runner"
    settings.SANDBOX_RUNNERS_JSON = '[{"base_url":"http://sandbox-runner"}]'
    try:
        result = await skill_service._invoke_sandbox_skill(
            SimpleNamespace(
                id="skill_context_failure",
                entity_id=None,
                name="context-failure",
                slug="context-failure",
                system_prompt="# Context failure",
                config={"source": "builtin", "skill_dir": str(skill_dir)},
            ),
            entity_id="ent_1",
            user_id="user_1",
            agent_id="agent_1",
            input_text="run",
            conversation_id="conversation_1",
            runtime_tool_context={
                "_runtime_run_id_from_context": "run_1",
                "_runtime_tool_call_id_from_context": "tool_1",
            },
            db=FakeDatabase(),
        )
    finally:
        (
            settings.MANOR_RUNTIME_EXECUTION_MODE,
            settings.SANDBOX_COORDINATION_MODE,
            settings.SANDBOX_RUNNERS_JSON,
        ) = previous

    assert result["stop_reason"] == "error"
    assert "owner context" in result["error"]
    assert commits == []


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
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
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
