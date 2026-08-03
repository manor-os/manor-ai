"""An installed workspace can tell whether its blueprint moved on without it.

Installing copies a blueprint's content into the workspace once; nothing
flows back. The faceless-stickman blueprint shipped its video skill as a
636-character summary from 2026-07-19 to 07-22, and a workspace installed on
07-22 got that summary as the skill's entire system prompt. On 07-27 the
blueprint was corrected to the full 4664-character procedure. The workspace
kept running the summary and had no way to learn a fix existed.

The install record could not answer it: it kept the slug and the timestamp,
and ``manifest.blueprint_version`` is the payload *format* version — it read
"1.1" across that whole rewrite. So content is fingerprinted instead.

Detection only. Applying an update is a separate, deliberate act: a
workspace may have edited what was installed, and nothing here may decide
those edits are stale.
"""
from __future__ import annotations

import copy

import pytest

from packages.core.blueprints.freshness import (
    CONTENT_FINGERPRINT_KEY,
    SECTION_FINGERPRINTS_KEY,
    BlueprintFreshness,
    blueprint_content_fingerprint,
    blueprint_freshness,
    blueprint_section_fingerprints,
    blueprint_update_summary,
)
from packages.core.blueprints.solo_company import get_solo_company_blueprint

SLUG = "solo-faceless-stickman-studio-v1"


@pytest.fixture
def payload():
    return get_solo_company_blueprint(SLUG)


def _installed(fingerprint=None, *, sections=None, slug=SLUG, blueprint_id=None):
    record = {
        # Identity is the id; the slug is a display name.
        "blueprint_id": blueprint_id if blueprint_id is not None else f"builtin:{slug}",
        "blueprint_slug": slug,
        "installed_at": "2026-07-22T23:24:48Z",
    }
    if fingerprint is not None:
        record[CONTENT_FINGERPRINT_KEY] = fingerprint
    if sections is not None:
        record[SECTION_FINGERPRINTS_KEY] = sections
    return {"_blueprint": record}


# ── The fingerprint ───────────────────────────────────────────────────


def test_the_same_content_fingerprints_the_same(payload):
    assert blueprint_content_fingerprint(payload) == blueprint_content_fingerprint(
        copy.deepcopy(payload)
    )


def test_key_order_and_formatting_are_not_changes(payload):
    """A badge that fires on a reformat is a badge people learn to dismiss."""
    reordered = {key: payload[key] for key in reversed(list(payload))}
    assert blueprint_content_fingerprint(reordered) == blueprint_content_fingerprint(payload)


def test_changing_installed_content_changes_the_fingerprint(payload):
    """The production case: the skill's system prompt was rewritten."""
    edited = copy.deepcopy(payload)
    edited["embedded"]["skills"][0]["system_prompt"] = "a much shorter summary"
    assert blueprint_content_fingerprint(edited) != blueprint_content_fingerprint(payload)


def test_presentation_copy_is_not_an_update(payload):
    """Nothing in the manifest reaches the workspace, so nothing there can
    make an install stale."""
    edited = copy.deepcopy(payload)
    edited["manifest"]["summary"] = "reworded for the marketplace card"
    edited["manifest"]["title"] = "Renamed"
    assert blueprint_content_fingerprint(edited) == blueprint_content_fingerprint(payload)


@pytest.mark.parametrize("value", [None, {}, "not a payload", []])
def test_an_unusable_payload_has_no_fingerprint(value):
    assert blueprint_content_fingerprint(value) == ""


# ── The verdict ───────────────────────────────────────────────────────


def test_a_fresh_install_is_current(payload):
    settings = _installed(blueprint_content_fingerprint(payload))
    assert blueprint_freshness(settings, payload) is BlueprintFreshness.CURRENT


def test_an_install_from_older_content_has_an_update(payload):
    older = copy.deepcopy(payload)
    older["embedded"]["skills"][0]["system_prompt"] = "the 636-character summary"
    settings = _installed(blueprint_content_fingerprint(older))
    assert blueprint_freshness(settings, payload) is BlueprintFreshness.UPDATE_AVAILABLE


def test_an_install_from_before_fingerprints_says_unknown(payload):
    """The real 07-22 record. It may or may not be behind — claiming either
    would be a guess, and a badge nobody can act on is worse than none."""
    assert blueprint_freshness(_installed(), payload) is BlueprintFreshness.UNKNOWN


def test_a_workspace_with_no_blueprint_is_not_judged(payload):
    for settings in (
        {}, {"_blueprint": {}}, None, {"_blueprint": "nonsense"},
        # A slug alone is not an identity.
        {"_blueprint": {"blueprint_slug": SLUG}},
    ):
        assert blueprint_freshness(settings, payload) is BlueprintFreshness.NOT_FROM_BLUEPRINT


def test_a_missing_blueprint_is_unknown_not_current(payload):
    """If the blueprint it names cannot be read, "up to date" is a lie."""
    settings = _installed(blueprint_content_fingerprint(payload))
    assert blueprint_freshness(settings, None) is BlueprintFreshness.UNKNOWN


# ── What to show ──────────────────────────────────────────────────────


def test_the_summary_names_what_changed_when_it_can(payload):
    older = copy.deepcopy(payload)
    older["embedded"]["skills"][0]["system_prompt"] = "the 636-character summary"
    settings = _installed(
        blueprint_content_fingerprint(older), sections=blueprint_section_fingerprints(older),
    )

    summary = blueprint_update_summary(settings, payload)
    assert summary["status"] == "update_available"
    assert summary["changed"] == ["embedded"]
    assert summary["blueprint_slug"] == SLUG
    assert summary["installed_at"] == "2026-07-22T23:24:48Z"


def test_section_fingerprints_stay_small(payload):
    """Recording the payload itself would answer the same question at 36KB
    per workspace, in a settings blob read on every page load."""
    assert len(str(blueprint_section_fingerprints(payload))) < 400


def test_an_older_record_reports_the_update_without_the_detail(payload):
    """Installs from before section fingerprints prove *that* it changed, not
    what. An empty list means "cannot say", never "nothing changed"."""
    older = copy.deepcopy(payload)
    older["embedded"]["skills"][0]["system_prompt"] = "short"
    summary = blueprint_update_summary(_installed(blueprint_content_fingerprint(older)), payload)
    assert summary["status"] == "update_available"
    assert summary["changed"] == []


def test_a_current_workspace_lists_no_changes(payload):
    summary = blueprint_update_summary(_installed(blueprint_content_fingerprint(payload)), payload)
    assert summary["status"] == "current" and summary["changed"] == []


# ── The two ends of the wire ──────────────────────────────────────────


def test_installing_records_the_fingerprint():
    """Without this the next install is as blind as the 07-22 one."""
    import inspect

    from packages.core.blueprints import installer

    body = inspect.getsource(installer)
    assert "CONTENT_FINGERPRINT_KEY: blueprint_content_fingerprint(payload)" in body
    assert "SECTION_FINGERPRINTS_KEY: blueprint_section_fingerprints(payload)" in body


def test_the_workspace_api_reports_it():
    import inspect

    from apps.api.routers import workspaces

    body = inspect.getsource(workspaces)
    assert "blueprint_update" in body
    assert "_blueprint_update_for(ws, blueprint_payload)" in body


def test_detection_does_not_apply_anything():
    """The module must not grow an upgrade path by accident — a workspace
    may have edited what the blueprint installed."""
    from packages.core.blueprints import freshness

    for forbidden in ("db.", "session", "commit(", "async def"):
        assert forbidden not in inspect_source(freshness), (
            f"freshness.py touched {forbidden!r} — detection only"
        )


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)


# ── Two ways in ───────────────────────────────────────────────────────


def test_marketplace_installs_are_resolved_by_id_not_slug():
    """A marketplace blueprint lives in workspace_blueprints and the install
    records its id. Resolving by slug alone finds nothing there, which would
    leave every marketplace install silently reporting "unknown" forever —
    the feature working only for the four built-in blueprints."""
    import inspect

    from apps.api.routers import workspaces

    body = inspect.getsource(workspaces._blueprint_payloads_for)
    assert "WorkspaceBlueprint" in body, (
        "payloads must come from the blueprint table"
    )
    assert "BLUEPRINT_ID_KEY" in body
    assert "_builtin_blueprint_payload" not in body, (
        "the platform's blueprints are rows now — no config-directory branch"
    )


def test_the_workspace_list_looks_payloads_up_once():
    """This feeds the list; one query, not one per workspace."""
    import inspect

    from apps.api.routers import workspaces

    body = inspect.getsource(workspaces.list_my_workspaces)
    assert "_blueprint_payloads_for(db, workspaces)" in body


def test_installing_records_the_fingerprint_for_any_payload():
    """Recording already worked for both routes — install_blueprint is the
    single door, and it fingerprints whatever payload it is handed. Only the
    reading side knew about built-ins."""
    import inspect

    from packages.core.blueprints import installer

    body = inspect.getsource(installer.install_blueprint)
    assert "blueprint_content_fingerprint(payload)" in body


# ── Identity is the id; the version is what moves ─────────────────────


def test_publishing_an_edit_bumps_the_version_by_itself():
    """The owner should never have to remember. Publishing IS the intent."""
    from packages.core.blueprints.freshness import next_content_version

    payload = get_solo_company_blueprint(SLUG)
    version, fingerprint = next_content_version(
        current_version="1.0.0", stored_fingerprint=None, payload=payload,
    )
    assert version == "1.0.1" and fingerprint


def test_republishing_the_same_content_does_not_bump():
    """Every installed workspace would otherwise be told it is behind for a
    no-op — the badge people learn to dismiss."""
    from packages.core.blueprints.freshness import next_content_version

    payload = get_solo_company_blueprint(SLUG)
    fingerprint = blueprint_content_fingerprint(payload)
    assert next_content_version(
        current_version="1.2.3", stored_fingerprint=fingerprint, payload=payload,
    ) == ("1.2.3", fingerprint)


def test_marketplace_copy_edits_do_not_bump():
    """Retitling a listing changes nothing an install received."""
    from packages.core.blueprints.freshness import next_content_version

    payload = get_solo_company_blueprint(SLUG)
    fingerprint = blueprint_content_fingerprint(payload)
    edited = copy.deepcopy(payload)
    edited["manifest"]["summary"] = "reworded for the listing"
    assert next_content_version(
        current_version="1.2.3", stored_fingerprint=fingerprint, payload=edited,
    )[0] == "1.2.3"


def test_editing_installed_content_bumps():
    from packages.core.blueprints.freshness import next_content_version

    payload = get_solo_company_blueprint(SLUG)
    fingerprint = blueprint_content_fingerprint(payload)
    edited = copy.deepcopy(payload)
    edited["embedded"]["skills"][0]["system_prompt"] = "a corrected procedure"
    version, new_fingerprint = next_content_version(
        current_version="1.2.3", stored_fingerprint=fingerprint, payload=edited,
    )
    assert version == "1.2.4" and new_fingerprint != fingerprint


def test_a_lower_installed_version_means_an_update(payload):
    from packages.core.blueprints.freshness import BLUEPRINT_VERSION_KEY

    settings = {"_blueprint": {"blueprint_id": f"builtin:{SLUG}", BLUEPRINT_VERSION_KEY: "1.0.2"}}
    assert blueprint_freshness(
        settings, payload, current_version="1.0.3",
    ) is BlueprintFreshness.UPDATE_AVAILABLE
    assert blueprint_freshness(
        settings, payload, current_version="1.0.2",
    ) is BlueprintFreshness.CURRENT
    # numeric, not lexical: "1.0.10" is newer than "1.0.9"
    assert blueprint_freshness(
        {"_blueprint": {"blueprint_id": f"builtin:{SLUG}", BLUEPRINT_VERSION_KEY: "1.0.9"}},
        payload, current_version="1.0.10",
    ) is BlueprintFreshness.UPDATE_AVAILABLE


def test_the_version_decides_even_when_the_payload_cannot_be_read(payload):
    """A version survives a payload the reader cannot fetch."""
    from packages.core.blueprints.freshness import BLUEPRINT_VERSION_KEY

    settings = {"_blueprint": {"blueprint_id": f"builtin:{SLUG}", BLUEPRINT_VERSION_KEY: "1.0.0"}}
    assert blueprint_freshness(
        settings, None, current_version="1.1.0",
    ) is BlueprintFreshness.UPDATE_AVAILABLE


def test_a_payload_install_adopts_the_published_row_it_matches():
    """A caller-supplied payload that IS a published blueprint installs as
    that blueprint, not as an anonymous copy nothing can later update."""
    import inspect

    from apps.api.routers import blueprints

    body = inspect.getsource(blueprints.install_from_payload)
    assert "platform_blueprint_id(payload_slug)" in body
    assert "blueprint_id=matched.id if matched else None" in body
    assert "blueprint_version=matched.content_version if matched else None" in body


def test_a_marketplace_install_records_the_version_it_took():
    import inspect

    from apps.api.routers import blueprints

    body = inspect.getsource(blueprints)
    assert "blueprint_version=row.content_version" in body
