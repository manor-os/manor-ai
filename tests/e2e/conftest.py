"""Fixtures for end-to-end smoke tests.

These tests differ from the parent ``tests/conftest.py`` in two ways:

  1. They hit a **running** API + sidecar container over HTTP rather
     than mounting a FastAPI test client in-process. That's the whole
     point — make sure the deployed image works, not just the source.
  2. They opt out of the parent conftest's ``client`` / DB fixtures.
     The e2e suite manages its own auth (JWT mint) and assumes the
     services are already up.

Run locally
───────────
::

    docker compose up -d api wechat-runner
    pytest tests/e2e/ -m "manual and e2e" -v

Run runtime-only tests without third-party network calls
───────────────────────────────────────────────────────
::

    pytest tests/e2e/ -m "manual and e2e and not network" -v

Default ``make test`` and PR smoke runs exclude this directory through
markers; use ``make test-e2e`` or ``make test-manual`` to opt in.

Override service URLs
─────────────────────
::

    MANOR_E2E_API_URL=https://api.example.com
    MANOR_E2E_RUNNER_URL=https://wechat-runner.internal:8800
"""

from __future__ import annotations

import os
from typing import Dict

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.manual]


# ── Marker registration — recognised in pytest -m queries ────────────


def pytest_configure(config: pytest.Config) -> None:
    for marker in (
        "e2e: end-to-end tests that require deployed/runtime surfaces or broad workflow setup",
        "manual: opt-in tests excluded from default CI",
        "network: test makes outbound HTTP to a third-party API "
        "(GitHub, Meta, etc.) — skip with `-m 'not network'` when offline.",
        "running_api: test requires a running Manor API over HTTP",
        "running_runner: test requires a running runner sidecar over HTTP",
    ):
        config.addinivalue_line("markers", marker)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    e2e_root = os.path.dirname(__file__)
    for item in items:
        item_path = str(getattr(item, "path", ""))
        if not item_path.startswith(e2e_root):
            continue
        item.add_marker(pytest.mark.e2e)
        item.add_marker(pytest.mark.manual)
        fixture_names = set(getattr(item, "fixturenames", ()))
        if "expect_running_api" in fixture_names or "api_url" in fixture_names:
            item.add_marker(pytest.mark.running_api)
        if "expect_running_runner" in fixture_names or "runner_url" in fixture_names:
            item.add_marker(pytest.mark.running_runner)


# ── Service URL fixtures (env-overridable) ───────────────────────────


@pytest.fixture(scope="session")
def api_url() -> str:
    """Base URL of the Manor API. Default matches docker compose's
    `8010:8000` mapping. Override with ``MANOR_E2E_API_URL`` for prod
    smoke runs."""
    return os.environ.get("MANOR_E2E_API_URL", "http://localhost:8010").rstrip("/")


@pytest.fixture(scope="session")
def runner_url() -> str:
    """Base URL of the WeChat ClawBot runner sidecar. Default matches
    the ``--profile wechat`` compose service's ``8801:8800`` mapping."""
    return os.environ.get("MANOR_E2E_RUNNER_URL", "http://localhost:8801").rstrip("/")


# ── Auth fixtures ────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_user_id() -> str:
    """Stable user id used across e2e tests. ``get_current_user``
    looks the JWT's ``sub`` up in the DB, so this value MUST exist
    as an active user. CI bootstraps one; locally, override via env
    or the auth_headers fixture will skip the affected tests."""
    return os.environ.get("MANOR_E2E_USER_ID", "01KQBQ4PTAQ0DDQ9WEJQ4H824X")


@pytest.fixture(scope="session")
def test_entity_id() -> str:
    return os.environ.get("MANOR_E2E_ENTITY_ID", "01KQBQ4PSW14PDZ415C8YS006A")


@pytest.fixture(scope="session")
def auth_headers(test_user_id: str, test_entity_id: str) -> Dict[str, str]:
    """JWT bearer header for authenticated endpoints. Minted with the
    same secret the API uses (loaded via packages.core.config)."""
    from packages.core.services.auth_service import create_access_token

    token = create_access_token(
        user_id=test_user_id,
        entity_id=test_entity_id,
        role="owner",
    )
    return {"Authorization": f"Bearer {token}"}


# ── HTTP client helpers ──────────────────────────────────────────────


@pytest.fixture
def http_get():
    """Sync GET helper bound to httpx.Client — keeps tests terse.
    Tests that need request-level config use httpx directly."""
    import httpx

    with httpx.Client(timeout=20.0) as client:
        yield client


@pytest.fixture
def expect_running_api(api_url: str) -> str:
    """Refuse to run the test if the API isn't reachable — gives a
    clearer error than 30 confusing connection refused traces."""
    import httpx

    try:
        r = httpx.get(f"{api_url}/health", timeout=5.0)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Manor API not reachable at {api_url}: {exc}")
    return api_url


@pytest.fixture
def expect_running_runner(runner_url: str) -> str:
    """Same idea for the WeChat runner sidecar — skip if not running
    rather than fail confusingly."""
    import httpx

    try:
        r = httpx.get(f"{runner_url}/health", timeout=5.0)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"WeChat runner not reachable at {runner_url}: {exc}")
    return runner_url


# ── Disable parent conftest's client fixture for e2e ─────────────────


@pytest.fixture
def client():
    """Block the parent conftest's in-process FastAPI test client from
    leaking into e2e tests — they should hit ``api_url`` over HTTP."""
    pytest.skip("e2e tests use the running container — request `api_url` + `auth_headers` instead of `client`.")
