from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


GENERIC_WEB_TOOLS = frozenset({"web_search", "web_fetch", "browse_web"})
LOCAL_CODING_BLOCKED_FIRST_PARTY_TOOLS = GENERIC_WEB_TOOLS | frozenset({
    "bash",
    "sandbox_exec",
    "sandbox_create",
    "sandbox_read_file",
    "sandbox_write_file",
    "invoke_skill",
})

SENSITIVE_FIRST_PARTY_PREFIXES = ("delete_", "cancel_", "remove_", "send_", "publish_")
DEFAULT_DEFERRED_TOOL_HINT_LIMIT = 12

MCP_PROVIDER_ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "e-mail", "mail", "imap", "smtp", "邮箱", "邮件", "垃圾邮件"),
    "gmail": ("gmail", "google mail", "谷歌邮箱", "gmail邮箱"),
    "outlook": ("outlook", "hotmail", "microsoft mail", "office 365", "微软邮箱"),
    "chrome": (
        "chrome",
        "google chrome",
        "local browser",
        "browser control",
        "browser automation",
        "本地浏览器",
        "浏览器操作",
        "chrome浏览器",
        "谷歌浏览器",
    ),
    "knowledge_local": (
        "knowledge local",
        "save knowledge local",
        "save to local",
        "knowledge to local",
        "local files",
        "保存到本地",
        "知识库保存本地",
        "知识库本地文件",
    ),
    "chrome_knowledge_local": (
        "chrome knowledge upload",
        "prepare upload",
        "prepare chrome upload",
        "knowledge upload",
        "upload knowledge",
        "local upload files",
        "知识库上传",
        "准备上传",
        "Chrome上传",
    ),
    "linkedin": ("linkedin", "linked in", "领英"),
    "linkedin_browser": ("linkedin", "linked in", "领英"),
    "twitter_x": ("twitter", "tweet", "x.com", "推特"),
    "facebook": ("facebook", "fb", "脸书"),
    "discord": ("discord",),
    "slack": ("slack",),
    "telegram": ("telegram", "tg", "电报"),
    "wechat_official": ("wechat", "weixin", "微信", "公众号"),
    "wechat_personal": ("wechat", "weixin", "微信"),
    "google_calendar": ("google calendar", "calendar", "日历"),
    "manor_mcp_calendar": (
        "manor calendar",
        "booking",
        "booking link",
        "booking links",
        "calendar settings",
        "calendar booking",
        "schedule",
        "agenda",
        "日程",
        "预约",
        "预约链接",
        "日历设置",
    ),
    "google_drive": ("google drive", "drive", "云盘"),
    "github": ("github", "git hub"),
    "notion": ("notion",),
    "replicate": ("replicate",),
    "tavily": ("tavily",),
    "perplexity_web": ("perplexity",),
    "jimeng": ("jimeng", "即梦"),
    "claude_code": (
        "claude code",
        "claude cli",
        "anthropic claude",
        "local claude",
        "local coding",
        "coding cli",
        "本地claude",
        "本地 claude",
        "本地编程",
        "编程cli",
    ),
    "codex_cli": (
        "codex",
        "codex cli",
        "openai codex",
        "local codex",
        "local coding",
        "coding cli",
        "本地codex",
        "本地 codex",
        "本地编程",
        "编程cli",
    ),
}


def runtime_search_terms(text: str) -> list[str]:
    """Tokenize search text while preserving short Chinese phrases."""
    return re.findall(r"[a-z0-9_.@+-]+|[\u4e00-\u9fff]+", text.lower())


def runtime_mcp_provider_aliases(provider: str) -> tuple[str, ...]:
    base = provider.lower()
    variants = {
        base,
        base.replace("_", " "),
        base.replace("_browser", ""),
        base.replace("_", ""),
    }
    variants.update(MCP_PROVIDER_ALIASES.get(provider, ()))
    return tuple(v for v in variants if v)


def runtime_apply_deferred_tool_discovery_hint(
    tool_schemas: list[dict],
    deferred_tool_names: Iterable[str],
    *,
    hint_limit: int = DEFAULT_DEFERRED_TOOL_HINT_LIMIT,
) -> list[dict]:
    """Append a compact deferred-tool preview to the search_tools schema."""
    sorted_deferred = sorted(str(name) for name in deferred_tool_names if str(name or "").strip())
    if not sorted_deferred:
        return tool_schemas

    preview = sorted_deferred[:hint_limit]
    more = len(sorted_deferred) - len(preview)
    suffix = f", ... (+{more} more)" if more > 0 else ""
    hint = (
        "\n\nDeferred tools available (use search_tools to load):\n"
        + ", ".join(preview)
        + suffix
    )
    for schema in tool_schemas:
        fn = schema.get("function", {}) if isinstance(schema, dict) else {}
        if fn.get("name") == "search_tools":
            fn["description"] = fn.get("description", "") + hint
            break
    return tool_schemas


def runtime_mcp_provider_text_score(provider: str, text: str | None) -> int:
    """Score whether text explicitly points at an MCP provider."""
    if not text:
        return 0
    haystack = text.lower()
    score = 0
    for alias in runtime_mcp_provider_aliases(provider):
        alias_l = alias.lower().strip()
        if alias_l and alias_l in haystack:
            score += 10 + min(len(alias_l), 20)
    if provider == "twitter_x" and "x" in runtime_search_terms(haystack):
        score += 11
    return score


def runtime_mcp_provider_from_tool_name(name: str) -> str | None:
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    return parts[1] if len(parts) >= 3 and parts[1] else None


def runtime_mcp_execution_mode(
    server_key: str,
    metadata: dict | None = None,
) -> str:
    metadata = metadata or {}
    haystack = " ".join(
        str(metadata.get(key) or "")
        for key in ("name", "endpoint", "auth_type")
    ).lower()
    if metadata.get("auth_type") == "cli_worker":
        return "cli_worker"
    if server_key.endswith("_browser") or "browser" in haystack:
        return "browser_automation"
    return "official_api"


def runtime_mcp_authorization_method(
    server_key: str,
    metadata: dict | None = None,
) -> str:
    metadata = metadata or {}
    mode = runtime_mcp_execution_mode(server_key, metadata)
    if mode == "browser_automation":
        return "browser_session"
    auth_type = str(metadata.get("auth_type") or "").strip()
    if auth_type == "oauth2":
        return "oauth"
    return auth_type or "unknown"


def runtime_mcp_provider_options(matches: Iterable[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for match in matches:
        name = str(match.get("name") or "")
        provider = match.get("server_key") or runtime_mcp_provider_from_tool_name(name)
        if not provider:
            continue
        provider = str(provider)
        option = grouped.setdefault(provider, {
            "server_key": provider,
            "name": match.get("integration_name") or provider,
            "auth_type": match.get("auth_type"),
            "authorization_method": match.get("authorization_method"),
            "execution_mode": match.get("execution_mode"),
            "ready": bool(match.get("available")),
            "available": bool(match.get("available")),
            "scope": match.get("scope"),
            "reason": match.get("reason"),
            "matched_tools": [],
        })
        if match.get("available"):
            option["ready"] = True
            option["available"] = True
            option["scope"] = match.get("scope")
            option["reason"] = match.get("reason")
        if name:
            option["matched_tools"].append(name)
    return list(grouped.values())


def runtime_mark_match_available(match: dict) -> dict:
    match["available"] = True
    match["ready"] = True
    return match


def runtime_mark_mcp_match_unavailable(match: dict, reason: str) -> dict:
    if str(match.get("name") or "").startswith("mcp__"):
        match["available"] = False
        match["reason"] = reason
    else:
        match["available"] = True
    return match


def runtime_apply_mcp_availability(
    match: dict,
    *,
    provider: str,
    metadata: dict | None,
    status: dict,
) -> dict:
    metadata = metadata or {}
    available = bool(status.get("available"))
    match["available"] = available
    match["ready"] = available
    match["server_key"] = provider
    match["integration_name"] = metadata.get("name") or provider
    match["auth_type"] = metadata.get("auth_type")
    match["authorization_method"] = runtime_mcp_authorization_method(provider, metadata)
    match["execution_mode"] = runtime_mcp_execution_mode(provider, metadata)
    match["scope"] = status.get("scope")
    match["reason"] = status.get("reason")
    if metadata.get("coming_soon"):
        match["coming_soon"] = True
    return match


def runtime_sort_available_matches(matches: list[dict]) -> list[dict]:
    matches.sort(key=lambda item: (0 if item.get("available") else 1))
    return matches


@dataclass(frozen=True)
class RuntimeSearchToolsRequest:
    query: str
    max_results: int
    search_pool_size: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def runtime_prepare_search_tools_request(
    *,
    query: object,
    max_results: object = None,
    default_max_results: int = 5,
    hard_cap: int = 8,
    min_results: int = 1,
    oversample_multiplier: int = 4,
    min_search_pool_size: int = 16,
) -> RuntimeSearchToolsRequest:
    query_text = str(query or "").strip()
    try:
        requested_max = int(max_results or default_max_results)
    except (TypeError, ValueError):
        requested_max = default_max_results
    normalized_max = max(min_results, min(requested_max, hard_cap))
    search_pool_size = max(normalized_max * oversample_multiplier, min_search_pool_size)
    return RuntimeSearchToolsRequest(
        query=query_text,
        max_results=normalized_max,
        search_pool_size=search_pool_size,
        error=None if query_text else "query is required",
    )


def runtime_search_tools_payload(
    *,
    matches: list[dict],
    query: str,
    suppressed_mcp: list[dict] | None = None,
    total_tool_count: int | None = None,
) -> dict:
    suppressed_mcp = list(suppressed_mcp or [])
    if not matches:
        payload: dict = {
            "matches": [],
            "query": query,
            "loaded_tools": [],
            "hint": f"No tools matched. {total_tool_count or 0} tools available.",
        }
        if suppressed_mcp:
            payload["suppressed_mcp"] = suppressed_mcp
            payload["hint"] = (
                "MCP providers matched the search query but were not loaded "
                "because they do not match the user's current request."
            )
        return payload

    unavailable = [
        {
            "name": match.get("name"),
            "server_key": match.get("server_key"),
            "authorization_method": match.get("authorization_method"),
            "execution_mode": match.get("execution_mode"),
            "reason": match.get("reason"),
        }
        for match in matches
        if str(match.get("name") or "").startswith("mcp__")
        and match.get("available") is False
    ]
    payload = {
        "matches": matches,
        "query": query,
        "loaded_tools": [
            match.get("name")
            for match in matches
            if match.get("name") and match.get("available") is not False
        ],
    }
    if suppressed_mcp:
        payload["suppressed_mcp"] = suppressed_mcp
    mcp_options = runtime_mcp_provider_options(matches)
    if mcp_options:
        payload["mcp_options"] = mcp_options
    hints: list[str] = []
    if suppressed_mcp:
        hints.append(
            "Some MCP providers matched the search query but were not loaded "
            "because they do not match the user's current request."
        )
    if unavailable:
        payload["unavailable_mcp"] = unavailable
        hints.append(
            "MCP candidates without connected credentials are listed for transparency "
            "but are not loaded or callable. Connect the integration under Settings \u2192 "
            "Integrations, or choose a ready alternate path."
        )
    if hints:
        payload["hint"] = " ".join(hints)
    return payload


def runtime_finalize_search_tools_payload(
    *,
    matches: list[dict],
    request: RuntimeSearchToolsRequest,
    suppressed_mcp: list[dict] | None = None,
    total_tool_count: int | None = None,
) -> dict:
    visible_matches = list(matches)[:request.max_results]
    return runtime_search_tools_payload(
        matches=visible_matches,
        query=request.query,
        suppressed_mcp=suppressed_mcp,
        total_tool_count=total_tool_count,
    )


def runtime_tool_query_score(name: str, description: str, query: str) -> int:
    """Keyword score used by search_tools for both first-party and MCP tools."""
    terms = runtime_search_terms(query)
    if not terms and query:
        terms = [query.lower()]
    name_lower = name.lower()
    desc_lower = description.lower()
    score = 0
    for word in terms:
        if word.startswith("+"):
            required = word[1:]
            if required and required not in name_lower:
                return -1
        elif word in name_lower:
            score += 3
        elif word in desc_lower:
            score += 1
    if name == "browse_web" and any(
        word in {
            "javascript",
            "js",
            "spa",
            "rendered",
            "dynamic",
            "browser",
            "website",
            "webpage",
        }
        for word in terms
    ):
        score += 8
    return score


def runtime_mcp_active_provider_scores(
    *,
    tool_names: Iterable[str],
    active_user_message: str | None,
) -> dict[str, int]:
    if not active_user_message:
        return {}
    providers = {
        provider
        for name in tool_names
        if (provider := runtime_mcp_provider_from_tool_name(str(name)))
    }
    return {
        provider: score
        for provider in providers
        if (score := runtime_mcp_provider_text_score(provider, active_user_message)) > 0
    }


def runtime_mcp_tool_names_for_active_intent(
    *,
    tool_names: Iterable[str],
    active_user_message: str | None,
) -> set[str]:
    """Return MCP tools whose provider is explicitly named this turn."""

    active_scores = runtime_mcp_active_provider_scores(
        tool_names=tool_names,
        active_user_message=active_user_message,
    )
    if not active_scores:
        return set()
    return {
        str(name)
        for name in tool_names
        if (provider := runtime_mcp_provider_from_tool_name(str(name)))
        and active_scores.get(provider, 0) > 0
    }


def runtime_social_browser_recovery_query(query: str | None) -> bool:
    # Legacy browser_action discovery used to temporarily lift social-tool
    # restrictions for recovery. Chrome now exposes explicit read/click
    # tools, and those are already present in the allowed social tool set.
    # Keep the hook for callers, but do not re-open the old generic surface.
    return False


def runtime_is_sensitive_first_party_tool(name: str) -> bool:
    return (
        not name.startswith("mcp__")
        and (
            name.startswith(SENSITIVE_FIRST_PARTY_PREFIXES)
            or name in {"archive_conversation"}
        )
    )


def runtime_sensitive_first_party_matches_active_intent(
    name: str,
    active_user_message: str | None,
) -> bool:
    if not active_user_message:
        return False
    text = active_user_message.lower()
    if "scheduled" in text or "schedule" in text or "automation" in text or "定时" in text or "任务" in text:
        if "scheduled" in name or "schedule" in name or "job" in name:
            return True
    if "file" in text or "document" in text or "文件" in text or "文档" in text:
        if "file" in name or "document" in name:
            return True
    if "skill" in text or "技能" in text:
        if "skill" in name:
            return True
    return False


@dataclass(frozen=True)
class RuntimeToolSearchScope:
    active_provider_scores: dict[str, int]
    local_coding_providers: tuple[str, ...] = ()
    restrict_social_tools: bool = False
    allowed_social_tool_names: frozenset[str] = frozenset()
    chrome_local_browser: bool = False
    preferred_chrome_tool_names: tuple[str, ...] = ()

    @property
    def enforce_active_scope(self) -> bool:
        return bool(self.active_provider_scores)

    def provider_allowed(self, provider: str | None) -> bool:
        return not provider or not self.enforce_active_scope or self.active_provider_scores.get(provider, 0) > 0

    def mcp_tool_allowed(self, tool_name: str) -> bool:
        if self.restrict_social_tools and tool_name.startswith("mcp__"):
            return tool_name in self.allowed_social_tool_names
        return True

    def first_party_tool_allowed(
        self,
        tool_name: str,
        active_user_message: str | None,
    ) -> bool:
        from packages.core.ai.runtime.chrome_routing import GENERIC_WEB_TOOLS

        if self.chrome_local_browser and tool_name in GENERIC_WEB_TOOLS:
            return False
        if (
            self.local_coding_providers
            and tool_name in LOCAL_CODING_BLOCKED_FIRST_PARTY_TOOLS
        ):
            return False
        if (
            self.enforce_active_scope
            and runtime_is_sensitive_first_party_tool(tool_name)
            and not runtime_sensitive_first_party_matches_active_intent(
                tool_name,
                active_user_message,
            )
        ):
            return False
        return True


def runtime_tool_search_scope(
    *,
    tool_names: Iterable[str],
    query: str | None,
    active_user_message: str | None,
) -> RuntimeToolSearchScope:
    from packages.core.ai.runtime.chrome_routing import (
        detect_chrome_local_browser_route,
    )
    from packages.core.ai.runtime.skill_routing import local_coding_provider_route

    active_scores = runtime_mcp_active_provider_scores(
        tool_names=tool_names,
        active_user_message=active_user_message,
    )
    chrome_local_route = next(
        (
            route
            for text in (active_user_message, query)
            if (route := detect_chrome_local_browser_route(text))
        ),
        None,
    )
    local_coding_providers = local_coding_provider_route(active_user_message)
    if local_coding_providers:
        active_scores = {
            provider: 100
            for provider in local_coding_providers
        }

    preferred_local_providers: set[str] = set()
    preferred_chrome_tool_names: tuple[str, ...] = ()
    if chrome_local_route:
        preferred_local_providers.update(chrome_local_route.provider_keys)
        preferred_chrome_tool_names = chrome_local_route.preferred_tool_names
    if preferred_local_providers:
        active_scores = {
            provider: 100
            for provider in preferred_local_providers
        }

    return RuntimeToolSearchScope(
        active_provider_scores=active_scores,
        local_coding_providers=tuple(local_coding_providers),
        restrict_social_tools=False,
        allowed_social_tool_names=frozenset(),
        chrome_local_browser=bool(chrome_local_route),
        preferred_chrome_tool_names=preferred_chrome_tool_names,
    )
