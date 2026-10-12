"""The runner health refresh reconciles instance rows against the runner's
own container listing, and keeps refreshing health when the listing fails."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from packages.core.models.runtime_run import SandboxRunnerStatus
from packages.core.tasks import runtime_tasks

pytestmark = pytest.mark.asyncio


class _Session:
    def __init__(self, runner):
        self.runner = runner
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _model, _runner_id):
        return self.runner

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.runner)

    async def commit(self):
        self.commits += 1


def _runner():
    return SimpleNamespace(
        id="runner-1",
        base_url="http://runner-1:8000",
        status=SandboxRunnerStatus.HEALTHY.value,
        active_count=4,
        executing_count=0,
        active_limit=5,
        executing_limit=2,
        config={},
        last_health_at=None,
        last_error=None,
    )


def _wire(monkeypatch, runner, *, listing):
    session = _Session(runner)
    monkeypatch.setattr(
        "packages.core.database.create_worker_session", lambda: (lambda: session),
    )
    monkeypatch.setattr(
        "packages.core.config.get_settings", lambda: SimpleNamespace(SANDBOX_API_TOKEN="t"),
    )

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def health(self):
            return {"active_sandboxes": 0, "reserved_active": 0, "executing": 0}

        async def list(self):
            if isinstance(listing, Exception):
                raise listing
            return [SimpleNamespace(sandbox_id=sandbox_id) for sandbox_id in listing]

    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", Client)
    sweeps: list[tuple[str, list[str]]] = []

    async def sweep(_db, *, runner_id, live_sandbox_ids):
        sweeps.append((runner_id, list(live_sandbox_ids)))
        return ["sbx-gone"]

    async def reconciled(_db, *, runner_id, observed_active):
        return observed_active

    monkeypatch.setattr(runtime_tasks, "release_vanished_sandbox_instances", sweep)
    monkeypatch.setattr(runtime_tasks, "reconciled_runner_active_count", reconciled)
    return session, sweeps


async def test_health_refresh_sweeps_instances_the_runner_no_longer_lists(monkeypatch) -> None:
    runner = _runner()
    session, sweeps = _wire(monkeypatch, runner, listing=["sbx-alive"])

    await runtime_tasks._refresh_runner_health("runner-1")

    assert sweeps == [("runner-1", ["sbx-alive"])]
    assert runner.status == SandboxRunnerStatus.HEALTHY.value
    assert runner.active_count == 0
    assert session.commits == 1


async def test_health_refresh_skips_the_sweep_when_the_listing_fails(monkeypatch) -> None:
    """No listing means no truth to reconcile against: health still updates,
    nothing is released on a guess."""
    runner = _runner()
    session, sweeps = _wire(monkeypatch, runner, listing=RuntimeError("listing down"))

    await runtime_tasks._refresh_runner_health("runner-1")

    assert sweeps == []
    assert runner.status == SandboxRunnerStatus.HEALTHY.value
    assert session.commits == 1
