"""Blocking Redis reads wait in slices shorter than the client's read timeout.

redis-py stops reading a reply after its socket timeout (5 s by default since
5.1). A BLPOP or XREAD block longer than that fails with "Timeout reading"
instead of waiting: worker long polls broke this way, so every instruction
to a remote worker (open a live view) waited for its next timed heartbeat. Every blocking read in the product slices its wait with
``packages.core.cache.blocking_slice_seconds``, or is listed here with the
fixed slice it uses.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BLOCKING_READ = re.compile(
    r"\.(blpop|brpop|blmove|blmpop|brpoplpush|bzpopmin|bzpopmax|bzmpop|xread|xreadgroup)\("
)
# File → the module constant that bounds each of its blocking reads.
_FIXED_SLICES = {
    "packages/core/services/chrome_mcp_relay.py": "_REDIS_RELAY_PHASE_TIMEOUT_SECONDS",
}
_DEFAULT_SOCKET_TIMEOUT_SECONDS = 5


def _product_sources():
    for top in ("packages", "apps"):
        for path in (ROOT / top).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or path.name.startswith("test_") or "/node_modules/" in rel:
                continue
            yield rel, path.read_text(encoding="utf-8", errors="replace")


def test_every_blocking_redis_read_waits_in_slices():
    unsliced = [
        rel for rel, text in _product_sources()
        if _BLOCKING_READ.search(text)
        and "blocking_slice_seconds" not in text
        and rel not in _FIXED_SLICES
    ]
    assert unsliced == [], (
        "These files block on Redis without slicing the wait below the client's read timeout; "
        f"use packages.core.cache.blocking_slice_seconds: {unsliced}"
    )


def test_fixed_slices_end_before_the_default_read_timeout():
    import importlib

    for rel, constant in _FIXED_SLICES.items():
        if not (ROOT / rel).exists():
            continue  # not in this build (the OSS export leaves cloud modules out)
        module = importlib.import_module(rel.removesuffix(".py").replace("/", "."))
        assert getattr(module, constant) < _DEFAULT_SOCKET_TIMEOUT_SECONDS, rel


def test_a_slice_ends_before_the_clients_read_timeout():
    from packages.core.cache import blocking_slice_seconds

    class Pool:
        def __init__(self, socket_timeout):
            self.connection_kwargs = {"socket_timeout": socket_timeout}

    class Client:
        def __init__(self, socket_timeout):
            self.connection_pool = Pool(socket_timeout)

    assert blocking_slice_seconds(Client(5), 20) < 5
    # Redis checks expired blocking reads on its timer. Allow a timer tick
    # plus scheduling/transport time even when the client timeout is short.
    assert blocking_slice_seconds(Client(0.5), 20) + 0.2 < 0.5
    assert blocking_slice_seconds(Client(None), 20) == 4.0
    assert blocking_slice_seconds(Client(5), 1.5) == 1.5
    assert blocking_slice_seconds(Client(5), -1) == 0
