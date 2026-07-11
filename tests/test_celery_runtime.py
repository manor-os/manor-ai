from __future__ import annotations

from packages.core.tasks._runtime import run_in_worker


def test_run_in_worker_detaches_stale_pool_before_task(monkeypatch):
    class FakeEngine:
        def __init__(self) -> None:
            self.dispose_calls: list[bool] = []

        async def dispose(self, *, close: bool = True) -> None:
            self.dispose_calls.append(close)

    import packages.core.database as dbmod

    fake_engine = FakeEngine()
    monkeypatch.setattr(dbmod, "engine", fake_engine)

    async def work() -> str:
        return "ok"

    assert run_in_worker(work()) == "ok"
    assert fake_engine.dispose_calls == [False, True]
