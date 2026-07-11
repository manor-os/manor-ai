"""
File system bridge for reading/writing files inside a sandbox container.

Inspired by OpenClaw's fs-bridge.ts — provides safe file operations
between the host API server and the sandboxed container.
"""

from __future__ import annotations

import logging
import shlex

from .docker_backend import DockerSandbox
from .models import ExecResponse

logger = logging.getLogger(__name__)


class FsBridge:
    """Read/write files inside a running sandbox container."""

    def __init__(self, sandbox: DockerSandbox):
        self._sandbox = sandbox

    @property
    def workdir(self) -> str:
        return self._sandbox.config.workdir

    async def read_file(self, path: str, max_size: int = 65536) -> tuple[str, bool]:
        """
        Read a file from the container.

        Returns (content, truncated).
        """
        safe_path = shlex.quote(self._resolve(path))
        result = await self._sandbox.exec(
            f"head -c {max_size + 1} {safe_path}",
            timeout=10,
        )
        if result.exit_code != 0:
            stderr = result.stderr.strip()
            if "No such file" in stderr:
                raise FileNotFoundError(f"File not found in sandbox: {path}")
            raise IOError(f"Failed to read {path}: {stderr}")

        content = result.stdout
        truncated = len(content) > max_size
        if truncated:
            content = content[:max_size]
        return content, truncated

    async def read_file_base64(
        self, path: str, max_size: int = 50 * 1024 * 1024,
    ) -> tuple[str, int]:
        """
        Read a binary file from the container as base64.

        Returns (base64_content, original_file_size).
        Raises FileNotFoundError / IOError on failure.
        """
        safe_path = shlex.quote(self._resolve(path))

        size_result = await self._sandbox.exec(
            f"stat -c '%s' {safe_path}", timeout=5,
        )
        if size_result.exit_code != 0:
            stderr = size_result.stderr.strip()
            if "No such file" in stderr:
                raise FileNotFoundError(f"File not found in sandbox: {path}")
            raise IOError(f"Failed to stat {path}: {stderr}")

        file_size = int(size_result.stdout.strip())
        if file_size > max_size:
            raise IOError(
                f"File too large: {file_size} bytes (max {max_size})"
            )

        result = await self._sandbox.exec(
            f"base64 -w0 {safe_path}", timeout=120,
        )
        if result.exit_code != 0:
            raise IOError(f"Failed to base64-encode {path}: {result.stderr.strip()}")

        return result.stdout.strip(), file_size

    async def write_file(
        self, path: str, content: str, *, mkdir: bool = True,
    ) -> None:
        """Write content to a file inside the container."""
        safe_path = self._resolve(path)

        if mkdir:
            parent = str(_posix_parent(safe_path))
            await self._sandbox.exec(f"mkdir -p {shlex.quote(parent)}", timeout=5)

        data = content.encode("utf-8")
        await self._pipe_bytes_to_file(safe_path, data)

    async def write_file_base64(
        self, path: str, content_base64: str, *, mkdir: bool = True,
    ) -> int:
        """Write a binary file into the container from base64 content.

        Returns the number of raw bytes written. Used to deliver generated
        binary assets (e.g. images) straight into the sandbox without a
        text/utf-8 round-trip that would corrupt them.
        """
        import base64

        safe_path = self._resolve(path)
        if mkdir:
            parent = str(_posix_parent(safe_path))
            await self._sandbox.exec(f"mkdir -p {shlex.quote(parent)}", timeout=5)

        try:
            data = base64.b64decode(content_base64, validate=True)
        except ValueError as exc:  # binascii.Error subclasses ValueError
            raise IOError(f"Invalid base64 content for {path}: {exc}") from exc
        if not data:
            raise IOError(f"Refusing to write empty file: {path}")

        await self._pipe_bytes_to_file(safe_path, data)
        return len(data)

    async def _pipe_bytes_to_file(self, safe_path: str, data: bytes) -> None:
        """Stream raw bytes into a container file via `docker exec -i cat >`."""
        from .docker_backend import _run_docker
        import asyncio

        args = [
            "docker", "exec", "-i",
            self._sandbox.container_name,
            "sh", "-c", f"cat > {shlex.quote(safe_path)}",
        ]
        await asyncio.to_thread(
            _run_docker, args, input_data=data, timeout=30,
        )

    async def stat(self, path: str) -> dict | None:
        """
        Stat a file. Returns {type, size, mtime_epoch} or None if missing.
        """
        safe_path = shlex.quote(self._resolve(path))
        result = await self._sandbox.exec(
            f"stat -c '%F|%s|%Y' {safe_path}",
            timeout=5,
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
        result = await self._sandbox.exec(f"ls -1 {safe_path}", timeout=5)
        if result.exit_code != 0:
            return []
        return [f for f in result.stdout.strip().split("\n") if f]

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        safe_path = shlex.quote(self._resolve(path))
        flags = "-rf" if recursive else "-f"
        result = await self._sandbox.exec(f"rm {flags} {safe_path}", timeout=10)
        if result.exit_code != 0:
            raise IOError(f"Failed to remove {path}: {result.stderr.strip()}")

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
            except (FileNotFoundError, IOError):
                continue
        return contents

    # ── internals ──

    def _resolve(self, path: str) -> str:
        """Ensure path is absolute within the container."""
        if path.startswith("/"):
            return path
        return f"{self.workdir}/{path}"


def _posix_parent(path: str) -> str:
    parts = path.rsplit("/", 1)
    return parts[0] if len(parts) > 1 and parts[0] else "/"
