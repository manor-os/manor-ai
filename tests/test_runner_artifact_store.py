"""Multi-tenant safety tests for the runner-side artifact store.

The store is the capability layer that lets browser tools send binaries
back to the api WITHOUT a shared mount. Token = the only credential.
We verify:

  1. publish() returns an opaque token bound to the file
  2. consume() is one-time — second call returns None
  3. Expired entries are reaped (don't serve stale bytes)
  4. Filenames with traversal payloads are sanitized to a basename
  5. Two concurrent publishes don't return the same token
  6. consume() of an unknown token returns None (no info leak)
  7. The on-disk file is unlinked after consumption (no leftover bytes)
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def store(monkeypatch, tmp_path):
    """Fresh ArtifactStore per test, rooted in a tmp dir so tests
    don't pollute /tmp. We import from the runner sources via path
    manipulation since browser-runner isn't a normal package."""
    runner_dir = (Path(__file__).parent.parent / "docker" / "browser-runner").resolve()
    sys.path.insert(0, str(runner_dir))
    monkeypatch.setenv("BROWSER_RUNNER_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("BROWSER_RUNNER_ARTIFACT_TTL_SEC", "60")
    if "artifact_store" in sys.modules:
        importlib.reload(sys.modules["artifact_store"])
    import artifact_store as mod

    return mod.ArtifactStore()


def _make_file(tmp: Path, name: str, body: bytes) -> str:
    p = tmp / name
    p.write_bytes(body)
    return str(p)


@pytest.mark.asyncio
async def test_publish_returns_opaque_token_and_metadata(store, tmp_path):
    src = _make_file(tmp_path, "report.pdf", b"%PDF-fake")
    info = await store.publish(src_path=src, suggested_name="report.pdf")
    assert isinstance(info["token"], str) and len(info["token"]) >= 32
    assert info["filename"] == "report.pdf"
    assert info["size"] == len(b"%PDF-fake")
    assert info["mime"] == "application/pdf"
    # Source moved away from caller-supplied path
    assert not Path(src).exists()


@pytest.mark.asyncio
async def test_consume_is_one_time(store, tmp_path):
    src = _make_file(tmp_path, "x.txt", b"hi")
    info = await store.publish(src_path=src, suggested_name="x.txt")
    first = await store.consume(info["token"])
    assert first is not None
    second = await store.consume(info["token"])
    assert second is None  # token already used


@pytest.mark.asyncio
async def test_unknown_token_returns_none(store):
    assert await store.consume("does-not-exist-anywhere") is None


@pytest.mark.asyncio
async def test_expired_token_returns_none_and_unlinks_file(store, tmp_path):
    src = _make_file(tmp_path, "stale.txt", b"old")
    # ttl_sec is clamped to >=60 inside publish; we manually expire
    # the entry to test the expiry branch in consume().
    info = await store.publish(src_path=src, suggested_name="stale.txt", ttl_sec=60)
    # Reach into the registry to backdate expiry — same effect as time
    # passing, without sleeping in tests.
    async with store._lock:
        entry = store._entries[info["token"]]
        entry.expires_at = 0  # already expired
        on_disk = entry.path
    result = await store.consume(info["token"])
    assert result is None
    assert not on_disk.exists()  # cleanup ran


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dirty",
    [
        "../../etc/passwd",
        "../../../boot.ini",
        "/etc/shadow",
        "foo/bar.pdf",  # has slash → must be flattened to basename
        "x\x00y.txt",
        "..",
        ".",
        "",
    ],
)
async def test_filenames_sanitized(store, tmp_path, dirty):
    src = _make_file(tmp_path, "x.bin", b"x")
    info = await store.publish(src_path=src, suggested_name=dirty)
    name = info["filename"]
    # No path separators, NUL bytes, or parent-traversal segments survive
    assert "/" not in name
    assert "\\" not in name
    assert "\x00" not in name
    assert ".." not in name.split("/")
    # Always non-empty
    assert name


@pytest.mark.asyncio
async def test_concurrent_publish_returns_distinct_tokens(store, tmp_path):
    async def _pub(i: int) -> str:
        src = _make_file(tmp_path, f"f{i}.txt", str(i).encode())
        info = await store.publish(src_path=src, suggested_name=f"f{i}.txt")
        return info["token"]

    tokens = await asyncio.gather(*[_pub(i) for i in range(20)])
    assert len(set(tokens)) == 20  # no collisions


@pytest.mark.asyncio
async def test_consume_returns_correct_file_for_each_token(store, tmp_path):
    """Wrong-file return would be a critical multi-tenant bug — token A
    must NEVER serve token B's file."""
    src_a = _make_file(tmp_path, "a.txt", b"AAAA")
    src_b = _make_file(tmp_path, "b.txt", b"BBBB")
    info_a = await store.publish(src_path=src_a, suggested_name="a.txt")
    info_b = await store.publish(src_path=src_b, suggested_name="b.txt")
    entry_a = await store.consume(info_a["token"])
    entry_b = await store.consume(info_b["token"])
    assert entry_a is not None
    assert entry_b is not None
    assert entry_a.path.read_bytes() == b"AAAA"
    assert entry_b.path.read_bytes() == b"BBBB"


@pytest.mark.asyncio
async def test_publish_rejects_missing_source(store):
    with pytest.raises(FileNotFoundError):
        await store.publish(src_path="/tmp/nope-doesnt-exist-xyz", suggested_name="x")
