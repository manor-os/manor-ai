import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "packages/core/ai/skills/pptx/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import import_system_image as isi  # noqa: E402


def _write(path: Path, data: bytes = b"img") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_resolve_source_exact_path(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "generated/images/cover.png")

    found = isi.resolve_source(ws, "generated/images/cover.png", retries=1)

    assert found == (ws / "generated/images/cover.png").resolve()


def test_resolve_source_falls_back_to_basename_search(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    # Tool reported one prefix, but the bytes actually landed under another dir.
    _write(ws / "media/2026/cover.png")

    found = isi.resolve_source(ws, "generated/images/cover.png", retries=1)

    assert found == (ws / "media/2026/cover.png").resolve()


def test_resolve_source_prefers_newest_match(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    old = _write(ws / "a/cover.png")
    new = _write(ws / "b/cover.png")
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    found = isi.resolve_source(ws, "x/cover.png", retries=1)

    assert found == new.resolve()


def test_resolve_source_ignores_empty_file(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    _write(ws / "generated/cover.png", b"")  # zero-byte, must be ignored

    with pytest.raises(FileNotFoundError):
        isi.resolve_source(ws, "generated/cover.png", retries=2, retry_delay=0)


def test_resolve_source_raises_with_actionable_message(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()

    with pytest.raises(FileNotFoundError) as excinfo:
        isi.resolve_source(ws, "generated/missing.png", retries=2, retry_delay=0)

    msg = str(excinfo.value)
    assert "missing.png" in msg
    assert "Needs-Manual" in msg  # tells the agent to stop, not derail


def test_search_workspace_by_name_returns_none_when_absent(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert isi.search_workspace_by_name(ws, "nope.png") is None


def test_workspace_relative_from_api_url(tmp_path: Path) -> None:
    rel = isi.workspace_relative_from_reference("/api/v1/fs/ent123/generated/cover.png")
    assert rel == "generated/cover.png"
