"""Bundle content must survive AI review and ordinary AI edits."""

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.runtime.skills import (
    runtime_skill_patch_current_definition,
    runtime_skill_review_prompt,
)
from packages.core.services import skill_generator


def test_review_sees_complete_bundle_content():
    spec = {
        "name": "report",
        "system_prompt": "Read references/rules.md and run scripts/build.py",
        "scripts": {"build.py": "print('source script sentinel')"},
        "references": {"rules.md": "Full source reference sentinel"},
    }
    prompt = runtime_skill_review_prompt(spec)
    assert "source script sentinel" in prompt
    assert "Full source reference sentinel" in prompt


def _existing():
    return SimpleNamespace(
        name="report", slug="report", display_name="Report", description="report",
        system_prompt="Read references/rules.md", tools=["sandbox"],
        input_schema={}, output_format="text", category=None, tags=[], version="1.0.0",
        config={
            "type": "sandbox", "minio_dir": "private-directory",
            "credentials": {"secret": "must-not-enter-prompt"},
            "scripts": {"build.py": "original script"},
            "extra_files": {"references/rules.md": "source rule", "references/keep.md": "keep"},
            "requirements": "example==1.0",
        },
    )


def test_patch_sees_bundle_content_without_storage_or_credentials():
    serialized = runtime_skill_patch_current_definition(_existing())
    assert "original script" in serialized
    assert "source rule" in serialized
    assert "example==1.0" in serialized
    assert "must-not-enter-prompt" not in serialized
    assert "private-directory" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [{}, {"rules.md": "revised rule"}])
async def test_review_omission_is_not_file_deletion(monkeypatch, returned):
    spec = {"system_prompt": "Read rules", "references": {"rules.md": "rule", "keep.md": "keep"}}

    async def review(*args, **kwargs):
        return SimpleNamespace(content=json.dumps({"system_prompt": "Read improved rules", "references": returned}))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_review_completion", review)
    draft = await skill_generator._review_skill_spec(spec, entity_id="test")
    assert draft.spec["references"] == {**spec["references"], **returned}


@pytest.mark.asyncio
@pytest.mark.parametrize("returned", [
    {"references": {"rules.md": "revised rule"}},
    {"config": {"extra_files": {"references/rules.md": "revised rule"}}},
])
async def test_ai_edit_updates_file_without_dropping_other_bundle_content(monkeypatch, returned):
    async def patch(*args, **kwargs):
        return SimpleNamespace(content=json.dumps(returned))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_patch_completion", patch)
    existing = _existing()
    result = await skill_generator.build_skill_update_patch(existing, "Improve rule", "test")
    assert result["config"]["extra_files"] == {
        "references/rules.md": "revised rule", "references/keep.md": "keep",
    }
    assert result["config"]["scripts"] == existing.config["scripts"]
    assert result["config"]["credentials"] == existing.config["credentials"]
    assert result["config"]["extra_file_paths"] == [
        "references/keep.md", "references/rules.md", "scripts/build.py",
    ]
    assert result["version"] == "1.0.1"


@pytest.mark.asyncio
async def test_prompt_only_ai_edit_does_not_replace_config(monkeypatch):
    async def patch(*args, **kwargs):
        return SimpleNamespace(content=json.dumps({"system_prompt": "Improved instructions"}))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_patch_completion", patch)
    result = await skill_generator.build_skill_update_patch(_existing(), "Improve instructions", "test")
    assert "config" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("script_path", ["scripts/build.py", "/scripts/build.py", "scripts\\build.py"])
async def test_ai_edit_script_paths_match_storage(monkeypatch, script_path):
    existing = _existing()
    existing.config["scripts"] = {script_path: "original script"}

    async def patch(*args, **kwargs):
        return SimpleNamespace(content=json.dumps({"scripts": {script_path: "new script"}}))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_patch_completion", patch)
    result = await skill_generator.build_skill_update_patch(existing, "Improve script", "test")
    assert result["config"]["extra_file_paths"] == [
        "references/keep.md", "references/rules.md", "scripts/build.py",
    ]


@pytest.mark.asyncio
async def test_ai_edit_combines_config_files_and_reference_patch(monkeypatch):
    returned = {
        "config": {"extra_files": {"references/from-config.md": "config body"}, "complexity": "worker"},
        "references": {"from-top-level.md": "reference body"},
    }

    async def patch(*args, **kwargs):
        return SimpleNamespace(content=json.dumps(returned))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_patch_completion", patch)
    existing = _existing()
    result = await skill_generator.build_skill_update_patch(existing, "Add rules", "test")
    assert result["config"]["extra_files"] == {
        **existing.config["extra_files"], "references/from-config.md": "config body",
        "references/from-top-level.md": "reference body",
    }
    assert result["config"]["complexity"] == "worker"
    assert result["config"]["type"] == "sandbox"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_body", [None, 42, ["body"], {"content": "body"}])
@pytest.mark.parametrize("shape", ["references", "scripts", "config"])
async def test_ai_edit_rejects_non_text_file_bodies(monkeypatch, invalid_body, shape):
    returned = ({"config": {"extra_files": {"references/rules.md": invalid_body}}}
                if shape == "config" else {shape: {"file": invalid_body}})

    async def patch(*args, **kwargs):
        return SimpleNamespace(content=json.dumps(returned))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_patch_completion", patch)
    with pytest.raises(ValueError, match="full text bodies"):
        await skill_generator.build_skill_update_patch(_existing(), "Change rules", "test")


@pytest.mark.asyncio
async def test_ai_edit_bundle_is_persisted_and_readable(client, monkeypatch):
    from auth_helpers import register_user_and_get_token

    registered = await register_user_and_get_token(client, json={
        "username": "bundle-editor", "email": "bundle-editor@test.com", "password": "TestPassword123!",
    })
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = await client.post("/api/v1/skills", headers=headers, json={
        "name": "Bundle edit example", "system_prompt": "Read references/rules.md",
        "type": "sandbox", "scripts": {"build.py": "print('keep')"},
        "extra_files": {"references/rules.md": "original", "references/keep.md": "keep"},
    })
    assert created.status_code == 201
    skill_id = created.json()["id"]

    async def patch(*args, **kwargs):
        return SimpleNamespace(content=json.dumps({"references": {"rules.md": "revised body"}}))

    monkeypatch.setattr(skill_generator, "runtime_execute_skill_patch_completion", patch)
    updated = await client.post(f"/api/v1/skills/{skill_id}/ai-update", headers=headers,
                                json={"prompt": "Improve the rules"})
    assert updated.status_code == 200
    assert updated.json()["version"] == "1.0.1"
    files = await client.get(f"/api/v1/skills/{skill_id}/files", headers=headers)
    assert files.status_code == 200
    assert files.json()["files"] == {
        "SKILL.md": "Read references/rules.md", "scripts/build.py": "print('keep')",
        "references/rules.md": "revised body", "references/keep.md": "keep",
    }
