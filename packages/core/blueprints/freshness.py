"""Is an installed workspace still the blueprint it was installed from?

Installing a blueprint copies its content — skills, agents, knowledge — into
the workspace once. Nothing flows back afterwards, so a blueprint fix reaches
new installs only.

That is not hypothetical. The faceless-stickman blueprint shipped its video
skill as a 636-character summary from 2026-07-19 to 2026-07-22; a workspace
installed on 07-22 got that summary as the skill's whole system prompt. On
07-27 the blueprint was corrected to the full 4664-character procedure. The
workspace installed five days earlier still runs the summary, and had no way
to know a fix existed.

Detecting that needs one thing the install record never kept: which *version*
of the blueprint it was. ``manifest.blueprint_version`` does not answer it —
that is the payload *format* version, and it read "1.1" across the whole
07-19..07-27 rewrite. So content is fingerprinted here instead: no number to
maintain by hand, and it cannot drift from what was actually installed.

Detection only. Applying an update is a separate, deliberate act — a
workspace may have edited what the blueprint installed, and nothing here is
allowed to decide that its edits are stale.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Optional

#: Where the install record keeps the fingerprint of what it copied.
BLUEPRINT_SETTINGS_KEY = "_blueprint"
CONTENT_FINGERPRINT_KEY = "content_fingerprint"
SECTION_FINGERPRINTS_KEY = "section_fingerprints"

#: Payload sections that decide what a workspace actually gets. Anything
#: outside these (presentation copy, marketplace pricing) can change without
#: making an install stale.
_FINGERPRINTED_SECTIONS = ("embedded", "recipe", "policy", "contract")


class BlueprintFreshness(str, Enum):
    """How an installed workspace stands against the current blueprint."""

    #: Installed content matches the blueprint as it is today.
    CURRENT = "current"

    #: The blueprint has changed since this workspace was installed.
    UPDATE_AVAILABLE = "update_available"

    #: Installed before fingerprints were recorded. It may or may not be
    #: behind; claiming either would be a guess, so the UI says so plainly
    #: rather than showing a badge nobody can act on.
    UNKNOWN = "unknown"

    #: The workspace was not installed from a blueprint at all.
    NOT_FROM_BLUEPRINT = "not_from_blueprint"


def blueprint_content_fingerprint(payload: dict[str, Any] | None) -> str:
    """A stable hash of the parts of ``payload`` that an install copies.

    Key order and whitespace must not matter: the same content reformatted is
    not an update, and telling a workspace otherwise trains people to ignore
    the badge.
    """
    if not isinstance(payload, dict):
        return ""
    material = {
        section: payload.get(section)
        for section in _FINGERPRINTED_SECTIONS
        if payload.get(section) is not None
    }
    if not material:
        return ""
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def blueprint_section_fingerprints(payload: dict[str, Any] | None) -> dict[str, str]:
    """One fingerprint per copied section, so a later comparison can say
    *what* changed rather than only *that* something did.

    Storing the payload itself would answer that too, at 36KB per workspace
    in a settings blob read on every page. Four hashes cost nothing and
    survive just as well.
    """
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for section in _FINGERPRINTED_SECTIONS:
        value = payload.get(section)
        if value is None:
            continue
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        out[section] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return out


#: Where an install records the blueprint identity it came from. Both are
#: always present going forward: a built-in blueprint has the marketplace id
#: ``builtin:<slug>``, so identity never depends on the slug alone.
BLUEPRINT_ID_KEY = "blueprint_id"
BLUEPRINT_VERSION_KEY = "blueprint_version"

#: The version every blueprint starts at, as major.minor.patch.
FIRST_CONTENT_VERSION = "1.0.0"

_VERSION_PART_MAX = 99


def parse_content_version(value: Any) -> tuple[int, int, int]:
    """``"1.2.3"`` → ``(1, 2, 3)``. Anything unreadable reads as the first
    version rather than raising — a malformed row must not take the
    marketplace down."""
    parts = str(value or "").strip().split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        try:
            numbers.append(max(0, int(part)))
        except (TypeError, ValueError):
            numbers.append(0)
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def format_content_version(parts: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in parts)


def bump_content_version(value: Any) -> str:
    """The next version after a content change.

    Publishing an edit is a patch release; the parts roll over at 99 so the
    number stays readable rather than growing a 400-wide patch component. An
    owner who wants to say "this is a major rewrite" edits the version
    directly — the automatic bump exists so a release can never *forget* to
    move, not to take the naming away.
    """
    major, minor, patch = parse_content_version(value or FIRST_CONTENT_VERSION)
    patch += 1
    if patch > _VERSION_PART_MAX:
        patch = 0
        minor += 1
    if minor > _VERSION_PART_MAX:
        minor = 0
        major += 1
    return format_content_version((major, minor, patch))


def next_content_version(
    *,
    current_version: Any,
    stored_fingerprint: Any,
    payload: dict[str, Any] | None,
) -> tuple[str, str]:
    """The (version, fingerprint) a blueprint should carry after publishing.

    The version is what an installed workspace compares against, so the owner
    should never have to remember to bump it — publishing an edit is the
    intent. But it only moves when the fingerprint says the *installable*
    content changed: republishing an untouched blueprint, or one whose
    marketplace copy was reworded, must not tell every installed workspace it
    is behind. A badge that fires on a no-op is one people learn to dismiss.
    """
    version = str(current_version or FIRST_CONTENT_VERSION)
    fingerprint = blueprint_content_fingerprint(payload)
    if not fingerprint:
        return version, str(stored_fingerprint or "")
    if str(stored_fingerprint or "") == fingerprint:
        return version, fingerprint
    return bump_content_version(version), fingerprint


def installed_blueprint_record(workspace_settings: Any) -> dict[str, Any]:
    """The ``_blueprint`` block an install left on the workspace."""
    if not isinstance(workspace_settings, dict):
        return {}
    record = workspace_settings.get(BLUEPRINT_SETTINGS_KEY)
    return record if isinstance(record, dict) else {}


def blueprint_freshness(
    workspace_settings: Any,
    current_payload: dict[str, Any] | None,
    *,
    current_version: str | None = None,
) -> BlueprintFreshness:
    """Compare what a workspace was installed from against the blueprint now."""
    record = installed_blueprint_record(workspace_settings)
    # Identity is the id. The slug is a display name and a per-owner
    # uniqueness rule; it was never a stable identity, and leaning on it is
    # what made platform blueprints the one shape needing its own branch.
    if not record.get(BLUEPRINT_ID_KEY):
        return BlueprintFreshness.NOT_FROM_BLUEPRINT

    # Version first: it is what publishing moves, and it survives a payload
    # the reader cannot fetch. The fingerprint remains only for installs made
    # before versions were recorded — no live blueprint needs it now that the
    # platform's own are published rows like everyone else's.
    installed_version = record.get(BLUEPRINT_VERSION_KEY)
    if installed_version is not None and current_version is not None:
        return (
            BlueprintFreshness.CURRENT
            if parse_content_version(installed_version) >= parse_content_version(current_version)
            else BlueprintFreshness.UPDATE_AVAILABLE
        )

    installed = str(record.get(CONTENT_FINGERPRINT_KEY) or "").strip()
    if not installed:
        return BlueprintFreshness.UNKNOWN

    current = blueprint_content_fingerprint(current_payload)
    if not current:
        # The blueprint it names is gone or unreadable — not something the
        # workspace can act on, and certainly not "up to date".
        return BlueprintFreshness.UNKNOWN
    return (
        BlueprintFreshness.CURRENT
        if installed == current
        else BlueprintFreshness.UPDATE_AVAILABLE
    )


def blueprint_update_summary(
    workspace_settings: Any,
    current_payload: dict[str, Any] | None,
    *,
    current_version: str | None = None,
) -> dict[str, Any]:
    """What to show a workspace about its blueprint, and what changed.

    The differences are named, not applied: "the skill Stickman Video Creator
    changed" is something an operator can judge. A bare "update available" is
    not, and this is the difference between a badge people act on and one
    they learn to dismiss.
    """
    record = installed_blueprint_record(workspace_settings)
    status = blueprint_freshness(
        workspace_settings, current_payload, current_version=current_version,
    )
    summary: dict[str, Any] = {
        "status": status.value,
        "blueprint_slug": record.get("blueprint_slug"),
        "blueprint_id": record.get(BLUEPRINT_ID_KEY),
        "installed_version": record.get(BLUEPRINT_VERSION_KEY),
        "current_version": current_version,
        "installed_at": record.get("installed_at"),
        "installed_fingerprint": record.get(CONTENT_FINGERPRINT_KEY) or None,
        "current_fingerprint": blueprint_content_fingerprint(current_payload) or None,
        "changed": [],
    }
    if status is BlueprintFreshness.UPDATE_AVAILABLE:
        summary["changed"] = _changed_sections(
            record.get(SECTION_FINGERPRINTS_KEY), current_payload,
        )
    return summary


def _changed_sections(
    installed_sections: Optional[dict[str, Any]],
    current_payload: dict[str, Any] | None,
) -> list[str]:
    """Name the sections that differ, when the install recorded enough to tell.

    Installs from before section fingerprints kept only the whole-payload
    hash, which proves *that* something changed but not what. An empty list
    means exactly that — never "nothing changed", which the caller already
    knows from the status.
    """
    if not isinstance(installed_sections, dict) or not installed_sections:
        return []
    current = blueprint_section_fingerprints(current_payload)
    if not current:
        return []
    return [
        section
        for section in _FINGERPRINTED_SECTIONS
        if installed_sections.get(section) != current.get(section)
    ]
