#!/usr/bin/env python
"""Assert every declared Celery queue has at least one live consumer.

A queue split is only half a deploy: the compose file can declare a worker
service and the deploy can still never start it, leaving that queue with no
consumer. Nothing else notices — the control plane keeps ticking, /health
stays green, and tasks accumulate on an unread queue until someone reports
that "nothing runs any more". That happened once (the ``work`` queue landed
in docker-compose but not in the deploy's service list), which is why this
check exists.

The queue set comes from ``packages.core.queues.CeleryQueue`` — the same
registry the router uses — so declaring a new queue automatically extends
this check. Nothing here is hardcoded or name-matched.

Usage (inside a container that shares the app's broker config)::

    python scripts/verify_queue_consumers.py            # one probe
    python scripts/verify_queue_consumers.py --timeout 60

Exit codes: 0 = every declared queue is consumed, 1 = at least one is not,
2 = the broker could not be reached at all.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Iterable


def _declared_queues() -> set[str]:
    from packages.core.queues import CeleryQueue

    return {queue.value for queue in CeleryQueue}


def _consumed_queues() -> dict[str, list[str]] | None:
    """Map queue name → consuming worker names, or None when unreachable.

    ``inspect.active_queues()`` returns ``{worker_name: [queue, ...]}`` and
    ``None`` when no worker replies at all — the two cases mean different
    things (broker down vs. workers not up yet), so they stay distinct.
    """
    from packages.core.celery_app import celery_app

    replies = celery_app.control.inspect(timeout=5).active_queues()
    if replies is None:
        return None

    consumers: dict[str, list[str]] = {}
    for worker_name, queues in (replies or {}).items():
        for queue in queues or []:
            name = queue.get("name")
            if name:
                consumers.setdefault(name, []).append(worker_name)
    return consumers


def _describe(consumers: dict[str, list[str]], queues: Iterable[str]) -> str:
    lines = []
    for queue in sorted(queues):
        workers = consumers.get(queue) or []
        mark = "ok " if workers else "MISSING"
        lines.append(f"  {mark} {queue}: {', '.join(sorted(workers)) or '(no consumer)'}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to keep retrying while workers register (default: 60)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="seconds between probes (default: 5)",
    )
    args = parser.parse_args()

    declared = _declared_queues()
    deadline = time.monotonic() + args.timeout
    consumers: dict[str, list[str]] = {}
    reachable = False

    while True:
        probe = _consumed_queues()
        if probe is not None:
            reachable = True
            consumers = probe
            if not (declared - consumers.keys()):
                print(f"All {len(declared)} declared queue(s) have a consumer:")
                print(_describe(consumers, declared))
                return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(args.interval)

    if not reachable:
        print(
            "No Celery worker replied to the inspect broadcast. Either the "
            "broker is unreachable or no worker is running at all.",
            file=sys.stderr,
        )
        return 2

    missing = sorted(declared - consumers.keys())
    print(
        f"{len(missing)} declared queue(s) have NO consumer: {', '.join(missing)}\n"
        "Every declared queue needs a worker started with -Q for it. Check that "
        "the deploy starts one service per queue (see docker-compose.yml and the "
        "APP_SERVICES list in .github/workflows/deploy.yml).\n"
        "Observed:",
        file=sys.stderr,
    )
    print(_describe(consumers, declared | consumers.keys()), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
