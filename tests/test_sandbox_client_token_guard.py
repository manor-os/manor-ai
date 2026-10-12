"""Every SandboxClient the runtime builds must carry the sandbox API token.

sandbox-service rejects every ``/api/v1/*`` request that lacks the
``X-Manor-Sandbox-Token`` header (``require_sandbox_api_token`` in
sandbox-service/main.py). ``SandboxClient`` only sends that header when it is
constructed with ``api_token=``, so a construction that omits it works against
a token-less local service and fails with ``401 Sandbox API token required``
on staging and production. video_edit_tools shipped exactly that way on
2026-08-10 and stayed broken for a month because nothing scanned for the
class of mistake; this guard does, repository-wide.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = (REPO_ROOT / "packages", REPO_ROOT / "apps")
SKIP_PARTS = {"tests", "test", "node_modules", ".venv", "__pycache__"}


def _is_sandbox_client_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "SandboxClient"
    if isinstance(func, ast.Attribute):
        return func.attr == "SandboxClient"
    return False


def _sandbox_client_calls_missing_token() -> list[str]:
    offenders: list[str] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if SKIP_PARTS.intersection(path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_sandbox_client_call(node):
                    continue
                if any(kw.arg == "api_token" for kw in node.keywords):
                    continue
                if any(kw.arg is None for kw in node.keywords):
                    # ``SandboxClient(**client_kwargs)``: the token travels in
                    # the mapping, which this static scan cannot see through.
                    continue
                rel = path.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{node.lineno}")
    return offenders


def test_every_sandbox_client_construction_passes_api_token() -> None:
    offenders = _sandbox_client_calls_missing_token()
    assert not offenders, (
        "SandboxClient(...) built without api_token= — sandbox-service answers "
        "401 'Sandbox API token required' to every call from such a client:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_scans_the_known_sandbox_call_sites() -> None:
    """The scan must actually see the runtime's client constructions.

    Named modules, not a count: consolidating two constructions into one
    helper must not read as "the scan roots are wrong".
    """
    seen: set[str] = set()
    for root in SCAN_ROOTS:
        for path in root.rglob("*.py"):
            if SKIP_PARTS.intersection(path.parts):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            if any(
                isinstance(node, ast.Call) and _is_sandbox_client_call(node)
                for node in ast.walk(tree)
            ):
                seen.add(path.name)
    expected = {
        "sandbox_session_service.py", "sandbox_tools.py", "video_edit_tools.py",
        "workflow_runner.py", "bash_tool.py", "runtime_tasks.py",
    }
    assert expected <= seen, f"guard did not see {sorted(expected - seen)}; scan roots wrong?"


def test_video_edit_sandbox_client_carries_the_api_token(monkeypatch) -> None:
    """The video-edit helper forwards SANDBOX_API_TOKEN to every client."""
    from types import SimpleNamespace

    from packages.core import config as core_config
    from packages.core.ai.tools import video_edit_tools
    from packages.core.services import sandbox_sdk

    captured: dict[str, object] = {}

    class FakeSandboxClient:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(sandbox_sdk, "SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(
        core_config,
        "get_settings",
        lambda: SimpleNamespace(SANDBOX_SERVICE_URL="http://sandbox", SANDBOX_API_TOKEN=" tok-123 "),
    )
    video_edit_tools._video_sandbox_client(base_url="http://sandbox", timeout=5.0)
    assert captured["api_token"] == "tok-123"
    assert captured["base_url"] == "http://sandbox"

    # Settings without the attribute (older fakes) fall back to the process env.
    captured.clear()
    monkeypatch.setattr(
        core_config, "get_settings", lambda: SimpleNamespace(SANDBOX_SERVICE_URL="http://sandbox")
    )
    monkeypatch.setenv("SANDBOX_API_TOKEN", "env-tok")
    video_edit_tools._video_sandbox_client(base_url="http://sandbox", timeout=5.0)
    assert captured["api_token"] == "env-tok"
