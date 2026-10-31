"""Blueprint releases are immutable history; installs name the release they run.

The serving payload columns keep working as before. What these tests protect
is what the history adds: a publish never loses the previous release, a
version number never changes meaning, the one-time backfill can be rerun, and
an install is tied to a release only when its recorded content proves it.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import select

from packages.core.blueprints.freshness import blueprint_content_fingerprint
from packages.core.blueprints.registry import (
    BlueprintVersionConflictError,
    package_spec_for_listing,
    record_release,
)
from packages.core.blueprints.registry_backfill import (
    adopt_workspace,
    backfill_registry,
    recover_git_history,
)
from packages.core.models.blueprint import WorkspaceBlueprint
from packages.core.models.blueprint_registry import (
    BlueprintDistTag,
    BlueprintPackage,
    BlueprintVersion,
)
from packages.core.models.blueprint_upgrade import WorkspaceBlueprintUpgradeState
from packages.core.models.client_delivery import ClientWorkspaceDelivery
from packages.core.models.workspace import Workspace

ENTITY = "01TESTENTITY0000000000000R"


def _payload(slug: str, prompt: str) -> dict:
    return {
        "manifest": {"blueprint_version": "1.1", "slug": slug, "title": slug},
        "contract": {"variables": []},
        "embedded": {"skills": [{"slug": "writer", "system_prompt": prompt}]},
        "recipe": {"workflows": []},
        "policy": {},
    }


def _listing(slug: str, payload: dict, *, entity_id=None, status="published", version="1.0.0", **fields):
    return WorkspaceBlueprint(
        id=fields.pop("id", f"builtin:{slug}" if entity_id is None else None),
        entity_id=entity_id, slug=slug, title=slug, payload=payload, status=status,
        content_version=version, content_fingerprint=blueprint_content_fingerprint(payload),
        **fields,
    )


async def _versions(db, ref):
    return (await db.execute(
        select(BlueprintVersion).where(BlueprintVersion.package_ref == ref).order_by(BlueprintVersion.version)
    )).scalars().all()


async def _latest(db, ref):
    return (await db.execute(select(BlueprintDistTag.version).where(
        BlueprintDistTag.package_ref == ref, BlueprintDistTag.tag == "latest",
    ))).scalar_one_or_none()


@pytest.mark.asyncio
async def test_each_release_is_kept_and_a_version_never_changes_content(db_session):
    first, second = _payload("notes-v1", "v1"), _payload("notes-v1", "v2")
    row = _listing("notes-v1", first)
    db_session.add(row)
    await db_session.flush()

    await record_release(db_session, row.id, first, "1.0.0", listing=row)
    await record_release(db_session, row.id, second, "1.0.1", listing=row)
    # An unchanged republish is not a release.
    await record_release(db_session, row.id, copy.deepcopy(second), "1.0.1", listing=row)

    versions = await _versions(db_session, row.id)
    assert [(v.version, v.payload["embedded"]["skills"][0]["system_prompt"]) for v in versions] == [
        ("1.0.0", "v1"), ("1.0.1", "v2"),
    ]
    assert await _latest(db_session, row.id) == "1.0.1"
    assert (row.payload, row.content_version) == (second, "1.0.1")
    assert row.content_fingerprint == blueprint_content_fingerprint(second)
    package = await db_session.get(BlueprintPackage, row.id)
    assert (package.name, package.kind) == ("@manor/notes-v1", "platform")

    with pytest.raises(BlueprintVersionConflictError):
        await record_release(db_session, row.id, _payload("notes-v1", "v3"), "1.0.1", listing=row)
    assert row.payload == second
    assert len(await _versions(db_session, row.id)) == 2


def _with_flow(payload: dict, *inputs: dict) -> dict:
    payload = copy.deepcopy(payload)
    payload["recipe"]["workflows"] = [{"slug": "intake", "steps": [], "run_inputs": list(inputs)}]
    return payload


def test_the_version_step_follows_what_an_install_relies_on():
    from packages.core.blueprints.release_diff import diff_releases

    base = _with_flow(_payload("steps", "v1"), {"key": "brief", "required": True})
    reworded = _with_flow(_payload("steps", "v2"), {"key": "brief", "required": True})
    assert diff_releases(base, reworded)["min_bump"] == "patch"
    optional = _with_flow(_payload("steps", "v1"), {"key": "brief", "required": True}, {"key": "tone"})
    assert diff_releases(base, optional)["min_bump"] == "minor"
    required = _with_flow(_payload("steps", "v1"), {"key": "brief", "required": True},
                          {"key": "deadline", "required": True})
    assert diff_releases(base, required)["min_bump"] == "major"
    no_flow = _payload("steps", "v1")
    removed = diff_releases(base, no_flow)
    assert removed["min_bump"] == "major" and removed["reasons"] == ["workflow:intake was removed"]
    assert diff_releases(no_flow, base)["min_bump"] == "minor"


@pytest.mark.asyncio
async def test_a_release_cannot_call_a_breaking_change_a_patch(db_session):
    from packages.core.blueprints.registry import next_release_version

    first = _with_flow(_payload("steps-v1", "v1"), {"key": "brief", "required": True})
    row = _listing("steps-v1", first)
    db_session.add(row)
    await db_session.flush()
    await record_release(db_session, row.id, first, "1.0.0", listing=row)

    breaking = _payload("steps-v1", "v1")  # the intake Flow is gone
    with pytest.raises(BlueprintVersionConflictError, match="at least 2.0.0"):
        await record_release(db_session, row.id, breaking, "1.0.1", listing=row)
    version = await next_release_version(
        db_session, row.id, breaking, current_version=row.content_version,
        stored_fingerprint=row.content_fingerprint,
    )
    assert version == "2.0.0"
    release = await record_release(db_session, row.id, breaking, version, listing=row)
    assert (release.min_bump, await _latest(db_session, row.id)) == ("major", "2.0.0")
    # Unchanged content keeps its number.
    assert await next_release_version(
        db_session, row.id, copy.deepcopy(breaking), current_version="2.0.0",
        stored_fingerprint=row.content_fingerprint,
    ) == "2.0.0"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_client_database_reset")
async def test_seeding_records_every_platform_blueprint_as_its_latest_release(db_session):
    from packages.core.blueprints.seed import seed_platform_blueprints

    published = await seed_platform_blueprints(db_session)
    again = await seed_platform_blueprints(db_session)
    assert again == published

    rows = (await db_session.execute(
        select(WorkspaceBlueprint).where(WorkspaceBlueprint.entity_id.is_(None))
    )).scalars().all()
    for row in rows:
        versions = await _versions(db_session, row.id)
        assert [v.version for v in versions] == [row.content_version], row.slug
        assert versions[0].content_fingerprint == row.content_fingerprint
        assert await _latest(db_session, row.id) == row.content_version


def _delivery(payload: dict, version: str | None) -> ClientWorkspaceDelivery:
    return ClientWorkspaceDelivery(
        id="01TESTDELIVERY000000000001", entity_id=ENTITY, client_id="c", contact_id="k",
        source_workspace_id="w", created_by="u", recipient_email="r@example.test",
        token_hash="t" * 64, expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        status="delivered", payload=payload,
        content_release={"version": version} if version else {},
    )


@pytest.mark.asyncio
async def test_backfill_gives_each_source_its_current_release_once(db_session):
    platform = _listing("retired-v1", _payload("retired-v1", "p"), status="archived", version="1.0.4")
    listed = _listing(
        "studio", _payload("studio", "m"), entity_id=ENTITY, id="01TESTLISTING0000000000001",
        author_handle="Jane Doe", version="1.2.0",
    )
    draft = _listing("draft", _payload("draft", "d"), entity_id=ENTITY, id="01TESTLISTING0000000000002", status="draft")
    db_session.add_all([platform, listed, draft, _delivery(_payload("delivered", "x"), None)])
    await db_session.flush()

    first = await backfill_registry(db_session)
    second = await backfill_registry(db_session)
    # Other suites may leave committed rows; only this test's sources count.
    assert first["versions"] >= 3
    assert (second["packages"], second["versions"]) == (0, 0)

    expected = {
        platform.id: ("@manor/retired-v1", ["1.0.4"]),
        listed.id: ("@jane-doe/studio", ["1.2.0"]),
        draft.id: ("@entity-01testen/draft", []),
        "client-delivery:01TESTDELIVERY000000000001": (
            "@delivery-01testen/01testdelivery000000000001", ["1.0.0"],
        ),
    }
    for ref, (name, versions) in expected.items():
        assert (await db_session.get(BlueprintPackage, ref)).name == name
        assert [(v.version, v.origin) for v in await _versions(db_session, ref)] == [
            (version, "backfill") for version in versions
        ]
        assert await _latest(db_session, ref) == (versions[0] if versions else None)


def _install(record: dict) -> Workspace:
    return Workspace(entity_id=ENTITY, name="Installed", settings={"_blueprint": record})


@pytest.mark.asyncio
async def test_adoption_names_a_release_only_when_content_proves_it(db_session):
    installed, current = _payload("notes-v1", "old"), _payload("notes-v1", "new")
    row = _listing("notes-v1", current, version="1.0.3")
    db_session.add(row)
    await db_session.flush()
    await record_release(db_session, row.id, installed, "1.0.2", spec=package_spec_for_listing(row))
    await record_release(db_session, row.id, current, "1.0.3", listing=row)

    def record(fingerprint, **extra):
        return {
            "blueprint_id": row.id, "source": "marketplace", "blueprint_version": "1.0.2",
            "content_fingerprint": fingerprint, **extra,
        }

    exact = _install(record(blueprint_content_fingerprint(installed)))
    unknown = _install(record("f" * 32))
    with_baseline = _install(record("e" * 32))
    raw = _install({"source": "raw_payload", "content_fingerprint": blueprint_content_fingerprint(current)})
    db_session.add_all([exact, unknown, with_baseline, raw])
    await db_session.flush()
    db_session.add(WorkspaceBlueprintUpgradeState(
        workspace_id=with_baseline.id, entity_id=ENTITY, baseline={"components": {"x": {}}},
    ))
    await db_session.flush()

    outcomes = {ws.id: await adopt_workspace(db_session, ws.id) for ws in (exact, unknown, with_baseline, raw)}
    assert outcomes == {
        exact.id: "adopted", unknown.id: "adopted_unverified",
        with_baseline.id: "adopted", raw.id: "not_from_blueprint",
    }

    def package(ws):
        return ws.settings["_blueprint"].get("package")

    assert {k: package(exact)[k] for k in ("ref", "name", "version", "base")} == {
        "ref": row.id, "name": "@manor/notes-v1", "version": "1.0.2", "base": "exact",
    }
    # Unknown content is never passed off as the release it claims.
    assert (package(unknown)["version"], package(unknown)["base"]) == ("1.0.3", "unverified")
    assert package(with_baseline)["version"] == "1.0.2-legacy.eeeeeeee"
    hidden = (await db_session.execute(select(BlueprintVersion).where(
        BlueprintVersion.version == "1.0.2-legacy.eeeeeeee",
    ))).scalar_one()
    assert (hidden.visibility, hidden.origin, hidden.payload) == ("hidden", "baseline", None)
    assert package(raw) is None
    # Adoption only adds the record; rerunning it changes nothing.
    assert await adopt_workspace(db_session, exact.id) == "already_adopted"
    assert await _latest(db_session, row.id) == "1.0.3"


def _studio(context: str, work: str, writer: str, editor: str, tone: str | None) -> dict:
    operating_model = {"context": context, "primary_work": work, "heartbeat_enabled": False}
    if tone is not None:
        operating_model["tone"] = tone
    return {
        "manifest": {"blueprint_version": "1.1", "title": "Synth studio", "slug": "synth-studio", "kind": "custom"},
        "contract": {"variables": [], "channels": [], "sessions": [],
                     "requires": {"manor_min_version": None, "tools": [], "mcp_servers": [], "skills": [], "agents": []}},
        "embedded": {"skills": [{"slug": "writer", "system_prompt": writer},
                                {"slug": "editor", "system_prompt": editor}],
                     "agents": [], "knowledge_packs": []},
        "recipe": {"operating_model": operating_model,
                   "strategist": None, "prompts": [], "subscriptions": [], "scheduled_jobs": [], "workflows": [],
                   "goals": [], "task_categories": [], "custom_fields": [], "sla_policies": [], "escalation_rules": []},
        "policy": {"governance": {}, "post_install_checks": [], "expected_baseline": None},
    }


@pytest.mark.asyncio
async def test_synthesized_baseline_updates_untouched_parts_and_asks_about_local_edits(db_session):
    from packages.core.blueprints import reconfigure
    from packages.core.blueprints.installer import InstallMode, install_blueprint
    from packages.core.blueprints.registry import PackageSpec
    from packages.core.blueprints.registry_backfill import (
        synthesizable_workspace_ids,
        synthesize_workspace_baseline,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.skill import Skill

    ref = "builtin:synth-studio"
    released = _studio("Old context", "Old work", "Old writer", "Old editor", "Plain")
    result = await install_blueprint(db_session, entity_id=generate_ulid(), payload=copy.deepcopy(released),
                                     mode=InstallMode.LIVE, blueprint_id=ref)
    workspace = await db_session.get(Workspace, result.workspace_id)
    await record_release(db_session, ref, released, "1.0.0",
                         spec=PackageSpec(ref=ref, name="@manor/synth-studio", kind="platform"))
    # An install from before v3 baselines existed, later edited locally.
    await db_session.delete(await db_session.get(WorkspaceBlueprintUpgradeState, workspace.id))
    workspace.operating_context = "My own context"
    workspace.operating_model = {**workspace.operating_model, "tone": "Warm"}
    editor = (await db_session.execute(select(Skill).where(
        Skill.workspace_id == workspace.id, Skill.slug == "editor",
    ))).scalar_one()
    editor.system_prompt = "My own editor"
    await db_session.flush()
    assert await adopt_workspace(db_session, workspace.id) == "adopted"

    upstream = _studio("New context", "New work", "New writer", "New editor", None)

    async def actions():
        reviewed = await reconfigure.plan(db_session, workspace=workspace, payload=upstream, source_payload=upstream)
        conflicts = {item["key"] for item in reviewed["items"] if item.get("conflict")}
        return reviewed["baseline_known"], {item["key"]: item["action"] for item in reviewed["items"]}, conflicts

    known, before, _conflicts = await actions()
    assert known is False
    # Without a baseline every upstream change reads as a local edit.
    assert before["skill:writer"] == "keep_yours"

    assert workspace.id in await synthesizable_workspace_ids(db_session)
    assert await synthesize_workspace_baseline(db_session, workspace.id) == "synthesized"
    known, after, conflicts = await actions()
    assert known is True
    assert after["skill:writer"] == "update"
    assert after["workspace_configuration:primary_work"] == "update"
    # Local edits are not recorded as installed content, so they still ask,
    # even where the release drops the item: it is not removed unseen.
    assert after["skill:editor"] == "keep_yours"
    assert {"workspace_configuration:operating_context", "workspace_configuration:operating_model/tone"} <= conflicts
    assert await synthesize_workspace_baseline(db_session, workspace.id) == "has_baseline"
    assert workspace.id not in await synthesizable_workspace_ids(db_session)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_git_history_recovers_only_exactly_matching_platform_releases(db_session, tmp_path):
    slug = "history-studio-v1"
    old, current = _payload(slug, "first draft"), _payload(slug, "shipped")
    config = tmp_path / "packages/core/blueprints/configs/creative" / f"{slug}.json"
    config.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q")
    for content in (old, current):
        config.write_text(json.dumps(content), encoding="utf-8")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "-c", "user.email=t@example.test", "-c", "user.name=t", "commit", "-qm", "config")

    row = _listing(slug, current, version="1.0.1")
    db_session.add(row)
    db_session.add_all([
        _install({"blueprint_id": row.id, "source": "marketplace", "blueprint_version": "1.0.0",
                  "content_fingerprint": blueprint_content_fingerprint(old)}),
        _install({"blueprint_id": row.id, "source": "marketplace", "blueprint_version": "1.0.0",
                  "content_fingerprint": "d" * 32}),
    ])
    await db_session.flush()
    await backfill_registry(db_session)

    assert await recover_git_history(db_session, tmp_path) == {"wanted": 2, "recovered": 1}
    assert await recover_git_history(db_session, tmp_path) == {"wanted": 1, "recovered": 0}
    recovered = [v for v in await _versions(db_session, row.id) if v.origin == "git_history"]
    assert [(v.version, v.visibility) for v in recovered] == [
        (f"1.0.0-git.{blueprint_content_fingerprint(old)[:8]}", "hidden"),
    ]
    assert recovered[0].payload == old
    assert await _latest(db_session, row.id) == "1.0.1"
