"""A Blueprint kept in git changes only where its Workspace changed.

Teams develop a Blueprint in Workspaces installed from it and export each
Workspace over the file in git. That works only when exporting an unchanged
Workspace reproduces the file: anything else shows up as an edit nobody made,
and every merge conflicts on it.
"""

import json
from pathlib import Path

import pytest

from packages.core.blueprints.export_alignment import align_export_to_release
from packages.core.blueprints.exporter import ExportContext, export_workspace
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.registry import PackageSpec, installed_release_payload, record_release
from packages.core.models.base import generate_ulid
from packages.core.services.builtin_skill_loader import seed_builtin_skills

BUILTIN_BLUEPRINTS = sorted(Path("packages/core/blueprints/configs").rglob("*.json"))


def _first_differences(a, b, path="", out=None, limit=5):
    out = [] if out is None else out
    if len(out) >= limit or a == b:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b), key=str):
            _first_differences(a.get(key), b.get(key), f"{path}.{key}", out, limit)
    elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for index, (x, y) in enumerate(zip(a, b)):
            _first_differences(x, y, f"{path}[{index}]", out, limit)
    else:
        out.append(path or "(root)")
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("config", BUILTIN_BLUEPRINTS, ids=lambda path: path.stem)
async def test_reexporting_an_unchanged_install_reproduces_the_export(db_session, isolated_test_redis, config):
    db = db_session
    await seed_builtin_skills(db)

    async def install_and_export(payload, *, release=None):
        result = await install_blueprint(
            db, payload=json.loads(json.dumps(payload)), entity_id=generate_ulid(),
            mode=InstallMode.LIVE, create_missing_agents=True,
        )
        await db.flush()
        exported = await export_workspace(
            db, result.workspace_id, title=payload["manifest"]["title"],
            context=ExportContext(include_embedded_agents=True, include_embedded_skills=True),
        )
        return align_export_to_release(
            json.loads(json.dumps(exported, default=str)), release if release is not None else payload,
        )

    # The first export normalises a hand-written file; from then on an
    # unchanged Workspace must export exactly what it was installed from.
    first = await install_and_export(json.loads(config.read_text()))
    second_input = json.loads(json.dumps(first))
    # Independent schedules have no execution order; their insertion order must not change the export.
    second_input["recipe"]["scheduled_jobs"].reverse()
    second = await install_and_export(second_input, release=first)
    assert second == first, _first_differences(first, second)


def test_an_export_keeps_the_release_order_listing_copy_and_unbound_channels():
    release = {
        "embedded": {"agents": [{"slug": "publisher", "mcp_bindings": [
            {"server_slug": "chrome", "allowed_tools": ["open"]},
            {"server_slug": "twitter_x", "allowed_tools": ["post"]},
        ]}]},
        "manifest": {"title": "Studio", "summary": "Make songs", "tags": ["music"], "author": {"handle": "jr"}},
        "contract": {"channels": [{"channel_type": "internal_chat", "linked_service_key": "ops"},
                                  {"channel_type": "slack", "linked_service_key": "gone"}]},
        "recipe": {"workflows": [{"slug": "b", "name": "B"}, {"slug": "a", "name": "A"}],
                   "subscriptions": [{"service_key": "ops"}]},
    }
    exported = {
        "embedded": {"agents": [{"slug": "publisher", "mcp_bindings": [
            {"server_slug": "twitter_x", "allowed_tools": ["post", "read"]},
            {"server_slug": "chrome", "allowed_tools": ["open"]},
            {"server_slug": "gmail", "allowed_tools": None},
        ]}]},
        "manifest": {"title": "Studio", "summary": None, "tags": [], "author": {"handle": None}},
        "contract": {"channels": []},
        "recipe": {"workflows": [{"slug": "a", "name": "A2"}, {"slug": "c", "name": "C"}, {"slug": "b", "name": "B"}],
                   "subscriptions": [{"service_key": "ops"}]},
    }
    aligned = align_export_to_release(exported, release)
    # Order follows the release; new items follow; edits stay.
    assert [w["slug"] for w in aligned["recipe"]["workflows"]] == ["b", "a", "c"]
    assert aligned["recipe"]["workflows"][1]["name"] == "A2"
    assert aligned["manifest"] == {"title": "Studio", "summary": "Make songs", "tags": ["music"], "author": {"handle": "jr"}}
    # A required channel the copy never connected stays; one whose service is gone does not.
    assert aligned["contract"]["channels"] == [{"channel_type": "internal_chat", "linked_service_key": "ops"}]
    assert exported["recipe"]["workflows"][0]["slug"] == "a"
    bindings = aligned["embedded"]["agents"][0]["mcp_bindings"]
    assert [binding["server_slug"] for binding in bindings] == ["chrome", "twitter_x", "gmail"]
    assert bindings[1]["allowed_tools"] == ["post", "read"]  # Scope edits are preserved.
    assert bindings[2]["allowed_tools"] is None  # New bindings retain their scope semantics.
    assert exported["embedded"]["agents"][0]["mcp_bindings"][0]["server_slug"] == "twitter_x"


@pytest.mark.asyncio
async def test_the_release_an_install_runs_is_what_its_export_aligns_to(db_session):
    payload = json.loads(BUILTIN_BLUEPRINTS[0].read_text())
    ref = f"test-{generate_ulid().lower()}"
    await record_release(db_session, ref, payload, "1.0.0", spec=PackageSpec(ref=ref, name=ref, kind="listing"))
    record = {"package": {"ref": ref, "version": "1.0.0", "base": "exact"}}
    assert await installed_release_payload(db_session, record) == payload
    assert await installed_release_payload(db_session, {"package": {**record["package"], "base": "unverified"}}) is None
