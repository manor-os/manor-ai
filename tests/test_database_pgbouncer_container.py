"""Actual PgBouncer transaction-pooling regression; opt in explicitly."""
from __future__ import annotations

import asyncio
import os
import socket
import struct
import subprocess
import time
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from packages.core import database
from packages.core.database_runtime import DatabaseAdmissionGroup, DatabaseEngineKind, database_admission_group


# Pulled 2026-10-04 as edoburu/pgbouncer:v1.24.1-p1.
PGBOUNCER_IMAGE = os.getenv(
    "MANOR_PGBOUNCER_IMAGE",
    "edoburu/pgbouncer@sha256:3db3d7223e93af52b4116f642951a1a5fa44702a88c2a59cf7562cac19320c9e",
)
pytestmark = [
    pytest.mark.docker,
    pytest.mark.manual,
    pytest.mark.skipif(
        os.getenv("MANOR_PGBOUNCER_CONTAINER_TEST") != "1",
        reason="Set MANOR_PGBOUNCER_CONTAINER_TEST=1 to run against a disposable PgBouncer container",
    ),
]


def _docker(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2) as connection:
                # Docker may accept TCP before PgBouncer starts. Require a
                # PostgreSQL SSLRequest response before handing the port out.
                connection.sendall(struct.pack("!II", 8, 80877103))
                if connection.recv(1) in {b"N", b"S"}:
                    return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"PgBouncer did not listen on localhost:{port}")


@pytest.fixture(scope="module")
def pgbouncer_container():
    """A transaction pool in front of only the local manor_test database."""
    _docker("pull", PGBOUNCER_IMAGE, timeout=120)
    digest = _docker("image", "inspect", PGBOUNCER_IMAGE, "--format", "{{index .RepoDigests 0}}")
    print(f"PgBouncer image: {digest}")
    name = f"manor-pgbouncer-regression-{uuid.uuid4().hex[:12]}"
    _docker(
        "run", "--detach", "--rm", "--name", name,
        "--add-host", "host.docker.internal:host-gateway",
        "--publish", "127.0.0.1::5432",
        "--env", "DATABASE_URL=postgresql://manor:manor_secret@host.docker.internal:5434/manor_test",
        "--env", "AUTH_TYPE=plain",
        "--env", "POOL_MODE=transaction",
        "--env", "DEFAULT_POOL_SIZE=2",
        "--env", "MAX_CLIENT_CONN=16",
        "--env", "ADMIN_USERS=manor",
        PGBOUNCER_IMAGE,
    )
    try:
        port = int(_docker(
            "inspect", name, "--format",
            "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}",
        ))
        _wait_for_port(port)
        yield name, port
    finally:
        _docker("rm", "--force", name)


def _settings(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        DATABASE_URL=f"postgresql+asyncpg://manor:manor_secret@127.0.0.1:{port}/manor_test",
        DATABASE_ECHO=False,
        DATABASE_POOL_MODE="pgbouncer",
        DATABASE_POOL_SIZE=2,
        DATABASE_MAX_OVERFLOW=0,
        DATABASE_POOL_TIMEOUT=1,
        DATABASE_POOL_RECYCLE=1800,
        DATABASE_ADMISSION_LIMIT=4,
        DATABASE_MAX_WAITERS=0,
        DATABASE_ADMISSION_TIMEOUT=2,
        DATABASE_HOLD_WARN_SECONDS=5,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admission_enabled", [True, False])
async def test_pgbouncer_transaction_pool_reuses_backend_after_managed_transactions(
    pgbouncer_container, _test_database_guard, admission_enabled,
):
    _, port = pgbouncer_container
    settings = _settings(port)
    settings.DATABASE_ADMISSION_ENABLED = admission_enabled
    if not admission_enabled:
        settings.DATABASE_ADMISSION_LIMIT = 1
    kind = DatabaseEngineKind.APPLICATION if admission_enabled else DatabaseEngineKind.WORKER
    engine = database.create_database_engine(settings, kind=kind)
    factory = database.create_session_factory(engine)
    both_started = asyncio.Event()
    release = asyncio.Event()
    backend_pids: list[int] = []

    async def hold_managed_transaction():
        async with factory() as session:
            backend_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
            if len(backend_pids) == 2:
                both_started.set()
            await release.wait()
            await session.commit()

    group = DatabaseAdmissionGroup(limit=1, timeout=0.2)
    token = database_admission_group.set(None if admission_enabled else group)
    tasks = [asyncio.create_task(hold_managed_transaction()) for _ in range(4)]
    database_admission_group.reset(token)
    try:
        await asyncio.wait_for(both_started.wait(), 10)
        runtime = engine.sync_engine._manor_database_runtime

        async def wait_for_all_checkouts():
            while runtime.checked_out != 4:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_all_checkouts(), 5)
        # Both modes allow four clients, while PgBouncer still runs only two
        # backend transactions. Disabled workers bypass the inherited group.
        assert len(backend_pids) == 2
        assert len(set(backend_pids)) == 2
        snapshot = runtime.snapshot()
        assert snapshot["active_transactions"] == (4 if admission_enabled else None)
        assert snapshot["waiting"] == (0 if admission_enabled else None)
        release.set()
        await asyncio.gather(*tasks)
        assert len(backend_pids) == 4
        assert len(set(backend_pids)) == 2
        async with factory(info={"database_probe": True}) as probe:
            assert await probe.scalar(text("SELECT 1")) == 1
        async with factory() as recycled:
            recycled_pid = await recycled.scalar(text("SELECT pg_backend_pid()"))
        assert recycled_pid in backend_pids
        assert runtime.snapshot()["active_transactions"] == (0 if admission_enabled else None)
        assert runtime.checked_out == 0
        assert group.peak_active == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
