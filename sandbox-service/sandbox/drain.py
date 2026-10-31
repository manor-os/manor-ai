"""
Graceful drain for Sandbox Service restarts.

A restart used to run lifespan shutdown straight away, which destroys every
sandbox — including one whose caller was between ``exec`` and reading the
result, or a background execution still rendering. The drain controller lets
the process keep serving the sandboxes it already owns while refusing new
ones, and reports when in-flight work has settled so the server can exit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class SandboxServiceDraining(Exception):
    """A new sandbox was requested while the service is draining for a restart."""

    def __init__(self) -> None:
        super().__init__("Sandbox Service is draining for a restart and is not accepting new sandboxes; retry shortly.")


class DrainController:
    """Tracks in-flight API work and the draining flag for one process."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._inflight = 0
        self._draining = False
        self._last_activity = clock()

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def inflight(self) -> int:
        return self._inflight

    def begin(self) -> bool:
        """Enter drain mode; returns False when it was already draining."""

        if self._draining:
            return False
        self._draining = True
        self.note_activity()
        return True

    def note_activity(self) -> None:
        self._last_activity = self._clock()

    @contextmanager
    def track(self) -> Iterator[None]:
        """Count one API request as in flight for its whole duration."""

        self._inflight += 1
        self.note_activity()
        try:
            yield
        finally:
            self._inflight -= 1
            self.note_activity()

    async def wait_idle(
        self,
        *,
        timeout: float,
        quiet_seconds: float,
        busy: Callable[[], int] = lambda: 0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Wait until nothing is in flight and the service has been quiet.

        ``busy`` reports work that is not an open request (background
        executions). Returns False when ``timeout`` elapsed first.
        """

        deadline = self._clock() + max(0.0, timeout)
        while True:
            now = self._clock()
            if self._inflight == 0 and busy() == 0 and now - self._last_activity >= quiet_seconds:
                return True
            if now >= deadline:
                return False
            await asyncio.sleep(min(poll_interval, max(0.0, deadline - now)))
