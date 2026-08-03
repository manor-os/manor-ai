"""The tree-walking file tools must not hold the event loop.

Production incident 2026-07-29: a user asked Manor to "整体搜一下" (search
everything). ``grep_files`` walked a 1.9 GB / 2410-file workspace, opening and
UTF-8 decoding every file — 1.4 GB of it .mp4 — and ran for 528 seconds. It was
declared ``async def`` but never awaited anything during the scan, so it held
the uvicorn worker's event loop for the whole 8m48s. The chat stream's 15s SSE
keepalive could not be emitted, Cloudflare cut the idle connection at ~115s, and
the browser drew "This response did not finish."

The answer had actually completed and been persisted. The user was shown a
failure that never happened, twice (the earlier one ran 228s).

These tests hold the *contract* — other coroutines keep running while a scan is
in flight — rather than the implementation, so a future rewrite that reintroduces
a blocking scan fails here.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time

import pytest

from packages.core.ai.tools import file_tools
from packages.core.ai.tools.file_tools import _glob_files, _grep_files, _list_files
from packages.core.config import get_settings

ENTITY = "01TESTENTITYEVENTLOOP00000"
SCAN_HOLD_SECONDS = 0.6
TICK_SECONDS = 0.02
# The loop should get ~30 ticks over a 0.6s scan. Assert a fraction of that so a
# loaded CI box cannot flake, while a blocked loop (which yields 0-1) still fails.
MIN_TICKS = 5


@pytest.fixture
def fs_root(tmp_path):
    settings = get_settings()
    old_enabled, old_root = settings.MANOR_FS_ENABLED, settings.MANOR_FS_ROOT
    settings.MANOR_FS_ENABLED = True
    settings.MANOR_FS_ROOT = str(tmp_path)
    root = tmp_path / ENTITY
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "keys.md").write_text(
        "ssh-ed25519 AAAAC3Nz test@example.com\nnothing here\n", encoding="utf-8",
    )
    (root / "notes" / "plain.txt").write_text("just prose\n", encoding="utf-8")
    try:
        yield root
    finally:
        settings.MANOR_FS_ENABLED = old_enabled
        settings.MANOR_FS_ROOT = old_root


@pytest.fixture
def slow_walk(monkeypatch):
    """Make the filesystem walk itself slow, with a BLOCKING sleep.

    time.sleep is the point: it is what a real 1.9 GB read/decode does to the
    thread it runs on. If the scan runs on the event loop, nothing else in the
    process makes progress for its duration.
    """
    real_walk = os.walk

    def walking_molasses(*args, **kwargs):
        time.sleep(SCAN_HOLD_SECONDS)
        yield from real_walk(*args, **kwargs)

    monkeypatch.setattr(file_tools.os, "walk", walking_molasses)


async def _count_ticks_during(coro):
    """Run `coro` while ticking on the event loop; return (result, tick count)."""
    ticks = 0
    task = asyncio.ensure_future(coro)
    while not task.done():
        await asyncio.sleep(TICK_SECONDS)
        ticks += 1
    return await task, ticks


@pytest.mark.asyncio
async def test_grep_does_not_freeze_the_event_loop(fs_root, slow_walk):
    """The exact incident: a long content scan must not stop the SSE keepalive."""
    raw, ticks = await _count_ticks_during(
        _grep_files(ENTITY, pattern="ssh-ed25519", max_matches=10)
    )
    body = json.loads(raw)
    assert body.get("count") == 1, body
    assert body["matches"][0]["file"].endswith("keys.md")
    assert ticks >= MIN_TICKS, (
        f"event loop only ran {ticks} times during a {SCAN_HOLD_SECONDS}s scan — "
        "the scan is back on the loop, so the chat keepalive would stall again"
    )


@pytest.mark.asyncio
async def test_glob_does_not_freeze_the_event_loop(fs_root, slow_walk):
    raw, ticks = await _count_ticks_during(_glob_files(ENTITY, pattern="**/*.md"))
    body = json.loads(raw)
    assert body.get("count") == 1, body
    assert ticks >= MIN_TICKS, f"event loop only ran {ticks} times"


@pytest.mark.asyncio
async def test_recursive_list_does_not_freeze_the_event_loop(fs_root, slow_walk):
    raw, ticks = await _count_ticks_during(_list_files(ENTITY, recursive=True))
    body = json.loads(raw)
    assert body.get("count") == 2, body
    assert ticks >= MIN_TICKS, f"event loop only ran {ticks} times"


@pytest.mark.asyncio
async def test_scan_errors_still_surface_as_tool_errors(fs_root, monkeypatch):
    """Moving the scan into a thread must not swallow its failures: the tool
    still answers with the same ``{"error": ...}`` shape the model expects."""
    def explode(*args, **kwargs):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(file_tools.os, "walk", explode)

    for raw in (
        await _grep_files(ENTITY, pattern="x"),
        await _glob_files(ENTITY, pattern="*"),
        await _list_files(ENTITY, recursive=True),
    ):
        assert json.loads(raw).get("error") == "disk went away", raw


@pytest.mark.asyncio
async def test_results_are_unchanged_by_the_offload(fs_root):
    """Same inputs, same output contract — offloading is not a behaviour change."""
    grep = json.loads(await _grep_files(ENTITY, pattern="ssh", max_matches=1))
    assert grep["pattern"] == "ssh"
    assert grep["limit"] == 1
    assert grep["offset"] == 0
    assert grep["has_more"] is False
    assert grep["next_offset"] is None

    glob = json.loads(await _glob_files(ENTITY, pattern="**/*.txt"))
    assert glob["files"] == [os.path.join("notes", "plain.txt")]

    listing = json.loads(await _list_files(ENTITY, path="notes"))
    assert {e["path"] for e in listing["entries"]} == {
        os.path.join("notes", "keys.md"),
        os.path.join("notes", "plain.txt"),
    }
    assert listing["total"] == 2


@pytest.mark.asyncio
async def test_case_insensitive_match_survives(fs_root):
    """The scan compiles with re.IGNORECASE; keep that after the move."""
    body = json.loads(await _grep_files(ENTITY, pattern="SSH-ED25519"))
    assert body["count"] == 1, body
    assert re.search("ssh-ed25519", body["matches"][0]["text"])
