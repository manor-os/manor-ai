from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_bash_uses_sdk_sandbox_without_local_fallback(monkeypatch) -> None:
    from packages.core.ai.tools import bash_tool
    from packages.core.config import get_settings

    calls: list[dict[str, object]] = []

    class FakeSandboxClient:
        def __init__(self, *, base_url: str, timeout: float, api_token: str | None = None):
            calls.append({"handler": "init", "base_url": base_url, "api_token": api_token})

        async def create_from_files(self, **kwargs):
            calls.append({"handler": "create", **kwargs})
            return SimpleNamespace(sandbox_id="sandbox_1", workdir="/skill")

        async def exec(self, sandbox_id: str, command: str, timeout: int, workdir: str | None = None):
            calls.append({
                "handler": "exec",
                "sandbox_id": sandbox_id,
                "command": command,
                "timeout": timeout,
                "workdir": workdir,
            })
            return SimpleNamespace(stdout="remote\n", stderr="", exit_code=0)

        async def destroy(self, sandbox_id: str):
            calls.append({"handler": "destroy", "sandbox_id": sandbox_id})

        async def close(self):
            calls.append({"handler": "close"})

    async def forbidden_local(*_args, **_kwargs):
        raise AssertionError("bash_tool must not fall back to local shell when sandbox is configured")

    settings = get_settings()
    old_url = settings.SANDBOX_SERVICE_URL
    old_token = getattr(settings, "SANDBOX_API_TOKEN", "")
    settings.SANDBOX_SERVICE_URL = "http://sandbox-service"
    setattr(settings, "SANDBOX_API_TOKEN", "runner-secret")
    monkeypatch.setenv("SANDBOX_SERVICE_URL", "http://sandbox-service")
    monkeypatch.setattr("packages.core.services.sandbox_sdk.SandboxClient", FakeSandboxClient)
    monkeypatch.setattr(bash_tool, "_execute_local", forbidden_local)
    try:
        payload = json.loads(await bash_tool._bash("entity_1", command="python3 --version"))
    finally:
        settings.SANDBOX_SERVICE_URL = old_url
        setattr(settings, "SANDBOX_API_TOKEN", old_token)

    assert payload["stdout"] == "remote\n"
    assert [call["handler"] for call in calls] == ["init", "create", "exec", "destroy", "close"]
    assert calls[0]["api_token"] == "runner-secret"
    assert calls[1]["config"]["network"] == "none"
