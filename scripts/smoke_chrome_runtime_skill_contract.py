#!/usr/bin/env python3
"""No-dependency contract for Codex-style Chrome runtime skill execution."""
from __future__ import annotations

import json
import pathlib
import sys
import asyncio


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


CHROME_MESSAGE = (
    "请使用 Chrome 打开 https://www.zhihu.com/，搜索 AI 视频剪辑工具，"
    "点击前2条内容并汇总帖子。"
)
NON_CHROME_MESSAGE = "帮我整理一下今天的任务。"


def _assert_contains(text: str, needle: str) -> None:
    if needle not in text:
        raise AssertionError(f"missing required Chrome skill phrase: {needle!r}")


def _assert_chrome_reference_bundle(skill_path: pathlib.Path) -> dict[str, int]:
    references = {
        "runtime": skill_path.parent / "references" / "runtime-contract.md",
        "api": skill_path.parent / "references" / "api-reference.md",
        "capabilities": skill_path.parent / "references" / "capabilities.md",
        "interactions": skill_path.parent / "references" / "interactions.md",
        "workflows": skill_path.parent / "references" / "workflows.md",
        "confirmations": skill_path.parent / "references" / "confirmations.md",
        "file_uploads": skill_path.parent / "references" / "file-uploads.md",
        "screenshots": skill_path.parent / "references" / "screenshots.md",
        "troubleshooting": skill_path.parent / "references" / "troubleshooting.md",
    }
    sizes: dict[str, int] = {}
    for label, path in references.items():
        if not path.exists():
            raise AssertionError(f"Chrome reference document missing: {path}")
        text = path.read_text(encoding="utf-8")
        if len(text) < 200:
            raise AssertionError(f"Chrome reference document too small: {path}")
        sizes[label] = len(text)

    required = {
        "runtime": ["open_or_reuse", "reused_noop", "claimToken", "finalize_tabs"],
        "api": ["Common Returns", "mcp__chrome__open_or_reuse", "mcp__chrome__read_page"],
        "capabilities": ["browser-group", "native-tab-group", "action-approval"],
        "interactions": ["Required Interaction Recipe", "Use the newest read_page", "Do not invent refs", "fallbackReason"],
        "workflows": ["LinkedIn Draft Workflow", "YouTube Upload Workflow", "Search And Result Workflows"],
        "confirmations": ["action-time", "approval_required", "LinkedIn", "YouTube"],
        "file_uploads": ["upload_candidates", "upload_targets", "shadow DOM"],
        "screenshots": ["mcp__chrome__screenshot", "visual confirmation", "read_page first"],
        "troubleshooting": ["extension reload", "native host", "stale", "mcp__chrome__status"],
    }
    for label, needles in required.items():
        text = references[label].read_text(encoding="utf-8")
        for needle in needles:
            if needle not in text:
                raise AssertionError(f"Chrome reference {label} missing phrase: {needle!r}")
    return sizes


async def _assert_no_inline_runtime_context_block() -> None:
    from packages.core.ai.runtime.context_blocks import (
        render_context_blocks,
        resolve_runtime_context_blocks,
    )
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal, RuntimePrincipalKind
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.surfaces import ChatSurface

    request = AIRuntimeRequest(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        entity_id="ent-smoke",
        user_id="user-smoke",
        conversation_id="conv-smoke",
        input_preview=CHROME_MESSAGE,
    )
    envelope = RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.OWNER,
            entity_id="ent-smoke",
            actor_user_id="user-smoke",
            execution_user_id="user-smoke",
        ),
        profile=RuntimeProfile.OWNER_COPILOT,
        entity_id="ent-smoke",
        user_id="user-smoke",
        conversation_id="conv-smoke",
    )
    blocks = await resolve_runtime_context_blocks(None, request, envelope)
    chrome_blocks = [block for block in blocks if block.kind == "chrome_runtime_skill"]
    if chrome_blocks:
        raise AssertionError(f"Chrome skill must not be injected as inline context: {chrome_blocks}")
    rendered = render_context_blocks(blocks)
    if "--- Chrome Runtime Skill ---" in rendered:
        raise AssertionError("Chrome SKILL.md should load only through invoke_skill, not context blocks")


def _assert_chrome_skill_tool_surface(config: dict) -> None:
    from packages.core.ai.runtime.envelope import RuntimeEnvelope
    from packages.core.ai.runtime.principals import RuntimePrincipal, RuntimePrincipalKind
    from packages.core.ai.runtime.profiles import RuntimeProfile
    from packages.core.ai.runtime.skills import runtime_prepare_prompt_skill_tool_surface
    from packages.core.ai.runtime.surfaces import ChatSurface

    class FakeSkill:
        tools = tuple(config["tools"])

    envelope = RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        principal=RuntimePrincipal(
            kind=RuntimePrincipalKind.OWNER,
            entity_id="ent-smoke",
            actor_user_id="user-smoke",
            execution_user_id="user-smoke",
        ),
        profile=RuntimeProfile.OWNER_COPILOT,
        entity_id="ent-smoke",
        user_id="user-smoke",
        conversation_id="conv-smoke",
        tool_names=tuple(config["tools"]),
        allowed_tool_names=tuple(config["tools"]),
    )
    surface = runtime_prepare_prompt_skill_tool_surface(
        FakeSkill(),
        allowed_tool_names=set(config["tools"]),
        runtime_envelope=envelope,
    )
    for tool_name in [
        "mcp__chrome__documentation",
        "mcp__chrome__capabilities",
        "mcp__chrome__status",
        "mcp__chrome__name_session",
        "mcp__chrome__open_new_tab",
        "mcp__chrome__open_or_reuse",
        "mcp__chrome__open_tabs",
        "mcp__chrome__get_group_state",
        "mcp__chrome__finalize_tabs",
        "mcp__chrome__close_group_tabs",
        "mcp__chrome__read_page",
        "mcp__chrome__click_element",
        "mcp__chrome__fill_or_select",
        "mcp__chrome__claim_tab",
        "mcp__chrome__activate_tab",
        "mcp__chrome__screenshot",
        "mcp__chrome__send_cdp",
    ]:
        if tool_name not in surface.skill_tool_names:
            raise AssertionError(f"Chrome prompt skill tool surface missing {tool_name}: {surface.skill_tool_names}")
    for tool_name in [
        "search_tools",
    ]:
        if tool_name in surface.skill_tool_names:
            raise AssertionError(f"Chrome prompt skill tool surface should expose Chrome MCP tools only: {surface.skill_tool_names}")
    if surface.harness is None:
        raise AssertionError("Chrome prompt skill tool surface must be backed by RuntimeHarness")
    decision = surface.harness.check_tool_call("mcp__chrome__read_page", {})
    if not decision.allowed:
        raise AssertionError(f"RuntimeHarness denied declared Chrome tool: {decision}")


def _assert_chrome_skill_ranking() -> None:
    from packages.core.ai.runtime.skill_routing import rank_skills_for_runtime_turn

    class Skill:
        def __init__(self, slug: str, description: str) -> None:
            self.slug = slug
            self.name = slug
            self.display_name = slug
            self.description = description
            self.category = ""
            self.output_format = ""
            self.tags = []

    ranked = rank_skills_for_runtime_turn(
        [
            Skill("paper-writing", "Write academic papers."),
            Skill("chrome", "Operate the user's existing local Chrome browser."),
            Skill("image-copywriter", "Prepare image captions and short copy."),
        ],
        active_user_message=CHROME_MESSAGE,
    )
    if ranked[0].slug != "chrome":
        raise AssertionError(f"Chrome skill should rank first for Chrome requests: {[skill.slug for skill in ranked]}")


async def main() -> int:
    skill_path = ROOT / "packages" / "core" / "ai" / "skills" / "chrome" / "SKILL.md"
    config_path = skill_path.with_name("config.json")
    if not skill_path.exists():
        raise AssertionError(f"Chrome runtime skill is missing: {skill_path}")
    if not config_path.exists():
        raise AssertionError(f"Chrome runtime skill config is missing: {config_path}")

    guidance = skill_path.read_text(encoding="utf-8")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("type") != "runtime_guidance":
        raise AssertionError(f"Chrome skill must be a runtime_guidance prompt skill: {config}")

    for needle in [
        "Chrome Runtime Skill",
        "Connection And Tabs",
        "Surface Selection",
        "Required MCP-Chrome Loop",
        "Reading The Page",
        "Action Rules",
        "Additional Documentation",
        "Additional Capabilities",
        "API Reference",
        "API Use",
        "MCP API Surface",
        "Browser-scoped capabilities",
        "Tab-scoped capabilities",
        "references/runtime-contract.md",
        "references/api-reference.md",
        "references/capabilities.md",
        "references/interactions.md",
        "references/workflows.md",
        'topic="runtime-contract"',
        'topic="api-reference"',
        'topic="capabilities"',
        'topic="interactions"',
        "Do not depend on host file tools",
        "Explicit Chrome or local-browser intent wins",
        "A URL or already-open Chrome tab is context, not browser intent",
        "purpose-built connector, API, CLI, or MCP package",
        "Do not initialize Chrome just because a request contains a URL",
        "If authentication fails in a connector, ask the user to fix auth or explicitly approve Chrome fallback",
        "open_or_reuse -> `mcp__chrome__read_page` -> choose target ref from `pageContent` or structured candidates -> `mcp__chrome__click_element` / `mcp__chrome__fill_or_select` / `mcp__chrome__computer` -> wait if needed -> `mcp__chrome__read_page`",
        "Use `active=false`",
        "mcp__chrome__name_session",
        "Visible Chrome tab group names must be human-readable task labels",
        "short hash suffixes",
        "neutral, friendly, task-relevant emoji",
        "if unsure, use",
        "🔎",
        "mcp__chrome__finalize_tabs",
        "Treat `mcp__chrome__finalize_tabs` as the final Chrome action",
        "Do not call Chrome tools after finalizing",
        "mcp__chrome__documentation",
        "mcp__chrome__capabilities",
        "mcp__chrome__status -> mcp__chrome__documentation -> mcp__chrome__capabilities",
        "Before a nontrivial Chrome task",
        "runtime-contract",
        "Do not call activate_tab or claim_tab",
        "Do not guess tab ids",
        "synthesize claim tokens",
        "snapshot_id",
        "snapshot_id_required",
        "snapshot_mismatch",
        "fallback_reason_required",
        "Confirm at action-time",
        "sensitive_input_requires_confirmation",
        "redacted `data_summary`",
        "screenshots when visual fallback is explicitly needed",
        "`active:false` is tab metadata, not a blocker",
        "`chrome_read_page` is the page-understanding source",
        "Do not repeat unchanged `mcp__chrome__read_page` calls",
        "For form uploads, prefer upload candidates and upload_targets returned by read_page",
        "Use `mcp__chrome__fill_or_select` for text inputs",
        "Use `mcp__chrome__click_element` for buttons",
        "Browser use was stopped in the extension",
    ]:
        _assert_contains(guidance, needle)

    context_blocks_source = (
        ROOT / "packages" / "core" / "ai" / "runtime" / "context_blocks.py"
    ).read_text(encoding="utf-8")
    for forbidden in ["chrome_runtime_skill", "runtime_chrome_skill_guidance_for_message", "inline_runtime_guidance"]:
        if forbidden in context_blocks_source:
            raise AssertionError(f"Chrome skill should not be injected inline via context block: {forbidden}")
    _assert_chrome_skill_tool_surface(config)
    _assert_chrome_skill_ranking()
    _assert_chrome_reference_bundle(skill_path)
    await _assert_no_inline_runtime_context_block()

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
