"""The bash tool's working copy of entity files inside the caller's sandbox.

``bash`` runs every command in its owner's sandbox -- one container shared
by all of the user's conversations, tasks and agents. Each acting agent of
each conversation (or task) has its own working copy, inside its own
directory of that work area (``MirrorLocation``: ``<agent_dir>/workspace``,
see ``RuntimeSandboxLayout``). The entity files a command names are copied
("staged") there first, at their entity-relative path, after the same read
ACL the old local bash applied. After the command, a snapshot of that copy
is compared with what was staged to find created, modified, deleted and
moved files; the bash tool saves those back through the file mutation guard.
Commands of the user's other conversations and agents run in the same
container at the same time, each on its own copy, so a command is only ever
credited with files of its own working copy.

What is staged is recorded per working copy (``MirrorManifest``, in the
cache, keyed by sandbox and location) so an unchanged file is never copied
twice. An epoch file outside the copy ties the manifest to it: if they
disagree the copy is wiped and nothing in it is trusted or saved.

Everything that runs inside the sandbox is a short stdlib Python script
passed on the command line, so it works with any sandbox image.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import posixpath
import re
import secrets
import tarfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Inside an agent's directory of its work area (``RuntimeSandboxLayout.agent_dir``,
# ``/workspace/c/<area>/agents/<agent>``, on the shared /workspace tmpfs):
# the working copy, the bookkeeping (epoch) beside it, and the command's home.
# Dot-directories are never part of the copy, so never saved back.
MIRROR_DIR_NAME = "workspace"
MIRROR_STATE_DIR_NAME = ".manor"
MIRROR_HOME_DIR_NAME = ".home"
# An absolute path into any agent's working copy: the entity path after it.
WORKING_COPY_PATH = re.compile(r"^/workspace/c/[^/]+/agents/[^/]+/" + MIRROR_DIR_NAME + r"(?:/(.*))?$")
_MANIFEST_KEY_PREFIX = "bash-mirror:"
_MANIFEST_TTL_SECONDS = 24 * 3600
# Directories a command may fill with tool output that is never user content.
IGNORED_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git", ".venv", ".cache", ".ipynb_checkpoints"})
_HELPER_TIMEOUT_SECONDS = 120


class MirrorError(RuntimeError):
    """The mirror could not be staged, read or snapshotted."""


@dataclass(frozen=True)
class MirrorLocation:
    """Where one acting agent's working copy lives in its owner's sandbox."""

    root: str
    state: str
    home: str

    @classmethod
    def for_layout(cls, layout: Any) -> "MirrorLocation":
        """The working copy inside ``layout``'s agent directory.

        Only a root the layout owns (``RuntimeSandboxLayout.owns_path``) may
        hold a copy: file changes are synced back from it and attributed to
        that agent and its conversation or task, never from anywhere else.
        """

        agent_dir = str(layout.agent_dir).rstrip("/")
        location = cls(
            root=f"{agent_dir}/{MIRROR_DIR_NAME}",
            state=f"{agent_dir}/{MIRROR_STATE_DIR_NAME}",
            home=f"{agent_dir}/{MIRROR_HOME_DIR_NAME}",
        )
        if not all(layout.owns_path(path) for path in (location.root, location.state, location.home)):
            raise MirrorError("The bash working copy must lie inside the caller's own work area.")
        return location

    @property
    def key(self) -> str:
        return hashlib.sha256(self.root.encode("utf-8")).hexdigest()[:16]

    def manifest_id(self, sandbox_id: str) -> str:
        """The working copy's identity: its sandbox and its location in it."""

        return f"{sandbox_id}:{self.key}"


@dataclass
class MirrorEntry:
    """One staged file: its server version and its sandbox inode after staging."""

    kind: str  # "file" (content staged) or "placeholder" (metadata only, mode 000)
    sha: str
    size: int
    mtime_ns: int
    ino: int = 0
    ssize: int = 0
    smtime_ns: int = 0

    def to_list(self) -> list[Any]:
        return [self.kind, self.sha, self.size, self.mtime_ns, self.ino, self.ssize, self.smtime_ns]

    @classmethod
    def from_list(cls, value: Any) -> "MirrorEntry | None":
        if not isinstance(value, list) or len(value) != 7:
            return None
        kind, sha, size, mtime_ns, ino, ssize, smtime_ns = value
        if kind not in {"file", "placeholder"}:
            return None
        try:
            return cls(str(kind), str(sha or ""), int(size), int(mtime_ns), int(ino), int(ssize), int(smtime_ns))
        except (TypeError, ValueError):
            return None


@dataclass
class MirrorManifest:
    # ``MirrorLocation.manifest_id``: one manifest per working copy.
    sandbox_id: str
    epoch: str
    files: dict[str, MirrorEntry] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=set)
    # False for a manifest nothing was stored for: the container's copy (if
    # any) is then untrusted and the next staging step starts it over.
    persisted: bool = False

    @property
    def staged_bytes(self) -> int:
        return sum(entry.size for entry in self.files.values() if entry.kind == "file")

    def to_record(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "files": {path: entry.to_list() for path, entry in self.files.items()},
            "dirs": sorted(self.dirs),
        }

    @classmethod
    def fresh(cls, sandbox_id: str) -> "MirrorManifest":
        return cls(sandbox_id=sandbox_id, epoch=secrets.token_hex(12))


def _manifest_key(sandbox_id: str) -> str:
    return f"{_MANIFEST_KEY_PREFIX}{sandbox_id}"


async def load_manifest(sandbox_id: str) -> MirrorManifest:
    """The sandbox's manifest, or a fresh one (new epoch) when none is stored."""

    from packages.core.cache import cache

    record = await cache.get(_manifest_key(sandbox_id))
    if not isinstance(record, dict) or not str(record.get("epoch") or ""):
        return MirrorManifest.fresh(sandbox_id)
    files: dict[str, MirrorEntry] = {}
    for path, raw in dict(record.get("files") or {}).items():
        entry = MirrorEntry.from_list(raw)
        if entry is not None:
            files[str(path)] = entry
    dirs = {str(path) for path in record.get("dirs") or [] if str(path)}
    return MirrorManifest(
        sandbox_id=sandbox_id, epoch=str(record["epoch"]), files=files, dirs=dirs, persisted=True,
    )


async def save_manifest(manifest: MirrorManifest) -> None:
    from packages.core.cache import cache

    if not await cache.set(_manifest_key(manifest.sandbox_id), manifest.to_record(), ttl=_MANIFEST_TTL_SECONDS):
        raise MirrorError("The bash working-copy state could not be saved (cache unavailable).")
    manifest.persisted = True


async def forget_manifest(sandbox_id: str) -> None:
    from packages.core.cache import cache

    await cache.delete(_manifest_key(sandbox_id))


def mirror_rel_path(token: str) -> str | None:
    """Entity-relative path for an absolute path into a working copy, else None."""

    text = str(token or "").replace("\\", "/")
    match = WORKING_COPY_PATH.match(text)
    if match is None:
        return None
    rest = posixpath.normpath(match.group(1) or ".")
    if rest == "." or rest == "":
        return ""
    if rest.startswith("../") or rest == "..":
        return None
    return rest


# ── scripts that run inside the sandbox ──

_STAGE_SCRIPT = r'''
import base64, io, json, os, shutil, sys, tarfile
job_path = sys.argv[1]
with open(job_path, encoding="utf-8") as handle:
    job = json.load(handle)
os.unlink(job_path)
root, state = job["root"], job["state"]
os.makedirs(root, exist_ok=True)
os.makedirs(state, exist_ok=True)
epoch_file = os.path.join(state, "epoch")
try:
    current = open(epoch_file, encoding="utf-8").read().strip()
except OSError:
    current = ""
reset = current != job["epoch"] or job.get("reset")
def remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)
if reset:
    for name in os.listdir(root):
        remove(os.path.join(root, name))
    with open(epoch_file, "w", encoding="utf-8") as handle:
        handle.write(job["epoch"])
real_root = os.path.realpath(root)
def safe(rel):
    full = os.path.normpath(os.path.join(root, rel))
    parent = os.path.realpath(os.path.dirname(full))
    if not (full.startswith(root.rstrip("/") + "/") and (parent == real_root or parent.startswith(real_root + "/"))):
        raise SystemExit("unsafe mirror path: %r" % rel)
    return full
for rel in job.get("deletes") or []:
    remove(safe(rel))
for rel in job.get("dirs") or []:
    path = safe(rel)
    if os.path.lexists(path) and not os.path.isdir(path):
        remove(path)
    os.makedirs(path, exist_ok=True)
data = job.get("tar_b64") or ""
if data:
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(data)), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                raise SystemExit("mirror archive holds a non-file member")
            target = safe(member.name)
            if os.path.lexists(target):
                remove(target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            source = archive.extractfile(member)
            with open(target, "wb") as out:
                shutil.copyfileobj(source, out)
            os.chmod(target, 0o644)
for rel, size in job.get("placeholders") or []:
    target = safe(rel)
    if os.path.lexists(target):
        remove(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.ftruncate(fd, int(size))
    finally:
        os.close(fd)
    os.chmod(target, 0)
stats = {}
for rel in job.get("report") or []:
    info = os.lstat(safe(rel))
    stats[rel] = [info.st_ino, info.st_size, info.st_mtime_ns]
print(json.dumps({"reset": bool(reset), "stats": stats}))
'''

_SNAPSHOT_SCRIPT = r'''
import hashlib, json, os, sys
job_path = sys.argv[1]
with open(job_path, encoding="utf-8") as handle:
    job = json.load(handle)
os.unlink(job_path)
root, state = job["root"], job["state"]
known = job.get("known") or {}
ignored_names = set(job.get("ignore") or [])
limit = int(job.get("max_entries") or 20000)
try:
    epoch = open(os.path.join(state, "epoch"), encoding="utf-8").read().strip()
except OSError:
    epoch = ""
entries, ignored, truncated = [], [], False
if os.path.isdir(root) and not os.path.islink(root):
    for directory, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(directory, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        kept = []
        for name in sorted(dirnames):
            if name.startswith("."):
                continue
            rel = os.path.join(rel_dir, name) if rel_dir else name
            full = os.path.join(directory, name)
            if os.path.islink(full):
                entries.append([rel, "l", 0, 0, 0, 0, None])
            elif name in ignored_names:
                ignored.append(rel)
            else:
                kept.append(name)
                entries.append([rel, "d", 0, 0, 0, 0, None])
        dirnames[:] = kept
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            rel = os.path.join(rel_dir, name) if rel_dir else name
            full = os.path.join(directory, name)
            info = os.lstat(full)
            if os.path.islink(full):
                entries.append([rel, "l", info.st_ino, 0, 0, 0, None])
                continue
            mode = info.st_mode & 0o7777
            digest = None
            if known.get(rel) != [info.st_ino, info.st_size, info.st_mtime_ns] and mode & 0o400:
                sha = hashlib.sha256()
                with open(full, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        sha.update(chunk)
                digest = sha.hexdigest()
            entries.append([rel, "f", info.st_ino, info.st_size, info.st_mtime_ns, mode, digest])
        if len(entries) > limit:
            truncated = True
            break
print(json.dumps({"epoch": epoch, "entries": entries, "ignored": ignored, "truncated": truncated}))
'''

_COLLECT_SCRIPT = r'''
import base64, io, json, os, sys, tarfile
job_path = sys.argv[1]
with open(job_path, encoding="utf-8") as handle:
    job = json.load(handle)
os.unlink(job_path)
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w") as archive:
    for rel in job["paths"]:
        archive.add(os.path.join(job["root"], rel), arcname=rel, recursive=False)
sys.stdout.write(base64.b64encode(buffer.getvalue()).decode("ascii"))
'''


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    type: str  # "f", "d" or "l"
    ino: int = 0
    size: int = 0
    mtime_ns: int = 0
    mode: int = 0
    sha: str | None = None


@dataclass(frozen=True)
class MirrorSnapshot:
    epoch: str
    entries: dict[str, SnapshotEntry]
    ignored: tuple[str, ...]
    truncated: bool

    @property
    def files(self) -> dict[str, SnapshotEntry]:
        return {path: entry for path, entry in self.entries.items() if entry.type == "f"}

    @property
    def dirs(self) -> set[str]:
        return {path for path, entry in self.entries.items() if entry.type == "d"}

    @property
    def links(self) -> set[str]:
        return {path for path, entry in self.entries.items() if entry.type == "l"}


class SandboxMirror:
    """Runs the mirror scripts on one working copy through a ``SandboxClient``.

    The scripts are bookkeeping of the platform's own -- moving files into
    and out of the working copy -- so they run on the runner's side channel
    (``side_channel=True``), next to the command slots like any file read or
    write: they never wait in the slot queue, so they can never wait behind
    the command they stage for or read back (which may itself be queued, its
    run parked), and a sandbox whose every slot is taken by the user's other
    work never holds a bash call in-process.
    """

    def __init__(
        self,
        client: Any,
        sandbox_id: str,
        location: MirrorLocation,
    ) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self.location = location

    @property
    def manifest_id(self) -> str:
        return self.location.manifest_id(self._sandbox_id)

    async def _run_job(self, script: str, job: dict[str, Any], *, tag: str) -> str:
        job_path = f"/tmp/manor-{tag}-{secrets.token_hex(8)}.json"
        payload = json.dumps(job, separators=(",", ":")).encode("utf-8")
        await self._client.write_file_base64(
            self._sandbox_id, job_path, base64.b64encode(payload).decode("ascii"),
        )
        result = await self._client.exec(
            sandbox_id=self._sandbox_id,
            command=f"python3 -c {_shell_quote(script)} {job_path}",
            timeout=_HELPER_TIMEOUT_SECONDS,
            workdir="/tmp",  # nosec B108 -- path is inside the sandbox container
            side_channel=True,
        )
        if result.exit_code != 0:
            raise MirrorError(
                f"bash working-copy {tag} failed in the sandbox: "
                f"{(result.stderr or result.stdout or '').strip()[:500]}"
            )
        return result.stdout or ""

    async def stage(
        self,
        *,
        epoch: str,
        reset: bool = False,
        files: dict[str, bytes] | None = None,
        placeholders: dict[str, int] | None = None,
        deletes: list[str] | None = None,
        dirs: list[str] | None = None,
    ) -> tuple[bool, dict[str, tuple[int, int, int]]]:
        """Apply one staging step; returns (mirror was reset, {path: (ino, size, mtime_ns)})."""

        archive = io.BytesIO()
        if files:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for rel, data in sorted(files.items()):
                    info = tarfile.TarInfo(rel)
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
        job = {
            "root": self.location.root,
            "state": self.location.state,
            "epoch": epoch,
            "reset": reset,
            "deletes": list(deletes or []),
            "dirs": sorted(dirs or []),
            "tar_b64": base64.b64encode(archive.getvalue()).decode("ascii") if files else "",
            "placeholders": sorted([rel, int(size)] for rel, size in (placeholders or {}).items()),
            "report": sorted(set(files or {}) | set(placeholders or {})),
        }
        output = await self._run_job(_STAGE_SCRIPT, job, tag="stage")
        try:
            parsed = json.loads(output.strip().splitlines()[-1])
            stats = {
                str(path): (int(value[0]), int(value[1]), int(value[2]))
                for path, value in dict(parsed.get("stats") or {}).items()
            }
            return bool(parsed.get("reset")), stats
        except (IndexError, TypeError, ValueError) as exc:
            raise MirrorError(f"bash working-copy staging returned no report: {exc}") from exc

    async def snapshot(self, manifest: MirrorManifest, *, max_entries: int) -> MirrorSnapshot:
        job = {
            "root": self.location.root,
            "state": self.location.state,
            "known": {
                path: [entry.ino, entry.ssize, entry.smtime_ns]
                for path, entry in manifest.files.items()
            },
            "ignore": sorted(IGNORED_DIR_NAMES),
            "max_entries": max_entries,
        }
        output = await self._run_job(_SNAPSHOT_SCRIPT, job, tag="snapshot")
        try:
            parsed = json.loads(output.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise MirrorError(f"bash working-copy snapshot returned no report: {exc}") from exc
        entries: dict[str, SnapshotEntry] = {}
        for raw in parsed.get("entries") or []:
            path, kind, ino, size, mtime_ns, mode, sha = raw
            entries[str(path)] = SnapshotEntry(
                path=str(path), type=str(kind), ino=int(ino), size=int(size),
                mtime_ns=int(mtime_ns), mode=int(mode), sha=str(sha) if sha else None,
            )
        return MirrorSnapshot(
            epoch=str(parsed.get("epoch") or ""),
            entries=entries,
            ignored=tuple(str(path) for path in parsed.get("ignored") or []),
            truncated=bool(parsed.get("truncated")),
        )

    async def collect(self, paths: list[str]) -> dict[str, bytes]:
        """Bytes of the given mirror files."""

        if not paths:
            return {}
        output = await self._run_job(
            _COLLECT_SCRIPT, {"root": self.location.root, "paths": sorted(paths)}, tag="collect",
        )
        try:
            raw = base64.b64decode(output.strip(), validate=True)
        except ValueError as exc:
            raise MirrorError(f"bash working-copy read-back was not readable: {exc}") from exc
        result: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                result[member.name] = handle.read() if handle else b""
        missing = set(paths) - set(result)
        if missing:
            raise MirrorError(f"bash working-copy read-back is missing {sorted(missing)[:5]}")
        return result


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
