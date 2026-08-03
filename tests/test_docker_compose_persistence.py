from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_api_and_worker_receive_cloud_model_routing_environment():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    for service_name in ("api", "worker"):
        environment = compose["services"][service_name]["environment"]
        assert environment["DEPLOYMENT_MODE"] == "${DEPLOYMENT_MODE:-oss}"
        assert environment["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY:-}"


def test_redis_aof_protects_juicefs_metadata():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = ROOT / "scripts" / "redis-aof-entrypoint.sh"

    redis_service = compose.split("  redis:\n", 1)[1].split("\n  #", 1)[0]
    assert "redis-aof-entrypoint.sh" in redis_service
    assert entrypoint.is_file()
    script = entrypoint.read_text(encoding="utf-8")
    assert "command -v gosu" in script
    assert "command -v su-exec" in script
    assert "command -v su" in script
    assert "CONFIG SET appendonly yes" in script
    assert "aof_rewrite_in_progress" in script
    assert "exec redis-server" in script
    assert "--appendonly yes" in script
    assert "--appendfsync everysec" in script


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is unavailable")
def test_redis_aof_entrypoint_migrates_existing_rdb_without_losing_db1(tmp_path):
    if subprocess.run(["docker", "info"], capture_output=True, text=True).returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    data = tmp_path / "redis-data"
    data.mkdir()
    entrypoint = ROOT / "scripts" / "redis-aof-entrypoint.sh"
    name = f"manor-redis-aof-test-{uuid.uuid4().hex[:10]}"

    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            check=check,
            capture_output=True,
            text=True,
            timeout=90,
        )

    try:
        run(
            "run", "-d", "--name", name,
            "-v", f"{data}:/data",
            "redis:7-alpine", "redis-server", "--appendonly", "no",
        )
        ready = False
        for _ in range(60):
            if run("exec", name, "redis-cli", "ping", check=False).stdout.strip() == "PONG":
                ready = True
                break
            time.sleep(0.1)
        if not ready:
            logs = run("logs", name, check=False)
            state = run("inspect", name, "--format", "{{json .State}}", check=False)
            pytest.fail(f"initial Redis failed to start: {logs.stdout}{logs.stderr}\n{state.stdout}{state.stderr}")
        run("exec", name, "redis-cli", "-n", "1", "SET", "juicefs:test:key", "preserved")
        run("exec", name, "redis-cli", "SAVE")
        run("rm", "-f", name)

        run(
            "run", "-d", "--name", name,
            "-v", f"{data}:/data",
            "-v", f"{entrypoint}:/usr/local/bin/manor-redis-aof-entrypoint.sh:ro",
            "redis:7-alpine", "/bin/sh", "-c",
            "rm -f /usr/local/bin/gosu; exec /bin/sh /usr/local/bin/manor-redis-aof-entrypoint.sh",
        )
        ready = False
        for _ in range(120):
            if run("exec", name, "redis-cli", "ping", check=False).stdout.strip() == "PONG":
                ready = True
                break
            time.sleep(0.1)
        if not ready:
            logs = run("logs", name, check=False)
            state = run("inspect", name, "--format", "{{json .State}}", check=False)
            pytest.fail(f"migrated Redis failed to start: {logs.stdout}{logs.stderr}\n{state.stdout}{state.stderr}")

        assert run("exec", name, "redis-cli", "-n", "1", "GET", "juicefs:test:key").stdout.strip() == "preserved"
        config = run("exec", name, "redis-cli", "CONFIG", "GET", "appendonly").stdout.splitlines()
        assert config[-1] == "yes"

        run("rm", "-f", name)
        run(
            "run", "-d", "--name", name,
            "-v", f"{data}:/data",
            "-v", f"{entrypoint}:/usr/local/bin/manor-redis-aof-entrypoint.sh:ro",
            "redis:7-alpine", "/bin/sh", "-c",
            "rm -f /usr/local/bin/gosu; exec /bin/sh /usr/local/bin/manor-redis-aof-entrypoint.sh",
        )
        ready = False
        for _ in range(120):
            if run("exec", name, "redis-cli", "ping", check=False).stdout.strip() == "PONG":
                ready = True
                break
            time.sleep(0.1)
        assert ready
        assert run("exec", name, "redis-cli", "-n", "1", "GET", "juicefs:test:key").stdout.strip() == "preserved"
    finally:
        run("rm", "-f", name, check=False)
