"""One shared sandbox per user, against a real runner and real Docker.

Skipped unless ``SANDBOX_E2E_URL`` (and ``SANDBOX_E2E_TOKEN``) point at a
running sandbox-service, for example::

    cd sandbox-service && SANDBOX_PORT=8471 SANDBOX_HOST=127.0.0.1 \\
      SANDBOX_IMAGE=sandbox-skill:deps-test SANDBOX_CONTAINER_PREFIX=e2e-pu- \\
      SANDBOX_SERVICE_SCOPE=e2e-pu- SANDBOX_API_TOKEN=e2e-pu-local \\
      SANDBOX_REDIS_URL= REDIS_URL= python -m uvicorn main:app --port 8471
    SANDBOX_E2E_URL=http://127.0.0.1:8471 SANDBOX_E2E_TOKEN=e2e-pu-local pytest tests/test_sandbox_per_user_real_docker.py

What it proves, each with commands of several "tasks" running at once in ONE
container (1 GiB, 512 pids, 4 slots):

* a command that exceeds the memory limit is killed alone with an accurate
  ``oom_killed`` outcome; a concurrent command completes and the sandbox
  keeps serving;
* a fork bomb is contained (``pids_killed``) while another command, which
  needs to fork, completes;
* two Skills coexist and run concurrently, each with its own /skill view,
  SKILL_DIR, cwd and env, and loading one never waits for the other's
  running command;
* work areas of two conversations are separate directories;
* commands beyond the slot limit wait in the fair FIFO and start in order,
  and a waiting command can be withdrawn.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

E2E_URL = os.getenv("SANDBOX_E2E_URL", "").strip()
E2E_TOKEN = os.getenv("SANDBOX_E2E_TOKEN", "").strip()

pytestmark = pytest.mark.skipif(not E2E_URL, reason="needs a running sandbox-service (SANDBOX_E2E_URL)")


def _client():
    from packages.core.services.sandbox_sdk import SandboxClient

    return SandboxClient(base_url=E2E_URL, timeout=300.0, api_token=E2E_TOKEN)


SKILL_A = {
    "SKILL.md": "# alpha\nRun `python3 /skill/scripts/who.py`.\n",
    "scripts/who.py": (
        "import os, pathlib\n"
        "print('skill=alpha dir=' + os.environ.get('SKILL_DIR', '') + ' cwd=' + os.getcwd()"
        " + ' key=' + os.environ.get('ALPHA_KEY', '-'))\n"
        "print('data=' + pathlib.Path('/skill/data.txt').read_text().strip())\n"
    ),
    "data.txt": "alpha-data\n",
}
SKILL_B = {
    "SKILL.md": "# beta\n",
    "scripts/who.py": (
        "import os, pathlib, time\n"
        "time.sleep(float(os.environ.get('NAP', '0')))\n"
        "print('skill=beta dir=' + os.environ.get('SKILL_DIR', '') + ' cwd=' + os.getcwd()"
        " + ' key=' + os.environ.get('BETA_KEY', '-') + ' alpha=' + os.environ.get('ALPHA_KEY', '-'))\n"
        "print('data=' + pathlib.Path('/skill/data.txt').read_text().strip())\n"
    ),
    "data.txt": "beta-data\n",
}


@pytest.fixture
async def sandbox():
    client = _client()
    created = await client.create_from_files(
        skill_name="alpha", files=dict(SKILL_A), env={"ALPHA_KEY": "a-secret"}, auto_install=False,
    )
    try:
        yield client, created
    finally:
        try:
            await client.destroy(created.sandbox_id)
        finally:
            await client.close()


async def _run(client, sandbox_id, command, *, timeout=60, **options):
    execution_id = f"e2e-{uuid.uuid4().hex[:12]}"
    started = await client.start_exec(
        sandbox_id=sandbox_id, command=command, timeout=timeout, execution_id=execution_id,
        queue_timeout=options.pop("queue_timeout", 120), **options,
    )
    status = started
    while not status.terminal:
        status = await client.execution_status(sandbox_id, execution_id, wait_seconds=30)
    return status


@pytest.mark.asyncio
async def test_memory_hog_is_killed_alone_and_reported_while_another_command_completes(sandbox):
    client, created = sandbox
    sid = created.sandbox_id
    steady = asyncio.create_task(_run(
        client, sid,
        "python3 -c \"import time; b=bytearray(80*1024*1024); time.sleep(6); print('steady ok', len(b))\"",
        skill="", fair_key="conv-steady",
    ))
    await asyncio.sleep(1.0)
    hog = await _run(
        client, sid,
        "python3 -c \"x=[]\nwhile True: x.append(bytearray(32*1024*1024))\"",
        skill="", fair_key="conv-hog",
    )
    assert hog.exit_code == 137, (hog.exit_code, hog.stderr)
    assert hog.outcome == "oom_killed", (hog.outcome, hog.stderr)
    assert "out of memory" in (hog.outcome_detail or "")
    assert "__MANOR_EXEC_STATUS__" not in (hog.stderr or "")
    steady_result = await steady
    assert steady_result.exit_code == 0 and "steady ok" in steady_result.stdout
    info = await client.status(sid)
    assert info.status in {"ready", "executing"}
    after = await _run(client, sid, "echo still-serving", skill="")
    assert after.exit_code == 0 and "still-serving" in after.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bomb_command",
    [
        # A classic bash fork bomb: every function call forks two more.
        "bash -c 'b(){ b|b & }; b; sleep 60'",
        # The same with its environment wiped: attributed by its session.
        "env -i /bin/bash -c 'b(){ b|b & }; b; sleep 60'",
    ],
    ids=["plain", "env-cleared"],
)
async def test_fork_bomb_is_contained_while_another_forking_command_completes(sandbox, bomb_command):
    client, created = sandbox
    sid = created.sandbox_id
    worker = asyncio.create_task(_run(
        client, sid,
        # bash retries a fork that fails while the bomb holds every pid.
        "bash -c 'for i in 1 2 3 4 5 6 7 8; do sleep 1; /bin/true; done; echo worker-done'",
        skill="", fair_key="conv-worker", timeout=120,
    ))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    bomb = await _run(client, sid, bomb_command, skill="", fair_key="conv-bomb", timeout=90)
    assert time.monotonic() - started < 60, "the bomb ran until its timeout instead of being stopped"
    assert bomb.outcome == "pids_killed", (bomb.exit_code, bomb.outcome, bomb.stderr[-500:] if bomb.stderr else "")
    result = await worker
    assert result.exit_code == 0 and "worker-done" in result.stdout, (result.exit_code, result.stderr)
    after = await _run(client, sid, "ps -e | wc -l", skill="")
    assert after.exit_code == 0 and int(after.stdout.strip()) < 50


@pytest.mark.asyncio
async def test_a_work_area_that_fills_the_workspace_is_stopped_alone_and_the_sandbox_survives(sandbox):
    """Disk quota (sandbox.disk_quota) on real Docker: two conversations work in
    one sandbox; one fills the shared /workspace tmpfs. Only its command is
    killed (``disk_killed``, naming its area), its new commands are refused as
    never started while cleanup runs, the other conversation's command
    completes, and the container keeps serving -- it is never recycled."""

    from packages.core.services.sandbox_sdk.exceptions import SandboxError

    client, created = sandbox
    sid = created.sandbox_id
    area_a, area_b = "/workspace/c/conv-A/agents/main", "/workspace/c/conv-B/agents/main"
    steady = asyncio.create_task(_run(
        client, sid, "for i in $(seq 1 8); do echo $i >> log.txt; sleep 1; done; wc -l < log.txt",
        skill="", workdir=area_b, fair_key="conv-B", timeout=120,
    ))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    # 245 MB of the 256 MB tmpfs (over its 92% limit), then it keeps running.
    hog = await _run(
        client, sid, "head -c 245M /dev/zero > big.bin && sleep 60",
        skill="", workdir=area_a, fair_key="conv-A", timeout=120,
    )
    assert time.monotonic() - started < 30, "the area was not stopped by the disk watchdog"
    assert hog.outcome == "disk_killed", (hog.exit_code, hog.outcome, hog.stderr)
    assert "/workspace/c/conv-A held 24" in (hog.outcome_detail or ""), hog.outcome_detail
    assert "__MANOR_EXEC_STATUS__" not in (hog.stderr or "")

    # The other conversation's command, running all along, completes.
    steady_result = await steady
    assert steady_result.exit_code == 0 and steady_result.stdout.strip() == "8", steady_result.stderr

    # The full area's new commands never start; another area's do.
    with pytest.raises(SandboxError) as refused:
        await client.start_exec(
            sandbox_id=sid, command="echo more > more.txt", timeout=30,
            execution_id=f"e2e-{uuid.uuid4().hex[:12]}", queue_timeout=30,
            skill="", workdir=area_a, fair_key="conv-A",
        )
    assert refused.value.code == "sandbox_disk_quota" and refused.value.status_code == 409
    assert (refused.value.body or {}).get("started") is False
    other = await _run(client, sid, "echo fine", skill="", workdir=area_b, fair_key="conv-B")
    assert other.exit_code == 0 and other.stdout.strip() == "fine"

    # Cleanup always runs; once the disk is back under its resume level the area works again.
    cleaned = await _run(client, sid, "rm -f big.bin", skill="", workdir=area_a, fair_key="conv-A")
    assert cleaned.exit_code == 0, cleaned.stderr
    deadline = time.monotonic() + 30
    while True:
        try:
            back = await _run(client, sid, "echo back", skill="", workdir=area_a, fair_key="conv-A")
            break
        except SandboxError as exc:
            assert exc.code == "sandbox_disk_quota" and time.monotonic() < deadline, exc
            await asyncio.sleep(1)
    assert back.exit_code == 0 and back.stdout.strip() == "back"
    info = await client.status(sid)
    assert info.sandbox_id == sid and info.status in {"ready", "executing"}


@pytest.mark.asyncio
async def test_a_command_filling_the_shared_dir_is_charged_not_the_largest_area(sandbox):
    """Bytes outside every work area (/workspace/shared) are charged to the
    command writing them, never to whichever area happens to be largest."""

    client, created = sandbox
    sid = created.sandbox_id
    area_a, area_b = "/workspace/c/conv-A/agents/main", "/workspace/c/conv-B/agents/main"
    # B holds the largest AREA (20 MB) and keeps working.
    steady = asyncio.create_task(_run(
        client, sid, "head -c 20M /dev/zero > keep.bin; for i in $(seq 1 8); do sleep 1; done; echo b-done",
        skill="", workdir=area_b, fair_key="conv-B", timeout=120,
    ))
    await asyncio.sleep(1.0)
    writer = await _run(
        client, sid,
        "mkdir -p /workspace/shared && python3 -c \"import time\nf = open('/workspace/shared/big.bin', 'wb')\n"
        "for _ in range(225): f.write(bytes(1048576)); f.flush()\ntime.sleep(60)\"",
        skill="", workdir=area_a, fair_key="conv-A", timeout=120,
    )
    assert writer.outcome == "disk_killed", (writer.exit_code, writer.outcome, writer.stderr)
    assert "writing to /workspace" in (writer.outcome_detail or ""), writer.outcome_detail
    result = await steady
    assert result.exit_code == 0 and result.stdout.strip() == "b-done", result.stderr
    cleaned = await _run(client, sid, "rm -f /workspace/shared/big.bin", skill="", workdir=area_a, fair_key="conv-A")
    assert cleaned.exit_code == 0, cleaned.stderr


@pytest.mark.asyncio
async def test_two_skills_coexist_and_run_concurrently_with_their_own_view(sandbox):
    client, created = sandbox
    sid = created.sandbox_id
    assert created.skill_dir == "/skills/alpha"
    # Beta loads while an alpha command runs: loads claim only their own directory.
    alpha_busy = asyncio.create_task(_run(client, sid, "sleep 4; echo alpha-busy", skill="alpha"))
    await asyncio.sleep(0.5)
    loaded = await client.load_skill(
        sandbox_id=sid, skill_name="beta", files=dict(SKILL_B), auto_install=False,
        idle_threshold=0, env={"BETA_KEY": "b-secret"},
    )
    assert loaded.skill_dir == "/skills/beta"
    assert not alpha_busy.done()
    alpha, beta = await asyncio.gather(
        _run(client, sid, "NAP=0 python3 /skill/scripts/who.py", skill="alpha", fair_key="conv-a"),
        _run(client, sid, "NAP=2 python3 /skill/scripts/who.py", skill="beta", fair_key="conv-b"),
    )
    assert (await alpha_busy).exit_code == 0
    assert alpha.exit_code == 0, alpha.stderr
    assert "skill=alpha dir=/skills/alpha cwd=/skills/alpha key=a-secret" in alpha.stdout
    assert "data=alpha-data" in alpha.stdout
    assert beta.exit_code == 0, beta.stderr
    # Beta sees only its own env: alpha's credential never reaches it.
    assert "skill=beta dir=/skills/beta cwd=/skills/beta key=b-secret alpha=-" in beta.stdout
    assert "data=beta-data" in beta.stdout
    # An agent with no Skill view gets neither Skill's env.
    plain = await _run(
        client, sid, "echo key=${ALPHA_KEY:--}${BETA_KEY:--} dir=${SKILL_DIR:--}", skill="",
    )
    assert "key=-- dir=-" in plain.stdout
    info = await client.status(sid)
    assert sorted(info.skills) == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_work_areas_of_two_conversations_are_separate(sandbox):
    client, created = sandbox
    sid = created.sandbox_id
    one = await _run(
        client, sid, "echo one > note.txt; pwd; ls",
        skill="", workdir="/workspace/c/conv1/agents/main", fair_key="conv1",
    )
    two = await _run(
        client, sid, "pwd; ls; test ! -e note.txt && echo isolated",
        skill="", workdir="/workspace/c/conv2/agents/main", fair_key="conv2",
    )
    assert one.stdout.splitlines()[0] == "/workspace/c/conv1/agents/main"
    assert two.stdout.splitlines()[0] == "/workspace/c/conv2/agents/main"
    assert "isolated" in two.stdout
    shared = await _run(
        client, sid, "mkdir -p /workspace/shared && echo hi > /workspace/shared/x && cat /workspace/shared/x",
        skill="", workdir="/workspace/c/conv1/agents/main",
    )
    assert shared.stdout.strip() == "hi"


@pytest.mark.asyncio
async def test_commands_beyond_the_slot_limit_wait_in_fifo_order_and_can_be_withdrawn(sandbox):
    client, created = sandbox
    sid = created.sandbox_id
    info = await client.status(sid)
    slots = int(info.max_concurrent_executions or 0)
    assert slots == 4
    ids = [f"fifo-{index}-{uuid.uuid4().hex[:6]}" for index in range(slots + 3)]
    starts = []
    for index, execution_id in enumerate(ids):
        started = await client.start_exec(
            sandbox_id=sid,
            # Running commands end 0.5 s apart, so the slots free one by one
            # and the order the waiters start in is the order they got slots.
            command=f"date +%s.%N; sleep {3 + 0.5 * index if index < slots else 1}; echo done-{index}",
            timeout=60,
            execution_id=execution_id,
            skill="",
            fair_key=f"task-{index}",
            queue_timeout=120,
        )
        starts.append(started)
    assert [item.status for item in starts[:slots]] == ["running"] * slots or all(
        item.status in {"queued", "running"} for item in starts[:slots]
    )
    queued = [item for item in starts[slots:]]
    assert [item.status for item in queued] == ["queued"] * 3
    assert [item.queue_position for item in queued] == [1, 2, 3]
    # Withdraw the last waiter: it never runs.
    cancel = await client.cancel_execution(sid, ids[-1])
    assert cancel.cancelled is True
    finals = []
    for execution_id in ids:
        status = await client.execution_status(sid, execution_id)
        while not status.terminal:
            status = await client.execution_status(sid, execution_id, wait_seconds=30)
        finals.append(status)
    assert finals[-1].status == "cancelled" and finals[-1].exit_code is None
    completed = finals[:-1]
    assert all(item.status == "completed" for item in completed), [(i.status, i.error) for i in completed]
    started_at = [float(item.stdout.split()[0]) for item in completed]
    # The two queued commands started only after slots freed, in FIFO order.
    assert min(started_at[slots:]) >= max(started_at[:slots]) + 2.0
    assert started_at[slots] + 0.3 <= started_at[slots + 1]


# ── the control plane against the real runner ───────────────────────────


@pytest.fixture
def control_plane(monkeypatch):
    from packages.core import cache as cache_module
    from packages.core.config import get_settings
    from packages.core.services import sandbox_session_service
    from tests.test_sandbox_session_service import FakeCache

    fake = FakeCache()
    for name in ("get", "set", "delete", "acquire_lease", "release_lease", "extend_lease"):
        monkeypatch.setattr(cache_module.cache, name, getattr(fake, name))
    settings = get_settings()
    monkeypatch.setattr(settings, "SANDBOX_SERVICE_URL", E2E_URL)
    monkeypatch.setattr(settings, "SANDBOX_API_TOKEN", E2E_TOKEN)
    monkeypatch.setattr(settings, "SANDBOX_COORDINATION_MODE", "local")
    monkeypatch.setattr(settings, "MANOR_RUNTIME_EXECUTION_MODE", "local")
    monkeypatch.setattr(settings, "SANDBOX_EXEC_QUEUE_WAIT_SECONDS", 120)
    sandbox_session_service._RUNNER_CAPABILITIES_CACHE.clear()
    return []


async def _create(entity, user, conversation, agent="main"):
    from packages.core.ai.tools import sandbox_tools

    text = await sandbox_tools._sandbox_create(
        entity_id=entity, user_id=user, conversation_id=conversation, _agent_id_from_context=agent,
    )
    for line in text.splitlines():
        if line.startswith("sandbox_id: "):
            return line.split(": ", 1)[1]
    raise AssertionError(text)


async def _tool_exec(entity, user, conversation, sandbox_id, command, timeout=60):
    from packages.core.ai.tools import sandbox_tools

    return await sandbox_tools._sandbox_exec(
        entity_id=entity, user_id=user, conversation_id=conversation, sandbox_id=sandbox_id,
        command=command, timeout=timeout, _agent_id_from_context="main",
    )


@pytest.mark.asyncio
async def test_the_control_plane_shares_queues_isolates_and_recovers_on_real_docker(control_plane):
    import json
    import subprocess

    entity = "ent_e2e_" + uuid.uuid4().hex[:6]
    user, other_user = "user_a", "user_b"
    conversations = [f"01E2E{index}CONVERSATIONAAAAAAAA"[:26] for index in range(6)]
    client = _client()
    try:
        # One container for the user, whatever the conversation.
        ids = {await _create(entity, user, conversation) for conversation in conversations}
        assert len(ids) == 1
        [sandbox_id] = ids
        control_plane.append(sandbox_id)

        # Six conversations run a command at once: 4 slots, 2 wait their turn.
        started = time.monotonic()
        results = await asyncio.gather(*(
            _tool_exec(entity, user, conversation, sandbox_id, "sleep 3; pwd")
            for conversation in conversations
        ))
        elapsed = time.monotonic() - started
        assert all(result.endswith("[exit_code: 0]") for result in results), results
        # Each ran in its own conversation's work area.
        assert sorted(result.split()[0] for result in results) == sorted(
            f"/workspace/c/{conversation}/agents/main" for conversation in conversations
        )
        assert 6.0 <= elapsed < 30, elapsed

        # Another user: their own container; mine is denied to them by id.
        theirs = await _create(entity, other_user, conversations[0])
        control_plane.append(theirs)
        assert theirs != sandbox_id
        denied = json.loads(await _tool_exec(entity, other_user, conversations[0], sandbox_id, "id"))
        assert denied["error"]["code"] == "sandbox_access_denied"

        # The container dies mid-flight: running commands are reported lost,
        # the command still waiting for a slot runs on the user's new container.
        running = [
            asyncio.create_task(_tool_exec(entity, user, conversation, sandbox_id, "sleep 30"))
            for conversation in conversations[:4]
        ]
        await asyncio.sleep(2.0)
        waiting = asyncio.create_task(_tool_exec(entity, user, conversations[4], sandbox_id, "echo moved"))
        for _ in range(100):
            info = await client.status(sandbox_id)
            if info.queued_execution_ids:
                break
            await asyncio.sleep(0.1)
        assert info.queued_execution_ids, "the fifth command waits for a slot"
        subprocess.run(["docker", "kill", info.container_name], check=True, capture_output=True)
        lost = [json.loads(result) for result in await asyncio.gather(*running)]
        assert {item["error"]["code"] for item in lost} == {"sandbox_lost_while_running"}
        moved = await waiting
        assert "was gone before this command started" in moved and "moved" in moved, moved
        new_id = moved.split("new sandbox ")[1].split(".")[0]
        control_plane.append(new_id)
        assert new_id != sandbox_id
    finally:
        for sandbox in control_plane:
            try:
                await client.destroy(sandbox)
            except Exception:  # noqa: BLE001 - teardown
                pass
        await client.close()
