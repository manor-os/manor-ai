"""Private runtime paths for local state that must never use shared /tmp names."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile


def private_runtime_dir(*parts: str) -> Path:
    """Return an owner-only runtime directory, rejecting symlink substitution."""
    base = Path(os.getenv("MANOR_RUNTIME_DIR") or tempfile.gettempdir())
    root = base / f"manor-runtime-{os.getuid()}"
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=False)
    except FileExistsError:
        pass
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Manor runtime directory is not a trusted owner directory")
    root.chmod(0o700)
    current = root
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise ValueError("runtime path components must be simple names")
        current = current / part
        try:
            current.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            pass
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError("Manor runtime path is not a trusted owner directory")
        current.chmod(0o700)
    return current


def atomic_write_text(path: Path, value: str) -> None:
    """Replace a runtime text file without following a pre-existing symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
