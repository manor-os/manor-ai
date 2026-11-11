"""Signed display-media URLs for the DB-free ``manor-media`` origin.

A list response that has already decided, in one batch, which Documents the
viewer may read (``DocumentAccessContext``) attaches a short-lived HMAC URL to
each eligible item. ``manor-media`` checks the
signature in nginx and sends the variant file from the shared filesystem, so
the bytes never touch the API process or the database admission queue.

* Holding a URL is enough to read the bytes until it expires, so it is only
  issued to first-party browser sessions, never to API keys, OAuth/MCP
  clients or agents (it must not reach a model context, message or log).
* ``RESTRICTED`` Documents never get one: a copyable URL is a download.
* Expiry is rounded to 10-minute windows with a 30-minute cap, so the same
  URL repeats inside a window and the browser HTTP cache can hit. The
  URLs remain valid until that expiry.
* The URL names a content version, not ``updated_at``: writing variant
  metadata must not change the version it describes.

The canonical message, which ``manor-media`` recomputes in njs::

    "v1\\n" variant "\\n" entity_id "\\n" document_id "\\n" version "\\n" ext "\\n" e "\\n" u
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

from packages.core.config import get_settings

MEDIA_URL_PREFIX = "/media/v1"
#: Hidden per-entity directory holding versioned display variants.
MEDIA_DIR = ".manor-media"
#: ``Document.metadata_`` key recording which variants exist for which version.
DISPLAY_VARIANTS_KEY = "display_variants"
#: Variant -> file extension. P1 serves thumbnails only.
VARIANT_EXTENSIONS: Mapping[str, str] = {"thumb": "jpg"}

WINDOW_SECONDS = 600
MAX_LIFETIME_SECONDS = 1800

_KEY_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_ID = re.compile(r"^[A-Za-z0-9]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION_PATTERN = re.compile(r"^[sf][0-9a-f]{32}$")
_MIN_SECRET_BYTES = 32

_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"})
_VIDEO_EXTENSIONS = frozenset({"mp4", "webm", "ogg", "mov", "avi", "mkv"})


class MediaSigningKeyError(ValueError):
    """``MEDIA_URL_SIGNING_KEYS`` is malformed."""


def parse_signing_keys(raw: str | None) -> dict[str, bytes]:
    """``"kid=secret[,kid=secret]"`` -> ordered ``{kid: secret}``; the first key signs."""
    keys: dict[str, bytes] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        kid, separator, secret = part.partition("=")
        kid, secret = kid.strip(), secret.strip()
        if not separator or not _KEY_ID.match(kid):
            raise MediaSigningKeyError("MEDIA_URL_SIGNING_KEYS entries must be kid=secret")
        if len(secret.encode()) < _MIN_SECRET_BYTES:
            raise MediaSigningKeyError(f"media signing key {kid!r} is shorter than {_MIN_SECRET_BYTES} bytes")
        if kid in keys:
            raise MediaSigningKeyError(f"media signing key {kid!r} is listed twice")
        keys[kid] = secret.encode()
    return keys


def window_expiry(now: float | None = None) -> int:
    """``floor(now / 10 min) * 10 min + 30 min``: stable inside a window, 20-30 min left."""
    current = int(time.time() if now is None else now)
    return current - current % WINDOW_SECONDS + MAX_LIFETIME_SECONDS


def _message(variant: str, entity_id: str, document_id: str, version: str, ext: str, expires: int, user_id: str) -> bytes:
    return "\n".join(("v1", variant, entity_id, document_id, version, ext, str(expires), user_id)).encode()


def _signature(secret: bytes, message: bytes) -> str:
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def media_rel_path(document_id: str, version: str, variant: str) -> str:
    """Variant path below the entity root; the origin derives the same path from the URL."""
    return f"{MEDIA_DIR}/{document_id}/{version}/{variant}.{VARIANT_EXTENSIONS[variant]}"


# ── content version ───────────────────────────────────────────────────────


def sha256_file(path: str, *, max_bytes: int | None = None) -> str | None:
    """Hex SHA-256 of a file, or ``None`` when it is larger than ``max_bytes``."""
    if max_bytes is not None and os.path.getsize(path) > max_bytes:
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_descriptor(file_descriptor: int) -> str:
    """Hex SHA-256 of an open file from offset 0, leaving the descriptor's offset alone.

    ``/dev/fd/N`` paths are not used: on macOS opening one shares the
    descriptor's offset, so a file already read to its end hashes as empty.
    """
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(file_descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _source_mtime_ns(document: Any) -> int | None:
    metadata = getattr(document, "metadata_", None)
    integrity = metadata.get("file_integrity") if isinstance(metadata, dict) else None
    value = integrity.get("mtime_ns") if isinstance(integrity, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def content_version(document: Any) -> str | None:
    """The source file's content version, or ``None`` when it cannot be named yet.

    Prefer the stored SHA-256. Before a writer or the variant task has stored
    one, fall back to a digest of (id, size, path, source mtime) as last seen
    by the Knowledge change pipeline. Never ``updated_at``: recording the
    variants is itself an update.
    """
    sha = str(getattr(document, "content_sha256", None) or "")
    if _SHA256.match(sha):
        return "s" + sha[:32]
    fs_path = getattr(document, "fs_path", None)
    file_size = getattr(document, "file_size", None)
    mtime_ns = _source_mtime_ns(document)
    if not fs_path or file_size is None or mtime_ns is None:
        return None
    seed = "\n".join((str(document.id), str(file_size), str(fs_path), str(mtime_ns)))
    return "f" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def thumbnail_source_kind(document: Any) -> str | None:
    """``"image"`` or ``"video"`` when the worker can make a ``thumb`` of this Document."""
    name = str(getattr(document, "name", None) or "")
    ext = str(getattr(document, "file_type", None) or os.path.splitext(name)[1].lstrip(".")).lower()
    mime = str(getattr(document, "mime_type", None) or "").lower()
    if mime == "image/svg+xml" or ext == "svg":
        return None
    if ext in _IMAGE_EXTENSIONS or mime.startswith("image/"):
        return "image"
    if ext in _VIDEO_EXTENSIONS or mime.startswith("video/"):
        return "video"
    return None


def recorded_variants(document: Any) -> tuple[str | None, frozenset[str]]:
    metadata = getattr(document, "metadata_", None)
    record = metadata.get(DISPLAY_VARIANTS_KEY) if isinstance(metadata, dict) else None
    if not isinstance(record, dict):
        return None, frozenset()
    variants = record.get("variants")
    return (
        str(record.get("version") or "") or None,
        frozenset(str(item) for item in variants) if isinstance(variants, list) else frozenset(),
    )


def with_recorded_variants(metadata: dict | None, *, version: str, variants: list[str]) -> dict:
    updated = dict(metadata or {}) if isinstance(metadata, dict) else {}
    updated[DISPLAY_VARIANTS_KEY] = {"version": version, "variants": sorted(set(variants))}
    return updated


# ── eligibility ───────────────────────────────────────────────────────────


def variant_version(document: Any, variant: str) -> str | None:
    """The version to sign ``variant`` for, or ``None`` when no URL may be issued.

    The caller must already have admitted the viewer to this Document with
    ``DocumentAccessContext`` (the batch twin of ``user_can_read_document``).
    Everything else that bars a URL is decided here, in one place.
    """
    from packages.core.models.permission import Classification

    if variant not in VARIANT_EXTENSIONS:
        return None
    if getattr(document, "is_trashed", False):
        return None
    if str(getattr(document, "quarantine_status", None) or "clean") != "clean":
        return None
    if str(getattr(document, "classification", None) or "").lower() == Classification.RESTRICTED:
        return None
    # Only files on the shared filesystem; a remote file_url has no local variant.
    if not getattr(document, "fs_path", None):
        return None
    if not _ID.match(str(getattr(document, "id", "") or "")) or not _ID.match(str(getattr(document, "entity_id", "") or "")):
        return None
    if thumbnail_source_kind(document) is None:
        return None
    version = content_version(document)
    recorded_version, variants = recorded_variants(document)
    # Sign only what the worker has written for this exact version.
    if version is None or recorded_version != version or variant not in variants:
        return None
    return version


def request_is_browser_session(request: Any) -> bool:
    """A first-party web session: the session header and a plain access token.

    API keys and personal access tokens carry ``azp``; OAuth/MCP clients and
    agents carry ``azp``/``act``/``scp``; collaborator and support sessions
    carry ``typ``. None of them receives a bearer-free URL.
    """
    headers = getattr(request, "headers", None)
    if headers is None or headers.get("x-manor-session") != "1":
        return False
    state = getattr(request, "state", None)
    claims = getattr(state, "auth_claims", None)
    if not isinstance(claims, dict) or not claims.get("sub"):
        return False
    if getattr(state, "auth_client", None) or getattr(state, "impersonation", None):
        return False
    return not any(claims.get(claim) for claim in ("azp", "act", "scp", "scope", "typ"))


# ── signing ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MediaUrlSigner:
    """Signs URLs for one viewer in one request; ``expires`` is fixed per request."""

    key_id: str
    secret: bytes
    user_id: str
    expires: int

    def url(self, document: Any, variant: str) -> str | None:
        version = variant_version(document, variant)
        if version is None:
            return None
        entity_id, document_id = str(document.entity_id), str(document.id)
        ext = VARIANT_EXTENSIONS[variant]
        signature = _signature(
            self.secret, _message(variant, entity_id, document_id, version, ext, self.expires, self.user_id),
        )
        return (
            f"{MEDIA_URL_PREFIX}/{variant}/{entity_id}/{document_id}/{version}.{ext}"
            f"?e={self.expires}&k={self.key_id}&u={self.user_id}&s={signature}"
        )

    def thumbnail_url(self, document: Any) -> str | None:
        return self.url(document, "thumb")


def media_signing_enabled() -> bool:
    settings = get_settings()
    return bool(settings.MEDIA_SIGNED_URLS_ENABLED and parse_signing_keys(settings.MEDIA_URL_SIGNING_KEYS))


def signer_for_request(request: Any, user: Any, *, now: float | None = None) -> MediaUrlSigner | None:
    """The viewer's signer, or ``None`` (flag off, no key, or not a browser session)."""
    settings = get_settings()
    if not settings.MEDIA_SIGNED_URLS_ENABLED or not request_is_browser_session(request):
        return None
    keys = parse_signing_keys(settings.MEDIA_URL_SIGNING_KEYS)
    user_id = str(getattr(user, "id", "") or "")
    if not keys or not _ID.match(user_id):
        return None
    key_id, secret = next(iter(keys.items()))
    return MediaUrlSigner(key_id=key_id, secret=secret, user_id=user_id, expires=window_expiry(now))


def verify_media_url(path: str, query: Mapping[str, str], keys: Mapping[str, bytes], *, now: float | None = None) -> bool:
    """Reference check mirrored by ``manor-media``'s njs handler (used by tests and tools)."""
    match = re.match(
        r"^/media/v1/([a-z]+)/([A-Za-z0-9]{1,64})/([A-Za-z0-9]{1,64})/([sf][0-9a-f]{32})\.([a-z0-9]{1,8})$", path,
    )
    if not match:
        return False
    variant, entity_id, document_id, version, ext = match.groups()
    if VARIANT_EXTENSIONS.get(variant) != ext:
        return False
    try:
        expires = int(query.get("e", ""))
    except ValueError:
        return False
    current = int(time.time() if now is None else now)
    if expires <= current or expires > current + MAX_LIFETIME_SECONDS:
        return False
    secret = keys.get(query.get("k", ""))
    user_id = query.get("u", "")
    if secret is None or not _ID.match(user_id):
        return False
    expected = _signature(secret, _message(variant, entity_id, document_id, version, ext, expires, user_id))
    return hmac.compare_digest(expected, query.get("s", ""))


# ── request scope ─────────────────────────────────────────────────────────

_CURRENT_SIGNER: ContextVar[MediaUrlSigner | None] = ContextVar("media_url_signer", default=None)


@contextmanager
def media_url_scope(signer: MediaUrlSigner | None) -> Iterator[None]:
    """Let list builders in this block attach signed URLs; ``None`` keeps them out."""
    token = _CURRENT_SIGNER.set(signer)
    try:
        yield
    finally:
        _CURRENT_SIGNER.reset(token)


def current_thumbnail_url(document: Any) -> str | None:
    """A signed ``thumb`` URL for an already-authorized Document, inside a scope."""
    signer = _CURRENT_SIGNER.get()
    return signer.thumbnail_url(document) if signer is not None else None


def with_thumbnail_url(item: dict[str, Any], document: Any) -> dict[str, Any]:
    """``item`` plus ``thumbnail_url`` when one may be issued; unchanged otherwise."""
    url = current_thumbnail_url(document)
    return {**item, "thumbnail_url": url} if url else item
