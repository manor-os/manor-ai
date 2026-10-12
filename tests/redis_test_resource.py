"""Process-owned Redis configuration for test fixtures that need real cache state."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


def _cache_prefix() -> str:
    return f"manor:test:{os.getpid()}:{uuid4().hex}:"


@dataclass(frozen=True)
class RedisTestResource:
    url: str
    cache_prefix: str
    directory: Path | None = None

    @classmethod
    def from_environment(cls) -> "RedisTestResource":
        configured_url = os.getenv("TEST_REDIS_URL")
        if configured_url:
            return cls(url=configured_url, cache_prefix=_cache_prefix())

        directory = Path(tempfile.mkdtemp(prefix="manor-test-redis-"))
        socket = directory / "redis.sock"
        return cls(
            url=f"unix://{socket}?db=0",
            cache_prefix=_cache_prefix(),
            directory=directory,
        )

    def start(self) -> subprocess.Popen[bytes] | None:
        if self.directory is None:
            return None

        redis_server = shutil.which("redis-server")
        if redis_server is None:
            raise RuntimeError("redis-server is required when TEST_REDIS_URL is not configured")

        socket = self.directory / "redis.sock"
        process = subprocess.Popen(
            [
                redis_server,
                "--save",
                "",
                "--appendonly",
                "no",
                "--port",
                "0",
                "--unixsocket",
                str(socket),
                "--unixsocketperm",
                "700",
                "--dir",
                str(self.directory),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 5
        while not socket.exists():
            if process.poll() is not None:
                raise RuntimeError("Isolated test Redis exited during startup")
            if time.monotonic() >= deadline:
                process.terminate()
                process.wait(timeout=2)
                raise RuntimeError("Timed out starting isolated test Redis")
            time.sleep(0.01)
        return process

    def cache_prefix_for_test(self) -> str:
        """Return a unique child namespace for one pytest item."""
        return f"{self.cache_prefix}{uuid4().hex}:"

    def cleanup(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)


def require_test_cache_namespace(prefix: str, expected_prefix: str) -> None:
    """Refuse wildcard cleanup unless it stays inside this pytest process."""
    if prefix != expected_prefix or not prefix.startswith("manor:test:"):
        raise RuntimeError("Refusing Redis cleanup outside the test cache namespace")
