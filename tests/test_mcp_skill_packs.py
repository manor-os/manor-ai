"""Per-MCP built-in guidance packs: structural + tool-grounding invariants.

Each ``packages/core/ai/skills/mcp_<server_key>/`` directory is a built-in
``runtime_guidance`` skill that teaches the model how to operate one MCP. These
tests keep the packs honest:

- slug / type conventions are uniform (``mcp_<server_key>``, runtime_guidance);
- every tool the pack declares actually exists on that MCP's tool surface, so a
  pack can never advertise a tool the MCP doesn't expose (catches drift when a
  tool is renamed/removed).
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

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
        if d.is_dir() and (d / "SKILL.md").exists() and (d / "config.json").exists()
    )


# Vendor-hosted remote MCPs (transport=http, OAuth): their tool surface is
# served by the vendor via tools/list at agent runtime, not by anything in
# this repo. For stripe specifically a legacy in-process module is retained on
# disk for import compatibility but is "no longer dispatched" (see
# mcp_builtin.py), so it is NOT authoritative — don't ground packs against it.
# These packs are validated structurally; their tool names track the vendor's
# published surface (see the SKILL.md note in each).
_REMOTE_VENDOR_SERVERS = frozenset({"stripe", "paypal"})


def _real_tool_names(server_key: str) -> set[str] | None:
    """Tool names this MCP really exposes, unioned across both authoritative
    sources, or None if the surface isn't locally knowable (vendor-hosted
    remote MCPs, or a server neither source knows):

    - the in-process module's ``list_tools()`` (when a module exists), and
    - the ``_SERVER_TOOL_SCHEMAS`` deferred-tool registry in mcp_builtin
      (covers no-module builtins like discord).
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


def test_mcp_skill_packs_exist() -> None:
    assert MCP_PACK_DIRS, "expected at least one packages/core/ai/skills/mcp_* pack"


@pytest.mark.parametrize("pack_dir", MCP_PACK_DIRS, ids=lambda d: d.name)
def test_mcp_pack_structure_and_tools_are_grounded(pack_dir: Path) -> None:
    slug = pack_dir.name
    server_key = slug[len("mcp_") :]

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
    assert declared, f"{slug}: must declare at least one mcp__{server_key}__* tool"

    # Every declared tool must be namespaced to this MCP.
    pattern = re.compile(rf"^mcp__{re.escape(server_key)}__(.+)$")
    parsed = []
    for full in declared:
        m = pattern.match(full)
        assert m, f"{slug}: tool {full!r} must be namespaced mcp__{server_key}__*"
        parsed.append(m.group(1))

    # If the MCP has a local module, every declared tool must really exist.
    real = _real_tool_names(server_key)
    if real is not None:
        missing = [name for name in parsed if name not in real]
        assert not missing, f"{slug}: declares tools not on the {server_key} MCP: {missing}"
