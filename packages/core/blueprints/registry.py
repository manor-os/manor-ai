"""Blueprint releases: the one way a payload becomes a package's current version.

A Blueprint used to keep only its current payload, overwritten on every
publish, so an installed Workspace could say *that* it was behind but never
*which* release it ran or what changed since. Every release is now also an
immutable ``blueprint_versions`` row, and ``latest`` points at one of them.

Invariants, all enforced here:

* ``record_release`` is the only write that makes a payload current. It locks
  the package, refuses a version number already released with different
  content, appends the version, moves ``latest`` and refreshes the serving
  copy (``workspace_blueprints.payload`` / ``client_workspace_deliveries``).
  ``tests/test_blueprint_release_write_guard.py`` fails on any other write.
* A version row is never changed or deleted: merge baselines and undo need
  the exact content an install came from.
* Draft content and listing copy are not releases. They change through
  ``set_draft_payload`` and ``set_listing_manifest``, which cannot touch what
  an installed Workspace upgrades toward.

The package's identity is the id installs already record as
``_blueprint.blueprint_id``; its ``@scope/name`` is only an alias.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
import re
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from packages.core.blueprints.freshness import (
    BLUEPRINT_ID_KEY,
    BLUEPRINT_SETTINGS_KEY,
    BLUEPRINT_VERSION_KEY,
    CONTENT_FINGERPRINT_KEY,
    blueprint_content_fingerprint,
    blueprint_section_fingerprints,
    parse_content_version,
)
from packages.core.constants.blueprints import BlueprintStatus
from packages.core.models.blueprint_registry import (
    BlueprintDistTag,
    BlueprintPackage,
    BlueprintVersion,
)
from packages.core.models.client_delivery import DELIVERY_BLUEPRINT_ID_PREFIX

logger = logging.getLogger(__name__)

PACKAGE_KIND_PLATFORM = "platform"
PACKAGE_KIND_MARKETPLACE = "marketplace"
PACKAGE_KIND_DELIVERY = "delivery"

ORIGIN_PUBLISHED = "published"
#: Recorded from a row's serving payload when the registry was introduced.
ORIGIN_BACKFILL = "backfill"
#: A platform config recovered from git because an install's fingerprint matched it.
ORIGIN_GIT_HISTORY = "git_history"
#: Names an install's v3 baseline whose payload is no longer known.
ORIGIN_BASELINE = "baseline"

VISIBILITY_LISTED = "listed"
#: Kept only so an install can name what it came from; never an upgrade target.
VISIBILITY_HIDDEN = "hidden"

LATEST_TAG = "latest"

#: ``workspace.settings._blueprint.package``.
PACKAGE_RECORD_KEY = "package"
#: The install's merge baseline is exact or can be derived from a known release.
BASE_EXACT = "exact"
#: Nothing proves which release the install came from.
BASE_UNVERIFIED = "unverified"
#: Install-local choices a re-resolved package record keeps.
_LOCAL_PACKAGE_KEYS = ("resolutions", "pinned")

_SEMVER = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


class BlueprintVersionConflictError(ValueError):
    """The version number is already released with different content."""


@dataclass(frozen=True)
class PackageSpec:
    """What a package row is created with the first time it is released."""

    ref: str
    name: str
    kind: str
    owner_entity_id: str | None = None
    listing_id: str | None = None
    delivery_id: str | None = None


def _name_segment(value: Any) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    return text.strip("-._")[:100]


def package_spec_for_listing(row: Any) -> PackageSpec:
    slug = _name_segment(row.slug) or "blueprint"
    if row.entity_id is None:
        return PackageSpec(
            ref=row.id, name=f"@manor/{slug}", kind=PACKAGE_KIND_PLATFORM,
            listing_id=row.id,
        )
    scope = _name_segment(row.author_handle) or f"entity-{_name_segment(row.entity_id[:8])}"
    return PackageSpec(
        ref=row.id, name=f"@{scope}/{slug}", kind=PACKAGE_KIND_MARKETPLACE,
        owner_entity_id=row.entity_id, listing_id=row.id,
    )


def delivery_package_ref(delivery_id: str) -> str:
    return f"{DELIVERY_BLUEPRINT_ID_PREFIX}{delivery_id}"


def package_spec_for_delivery(row: Any) -> PackageSpec:
    return PackageSpec(
        ref=delivery_package_ref(row.id),
        name=f"@delivery-{_name_segment(row.entity_id[:8])}/{_name_segment(row.id)}",
        kind=PACKAGE_KIND_DELIVERY,
        owner_entity_id=row.entity_id,
        delivery_id=row.id,
    )


def version_sort_key(value: Any) -> tuple:
    """Semver precedence: a pre-release sorts before its release.

    Versions that are not semver (none are written today) fall back to the
    lenient ``parse_content_version`` reading instead of raising.
    """
    match = _SEMVER.match(str(value or "").strip())
    if match is None:
        return (*parse_content_version(value), 1, ())
    major, minor, patch, prerelease = match.groups()
    if prerelease is None:
        return (int(major), int(minor), int(patch), 1, ())
    identifiers = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in prerelease.split(".")
    )
    return (int(major), int(minor), int(patch), 0, identifiers)


def is_prerelease(value: Any) -> bool:
    match = _SEMVER.match(str(value or "").strip())
    return bool(match and match.group(4))


def _payload_format(payload: Mapping[str, Any], fallback: str | None = None) -> str:
    from packages.core.blueprints.payload import PayloadError, detect_version

    try:
        return detect_version(dict(payload))[:10]
    except PayloadError:
        return (fallback or "1.0")[:10]


def _release_notes(payload: Mapping[str, Any]) -> str | None:
    manifest = payload.get("manifest") if isinstance(payload, Mapping) else None
    notes = manifest.get("release_notes") if isinstance(manifest, Mapping) else None
    return (notes.strip() or None) if isinstance(notes, str) else None


async def _lock_package(db, package_ref: str, spec: PackageSpec | None) -> BlueprintPackage:
    statement = select(BlueprintPackage).where(BlueprintPackage.ref == package_ref).with_for_update()
    package = (await db.execute(statement)).scalar_one_or_none()
    if package is not None:
        return package
    if spec is None:
        raise LookupError(f"Blueprint package {package_ref!r} has no releases yet")
    # Another package may already hold the preferred alias (two creators
    # share a handle). The alias is cosmetic; the ref is the identity.
    suffix = hashlib.sha256(package_ref.encode()).hexdigest()[:8]
    for name in (spec.name, f"{spec.name[:205]}-{suffix}"):
        await db.execute(pg_insert(BlueprintPackage).values(
            ref=package_ref, name=name, kind=spec.kind,
            owner_entity_id=spec.owner_entity_id, listing_id=spec.listing_id,
            delivery_id=spec.delivery_id,
        ).on_conflict_do_nothing())
        package = (await db.execute(statement)).scalar_one_or_none()
        if package is not None:
            return package
    raise LookupError(f"Blueprint package {package_ref!r} could not be created")


async def ensure_package(db, spec: PackageSpec) -> BlueprintPackage:
    """The package row for ``spec``, created when missing; locked."""
    with db.no_autoflush:
        return await _lock_package(db, spec.ref, spec)


async def _move_tag(db, package_ref: str, tag: str, version: str, updated_by: str | None) -> None:
    await db.execute(pg_insert(BlueprintDistTag).values(
        package_ref=package_ref, tag=tag, version=version, updated_by=updated_by,
    ).on_conflict_do_update(
        index_elements=[BlueprintDistTag.package_ref, BlueprintDistTag.tag],
        set_={"version": version, "updated_at": func.now(), "updated_by": updated_by},
    ))


async def latest_release(db, package_ref: str) -> BlueprintVersion | None:
    """The release ``latest`` names, or None before the package's first.

    Never flushes: a caller's serving row may still be pending without its
    payload (see ``record_release``).
    """
    with db.no_autoflush:
        return await _latest_release(db, package_ref)


async def _latest_release(db, package_ref: str) -> BlueprintVersion | None:
    return (await db.execute(select(BlueprintVersion).join(
        BlueprintDistTag,
        (BlueprintDistTag.package_ref == BlueprintVersion.package_ref)
        & (BlueprintDistTag.version == BlueprintVersion.version),
    ).where(
        BlueprintDistTag.package_ref == package_ref, BlueprintDistTag.tag == LATEST_TAG,
    ))).scalar_one_or_none()


async def listed_release(db, package_ref: str, version: str) -> BlueprintVersion | None:
    """A release an install may upgrade (or go back) to: listed, with content."""
    return (await db.execute(select(BlueprintVersion).where(
        BlueprintVersion.package_ref == package_ref,
        BlueprintVersion.version == str(version or "").strip(),
        BlueprintVersion.visibility == VISIBILITY_LISTED,
        BlueprintVersion.payload.is_not(None),
    ))).scalar_one_or_none()


async def package_releases(db, package_ref: str) -> dict[str, Any] | None:
    """A package's listed releases, newest first, with its tags (≈ an npm packument)."""
    package = await db.get(BlueprintPackage, package_ref)
    if package is None:
        return None
    rows = (await db.execute(select(
        BlueprintVersion.version, BlueprintVersion.published_at, BlueprintVersion.changelog,
        BlueprintVersion.min_bump, BlueprintVersion.deprecated, BlueprintVersion.format,
    ).where(
        BlueprintVersion.package_ref == package_ref,
        BlueprintVersion.visibility == VISIBILITY_LISTED,
        BlueprintVersion.payload.is_not(None),
    ))).all()
    tags = dict((await db.execute(select(BlueprintDistTag.tag, BlueprintDistTag.version).where(
        BlueprintDistTag.package_ref == package_ref,
    ))).all())
    return {
        "name": package.name,
        "deprecated": package.deprecated,
        "tags": tags,
        "versions": [
            {
                "version": row.version,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "changelog": row.changelog,
                "min_bump": row.min_bump,
                "deprecated": row.deprecated,
                "format": row.format,
                "prerelease": is_prerelease(row.version),
            }
            for row in sorted(rows, key=lambda row: version_sort_key(row.version), reverse=True)
        ],
    }


async def next_release_version(
    db, package_ref: str, payload: dict[str, Any], *, current_version: Any, stored_fingerprint: Any,
) -> str:
    """The version a publish of ``payload`` releases as.

    Unchanged installable content keeps ``current_version``; otherwise the
    number steps by what changed since ``latest`` (``release_diff``), so a
    removed component can never ship as a patch. A package with no release
    yet steps a patch, as every publish did before releases were recorded.
    """
    from packages.core.blueprints.freshness import (
        FIRST_CONTENT_VERSION, bump_content_version, next_content_version,
    )
    from packages.core.blueprints.release_diff import minimum_bump

    current = str(current_version or FIRST_CONTENT_VERSION)
    if next_content_version(current_version=current, stored_fingerprint=stored_fingerprint,
                            payload=payload)[0] == current:
        return current
    previous = await latest_release(db, package_ref)
    level = minimum_bump(previous.payload, payload) if previous is not None and previous.payload else "patch"
    return bump_content_version(current, level)


def _release_floor(previous: BlueprintVersion, payload: Mapping[str, Any]) -> tuple[str, str, list[str]]:
    """The lowest version a release after ``previous`` may take, and why."""
    from packages.core.blueprints.freshness import bump_content_version
    from packages.core.blueprints.release_diff import diff_releases

    diff = diff_releases(previous.payload, payload)
    return bump_content_version(previous.version, diff["min_bump"]), diff["min_bump"], diff["reasons"]


async def record_release(
    db,
    package_ref: str,
    payload: dict[str, Any],
    version: str,
    *,
    listing: Any = None,
    delivery: Any = None,
    spec: PackageSpec | None = None,
    payload_version: str | None = None,
    changelog: str | None = None,
    published_by: str | None = None,
    published_at: datetime | None = None,
    origin: str = ORIGIN_PUBLISHED,
    visibility: str = VISIBILITY_LISTED,
) -> BlueprintVersion:
    """Make ``payload`` release ``version`` of ``package_ref``. Caller commits.

    Recording the same version with the same installable content again is a
    no-op that returns the existing row, so an unchanged seed or a repeated
    backfill writes no history. The same version with different content
    raises ``BlueprintVersionConflictError``: a published version never
    changes, the release needs a higher number.

    ``listing`` (a ``WorkspaceBlueprint``) or ``delivery`` (a
    ``ClientWorkspaceDelivery``) is the serving copy refreshed to this
    release; leave both out to record history only (backfill). A listed,
    non-pre-release version becomes ``latest``.
    """
    if listing is not None and delivery is not None:
        raise ValueError("a release has one serving copy")
    if listing is not None:
        if listing.id != package_ref:
            raise ValueError("the listing is not this package")
        spec = spec or package_spec_for_listing(listing)
    if delivery is not None:
        if delivery_package_ref(delivery.id) != package_ref:
            raise ValueError("the delivery is not this package")
        spec = spec or package_spec_for_delivery(delivery)
    version = str(version or "").strip()
    if not version or len(version) > 64:
        raise ValueError("a release needs a version of at most 64 characters")
    if not isinstance(payload, dict):
        raise ValueError("a release needs a payload")
    fingerprint = blueprint_content_fingerprint(payload)

    # The serving row may still be pending without its payload; nothing may
    # flush it before the payload is assigned below.
    with db.no_autoflush:
        package = await _lock_package(db, package_ref, spec)
        release = (await db.execute(select(BlueprintVersion).where(
            BlueprintVersion.package_ref == package_ref,
            BlueprintVersion.version == version,
        ))).scalar_one_or_none()
    if release is not None and release.content_fingerprint != fingerprint:
        raise BlueprintVersionConflictError(
            f"{package.name}@{version} is already published with different content; "
            "release it under a higher version"
        )
    min_bump = None
    if release is None and origin == ORIGIN_PUBLISHED and visibility == VISIBILITY_LISTED:
        previous = await latest_release(db, package_ref)
        if previous is not None and previous.payload and previous.version != version:
            floor, min_bump, reasons = _release_floor(previous, payload)
            # A pre-release of the floor (2.0.0-beta.1) is allowed ahead of it.
            if version_sort_key(version)[:3] < version_sort_key(floor)[:3]:
                raise BlueprintVersionConflictError(
                    f"{package.name}@{version} must be at least {floor} after {previous.version}: "
                    + "; ".join(reasons[:5])
                )
    if release is None:
        manifest = payload.get("manifest")
        release = BlueprintVersion(
            package_ref=package_ref,
            version=version,
            format=_payload_format(payload, payload_version),
            content_fingerprint=fingerprint,
            section_fingerprints=blueprint_section_fingerprints(payload),
            payload=deepcopy(payload),
            manifest=deepcopy(manifest) if isinstance(manifest, dict) else {},
            changelog=changelog if changelog is not None else _release_notes(payload),
            min_bump=min_bump,
            visibility=visibility,
            origin=origin,
            published_at=published_at or datetime.now(timezone.utc),
            published_by=published_by,
        )
        db.add(release)
    if listing is not None:
        listing.payload = payload
        listing.payload_version = payload_version or _payload_format(payload, listing.payload_version)
        listing.content_version = version
        listing.content_fingerprint = fingerprint
    if delivery is not None:
        delivery.payload = payload
        delivery.content_release = {**dict(delivery.content_release or {}), "version": version}
    if visibility == VISIBILITY_LISTED and not is_prerelease(version):
        await _move_tag(db, package_ref, LATEST_TAG, version, published_by)
    await db.flush()
    return release


async def record_baseline_identity(
    db,
    package_ref: str,
    *,
    installed_version: str | None,
    fingerprint: str,
    section_fingerprints: Mapping[str, Any] | None,
    manifest: Mapping[str, Any] | None,
) -> BlueprintVersion:
    """A hidden version naming content an install's v3 baseline came from.

    The payload is gone (it was overwritten before history was kept), so the
    row carries only identity. It is never an upgrade target and never moves
    ``latest``; the Workspace's own baseline remains the merge base.
    """
    if not fingerprint:
        raise ValueError("a baseline identity needs the installed fingerprint")
    version = f"{installed_version or '0.0.0'}-legacy.{fingerprint[:8]}"[:64]
    with db.no_autoflush:
        await _lock_package(db, package_ref, None)
        release = (await db.execute(select(BlueprintVersion).where(
            BlueprintVersion.package_ref == package_ref,
            BlueprintVersion.version == version,
        ))).scalar_one_or_none()
    if release is not None:
        if release.content_fingerprint != fingerprint:
            raise BlueprintVersionConflictError(f"{package_ref}@{version} names different content")
        return release
    release = BlueprintVersion(
        package_ref=package_ref, version=version, format="1.1",
        content_fingerprint=fingerprint,
        section_fingerprints=dict(section_fingerprints or {}),
        payload=None, manifest=dict(manifest or {}),
        visibility=VISIBILITY_HIDDEN, origin=ORIGIN_BASELINE,
        published_at=datetime.now(timezone.utc),
    )
    db.add(release)
    await db.flush()
    return release


def set_draft_payload(row: Any, payload: dict[str, Any], *, payload_version: str | None = None) -> None:
    """Replace an unpublished Blueprint's editable content.

    A published row's payload is a release that installed Workspaces upgrade
    toward; it changes only through ``record_release``.
    """
    if row.status == BlueprintStatus.PUBLISHED.value:
        raise ValueError("a published Blueprint changes only through a release")
    row.payload = payload
    if payload_version:
        row.payload_version = payload_version


def set_listing_manifest(row: Any, manifest: dict[str, Any]) -> None:
    """Replace the listing copy a payload carries in ``manifest``.

    The manifest is outside the fingerprinted sections, so this never changes
    what an install copies; the release's own version row keeps the manifest
    it was published with.
    """
    row.payload = {**dict(row.payload or {}), "manifest": manifest}


# ── Install records ─────────────────────────────────────────────────────

def package_record(
    package: BlueprintPackage,
    release: BlueprintVersion,
    *,
    base: str,
    previous: Any = None,
) -> dict[str, Any]:
    """The ``_blueprint.package`` block naming ``release``."""
    record = {
        "ref": package.ref,
        "name": package.name,
        "version": release.version,
        "integrity": release.integrity,
        "format": release.format,
        "base": base,
    }
    if isinstance(previous, dict):
        record.update({key: deepcopy(previous[key]) for key in _LOCAL_PACKAGE_KEYS if key in previous})
    return record


def settings_without_package_alias(settings: Any) -> Any:
    """A Workspace's settings as its readers get them.

    The install record's ``package.name`` is cosmetic, and a marketplace package's
    alias holds its author's personal handle from when it was first released, which
    a buyer has no business reading once the listing is under a company's name.
    Nothing reads it back: ``ref`` is the identity, and it stays. The stored record
    is left alone, because its alias can't be told apart from a current one.
    """
    record = settings.get(BLUEPRINT_SETTINGS_KEY) if isinstance(settings, dict) else None
    package = record.get(PACKAGE_RECORD_KEY) if isinstance(record, dict) else None
    if not isinstance(package, dict) or "name" not in package:
        return settings
    without_alias = {key: value for key, value in package.items() if key != "name"}
    return {**settings, BLUEPRINT_SETTINGS_KEY: {**record, PACKAGE_RECORD_KEY: without_alias}}


async def release_for_fingerprint(
    db,
    package_ref: str,
    fingerprint: str,
    *,
    preferred_version: str | None = None,
) -> BlueprintVersion | None:
    """The release whose content is exactly ``fingerprint``.

    Content may legitimately repeat across versions (an author reverts); the
    recorded version wins when it is one of them, else the highest.
    """
    if not package_ref or not fingerprint:
        return None
    rows = (await db.execute(select(BlueprintVersion).where(
        BlueprintVersion.package_ref == package_ref,
        BlueprintVersion.content_fingerprint == fingerprint,
    ))).scalars().all()
    if not rows:
        return None
    for row in rows:
        if preferred_version and row.version == preferred_version:
            return row
    return max(rows, key=lambda row: version_sort_key(row.version))


async def sync_installed_package(db, record: dict[str, Any]) -> None:
    """Keep ``record['package']`` naming the release the record now describes.

    Every writer that moves ``blueprint_version`` / ``content_fingerprint``
    (install, upgrade, undo) calls this, so the package block can never claim
    a release the Workspace no longer runs. Those writers always record the
    exact version and content they applied, so one exact lookup suffices.
    When it misses, the block is dropped rather than left stale; an
    ``unverified`` block for the same package stays, since it never claimed
    to know the content.
    """
    previous = record.get(PACKAGE_RECORD_KEY)
    ref = str(record.get(BLUEPRINT_ID_KEY) or "").strip()
    version = str(record.get(BLUEPRINT_VERSION_KEY) or "").strip()
    fingerprint = str(record.get(CONTENT_FINGERPRINT_KEY) or "").strip()
    release = package = None
    if ref and version and fingerprint and record.get("source") != "raw_payload":
        # Derived bookkeeping: a failed lookup must not fail the install or
        # upgrade it rides along with. The block is then left out, and the
        # adoption backfill can add it later.
        try:
            async with db.begin_nested():
                release = (await db.execute(select(BlueprintVersion).where(
                    BlueprintVersion.package_ref == ref,
                    BlueprintVersion.version == version,
                    BlueprintVersion.content_fingerprint == fingerprint,
                ))).scalar_one_or_none()
                if release is not None:
                    package = await db.get(BlueprintPackage, ref)
        except SQLAlchemyError:
            logger.warning("blueprint package lookup failed for %s@%s", ref, version, exc_info=True)
            release = package = None
    if package is not None:
        record[PACKAGE_RECORD_KEY] = package_record(package, release, base=BASE_EXACT, previous=previous)
    elif not (
        isinstance(previous, dict)
        and previous.get("base") == BASE_UNVERIFIED
        and previous.get("ref") == ref
    ):
        record.pop(PACKAGE_RECORD_KEY, None)


async def installed_release_payload(db, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The exact release an install runs (``base: exact``), or ``None``."""
    package = record.get(PACKAGE_RECORD_KEY)
    if not (isinstance(package, dict) and package.get("base") == BASE_EXACT and package.get("ref") and package.get("version")):
        return None
    release = await listed_release(db, str(package["ref"]), str(package["version"]))
    return deepcopy(release.payload) if release is not None and isinstance(release.payload, dict) else None


def installed_release_version(record: Mapping[str, Any]) -> str | None:
    """The version an install provably runs, for counting releases since."""
    package = record.get(PACKAGE_RECORD_KEY)
    if isinstance(package, dict) and package.get("base") == BASE_EXACT and package.get("version"):
        return str(package["version"])
    version = record.get(BLUEPRINT_VERSION_KEY)
    return str(version) if version else None


# ── Release ranges ──────────────────────────────────────────────────────

async def release_ranges(
    db,
    installs: Mapping[str, tuple[str, str | None]],
) -> dict[str, dict[str, Any]]:
    """For each ``key -> (package_ref, installed_version)``: the latest version,
    how many listed releases the install is behind, and their changelogs.

    Two queries for any number of installs. Keys whose package has no
    ``latest`` release are left out.
    """
    refs = {ref for ref, _version in installs.values() if ref}
    if not refs:
        return {}
    latest = dict((await db.execute(select(
        BlueprintDistTag.package_ref, BlueprintDistTag.version,
    ).where(
        BlueprintDistTag.package_ref.in_(refs), BlueprintDistTag.tag == LATEST_TAG,
    ))).all())
    if not latest:
        return {}
    listed: dict[str, list[Any]] = {}
    for row in (await db.execute(select(
        BlueprintVersion.package_ref, BlueprintVersion.version,
        BlueprintVersion.changelog, BlueprintVersion.published_at,
    ).where(
        BlueprintVersion.package_ref.in_(latest), BlueprintVersion.visibility == VISIBILITY_LISTED,
    ))).all():
        listed.setdefault(row.package_ref, []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for key, (ref, installed) in installs.items():
        if ref not in latest:
            continue
        latest_key = version_sort_key(latest[ref])
        between: list[Any] = []
        if installed:
            installed_key = version_sort_key(installed)
            between = sorted(
                (
                    row for row in listed.get(ref, [])
                    if installed_key < version_sort_key(row.version) <= latest_key
                ),
                key=lambda row: version_sort_key(row.version),
            )
        out[key] = {
            "latest_version": latest[ref],
            "versions_behind": len(between) if installed else None,
            "changelog": [
                {
                    "version": row.version,
                    "changelog": row.changelog,
                    "published_at": row.published_at.isoformat() if row.published_at else None,
                }
                for row in between
            ],
        }
    return out


async def release_range_for(db, record: Mapping[str, Any]) -> dict[str, Any]:
    """``release_ranges`` for one install record; empty when unknown."""
    ref = str(record.get(BLUEPRINT_ID_KEY) or "").strip()
    if not ref or record.get("source") == "raw_payload":
        return {}
    return (await release_ranges(db, {"install": (ref, installed_release_version(record))})).get("install", {})

