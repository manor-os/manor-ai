"""Knowledge visibility policy tests."""

from packages.core.services.knowledge_visibility import (
    is_storage_only_path,
    is_user_visible_folder_path,
    is_user_visible_path,
)
from apps.api.routers.filesystem import _is_public_raw_file_path


def test_visible_user_files_and_folders():
    assert is_user_visible_path("融资/Manor AI 融资介绍.md")
    assert is_user_visible_path("docs/brief.md")
    assert is_user_visible_path("report.pdf")


def test_runtime_and_internal_paths_are_hidden():
    assert not is_user_visible_path("uploads/report.pdf")
    assert not is_user_visible_path("tasks/run/result.json")
    assert not is_user_visible_path("sandbox-output/report.pdf")


def test_storage_only_media_files_are_documents_but_not_folders():
    assert is_user_visible_path("images/generated.png")
    assert is_user_visible_path("videos/generated.mp4")
    assert is_storage_only_path("images/generated.png")
    assert not is_user_visible_folder_path("images")
    assert not is_user_visible_folder_path("videos")


def test_hidden_system_and_memory_paths():
    assert not is_user_visible_path("MANOR.md")
    assert not is_user_visible_path("index.md")
    assert not is_user_visible_path("log.md")
    assert not is_user_visible_path(".ai/workspaces/ws_123/memory/facts/company.md")
    assert not is_user_visible_path(".ai/agents/agent_123/memory/facts/user.md")
    assert not is_user_visible_path("docs/.ai/private.md")
    assert not is_user_visible_path("docs/.cache/result.json")


def test_raw_file_serving_allows_only_public_or_visible_paths():
    assert _is_public_raw_file_path("avatars/user.png")
    assert not _is_public_raw_file_path("docs/report.pdf")
    assert not _is_public_raw_file_path("images/generated.png")
    assert not _is_public_raw_file_path("uploads/chat/reference.png")
    assert not _is_public_raw_file_path("uploads/private.png")
    assert not _is_public_raw_file_path("sandbox-output/result.pdf")
    assert not _is_public_raw_file_path(".ai/workspaces/ws_123/memory/facts/company.md")
