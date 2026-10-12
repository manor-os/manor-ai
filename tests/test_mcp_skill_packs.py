"""Per-MCP built-in guidance packs: structural + tool-grounding invariants.

Each static Integration resolves to one complete built-in ``runtime_guidance``
Skill. Provider-specific packs live at
``packages/core/ai/skills/mcp_<server_key>/``. These tests keep the catalog,
route registry, manifests, and real tool surfaces in lockstep:

- slug / type conventions are uniform (``mcp_<server_key>``, runtime_guidance);
- every tool the pack declares actually exists on that MCP's tool surface, so a
  pack can never advertise a tool the MCP doesn't expose (catches drift when a
  tool is renamed/removed).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services.builtin_skill_loader import (
    _parse_frontmatter,
    _read_skill_config,
)

_SKILLS_ROOT = Path("packages/core/ai/skills")


def _mcp_pack_dirs() -> list[Path]:
    return sorted(
        d
        for d in _SKILLS_ROOT.glob("mcp_*")
        if d.is_dir()
    )


# Vendor-hosted remote MCPs (transport=http, OAuth): their tool surface is
# served by the vendor via tools/list at agent runtime, not by anything in
# this repo. For stripe specifically a legacy in-process module is retained on
# disk for import compatibility but is "no longer dispatched" (see
# mcp_builtin.py), so it is NOT authoritative — don't ground packs against it.
# These packs are validated structurally; their tool names track the vendor's
# published surface (see the SKILL.md note in each).
_REMOTE_VENDOR_SERVERS = frozenset({"stripe", "paypal", "robinhood"})
_LIVE_DISCOVERY_ONLY_SERVERS = frozenset({"robinhood"})

# These Google packs are the exact user-facing capability contract submitted
# for OAuth review. Unlike intentionally curated MCP packs, they must expose
# every operation implemented by their local MCP module; otherwise the Agent
# advertises a reviewed capability but cannot see its schema at runtime.
_FULL_LOCAL_SURFACE_SERVERS = frozenset({
    "gmail",
    "google_calendar",
    "google_drive",
    "youtube",
})


def _real_tool_names(server_key: str) -> set[str] | None:
    """Tool names this MCP really exposes, unioned across both authoritative
    sources, or None if the surface isn't locally knowable (vendor-hosted
    remote MCPs, or a server neither source knows):

    - the in-process module's ``list_tools()`` (when a module exists), and
    - the ``_SERVER_TOOL_SCHEMAS`` deferred-tool registry in mcp_builtin.
    """
    if server_key in _REMOTE_VENDOR_SERVERS:
        return None

    names: set[str] = set()
    found = False

    try:
        from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS

        if server_key in _SERVER_TOOL_SCHEMAS:
            found = True
            names |= {t["name"] for t in _SERVER_TOOL_SCHEMAS[server_key]}
    except Exception:
        pass

    try:
        mod = importlib.import_module(f"packages.core.ai.mcp.{server_key}")
        found = True
        names |= {t["name"] for t in mod.list_tools()}
    except ModuleNotFoundError:
        pass

    return names if found else None


MCP_PACK_DIRS = _mcp_pack_dirs()


def _integration_child_skill_slugs() -> tuple[str, ...]:
    from packages.core.ai.runtime.integration_skill_registry import (
        INTEGRATION_SKILL_ROUTES,
    )

    return tuple(sorted({
        route.child_skill for route in INTEGRATION_SKILL_ROUTES.values()
    }))


INTEGRATION_CHILD_SKILL_SLUGS = _integration_child_skill_slugs()


def test_mcp_skill_packs_exist() -> None:
    assert MCP_PACK_DIRS, "expected at least one packages/core/ai/skills/mcp_* pack"
    assert (_SKILLS_ROOT / "mcp_webhook").is_dir(), "Webhook MCP needs a built-in guidance pack"


def test_every_integration_routes_to_one_complete_builtin_skill() -> None:
    from packages.core.ai.runtime.integration_skill_registry import (
        BROWSER_ONLY_PLATFORM_ROUTES,
        INTEGRATION_SKILL_ROUTES,
        integration_provider_keys_for_skill,
    )
    from packages.core.services.mcp_seed import _MCP_CATALOG

    catalog_keys = {row[0] for row in _MCP_CATALOG}
    assert set(INTEGRATION_SKILL_ROUTES) == catalog_keys

    problems: list[str] = []
    for provider_key, route in INTEGRATION_SKILL_ROUTES.items():
        if route.child_skill != "chrome" and route.child_skill != f"mcp_{provider_key}":
            problems.append(
                f"{provider_key}: child Skill must be mcp_{provider_key} or chrome"
            )
        skill_dir = _SKILLS_ROOT / route.child_skill
        for filename in ("SKILL.md", "config.json"):
            if not (skill_dir / filename).is_file():
                problems.append(
                    f"{provider_key}: {route.child_skill}/{filename} is missing"
                )
        if provider_key not in integration_provider_keys_for_skill(
            route.child_skill
        ):
            problems.append(
                f"{provider_key}: child Skill has no derived companion ownership"
            )
    for route in BROWSER_ONLY_PLATFORM_ROUTES:
        if route.child_skill != "chrome":
            problems.append(
                f"{route.provider_key}: browser-only route must use chrome"
            )
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "skill_slug",
    INTEGRATION_CHILD_SKILL_SLUGS,
)
def test_every_integration_child_skill_materializes_its_declared_tool_scope(
    skill_slug: str,
) -> None:
    """Exercise the parent-scope -> child-Skill projection for every provider."""

    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChatSurface,
        RuntimeResolver,
        runtime_prepare_prompt_skill_tool_surface,
    )
    from packages.core.ai.runtime.integration_skill_registry import (
        integration_provider_keys_for_skill,
    )

    config = _read_skill_config(_SKILLS_ROOT / skill_slug)
    declared = tuple(str(name) for name in (config.get("tools") or ()))
    skill = SimpleNamespace(
        entity_id=None,
        slug=skill_slug,
        name=skill_slug,
        config={"source": "builtin", **config},
        tools=list(declared),
    )
    parent_envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            entity_id="entity-integration-scope",
            user_id="owner-integration-scope",
        ),
        allowed_tool_names={"invoke_skill", "search_tools"},
    )

    surface = runtime_prepare_prompt_skill_tool_surface(
        skill,
        allowed_tool_names={"invoke_skill", "search_tools"},
        runtime_envelope=parent_envelope,
        get_schemas_for_names=lambda names: [
            {"type": "function", "function": {"name": name}}
            for name in names
        ],
        get_registered_tool_names=lambda: (),
    )

    assert surface.skill_tool_names == declared
    assert set(declared).issubset(surface.allowed_tool_names or ())
    assert {
        schema["function"]["name"] for schema in surface.tools
    } == set(declared)

    provider_keys = set(integration_provider_keys_for_skill(skill_slug))
    provider_keys.update(config.get("discoverable_provider_keys") or ())
    materialized_mcp_providers = {
        name.split("__", 2)[1]
        for name in (surface.allowed_tool_names or ())
        if name.startswith("mcp__")
    }
    assert materialized_mcp_providers.issubset(provider_keys)


@pytest.mark.parametrize(
    "skill_slug",
    INTEGRATION_CHILD_SKILL_SLUGS,
)
def test_every_integration_child_skill_respects_exact_provider_action_scope(
    skill_slug: str,
) -> None:
    """An Integration Skill may project an action, never its provider siblings."""

    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChatSurface,
        RuntimeMCPProviderToolScopeFactory,
        RuntimeResolver,
        runtime_prepare_prompt_skill_tool_surface,
    )

    config = _read_skill_config(_SKILLS_ROOT / skill_slug)
    declared = tuple(str(name) for name in (config.get("tools") or ()))
    declared_by_provider: dict[str, list[str]] = {}
    for name in declared:
        if not name.startswith("mcp__"):
            continue
        declared_by_provider.setdefault(name.split("__", 2)[1], []).append(name)
    skill = SimpleNamespace(
        entity_id=None,
        slug=skill_slug,
        name=skill_slug,
        config={"source": "builtin", **config},
        tools=list(declared),
    )

    for provider, provider_tools in declared_by_provider.items():
        allowed_tool = provider_tools[0]
        allowed_action = allowed_tool.split("__", 2)[2]
        parent_envelope = RuntimeResolver().resolve_trace_envelope(
            AIRuntimeRequest(
                surface=ChatSurface.WORKSPACE_CHAT,
                entity_id="entity-integration-scope",
                user_id="member-integration-scope",
                agent_id="agent-integration-scope",
                workspace_id="workspace-integration-scope",
            ),
            allowed_tool_names={"invoke_skill", "search_tools"},
            mcp_provider_scopes=RuntimeMCPProviderToolScopeFactory.create({
                provider: {allowed_action},
            }),
        )

        surface = runtime_prepare_prompt_skill_tool_surface(
            skill,
            allowed_tool_names={"invoke_skill", "search_tools"},
            runtime_envelope=parent_envelope,
            get_schemas_for_names=lambda names: [
                {"type": "function", "function": {"name": name}}
                for name in names
            ],
            get_registered_tool_names=lambda: (),
        )

        projected_mcp_tools = {
            name for name in surface.skill_tool_names if name.startswith("mcp__")
        }
        assert projected_mcp_tools == {allowed_tool}


@pytest.mark.asyncio
async def test_all_integration_child_skills_seed_and_invoke_end_to_end(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB seed -> invoke_skill -> child Runtime surface for all Integrations."""

    from packages.core.ai.runtime import (
        AIRuntimeRequest,
        ChatSurface,
        RuntimeResolver,
    )
    from packages.core.ai.runtime.integration_skill_registry import (
        integration_provider_keys_for_skill,
    )
    from packages.core.services.builtin_skill_loader import seed_builtin_skills
    from packages.core.services.skill_service import invoke_skill

    seeded = {
        skill.slug: skill for skill in await seed_builtin_skills(db_session)
    }
    missing = sorted(set(INTEGRATION_CHILD_SKILL_SLUGS) - set(seeded))
    assert not missing, f"Integration child Skills were not seeded: {missing}"
    assert all(seeded[slug].status == "active" for slug in INTEGRATION_CHILD_SKILL_SLUGS)

    observed: dict[str, dict] = {}
    active_skill_slug = ""

    async def fake_skill_agent_loop(**kwargs):
        observed[active_skill_slug] = kwargs
        return SimpleNamespace(
            content="integration skill invoked",
            usage={},
            tool_calls_made=[],
            rounds=1,
            stop_reason="completed",
            control={},
            error=None,
        )

    monkeypatch.setattr(
        "packages.core.ai.runtime.runtime_execute_skill_agent_loop",
        fake_skill_agent_loop,
    )
    parent_envelope = RuntimeResolver().resolve_trace_envelope(
        AIRuntimeRequest(
            surface=ChatSurface.GLOBAL_OWNER_CHAT,
            entity_id="entity-integration-e2e",
            user_id="owner-integration-e2e",
            agent_id="manor-agent-integration-e2e",
        ),
        allowed_tool_names={"invoke_skill", "search_tools"},
        mcp_scope_unrestricted=True,
    )

    for skill_slug in INTEGRATION_CHILD_SKILL_SLUGS:
        active_skill_slug = skill_slug
        result = await invoke_skill(
            db_session,
            skill_slug,
            "entity-integration-e2e",
            "Exercise the Integration Skill runtime surface.",
            agent_id="manor-agent-integration-e2e",
            enforce_agent_access=False,
            user_id="owner-integration-e2e",
            allowed_tool_names={"invoke_skill", "search_tools"},
            runtime_envelope=parent_envelope,
        )
        assert result["content"] == "integration skill invoked", skill_slug

        declared = {
            str(name) for name in (seeded[skill_slug].tools or ())
        }
        child_allowed = set(observed[skill_slug]["allowed_tool_names"] or ())
        assert declared.issubset(child_allowed), skill_slug
        child_envelope = observed[skill_slug]["runtime_envelope"]
        if declared:
            expected_providers = set(
                integration_provider_keys_for_skill(skill_slug)
            )
            projected_providers = set(
                child_envelope.metadata["prompt_skill_tool_discovery"][
                    "provider_keys"
                ]
            )
            assert expected_providers.issubset(projected_providers), skill_slug


@pytest.mark.parametrize("pack_dir", MCP_PACK_DIRS, ids=lambda d: d.name)
def test_mcp_pack_structure_and_tools_are_grounded(pack_dir: Path) -> None:
    slug = pack_dir.name
    server_key = slug[len("mcp_") :]

    assert (pack_dir / "SKILL.md").is_file(), f"{slug}: missing SKILL.md"
    assert (pack_dir / "config.json").is_file(), f"{slug}: missing config.json"

    fm, body = _parse_frontmatter((pack_dir / "SKILL.md").read_text(encoding="utf-8"))
    cfg = _read_skill_config(pack_dir)

    # Uniform conventions.
    assert cfg.get("type") == "runtime_guidance", f"{slug}: config type must be runtime_guidance"
    assert cfg.get("id") == slug, f"{slug}: config id must equal the dir/slug"
    assert cfg.get("name") == slug, f"{slug}: config name must equal the dir/slug"
    assert fm.get("name") == slug, f"{slug}: SKILL.md frontmatter name must equal the slug"
    assert fm.get("description"), f"{slug}: SKILL.md needs a description"
    assert body.strip(), f"{slug}: SKILL.md needs a body"

    declared = list(cfg.get("tools") or [])
    declared_mcp_tools = [
        name for name in declared if str(name).startswith("mcp__")
    ]
    control_tools = sorted(set(declared) - set(declared_mcp_tools))
    assert set(control_tools).issubset({"search_tools"}), (
        f"{slug}: unsupported non-MCP control tools: {control_tools}"
    )
    if not declared_mcp_tools:
        from packages.core.ai.runtime.integration_skill_registry import (
            INTEGRATION_SKILL_ROUTES,
        )

        route = INTEGRATION_SKILL_ROUTES[server_key]
        assert (
            route.status == "catalog_only"
            or server_key in _LIVE_DISCOVERY_ONLY_SERVERS
        ), f"{slug}: executable MCP packs must declare their concrete tools"
        if server_key in _LIVE_DISCOVERY_ONLY_SERVERS:
            assert "search_tools" in declared
            assert "search_tools" in body

    # Every declared tool must be namespaced to this MCP.
    pattern = re.compile(rf"^mcp__{re.escape(server_key)}__(.+)$")
    parsed = []
    for full in declared_mcp_tools:
        m = pattern.match(full)
        assert m, f"{slug}: tool {full!r} must be namespaced mcp__{server_key}__*"
        parsed.append(m.group(1))

    # If the MCP has a local module, every declared tool must really exist.
    real = _real_tool_names(server_key)
    if real is not None:
        missing = [name for name in parsed if name not in real]
        assert not missing, f"{slug}: declares tools not on the {server_key} MCP: {missing}"
        if server_key in _FULL_LOCAL_SURFACE_SERVERS:
            hidden = sorted(real - set(parsed))
            assert not hidden, (
                f"{slug}: local MCP tools hidden from the reviewed runtime skill: {hidden}"
            )
