"""A missing local patch target is no write, never an ambiguous provider error."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime.tool_results import RuntimeNoEffectReason, RuntimeNoEffectToolResult


def _missing(path="missing.md"):
    return RuntimeNoEffectToolResult.patch_target_not_found(json.dumps({"error": f"File not found: {path}"}))


def test_missing_target_proof_is_exact_and_cannot_be_minted_by_json():
    proof = _missing()
    assert proof.matches_call("patch_file", {"path": "missing.md"})
    payload = json.loads(proof)
    assert payload["contract"] == "tool_error"
    assert payload["error"] == "File not found: missing.md"
    assert "hint" not in payload and "recovery_hint" not in payload
    assert not proof.matches_call("patch_file", {"path": "other.md"})
    assert not proof.matches_call("mcp__provider__patch_file", {"path": "missing.md"})
    assert not proof.matches_call("generate_file", {"kind": "document", "path": "missing.md"})
    assert not isinstance(json.loads(json.dumps(proof)), RuntimeNoEffectToolResult)
    assert not RuntimeNoEffectToolResult.patch_target_not_found('{"error":"write failed"}').matches_call("patch_file", {"path": "missing.md"})


def test_missing_target_diagnostics_preserve_candidates_without_selecting_a_repair():
    proof = RuntimeNoEffectToolResult.patch_target_not_found(json.dumps({
        "error": "File not found: missing.md",
        "candidates": ["documents/missing.md", "archive/missing.md"],
        "hint": "Retry one of the candidates",
        "recovery_hint": "Create a new file",
    }))
    assert json.loads(proof)["details"] == {
        "candidates": ["documents/missing.md", "archive/missing.md"],
    }
    assert proof.matches_call("patch_file", {"path": "missing.md"})
    assert not proof.matches_call("patch_file", {"path": "documents/missing.md"})


@pytest.mark.asyncio
async def test_missing_patch_target_stops_under_lock_before_mutation_admission(tmp_path, monkeypatch):
    from packages.core.ai.tools import file_tools

    locked = False

    @asynccontextmanager
    async def lock(root):
        nonlocal locked
        assert root == str(tmp_path)
        locked = True
        try:
            yield
        finally:
            locked = False

    async def locate(**kwargs):
        assert locked
        return None, []

    forbidden = AsyncMock(side_effect=AssertionError("missing target must not cross write admission"))
    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _: str(tmp_path))
    monkeypatch.setattr(file_tools, "runtime_entity_filesystem_mutation_lock", lock)
    monkeypatch.setattr(file_tools, "_locate_entity_file", locate)
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", forbidden)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", forbidden)
    monkeypatch.setattr(file_tools, "_commit_file_projection", forbidden)
    result = await file_tools._patch_file("entity", path="missing.md", operations=[{"op": "text.set", "text": "new"}])
    assert isinstance(result, RuntimeNoEffectToolResult)
    assert result.reason is RuntimeNoEffectReason.PATCH_TARGET_NOT_FOUND
    assert result.matches_call("patch_file", {"path": "missing.md"})
    assert list(tmp_path.iterdir()) == []
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
async def test_postcommit_patch_exception_is_not_no_effect(tmp_path, monkeypatch):
    from packages.core.ai.tools import file_tools

    target = tmp_path / "existing.md"
    target.write_text("before", encoding="utf-8")

    async def fail_after_write(**kwargs):
        target.write_bytes(kwargs["data"])
        raise OSError("projection failed after bytes were written")

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _: str(tmp_path))
    monkeypatch.setattr(file_tools, "_locate_entity_file", AsyncMock(return_value=(str(target), [])))
    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", AsyncMock(return_value=None))
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", AsyncMock(return_value=None))
    monkeypatch.setattr(file_tools, "_commit_file_projection", fail_after_write)
    result = await file_tools._patch_file_locked("entity", path="existing.md", operations=[{"op": "text.set", "text": "after"}])
    assert target.read_text(encoding="utf-8") == "after"
    assert json.loads(result)["error"] == "projection failed after bytes were written"
    assert not isinstance(result, RuntimeNoEffectToolResult)
