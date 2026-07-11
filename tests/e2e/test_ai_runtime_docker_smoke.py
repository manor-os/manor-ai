from __future__ import annotations

import os
import re
import uuid

import httpx
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.manual,
    pytest.mark.network,
    pytest.mark.docker,
    pytest.mark.running_api,
    pytest.mark.running_runner,
]


def _require_docker_ai_e2e() -> None:
    if os.getenv("MANOR_DOCKER_AI_E2E") != "1":
        pytest.skip("set MANOR_DOCKER_AI_E2E=1 to run local Docker AI runtime smoke tests")


def _api_base_url() -> str:
    return os.getenv("MANOR_E2E_BASE_URL", "http://localhost:8010").rstrip("/")


def _sandbox_base_url() -> str:
    return os.getenv("MANOR_E2E_SANDBOX_URL", "http://localhost:8110").rstrip("/")


async def _auth_headers(client: httpx.AsyncClient) -> dict[str, str]:
    token = os.getenv("MANOR_E2E_AUTH_TOKEN", "").strip()
    if token:
        return {"Authorization": f"Bearer {token}"}

    email = os.getenv("MANOR_E2E_EMAIL", "").strip()
    password = os.getenv("MANOR_E2E_PASSWORD", "").strip()
    if email and password:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload.get("requires_verification"):
            pytest.skip("MANOR_E2E_EMAIL requires email verification; provide MANOR_E2E_AUTH_TOKEN instead")
        assert "access_token" in payload, response.text
        return {"Authorization": f"Bearer {payload['access_token']}"}

    suffix = uuid.uuid4().hex[:10]
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": f"ai_runtime_{suffix}",
            "email": f"ai-runtime-{suffix}@example.test",
            "password": "pass123",
        },
    )
    assert response.status_code in {200, 201}, response.text
    payload = response.json()
    if payload.get("requires_verification"):
        pytest.skip(
            "local API requires email verification; set MANOR_E2E_AUTH_TOKEN or "
            "MANOR_E2E_EMAIL/MANOR_E2E_PASSWORD for authenticated Docker smoke tests"
        )
    assert "access_token" in payload, response.text
    return {"Authorization": f"Bearer {payload['access_token']}"}


def _parse_sse_events(text: str) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    event = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((event, line[len("data:") :].strip()))
    return events


@pytest.mark.asyncio
async def test_docker_ai_runtime_health_surfaces_are_up() -> None:
    _require_docker_ai_e2e()

    async with httpx.AsyncClient(timeout=10.0) as client:
        api = await client.get(f"{_api_base_url()}/health")
        sandbox = await client.get(f"{_sandbox_base_url()}/health")

    assert api.status_code == 200
    assert api.json()["status"] == "ok"
    assert sandbox.status_code == 200
    assert sandbox.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_docker_skill_invoke_returns_full_contract_and_runs_sandbox_script() -> None:
    """Live local smoke: API skill storage -> sandbox create -> sandbox exec."""

    _require_docker_ai_e2e()

    skill_slug = f"runtime-smoke-{uuid.uuid4().hex[:8]}"
    skill_md = "\n".join(
        [
            f"# {skill_slug}",
            "",
            "This external-format SKILL.md has no Manor-specific contract heading.",
            "",
            "Run `python /skill/scripts/run.py` and verify the output file.",
            "",
            "DOCKER-SKILL-TAIL-2aa97c",
        ]
    )
    script = (
        "from pathlib import Path\n"
        "out = Path('/skill/projects/runtime_smoke/exports')\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'result.txt').write_text('sandbox-ok', encoding='utf-8')\n"
        "print(out / 'result.txt')\n"
    )

    sandbox_id: str | None = None
    async with httpx.AsyncClient(base_url=_api_base_url(), timeout=180.0) as client:
        headers = await _auth_headers(client)
        created = await client.post(
            "/api/v1/skills",
            headers=headers,
            json={
                "name": skill_slug,
                "slug": skill_slug,
                "description": "Docker AI runtime smoke skill",
                "system_prompt": skill_md,
                "type": "sandbox",
                "scripts": {"scripts/run.py": script},
            },
        )
        assert created.status_code == 201, created.text
        skill_id = created.json()["id"]

        try:
            invoked = await client.post(
                f"/api/v1/skills/{skill_id}/invoke",
                headers=headers,
                json={"input": "Run the smoke skill."},
            )
            assert invoked.status_code == 200, invoked.text
            payload = invoked.json()
            content = payload["content"]
            assert payload["stop_reason"] == "sandbox_ready"
            assert "## Runtime Skill Execution Contract" in content
            assert "## Skill Instructions" in content
            assert skill_md in content
            assert "DOCKER-SKILL-TAIL-2aa97c" in content
            assert "## Manor Runtime Harness Contract" not in content

            match = re.search(r"^sandbox_id:\s*([A-Za-z0-9_.-]+)", content, re.MULTILINE)
            assert match, content
            sandbox_id = match.group(1)

            from packages.core.services.sandbox_sdk import SandboxClient

            sandbox = SandboxClient(base_url=_sandbox_base_url(), timeout=60.0)
            try:
                exec_result = await sandbox.exec(
                    sandbox_id=sandbox_id,
                    command=("python /skill/scripts/run.py && cat /skill/projects/runtime_smoke/exports/result.txt"),
                    timeout=30,
                )
                assert exec_result.exit_code == 0
                assert "sandbox-ok" in exec_result.stdout
            finally:
                await sandbox.destroy(sandbox_id)
                await sandbox.close()
                sandbox_id = None
        finally:
            await client.delete(f"/api/v1/skills/{skill_id}", headers=headers)

    if sandbox_id:
        from packages.core.services.sandbox_sdk import SandboxClient

        sandbox = SandboxClient(base_url=_sandbox_base_url(), timeout=30.0)
        try:
            await sandbox.destroy(sandbox_id)
        finally:
            await sandbox.close()


@pytest.mark.asyncio
async def test_docker_global_chat_stream_smoke_when_llm_enabled() -> None:
    _require_docker_ai_e2e()
    if os.getenv("MANOR_DOCKER_AI_E2E_LLM") != "1":
        pytest.skip("set MANOR_DOCKER_AI_E2E_LLM=1 to spend a real model call")

    async with httpx.AsyncClient(base_url=_api_base_url(), timeout=180.0) as client:
        headers = await _auth_headers(client)
        response = await client.post(
            "/api/v1/chat/stream",
            headers=headers,
            data={"message": "只回复 OK，不调用工具。"},
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    event_names = [name for name, _data in _parse_sse_events(response.text)]
    assert "stream_start" in event_names
    assert "stream_end" in event_names
