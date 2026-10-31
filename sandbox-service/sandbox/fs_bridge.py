"""
File system bridge for reading/writing files inside a sandbox container.

Inspired by OpenClaw's fs-bridge.ts — provides safe file operations
between the host API server and the sandboxed container.
"""

from __future__ import annotations

import asyncio
import logging
import shlex

from .docker_backend import DockerSandbox, rewrite_skill_paths, skill_dir
from .errors import (
    FileOperationFailed,
    FileTooLarge,
    InvalidPath,
    InvalidFileContent,
    SandboxFileNotFound,
)

logger = logging.getLogger(__name__)

# Files whose /skill paths are mapped to the writer's Skill directory when a
# Skill's view writes them: scripts and configs it will run or read later.
_SKILL_VIEW_TEXT_SUFFIXES = (
    ".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
)

# File commands include Docker exec startup, even when the command itself is small.
_FILE_COMMAND_TIMEOUT = 30


_PATH_ERROR_MARKERS = (
    "Read-only file system",
    "No such file or directory",
    "Directory nonexistent",
    "Permission denied",
    "Not a directory",
    "Is a directory",
)


def _is_path_error(message: str) -> bool:
    return any(marker in message for marker in _PATH_ERROR_MARKERS)


class FsBridge:
    """Read/write files inside a running sandbox container.

    Every operation is a side-channel command (``DockerSandbox.side_exec``):
    it runs next to a running command, refreshes the sandbox's idle lease and
    reports a vanished container as ``SandboxLost``. Failures are typed:
    missing file -> ``SandboxFileNotFound``, unusable path -> ``InvalidPath``,
    oversized file -> ``FileTooLarge``, anything else -> ``FileOperationFailed``.
    """

    def __init__(self, sandbox: DockerSandbox):
        self._sandbox = sandbox

    @property
    def workdir(self) -> str:
        """Relative paths of a caller naming no Skill: the current Skill's directory."""
        return self._base(self._slot(None))

    def _slot(self, skill: str | None) -> str | None:
        """The Skill slot a request sees as /skill (None: no view, or a single-/skill sandbox)."""
        resolve = getattr(self._sandbox, "resolve_view", None)
        return resolve(skill) if resolve is not None else None

    def _base(self, slot: str | None) -> str:
        if slot:
            return skill_dir(slot)
        if getattr(self._sandbox, "resolve_view", None) is not None:
            return self._sandbox.default_cwd()
        return self._sandbox.config.workdir

    async def _run(self, command: str, *, timeout: float, input_data: bytes | None = None):
        if input_data is None:
            return await self._sandbox.side_exec(command, timeout=timeout)
        return await self._sandbox.side_exec(command, timeout=timeout, input_data=input_data)

    async def read_file(
        self, path: str, max_size: int = 65536, *, skill: str | None = None,
    ) -> tuple[str, bool]:
        """
        Read a file from the container.

        Returns (content, truncated).
        """
        safe_path = shlex.quote(self._resolve(path, skill=skill))
        result = await self._run(
            f"head -c {max_size + 1} {safe_path}",
            timeout=_FILE_COMMAND_TIMEOUT,
        )
        if result.exit_code != 0:
            _raise_for_file_error(path, "read", result.stderr)

        content = result.stdout
        truncated = len(content) > max_size
        if truncated:
            content = content[:max_size]
        return content, truncated

    async def read_file_base64(
        self, path: str, max_size: int = 50 * 1024 * 1024, *, skill: str | None = None,
    ) -> tuple[str, int]:
        """
        Read a binary file from the container as base64.

        Returns (base64_content, original_file_size).
        """
        safe_path = shlex.quote(self._resolve(path, skill=skill))

        # This budget includes Docker exec startup, which can exceed five
        # seconds after rendering under load. Keep binary delivery bounded.
        size_result = await self._run(
            f"stat -L -c '%s' {safe_path}", timeout=_FILE_COMMAND_TIMEOUT,
        )
        if size_result.exit_code != 0:
            _raise_for_file_error(path, "stat", size_result.stderr)

        try:
            file_size = int(size_result.stdout.strip())
        except ValueError:
            raise FileOperationFailed(
                f"Failed to stat {path}: unexpected output {size_result.stdout.strip()[:100]!r}"
            ) from None
        if file_size > max_size:
            raise FileTooLarge(
                f"File too large: {file_size} bytes (max {max_size})"
            )

        result = await self._run(
            f"base64 -w0 {safe_path}", timeout=120,
        )
        if result.exit_code != 0:
            _raise_for_file_error(path, "base64-encode", result.stderr)

        return result.stdout.strip(), file_size

    async def write_file(
        self, path: str, content: str, *, mkdir: bool = True, skill: str | None = None,
    ) -> None:
        """Write content to a file inside the container.

        Through a Skill's view, a script or config it writes has its
        ``/skill`` paths mapped to the Skill's directory, like the Skill's
        own files, so it runs the same next to other Skills.
        """
        safe_path = self._resolve(path, skill=skill)
        slot = self._slot(skill)
        if slot and safe_path.lower().endswith(_SKILL_VIEW_TEXT_SUFFIXES):
            content = rewrite_skill_paths(content, slot)

        if mkdir:
            await self._ensure_parent_dir(safe_path)

        data = content.encode("utf-8")
        await self._pipe_bytes_to_file(safe_path, data)

    async def write_file_base64(
        self, path: str, content_base64: str, *, mkdir: bool = True, skill: str | None = None,
    ) -> int:
        """Write a binary file into the container from base64 content.

        Returns the number of raw bytes written. Used to deliver generated
        binary assets (e.g. images) straight into the sandbox without a
        text/utf-8 round-trip that would corrupt them.
        """
        import base64

        safe_path = self._resolve(path, skill=skill)
        try:
            # Uploads reach tens of MB; decoding on the event loop stalls /health.
            data = await asyncio.to_thread(base64.b64decode, content_base64, validate=True)
        except ValueError as exc:  # binascii.Error subclasses ValueError
            raise InvalidFileContent(f"Invalid base64 content for {path}: {exc}") from exc
        if not data:
            raise InvalidFileContent(f"Refusing to write empty file: {path}")

        if mkdir:
            await self._ensure_parent_dir(safe_path)
        await self._pipe_bytes_to_file(safe_path, data)
        return len(data)

    async def _ensure_parent_dir(self, safe_path: str) -> None:
        """Create the parent directory, or explain why the path is not writable.

        The container root is read-only; only the skill workdir and ``/tmp``
        accept writes. A silent ``mkdir`` failure used to surface later as an
        opaque 500 from ``cat >``; the agent needs the real reason to pick a
        writable path instead.
        """
        parent = str(_posix_parent(safe_path))
        # Include Docker exec startup, as in the subsequent bounded byte transfer.
        result = await self._run(f"mkdir -p {shlex.quote(parent)}", timeout=_FILE_COMMAND_TIMEOUT)
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit {result.exit_code}"
            raise InvalidPath(
                f"Cannot create directory {parent} for {safe_path}: {detail}. "
                "Writable locations are the Skill's directory (/skill, its working directory), your "
                "work area under "
                "/workspace, /tmp, and any mounted path listed in the sandbox contract."
            )

    async def _pipe_bytes_to_file(self, safe_path: str, data: bytes) -> None:
        """Stream raw bytes into a container file via `docker exec -i cat >`."""
        result = await self._run(
            f"cat > {shlex.quote(safe_path)}",
            timeout=120,
            input_data=data,
        )
        if result.exit_code != 0:
            stderr = (result.stderr or "").strip() or f"exit {result.exit_code}"
            # Only a path problem the caller can fix becomes a caller error; a
            # dead daemon, a vanished container or a timeout is raised by
            # side_exec as its own typed fault before we get here.
            if _is_path_error(stderr):
                raise InvalidPath(f"Cannot write {safe_path}: {stderr}")
            raise FileOperationFailed(f"Failed to write {safe_path}: {stderr}")

    async def stat(self, path: str) -> dict | None:
        """
        Stat a file. Returns {type, size, mtime_epoch} or None if missing.
        """
        safe_path = shlex.quote(self._resolve(path))
        result = await self._run(
            f"stat -L -c '%F|%s|%Y' {safe_path}",
            timeout=_FILE_COMMAND_TIMEOUT,
        )
        if result.exit_code != 0:
            return None
        parts = result.stdout.strip().split("|")
        if len(parts) < 3:
            return None
        ftype = "directory" if "directory" in parts[0] else "file"
        return {
            "type": ftype,
            "size": int(parts[1]),
            "mtime_epoch": int(parts[2]),
        }

    async def list_dir(self, path: str) -> list[str]:
        """List files in a directory inside the container."""
        safe_path = shlex.quote(self._resolve(path))
        result = await self._run(f"ls -1 {safe_path}", timeout=_FILE_COMMAND_TIMEOUT)
        if result.exit_code != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        safe_path = shlex.quote(self._resolve(path))
        flags = "-rf" if recursive else "-f"
        result = await self._run(f"rm {flags} {safe_path}", timeout=_FILE_COMMAND_TIMEOUT)
        if result.exit_code != 0:
            _raise_for_file_error(path, "remove", result.stderr)

    async def read_key_skill_files(
        self,
        skill_scripts: list[str],
        entry_hint: str | None = None,
        max_files: int = 5,
        max_size: int = 8000,
    ) -> dict[str, str]:
        """
        Read key script files from the sandbox to build LLM context.
        Returns {relative_path: content}.
        """
        priority: list[str] = []
        if entry_hint:
            priority.append(entry_hint)
        priority.extend(s for s in skill_scripts if s not in priority)

        contents: dict[str, str] = {}
        for script_path in priority[:max_files]:
            try:
                full_path = f"{self.workdir}/{script_path}"
                content, _ = await self.read_file(full_path, max_size=max_size)
                contents[script_path] = content
            except OSError:
                # A missing or unreadable script is skipped; a lost
                # container or a Docker fault is not an OSError and propagates.
                continue
        return contents

    # ── internals ──

    def _resolve(self, path: str, *, skill: str | None = None) -> str:
        """Ensure path is absolute within the container, under the caller's Skill view.

        ``/skill/...`` is the viewed Skill's own directory; a relative path
        is relative to it (or, with no Skill view, to the shared /workspace).
        """
        if not isinstance(path, str) or not path.strip():
            raise InvalidPath("path must not be empty")
        if "\x00" in path or "\n" in path or "\r" in path:
            raise InvalidPath("path must not contain NUL or newline characters")
        slot = self._slot(skill)
        if path.startswith("/"):
            return rewrite_skill_paths(path, slot)
        return f"{self._base(slot)}/{path}"


def _raise_for_file_error(path: str, operation: str, stderr: str) -> None:
    detail = (stderr or "").strip()
    if "No such file" in detail:
        raise SandboxFileNotFound(f"File not found in sandbox: {path}")
    if _is_path_error(detail):
        raise InvalidPath(f"Cannot {operation} {path}: {detail}")
    raise FileOperationFailed(f"Failed to {operation} {path}: {detail or 'no error details'}")


def _posix_parent(path: str) -> str:
    parts = path.rsplit("/", 1)
    return parts[0] if len(parts) > 1 and parts[0] else "/"
