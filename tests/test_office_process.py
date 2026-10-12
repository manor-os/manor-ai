import os

import pytest

from packages.core.services.office_process import office_conversion_file_limit


def test_office_conversion_file_limit_applies_hard_posix_limit(monkeypatch):
    callback = office_conversion_file_limit(12345)
    if os.name != "posix":
        assert callback is None
        pytest.skip("POSIX file-size limits are unavailable")

    import resource

    observed = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: observed.append((kind, limits)))

    assert callback is not None
    callback()

    assert observed == [(resource.RLIMIT_FSIZE, (12345, 12345))]


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); treat it as gone once it is not running.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        pass
    import subprocess

    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_run_in_process_group_timeout_kills_forked_descendants(tmp_path):
    import subprocess
    import time

    from packages.core.services.office_process import run_in_process_group

    pid_file = tmp_path / "grandchild.pid"
    # Like the soffice launcher: fork a long-lived helper, then keep waiting.
    script = f"sleep 60 & echo $! > {pid_file}; wait"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_in_process_group(
            ["/bin/sh", "-c", script],
            timeout=0.5,
            capture_output=True,
            terminate_grace_seconds=0.5,
        )
    assert time.monotonic() - started < 10
    grandchild = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while _process_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_alive(grandchild)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_run_in_process_group_matches_subprocess_run_result():
    from packages.core.services.office_process import run_in_process_group

    result = run_in_process_group(
        ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"],
        timeout=10,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_run_in_process_group_sweeps_helpers_left_by_a_finished_leader(tmp_path):
    import time

    from packages.core.services.office_process import run_in_process_group

    pid_file = tmp_path / "straggler.pid"
    result = run_in_process_group(
        ["/bin/sh", "-c", f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}"],
        timeout=10,
    )
    assert result.returncode == 0
    straggler = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while _process_alive(straggler) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _process_alive(straggler)


def test_office_converters_use_process_group_runner():
    """Every server-side LibreOffice invocation must go through the group killer."""
    import re
    from pathlib import Path

    services = Path(__file__).resolve().parents[1] / "packages" / "core" / "services"
    offenders = []
    for path in services.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"subprocess\.(?:run|Popen|call|check_call|check_output)\(\s*\[\s*([^\],]+)", source):
            first_argument = match.group(1)
            if re.search(r"soffice|libreoffice|converter", first_argument):
                offenders.append(f"{path.name}: {first_argument.strip()}")
    assert offenders == []
