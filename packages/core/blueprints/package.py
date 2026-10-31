"""Blueprint packages: a payload plus the files its App and Skills point at.

A Blueprint payload is configuration. Some of that configuration names files:
an App cover or motion cover, a choice's picture or audio sample, an entry
picture. When those files are the Workspace's own uploads, the payload alone
carries only their addresses, which mean nothing on another account or
deployment, so the installed App shows empty frames. A package carries the
files themselves.

What travels as a file: every payload value that is *exactly* an address of a
file in the exporting entity (``/api/v1/fs/<entity>/...``, ``/viewer/<id>``,
``/documents/<id>``) the exporter can read. Text that merely mentions an
address is left alone, as are the platform's own pictures (``/blueprints/...``,
the same on every deployment) and ``https://`` links. Knowledge files and run
data never travel: they belong to the Workspace, not its Blueprint.

Layout (a gzip tar, deterministic so the same content packs to the same bytes)::

    package/manifest.json   format, title, payload digest, one entry per asset
    package/blueprint.json  the payload, each embedded address replaced by
                            ``/manor-package-assets/<sha256><ext>``
    package/assets/<sha256><ext>

Installing writes each asset into the installing entity's file area and puts
its new address back where the placeholder was, then installs the payload as
any other.
"""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import mimetypes
import posixpath
import re
import tarfile
from typing import Any, Callable

FORMAT = "manor-package/1"
#: Placeholder prefix for a carried file. A site path, so the App and Flow
#: validators accept it like any other picture address until install.
ASSET_PREFIX = "/manor-package-assets/"
#: Where installed assets live in the entity file area (served to members).
ASSET_DIR = "marketplace/blueprint-assets"
MAX_ASSET_BYTES = 50 * 1024 * 1024
MAX_PACKAGE_BYTES = 200 * 1024 * 1024
MAX_ASSETS = 200
_EPOCH = 499162500  # 1985-10-26T08:15:00Z, as npm pack uses
_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXTENSION = re.compile(r"\.[a-z0-9]{1,8}")
_INSTALLED_PATH = re.compile(rf"{re.escape(ASSET_DIR)}/[0-9a-f]{{64}}(\.[a-z0-9]{{1,8}})?")
_PLACEHOLDER = re.compile(rf"{re.escape(ASSET_PREFIX)}([0-9a-f]{{64}})(\.[a-z0-9]{{1,8}})?")


class PackageError(ValueError):
    """The package is malformed, too large or does not match its manifest."""


@dataclass(frozen=True)
class Asset:
    sha256: str
    data: bytes
    mime_type: str
    extension: str
    name: str

    @property
    def path(self) -> str:
        return f"assets/{self.sha256}{self.extension}"

    @property
    def placeholder(self) -> str:
        return f"{ASSET_PREFIX}{self.sha256}{self.extension}"


def is_package_asset_path(value: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(value or ""))


def is_installed_asset_url(value: str) -> bool:
    """Where ``store_assets`` put a package's file. Exporting again carries it
    as a file again, so it is as portable as the placeholder it replaced."""
    from packages.core.services.file_refs import parse_file_reference

    ref = parse_file_reference(value)
    return bool(ref and ref.entity_id and _INSTALLED_PATH.fullmatch(ref.fs_path or ""))


def _extension(name: str, mime_type: str) -> str:
    ext = posixpath.splitext(name or "")[1].lower()
    if not _EXTENSION.fullmatch(ext or ""):
        ext = mimetypes.guess_extension(mime_type or "") or ""
    return ext if _EXTENSION.fullmatch(ext or "") else ""


def _walk(value: Any, replace: Callable[[str], str | None]) -> Any:
    """A copy of ``value`` with each string ``replace`` maps swapped."""
    if isinstance(value, dict):
        return {key: _walk(item, replace) for key, item in value.items()}
    if isinstance(value, list):
        return [_walk(item, replace) for item in value]
    if isinstance(value, str):
        replaced = replace(value)
        return value if replaced is None else replaced
    return value


def string_values(value: Any) -> list[str]:
    found: list[str] = []
    _walk(value, lambda text: found.append(text))
    return found


def embed_assets(payload: dict[str, Any], resolve: Callable[[str], Asset | None]) -> tuple[dict[str, Any], list[Asset]]:
    """Swap each value ``resolve`` turns into a file for its placeholder.

    ``resolve`` is called once per distinct string and returns None for
    anything that is not a readable file of the exporting entity.
    """
    resolved: dict[str, Asset | None] = {}
    assets: dict[str, Asset] = {}

    def replace(text: str) -> str | None:
        if text not in resolved:
            resolved[text] = resolve(text)
        asset = resolved[text]
        if asset is None:
            return None
        if len(asset.data) > MAX_ASSET_BYTES:
            raise PackageError(f"{asset.name} is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB")
        assets[asset.sha256] = asset
        if len(assets) > MAX_ASSETS:
            raise PackageError(f"a package carries at most {MAX_ASSETS} files")
        return asset.placeholder

    return _walk(payload, replace), sorted(assets.values(), key=lambda asset: asset.sha256)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(f"package/{name}")
    info.size = len(data)
    info.mtime = _EPOCH
    info.mode = 0o644
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def pack(payload: dict[str, Any], assets: list[Asset]) -> bytes:
    """The package bytes; the same payload and assets always give the same bytes."""
    blueprint = _canonical(payload)
    manifest = {
        "format": FORMAT,
        "title": (payload.get("manifest") or {}).get("title"),
        "payload_format": (payload.get("manifest") or {}).get("blueprint_version"),
        "blueprint_sha256": hashlib.sha256(blueprint).hexdigest(),
        "assets": [
            {"sha256": asset.sha256, "path": asset.path, "size": len(asset.data),
             "mime_type": asset.mime_type, "name": asset.name}
            for asset in assets
        ],
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as archive:
        _add(archive, "manifest.json", _canonical(manifest))
        _add(archive, "blueprint.json", blueprint)
        for asset in assets:
            _add(archive, asset.path, asset.data)
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, filename="") as zipped:
        zipped.write(raw.getvalue())
    return out.getvalue()


def is_package(data: bytes) -> bool:
    return data[:2] == b"\x1f\x8b"


def unpack(data: bytes) -> tuple[dict[str, Any], list[Asset]]:
    """The payload (placeholders intact) and its verified assets.

    Refuses anything but regular files under ``package/``, oversize content,
    and any file whose bytes do not match its recorded digest.
    """
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError("the package is too large")
    try:
        raw = gzip.decompress(data)
    except (OSError, EOFError) as exc:
        raise PackageError("not a Blueprint package") from exc
    if len(raw) > MAX_PACKAGE_BYTES:
        raise PackageError("the package is too large")
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive.getmembers():
                name = member.name
                if not member.isfile() or not name.startswith("package/") or ".." in name.split("/"):
                    raise PackageError(f"unexpected entry {name!r} in the package")
                handle = archive.extractfile(member)
                files[name.removeprefix("package/")] = handle.read() if handle else b""
    except tarfile.TarError as exc:
        raise PackageError("not a Blueprint package") from exc
    try:
        manifest = json.loads(files["manifest.json"])
        blueprint = files["blueprint.json"]
        payload = json.loads(blueprint)
    except (KeyError, ValueError) as exc:
        raise PackageError("the package has no readable manifest or blueprint") from exc
    if manifest.get("format") != FORMAT:
        raise PackageError(f"unsupported package format {manifest.get('format')!r}")
    if hashlib.sha256(blueprint).hexdigest() != manifest.get("blueprint_sha256"):
        raise PackageError("blueprint.json does not match the manifest")
    if not isinstance(payload, dict):
        raise PackageError("blueprint.json is not a payload")
    assets = []
    for entry in manifest.get("assets") or []:
        sha = str(entry.get("sha256") or "")
        if not _SHA256.fullmatch(sha):
            raise PackageError("an asset has no valid digest")
        content = files.get(str(entry.get("path") or ""))
        if content is None or hashlib.sha256(content).hexdigest() != sha:
            raise PackageError(f"asset {sha[:12]} is missing or does not match its digest")
        mime_type = str(entry.get("mime_type") or "application/octet-stream")
        name = str(entry.get("name") or sha)
        assets.append(Asset(sha, content, mime_type, _extension(str(entry.get("path") or ""), mime_type), name))
    known = {asset.sha256 for asset in assets}
    referenced = {match[1] for text in string_values(payload) if (match := _PLACEHOLDER.fullmatch(text))}
    if referenced - known:
        raise PackageError("the payload names files the package does not carry")
    return payload, assets


def restore_assets(payload: dict[str, Any], addresses: dict[str, str]) -> dict[str, Any]:
    """Put each installed asset's address back where its placeholder was."""
    def replace(text: str) -> str | None:
        match = _PLACEHOLDER.fullmatch(text)
        return addresses[match[1]] if match else None

    return _walk(payload, replace)


def asset_from_bytes(data: bytes, *, name: str, mime_type: str | None) -> Asset:
    mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Asset(hashlib.sha256(data).hexdigest(), data, mime, _extension(name, mime), posixpath.basename(name) or "file")


def installed_asset_path(asset: Asset) -> str:
    return f"{ASSET_DIR}/{asset.sha256}{asset.extension}"


def _read_file(path: str) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ASSET_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= MAX_ASSET_BYTES else None


class FileEmbedder:
    """Replaces addresses of the exporting entity's files with package placeholders.

    Only a value that is exactly such an address is a file; only files the
    exporting user may open travel. ``assets`` collects what was carried.
    """

    def __init__(self, db: Any, *, entity_id: str, user: Any, workspace_id: str | None = None):
        self._db, self._entity_id, self._user, self._workspace_id = db, entity_id, user, workspace_id
        self._seen: dict[str, Asset | None] = {}
        self.assets: dict[str, Asset] = {}

    async def _resolve(self, text: str) -> Asset | None:
        import asyncio

        from sqlalchemy import select

        from packages.core.models.document import Document
        from packages.core.services.document_access import user_can_read_document
        from packages.core.services.entity_fs import resolve_path
        from packages.core.services.file_refs import is_open_address, parse_file_reference
        from packages.core.services.knowledge_visibility import is_user_visible_path

        if text.startswith(("http://", "https://")) or not is_open_address(text):
            return None
        ref = parse_file_reference(text, entity_id=self._entity_id)
        if ref is None or (ref.entity_id and ref.entity_id != self._entity_id):
            return None
        if ref.document_id:
            document = await self._db.get(Document, ref.document_id)
            fs_path = document.fs_path if document is not None else None
        else:
            fs_path = ref.fs_path
            document = (await self._db.execute(select(Document).where(
                Document.entity_id == self._entity_id, Document.fs_path == fs_path,
                Document.is_trashed.is_(False),
            ).limit(1))).scalar_one_or_none()
        if not fs_path:
            return None
        if document is not None:
            if document.entity_id != self._entity_id or document.is_trashed or not await user_can_read_document(
                self._db, document, entity_id=self._entity_id, user_id=self._user.id,
                role=getattr(self._user, "role", None), workspace_id=self._workspace_id,
            ):
                return None
        elif not is_user_visible_path(fs_path):
            return None
        full_path = resolve_path(self._entity_id, fs_path)
        data = await asyncio.to_thread(_read_file, full_path) if full_path else None
        if not data:
            return None
        name = document.name if document is not None else posixpath.basename(fs_path)
        return asset_from_bytes(data, name=name, mime_type=document.mime_type if document is not None else None)

    async def __call__(self, value: Any) -> Any:
        for text in set(string_values(value)):
            if text not in self._seen:
                self._seen[text] = await self._resolve(text)
        embedded, assets = embed_assets(value, self._seen.get)
        self.assets.update({asset.sha256: asset for asset in assets})
        return embedded


async def store_assets(entity_id: str, assets: list[Asset]) -> dict[str, str]:
    """Write each asset into the entity's file area; its address by digest."""
    import asyncio

    from packages.core.services.entity_fs import write_entity_file_atomic
    from packages.core.services.generated_file_refs import entity_fs_open_url

    addresses = {}
    for asset in assets:
        path = installed_asset_path(asset)
        await asyncio.to_thread(
            write_entity_file_atomic, entity_id, path, asset.data, expected_size=len(asset.data),
        )
        addresses[asset.sha256] = entity_fs_open_url(entity_id, path)
    return addresses
