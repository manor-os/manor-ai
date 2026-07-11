"""Skill file storage — save and load skill files in MinIO.

Each entity-owned skill stores its actual content as objects in MinIO.
The directory name under the ``skills/`` prefix is derived from the skill's
source metadata when available, giving human-readable paths:

    {entity_id}/skills/{owner}--{display_name}/SKILL.md
    {entity_id}/skills/{owner}--{display_name}/config.json
    {entity_id}/skills/{owner}--{display_name}/requirements.txt
    {entity_id}/skills/{owner}--{display_name}/scripts/{name}
    {entity_id}/skills/{owner}--{display_name}/credentials.json

For skills without an owner (user-created, AI-generated) the directory name
falls back to the skill's slug, then to the raw skill_id.

The computed directory name is stored in ``config["minio_dir"]`` on save so
all subsequent reads and deletes can resolve it without re-deriving it.

Platform skills (entity_id IS NULL) are seeded from disk and use the DB only.

All functions are synchronous and best-effort — failures are logged but never
propagate to callers so the DB path remains functional when MinIO is absent.
"""
from __future__ import annotations

import io
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Extensions recognised as script files (match old skill_runner.py)
_SCRIPT_EXTENSIONS = {".py", ".sh", ".bash", ".js", ".ts", ".rb"}


# ---------------------------------------------------------------------------
# MinIO client
# ---------------------------------------------------------------------------

def _get_client():
    """Return a (minio.Minio, bucket) pair or (None, None) when not configured."""
    from packages.core.config import get_settings
    s = get_settings()
    if not s.MINIO_ENDPOINT:
        return None, None
    try:
        from minio import Minio
        client = Minio(
            s.MINIO_ENDPOINT.replace("http://", "").replace("https://", ""),
            access_key=s.MINIO_ACCESS_KEY,
            secret_key=s.MINIO_SECRET_KEY,
            secure=s.MINIO_ENDPOINT.startswith("https"),
        )
        bucket = s.MINIO_BUCKET
        try:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
        except Exception:
            pass
        return client, bucket
    except Exception as exc:
        logger.warning("[skill_file_storage] MinIO client init failed: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# Directory-name helpers
# ---------------------------------------------------------------------------


def compute_skill_dir(
    skill_id: str,
    config: Optional[dict] = None,
    display_name: Optional[str] = None,
) -> str:
    """Return the MinIO directory name for a skill.

    Priority:
    1. ``config["minio_dir"]`` — stored on first save; callers pre-set this to
       the canonical identifier they want (e.g. the OpenClaw bundle id
       ``547895019--qwen-video``).
    2. ``skill_id`` — ULID fallback for skills without a stored minio_dir.
    """
    stored = str((config or {}).get("minio_dir") or "").strip()
    return stored if stored else skill_id


# ---------------------------------------------------------------------------
# Object key helpers
# ---------------------------------------------------------------------------

def _prefix(entity_id: str, skill_dir: str) -> str:
    """Full MinIO prefix: ``{entity_id}/{MINIO_SKILL_PREFIX}/{skill_dir}``."""
    from packages.core.config import get_settings
    root = get_settings().MINIO_SKILL_PREFIX.strip("/")
    return f"{entity_id}/{root}/{skill_dir}"


def _key(entity_id: str, skill_dir: str, rel: str) -> str:
    return f"{_prefix(entity_id, skill_dir)}/{rel.lstrip('/')}"


# ---------------------------------------------------------------------------
# Low-level put / get / delete
# ---------------------------------------------------------------------------

def _put(client, bucket: str, key: str, content: str) -> bool:
    try:
        data = content.encode("utf-8")
        client.put_object(
            bucket, key,
            io.BytesIO(data), len(data),
            content_type="text/plain; charset=utf-8",
        )
        return True
    except Exception as exc:
        logger.warning("[skill_file_storage] put failed key=%s: %s", key, exc)
        return False


def _get(client, bucket: str, key: str) -> Optional[str]:
    try:
        resp = client.get_object(bucket, key)
        try:
            return resp.read().decode("utf-8")
        finally:
            resp.close()
            resp.release_conn()
    except Exception as exc:
        if "NoSuchKey" not in str(exc) and "NoSuchKey" not in type(exc).__name__:
            logger.debug("[skill_file_storage] get miss key=%s: %s", key, exc)
        return None


def _delete_prefix(client, bucket: str, prefix: str) -> int:
    """Delete all objects with the given prefix. Returns count deleted."""
    if not prefix.endswith("/"):
        prefix += "/"
    count = 0
    try:
        objects = list(client.list_objects(bucket, prefix=prefix, recursive=True))
        for obj in objects:
            try:
                client.remove_object(bucket, obj.object_name)
                count += 1
            except Exception as exc:
                logger.debug("[skill_file_storage] delete failed key=%s: %s", obj.object_name, exc)
    except Exception as exc:
        logger.warning("[skill_file_storage] list for delete failed prefix=%s: %s", prefix, exc)
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_skill_files(
    entity_id: str,
    skill_id: str,
    *,
    prompt: str,
    scripts: Optional[dict[str, str]] = None,
    requirements: str = "",
    extra_files: Optional[dict[str, str]] = None,
    config_snapshot: Optional[dict] = None,
    skill_dir: Optional[str] = None,
) -> str:
    """Write skill files to MinIO and return the resolved directory name.

    The returned value should be stored in ``config["minio_dir"]`` so future
    reads and deletes can locate the files without re-deriving the name.

    ``skill_dir`` overrides automatic derivation when provided.  When omitted,
    the directory name is computed from ``config_snapshot`` (owner, skill_name)
    with a fallback to ``skill_id``.

    - ``scripts``: bare filename → content; stored under ``scripts/{filename}``.
    - ``extra_files``: rel-path → content; stored at their exact paths.
      A ``requirements.txt`` entry acts as the requirements file when the
      top-level ``requirements`` argument is empty.
    - ``requirements``: explicit requirements content (takes precedence).

    Returns the resolved ``skill_dir`` string on success or empty string on
    failure (callers must not rely on the return value for correctness).
    """
    if not entity_id or not skill_id:
        return ""
    client, bucket = _get_client()
    if client is None:
        return ""

    # Resolve directory name
    display_name = (config_snapshot or {}).get("name") or ""
    resolved_dir = skill_dir or compute_skill_dir(
        skill_id,
        config=config_snapshot,
        display_name=display_name,
    )

    # Stamp minio_dir into the snapshot so it's persisted in config.json
    if config_snapshot is not None:
        config_snapshot = {**config_snapshot, "minio_dir": resolved_dir}

    # Resolve requirements / extra_files
    resolved_requirements = requirements or ""
    resolved_extra: dict[str, str] = {}
    for rel, content in (extra_files or {}).items():
        safe = rel.replace("\\", "/").strip().lstrip("/")
        if not safe or ".." in safe or safe == "SKILL.md" or safe == "config.json":
            continue
        if safe == "requirements.txt":
            if not resolved_requirements:
                resolved_requirements = content
        else:
            resolved_extra[safe] = content

    ok = True
    ok &= _put(client, bucket, _key(entity_id, resolved_dir, "SKILL.md"), prompt or "")

    if config_snapshot:
        ok &= _put(
            client, bucket,
            _key(entity_id, resolved_dir, "config.json"),
            json.dumps(config_snapshot, indent=2, ensure_ascii=False),
        )

    if resolved_requirements:
        ok &= _put(client, bucket, _key(entity_id, resolved_dir, "requirements.txt"), resolved_requirements)

    for filename, content in (scripts or {}).items():
        safe = filename.replace("\\", "/").lstrip("/")
        if "/" not in safe:
            safe = f"scripts/{safe}"
        ok &= _put(client, bucket, _key(entity_id, resolved_dir, safe), content)

    for rel, content in resolved_extra.items():
        ok &= _put(client, bucket, _key(entity_id, resolved_dir, rel), content)

    logger.info(
        "[skill_file_storage] saved entity=%s dir=%s scripts=%s extra=%s reqs=%s",
        entity_id, resolved_dir,
        list((scripts or {}).keys()),
        list(resolved_extra.keys()),
        bool(resolved_requirements),
    )
    return resolved_dir if ok else ""


def load_skill_prompt(
    entity_id: str,
    skill_id: str,
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> Optional[str]:
    """Read SKILL.md from MinIO. Returns None when not found or unavailable."""
    if not entity_id or not skill_id:
        return None
    client, bucket = _get_client()
    if client is None:
        return None
    d = skill_dir or compute_skill_dir(skill_id, config=config)
    return _get(client, bucket, _key(entity_id, d, "SKILL.md"))


def load_skill_requirements(
    entity_id: str,
    skill_id: str,
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> str:
    """Read requirements.txt from MinIO. Returns empty string when absent."""
    if not entity_id or not skill_id:
        return ""
    client, bucket = _get_client()
    if client is None:
        return ""
    d = skill_dir or compute_skill_dir(skill_id, config=config)
    return _get(client, bucket, _key(entity_id, d, "requirements.txt")) or ""


def load_skill_scripts(
    entity_id: str,
    skill_id: str,
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> dict[str, str]:
    """Return all script files under ``scripts/``.

    Keys are bare filenames/sub-paths, values are file contents.
    Returns {} when MinIO is unavailable or no scripts exist.
    """
    if not entity_id or not skill_id:
        return {}
    client, bucket = _get_client()
    if client is None:
        return {}

    d = skill_dir or compute_skill_dir(skill_id, config=config)
    scripts_prefix = _key(entity_id, d, "scripts/")
    results: dict[str, str] = {}
    try:
        objects = client.list_objects(bucket, prefix=scripts_prefix, recursive=True)
        for obj in objects:
            name = obj.object_name
            rel = name[len(scripts_prefix):]
            if not rel or rel.endswith("/"):
                continue
            ext = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
            if ext not in _SCRIPT_EXTENSIONS:
                continue
            content = _get(client, bucket, name)
            if content is not None:
                results[rel] = content
    except Exception as exc:
        logger.debug("[skill_file_storage] list scripts failed entity=%s dir=%s: %s",
                     entity_id, d, exc)
    return results


def load_skill_extra_files(
    entity_id: str,
    skill_id: str,
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> dict[str, str]:
    """Return all extra files (scripts + others) excluding core files.

    Keys are paths relative to the skill root (e.g. ``scripts/main.py``).
    """
    if not entity_id or not skill_id:
        return {}
    client, bucket = _get_client()
    if client is None:
        return {}

    d = skill_dir or compute_skill_dir(skill_id, config=config)
    skill_prefix = _prefix(entity_id, d) + "/"
    exclude = {"SKILL.md", "config.json", "credentials.json"}
    results: dict[str, str] = {}
    try:
        objects = client.list_objects(bucket, prefix=skill_prefix, recursive=True)
        for obj in objects:
            name = obj.object_name
            rel = name[len(skill_prefix):]
            if not rel or rel.endswith("/") or rel in exclude:
                continue
            content = _get(client, bucket, name)
            if content is not None:
                results[rel] = content
    except Exception as exc:
        logger.debug("[skill_file_storage] list extra files failed entity=%s dir=%s: %s",
                     entity_id, d, exc)
    return results


def delete_skill_files(
    entity_id: str,
    skill_id: str,
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> int:
    """Delete all MinIO objects for this skill. Returns number of objects deleted."""
    if not entity_id or not skill_id:
        return 0
    client, bucket = _get_client()
    if client is None:
        return 0
    d = skill_dir or compute_skill_dir(skill_id, config=config)
    count = _delete_prefix(client, bucket, _prefix(entity_id, d))
    logger.info("[skill_file_storage] deleted %d file(s) entity=%s dir=%s", count, entity_id, d)
    return count


# ---------------------------------------------------------------------------
# Credentials (per-entity env-var values)
# ---------------------------------------------------------------------------

def save_skill_credentials(
    entity_id: str,
    skill_id: str,
    credentials: dict[str, str],
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> bool:
    """Persist credential key-value pairs to MinIO credentials.json."""
    if not entity_id or not skill_id:
        return False
    client, bucket = _get_client()
    if client is None:
        return False
    d = skill_dir or compute_skill_dir(skill_id, config=config)
    content = json.dumps(credentials, indent=2, ensure_ascii=False)
    return _put(client, bucket, _key(entity_id, d, "credentials.json"), content)


def load_skill_credentials(
    entity_id: str,
    skill_id: str,
    *,
    skill_dir: Optional[str] = None,
    config: Optional[dict] = None,
) -> dict[str, str]:
    """Load credential values from MinIO credentials.json. Returns {} on any failure."""
    if not entity_id or not skill_id:
        return {}
    client, bucket = _get_client()
    if client is None:
        return {}
    d = skill_dir or compute_skill_dir(skill_id, config=config)
    raw = _get(client, bucket, _key(entity_id, d, "credentials.json"))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}
