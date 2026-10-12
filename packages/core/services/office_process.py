"""Process limits shared by server-side Office conversions."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)


def office_conversion_file_limit(max_bytes: int) -> Callable[[], None] | None:
    """Return a child-only hard file-size limit for LibreOffice outputs."""
    if os.name != "posix":
        return None
    limit = max(1, int(max_bytes))

    def apply_limit() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return apply_limit


OFFICE_PROCESS_TERMINATE_GRACE_SECONDS = 5.0


def _signal_process_group(process_group_id: int, signum: int) -> bool:
    try:
        os.killpg(process_group_id, signum)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def terminate_process_group(
    process: subprocess.Popen,
    *,
    grace_seconds: float = OFFICE_PROCESS_TERMINATE_GRACE_SECONDS,
) -> None:
    """Stop a session leader and every process it forked (SIGTERM, then SIGKILL).

    LibreOffice's ``soffice`` launcher forks ``soffice.bin``; killing only the
    launcher leaves the real converter orphaned and still holding CPU, memory
    and the temporary profile. ``process`` must have been started with
    ``start_new_session=True`` so its pid is also the process group id.
    """
    process_group_id = process.pid
    if _signal_process_group(process_group_id, signal.SIGTERM):
        try:
            process.wait(timeout=max(0.0, grace_seconds))
        except subprocess.TimeoutExpired:
            pass
    # Always sweep the group: descendants can outlive the leader.
    _signal_process_group(process_group_id, signal.SIGKILL)
    try:
        process.wait(timeout=max(1.0, grace_seconds))
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is not ignorable
        logger.warning("Office process %s did not exit after SIGKILL", process_group_id)


def run_in_process_group(
    args,
    *,
    timeout: float | None,
    capture_output: bool = False,
    input=None,
    check: bool = False,
    terminate_grace_seconds: float = OFFICE_PROCESS_TERMINATE_GRACE_SECONDS,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for external converters that fork helper processes.

    The command runs as the leader of a new session/process group. On timeout
    (or any interruption) the whole group is terminated, not just the direct
    child, and ``subprocess.TimeoutExpired`` is raised like ``subprocess.run``.
    Stragglers left behind by a leader that already exited are swept as well.
    """
    if os.name != "posix":
        return subprocess.run(
            args,
            timeout=timeout,
            capture_output=capture_output,
            input=input,
            check=check,
            **popen_kwargs,
        )
    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
    popen_kwargs["start_new_session"] = True
    with subprocess.Popen(args, **popen_kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process_group(process, grace_seconds=terminate_grace_seconds)
            raise subprocess.TimeoutExpired(
                exc.cmd,
                exc.timeout,
                output=exc.output,
                stderr=exc.stderr,
            ) from None
        except BaseException:
            terminate_process_group(process, grace_seconds=terminate_grace_seconds)
            raise
        finally:
            # A leader that exited normally can still leave forked helpers.
            _signal_process_group(process.pid, signal.SIGKILL)
        returncode = process.poll()
        if returncode is None:  # pragma: no cover - communicate() waits
            returncode = process.wait()
    completed = subprocess.CompletedProcess(args, returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed
