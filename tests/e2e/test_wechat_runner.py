"""E2E for the WeChat ClawBot runner sidecar (multi-session iLink).

Hits the runner's HTTP surface directly:

  * health probe
  * spawn a session, expect a fresh QR PNG on disk
  * status polling
  * concurrent sessions don't cross-contaminate
  * delete tears down

The runner makes ONE outbound call to Tencent (``get_bot_qrcode``)
during /sessions. That's a network dependency — tests that exercise
the spawn-flow are marked ``network``. The pure-shape tests (URL
schema, error cases) run offline.
"""

from __future__ import annotations


import httpx
import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.manual, pytest.mark.running_runner]


@pytest.fixture
def cleanup_sessions(runner_url: str, http_get: httpx.Client):
    """Track session ids spawned during a test and DELETE them at
    teardown — keeps the runner from accumulating orphans."""
    spawned: list[str] = []
    yield spawned
    for sid in spawned:
        try:
            http_get.delete(f"{runner_url}/sessions/{sid}", timeout=5.0)
        except Exception:
            pass


def test_health(expect_running_runner: str, http_get: httpx.Client) -> None:
    r = http_get.get(f"{expect_running_runner}/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body.get("protocol") == "ilink"


@pytest.mark.network
def test_spawn_session_returns_session_id_and_qr(
    expect_running_runner: str,
    http_get: httpx.Client,
    cleanup_sessions: list[str],
) -> None:
    r = http_get.post(f"{expect_running_runner}/sessions", timeout=15.0)
    assert r.status_code == 200, r.text
    body = r.json()
    sid = body["session_id"]
    cleanup_sessions.append(sid)
    assert sid and len(sid) > 8
    assert body["qr_path"] == f"/sessions/{sid}/qr.png"

    # QR PNG should be on disk by the time /sessions returns (we made
    # /sessions block on _initialize_qr in the rewrite specifically to
    # avoid the prior 404 race).
    img = http_get.get(f"{expect_running_runner}/sessions/{sid}/qr.png")
    assert img.status_code == 200
    assert img.headers["content-type"].startswith("image/png")
    assert len(img.content) > 200, "QR PNG suspiciously small"


@pytest.mark.network
def test_status_returns_pending_until_scanned(
    expect_running_runner: str,
    http_get: httpx.Client,
    cleanup_sessions: list[str],
) -> None:
    sid = http_get.post(f"{expect_running_runner}/sessions", timeout=15.0).json()["session_id"]
    cleanup_sessions.append(sid)
    s = http_get.get(f"{expect_running_runner}/sessions/{sid}/status").json()
    assert s["session_id"] == sid
    assert s["online"] is False
    assert s["qr_pending"] is True
    assert s.get("account") in (None, {"user_name": "self", "nick_name": None})


@pytest.mark.network
def test_concurrent_sessions_isolated(
    expect_running_runner: str,
    http_get: httpx.Client,
    cleanup_sessions: list[str],
) -> None:
    """Two sessions in flight at once should not share state — the
    multi-session rewrite specifically fixed this."""
    s1 = http_get.post(f"{expect_running_runner}/sessions", timeout=15.0).json()["session_id"]
    s2 = http_get.post(f"{expect_running_runner}/sessions", timeout=15.0).json()["session_id"]
    cleanup_sessions.extend([s1, s2])
    assert s1 != s2

    listing = http_get.get(f"{expect_running_runner}/sessions").json()
    ids = {row["session_id"] for row in listing}
    assert s1 in ids and s2 in ids

    # Each session has its own QR (even just a few bytes of payload
    # diff is fine — the underlying qrcode_key is per-session).
    img1 = http_get.get(f"{expect_running_runner}/sessions/{s1}/qr.png").content
    img2 = http_get.get(f"{expect_running_runner}/sessions/{s2}/qr.png").content
    assert img1 != img2, "two fresh sessions returned identical QR PNGs"


def test_delete_unknown_session_is_idempotent(
    expect_running_runner: str,
    http_get: httpx.Client,
) -> None:
    """DELETE /sessions/{sid} for a non-existent session should 200 (or
    204) rather than error — the runner GCs orphans this way."""
    r = http_get.delete(f"{expect_running_runner}/sessions/never-existed-xyz")
    assert r.status_code in (200, 204), r.text


def test_legacy_endpoints_410(expect_running_runner: str, http_get: httpx.Client) -> None:
    """The pre-rewrite single-session paths (/status, /qr.png at root,
    /messages without sid) should 410 Gone with the migration hint."""
    for path in ("/status", "/qr.png", "/groups", "/contacts"):
        r = http_get.get(f"{expect_running_runner}{path}")
        assert r.status_code == 410, f"{path} should be 410 Gone, got {r.status_code}"
