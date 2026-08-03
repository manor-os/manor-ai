"""grep_files must not read every byte of a workspace to answer a question.

Production incident: a 1.9 GB / 2410-file workspace was 99.7% binary
(.mp4/.png/.wav). grep_files opened and UTF-8-decoded every one of those bytes
because nothing skipped binaries, and the scan took 8m48s (a second incident
the same day ran 228s). #424 moved that scan off the event loop so it stopped
freezing the chat stream for every user; this fixes the 8m48s itself.

These tests hold four contracts:
  1. Binary files are skipped entirely (not opened for content) and counted.
  2. A single file's read is bounded (defends memory/CPU on an oversized or
     no-newline file) and the truncation is reported, not silently dropped.
  3. The whole scan has a byte budget; hitting it returns `truncated=True` and
     a `resume_cursor` that a follow-up call can use via `after=` to continue
     without rescanning already-covered files.
  4. A stale cursor (file moved/deleted between calls) falls back to a full
     scan instead of silently reporting "no more matches" when it never
     actually looked.
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.tools import file_tools
from packages.core.ai.tools.file_tools import _grep_files
from packages.core.config import get_settings

ENTITY = "01TESTENTITYGREPBOUNDS0000"


@pytest.fixture
def fs_root(tmp_path):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    root = tmp_path / ENTITY
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


def _write(root, rel, data: bytes):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.mark.asyncio
async def test_binary_files_are_skipped_and_counted(fs_root):
    # A real .mp4-shaped file: NUL bytes in the first chunk, and the exact
    # target string sitting further in — a naive scan would still find it.
    binary = b"\x00\x01\x02\x03" + b"\xff" * 100 + b"ssh-ed25519 AAAA fake" + b"\x00" * 100
    _write(fs_root, "video.mp4", binary)
    _write(fs_root, "notes/keys.md", b"ssh-ed25519 AAAA real\n")

    body = json.loads(await _grep_files(ENTITY, pattern="ssh-ed25519"))
    assert body["count"] == 1, body
    assert body["matches"][0]["file"] == "notes/keys.md"
    assert body["files_skipped_binary"] == 1
    assert body["files_scanned"] == 1
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_oversized_file_is_read_partially_and_disclosed(fs_root, monkeypatch):
    monkeypatch.setattr(file_tools, "GREP_MAX_FILE_READ_BYTES", 100)
    content = ("x" * 100) + "NEEDLE" + ("y" * 100)
    _write(fs_root, "big.txt", content.encode())

    body = json.loads(await _grep_files(ENTITY, pattern="NEEDLE"))
    assert body["count"] == 0, "match sits past the per-file read cap"
    assert body["files_truncated"] == 1
    assert body["files_scanned"] == 1
    assert body["bytes_scanned"] == 100


@pytest.mark.asyncio
async def test_line_length_cap_is_reported_via_truncated_file(fs_root, monkeypatch):
    """A single no-newline 'line' longer than the per-line cap must not blow
    up regex cost — the tail past the cap is simply not searched."""
    monkeypatch.setattr(file_tools, "GREP_MAX_LINE_CHARS", 50)
    content = ("a" * 50) + "NEEDLE"  # no newline: one giant "line"
    _write(fs_root, "oneline.txt", content.encode())

    body = json.loads(await _grep_files(ENTITY, pattern="NEEDLE"))
    assert body["count"] == 0, "match sits past the per-line cap"

    # Content within the cap is still found.
    _write(fs_root, "oneline.txt", ("NEEDLE" + "a" * 50).encode())
    body2 = json.loads(await _grep_files(ENTITY, pattern="NEEDLE"))
    assert body2["count"] == 1, body2


@pytest.mark.asyncio
async def test_scan_budget_truncates_and_resume_cursor_continues(fs_root, monkeypatch):
    monkeypatch.setattr(file_tools, "GREP_SCAN_BYTE_BUDGET", 10)
    _write(fs_root, "a.txt", b"NEEDLE in a\n")
    _write(fs_root, "b.txt", b"NEEDLE in b\n")
    _write(fs_root, "c.txt", b"NEEDLE in c\n")

    first = json.loads(await _grep_files(ENTITY, pattern="NEEDLE"))
    assert first["truncated"] is True
    assert first["resume_cursor"], "must hand back where to resume"
    assert first["count"] <= 1, "budget of 10 bytes must not let a second file be read"
    assert "hint" in first, "truncated=true without a hint reads as a confident zero result"
    assert "resume_cursor" in first["hint"]
    seen = {m["file"] for m in first["matches"]}

    covered = set(seen)
    cursor = first["resume_cursor"]
    covered.add(cursor)
    guard = 0
    while True:
        guard += 1
        assert guard < 10, "resume loop did not converge"
        page = json.loads(await _grep_files(ENTITY, pattern="NEEDLE", after=cursor))
        seen |= {m["file"] for m in page["matches"]}
        if not page["truncated"]:
            break
        assert page["resume_cursor"] not in covered, "resume must make forward progress"
        covered.add(page["resume_cursor"])
        cursor = page["resume_cursor"]

    assert seen == {"a.txt", "b.txt", "c.txt"}, seen


@pytest.mark.asyncio
async def test_no_hint_when_scan_completed(fs_root):
    """The hint only costs a response when it's actually needed."""
    _write(fs_root, "a.txt", b"NEEDLE\n")
    body = json.loads(await _grep_files(ENTITY, pattern="NEEDLE"))
    assert body["truncated"] is False
    assert "hint" not in body


@pytest.mark.asyncio
async def test_wall_clock_budget_also_truncates(fs_root, monkeypatch):
    """Byte budget and wall-clock budget are independent OR'd conditions —
    prove the clock one trips too, deterministically (no real sleeping)."""
    _write(fs_root, "a.txt", b"NEEDLE in a\n")
    _write(fs_root, "b.txt", b"NEEDLE in b\n")

    real_monotonic = file_tools.time.monotonic
    calls = {"n": 0}

    def fake_monotonic():
        calls["n"] += 1
        # First call establishes `start`; every call after looks like the
        # budget has already elapsed, without an actual sleep.
        return real_monotonic() if calls["n"] == 1 else real_monotonic() + 999

    monkeypatch.setattr(file_tools.time, "monotonic", fake_monotonic)

    body = json.loads(await _grep_files(ENTITY, pattern="NEEDLE"))
    assert body["truncated"] is True
    assert body["resume_cursor"] is None, "budget must trip before the first file is scanned"
    assert body["count"] == 0


@pytest.mark.asyncio
async def test_stale_cursor_falls_back_to_a_full_scan(fs_root):
    """The file named in `after` no longer exists (renamed/deleted between
    calls) — replay never finds it, so we must rescan from the start rather
    than silently report zero further matches."""
    _write(fs_root, "only.txt", b"NEEDLE here\n")

    body = json.loads(await _grep_files(ENTITY, pattern="NEEDLE", after="deleted_file.txt"))
    assert body["count"] == 1, body
    assert body["matches"][0]["file"] == "only.txt"


@pytest.mark.asyncio
async def test_case_insensitive_and_result_shape_survive_the_rewrite(fs_root):
    _write(fs_root, "keys.md", b"SSH-ED25519 upper\n")
    body = json.loads(await _grep_files(ENTITY, pattern="ssh-ed25519", max_matches=5))
    assert body["count"] == 1, body
    for key in (
        "pattern", "count", "limit", "offset", "next_offset", "has_more", "matches",
        "files_scanned", "files_skipped_binary", "files_truncated",
        "bytes_scanned", "truncated", "resume_cursor",
    ):
        assert key in body, f"missing {key}"
    assert body["resume_cursor"] is None
    assert body["truncated"] is False


@pytest.mark.asyncio
async def test_errors_still_surface_in_the_original_shape(fs_root, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(file_tools.os, "walk", explode)
    body = json.loads(await _grep_files(ENTITY, pattern="x"))
    assert body.get("error") == "disk went away", body
