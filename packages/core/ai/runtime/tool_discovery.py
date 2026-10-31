from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from packages.core.ai.runtime.integration_setup_links import runtime_integration_setup_link
from packages.core.ai.runtime.tool_visibility import runtime_tool_is_catalog_bindable
from packages.core.constants.integrations import (
    INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT,
)

logger = logging.getLogger(__name__)


GENERIC_WEB_TOOLS = frozenset({"web_search", "web_fetch", "browse_web"})
LOCAL_CODING_BLOCKED_FIRST_PARTY_TOOLS = GENERIC_WEB_TOOLS | frozenset({
    "bash",
    "sandbox",
    "sandbox_exec",
    "sandbox_status",
    "sandbox_respond",
    "sandbox_cancel",
    "sandbox_create",
    "sandbox_read_file",
    "sandbox_write_file",
    "invoke_skill",
})

SENSITIVE_FIRST_PARTY_PREFIXES = ("delete_", "cancel_", "remove_", "send_", "publish_")
DEFAULT_DEFERRED_TOOL_HINT_LIMIT = 12
SEARCH_TOOLS_HARD_CAP = 8
# A multi-capability request searches each step: at most this many queries,
# a few results each by default (still within the hard cap).
SEARCH_TOOLS_MAX_QUERIES = 4
SEARCH_TOOLS_RESULTS_PER_QUERY = 3
SOCIAL_MCP_PROVIDERS = frozenset({
    "chrome",
    "facebook",
    "instagram",
    "linkedin",
    "reddit",
    "tiktok",
    "twitter_x",
    "youtube",
})

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
    "linkedin": ("linkedin", "linked in", "领英"),
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


_SERVER_INDEX_CACHE: dict[str, dict] | None = None


def runtime_server_index() -> dict[str, dict]:
    """Per-server metadata for server-first search ranking.

    Built lazily once per process from mcp_seed._MCP_CATALOG (name,
    description \u2014 plain import, no DB), MCP_PROVIDER_ALIASES, and the live
    _SERVER_TOOL_SCHEMAS keys. Servers registered in the schema catalog but
    missing from the seed catalog degrade to key-only entries. Failure to
    build degrades to {} \u2014 callers must treat that as "v1 behavior". A
    FAILED build is never cached (only a successful one is), so a transient
    failure (e.g. a circular import at an unlucky moment during boot) gets
    retried on the next call instead of permanently degrading the process.
    """
    global _SERVER_INDEX_CACHE
    if _SERVER_INDEX_CACHE is not None:
        return _SERVER_INDEX_CACHE
    try:
        from packages.core.services.mcp_seed import _MCP_CATALOG
        from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS

        catalog = {row[0]: row for row in _MCP_CATALOG}
        index: dict[str, dict] = {}
        for key, tools in _SERVER_TOOL_SCHEMAS.items():
            row = catalog.get(key)
            index[key] = {
                "key": key,
                "name": (row[1] if row else key),
                "description": (row[2] if row else ""),
                "aliases": MCP_PROVIDER_ALIASES.get(key, ()),
                "tool_count": len(tools),
            }
    except Exception:  # degrade, never break search; don't cache the failure
        logger.warning("runtime_server_index build failed", exc_info=True)
        return {}
    _SERVER_INDEX_CACHE = index
    return index


def runtime_server_query_score(server_entry: dict, query: str) -> int:
    """Substring scorer over server name (+3), description (+1), aliases
    (existing provider-text weights via runtime_mcp_provider_text_score)."""
    terms = runtime_search_terms(query)
    if not terms and query:
        terms = [query.lower()]
    name_lower = str(server_entry.get("name") or "").lower()
    desc_lower = str(server_entry.get("description") or "").lower()
    score = 0
    for word in terms:
        if word.startswith("+"):
            word = word[1:]
            if not word:
                continue
        if word in name_lower:
            score += 3
        if word in desc_lower:
            score += 1
    score += runtime_mcp_provider_text_score(
        str(server_entry.get("key") or ""), query.lower()
    )
    return score


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
    from packages.core.ai.runtime.skill_routing import (
        is_local_coding_provider_key,
        local_coding_cli_intent,
    )

    if is_local_coding_provider_key(provider) and not local_coding_cli_intent(text):
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
        if match.get("account_options"):
            option["account_options"] = list(match.get("account_options") or [])
            option["default_account_id"] = match.get("default_account_id")
            if match.get("requires_explicit_account"):
                option["requires_explicit_account"] = True
        if match.get("available"):
            option["ready"] = True
            option["available"] = True
            option["scope"] = match.get("scope")
            option["reason"] = match.get("reason")
        if not option.get("account_options") and match.get("account_options"):
            option["account_options"] = list(match.get("account_options") or [])
            option["default_account_id"] = match.get("default_account_id")
            if match.get("requires_explicit_account"):
                option["requires_explicit_account"] = True
        if name:
            option["matched_tools"].append(name)
    return list(grouped.values())


def runtime_apply_integration_account_options_to_schema(
    schema: dict,
    account_options: Iterable[dict] | None,
    *,
    requires_explicit_account: bool = False,
) -> dict:
    """Add user-scoped connected-account choices to one MCP tool schema."""
    options = [option for option in (account_options or []) if option.get("id")]
    if not options:
        return schema

    out = copy.deepcopy(schema)
    fn = out.get("function") if isinstance(out, dict) else None
    if not isinstance(fn, dict):
        return schema
    parameters = fn.setdefault("parameters", {"type": "object", "properties": {}})
    if not isinstance(parameters, dict):
        return schema
    properties = parameters.setdefault("properties", {})
    if not isinstance(properties, dict):
        return schema
    properties.pop(INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT, None)
    required = parameters.get("required")
    if isinstance(required, list):
        parameters["required"] = [
            item
            for item in required
            if item != INTEGRATION_ACCOUNT_CONTINUATION_ARGUMENT
        ]

    labels = "; ".join(
        f"{option['id']} = {option.get('display_name') or 'Connected account'}"
        + (" (default)" if option.get("is_default") else "")
        for option in options
    )
    tool_name = str(fn.get("name") or "")
    supports_all_accounts = (
        runtime_mcp_tool_supports_all_accounts(tool_name)
        and not requires_explicit_account
    )
    all_accounts_hint = (
        " For a read-only query across every account, set "
        "integration_account_selection to 'all'."
        if supports_all_accounts
        else ""
    )
    selection_requirement = (
        " The account registry is incomplete, so you must select one exact "
        "listed account; omitting this argument is blocked."
        if requires_explicit_account
        else (
            " Every listed account is callable; the default only controls "
            "which account is used when this argument is omitted."
        )
    )
    properties["integration_account_id"] = {
        "type": "string",
        "enum": [str(option["id"]) for option in options],
        "description": (
            "Connected account to use for this operation."
            f"{selection_requirement}"
            f"{all_accounts_hint} "
            f"Available accounts: {labels}"
        ),
    }
    if requires_explicit_account:
        required = parameters.setdefault("required", [])
        if isinstance(required, list) and "integration_account_id" not in required:
            required.append("integration_account_id")
    if supports_all_accounts:
        properties["integration_account_selection"] = {
            "type": "string",
            "enum": ["default", "all"],
            "default": "default",
            "description": (
                "Use 'default' for one prioritized or explicitly selected account. "
                "Use 'all' to execute this read-only tool against every listed "
                "account and return one combined result."
            ),
        }
    else:
        properties.pop("integration_account_selection", None)
    return out


def runtime_mcp_tool_supports_all_accounts(tool_name: str) -> bool:
    """Use the governance classifier as the canonical ALL-mode capability."""
    if not str(tool_name or "").startswith("mcp__"):
        return False
    from packages.core.ai.runtime.approval_classifier import (
        RuntimeToolEffect,
        classify_runtime_tool,
    )

    return (
        classify_runtime_tool(str(tool_name), {}).effect
        is RuntimeToolEffect.READ_ONLY
    )


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
    if status.get("requires_explicit_account"):
        match["requires_explicit_account"] = True
    if metadata.get("coming_soon"):
        match["coming_soon"] = True
    return match


def runtime_sort_available_matches(matches: list[dict]) -> list[dict]:
    matches.sort(key=lambda item: (0 if item.get("available") else 1))
    return matches


@dataclass(frozen=True)
class RuntimeSearchToolsRequest:
    mode: str
    query: str
    provider_key: str | None
    exact_names: tuple[str, ...]
    max_results: int
    search_pool_size: int
    error: str | None = None
    # One query per step of a multi-capability request; ``query`` joins them.
    queries: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.error is None


def runtime_prepare_search_tools_request(
    *,
    query: object = None,
    mode: object = None,
    provider_key: object = None,
    exact_names: object = None,
    max_results: object = None,
    queries: object = None,
    default_max_results: int = 5,
    hard_cap: int = SEARCH_TOOLS_HARD_CAP,
    min_results: int = 1,
    oversample_multiplier: int = 4,
    min_search_pool_size: int = 16,
) -> RuntimeSearchToolsRequest:
    normalized_mode = str(mode or "semantic").strip().casefold()
    query_list = tuple(dict.fromkeys(
        str(item or "").strip() for item in queries if str(item or "").strip()
    )) if isinstance(queries, (list, tuple)) else ()
    query_text = str(query or "").strip()
    too_many_queries = len(query_list) > SEARCH_TOOLS_MAX_QUERIES
    if query_list and query_text:
        # Models often send both: per-step queries and one summarizing the
        # request. The summary is ranked as one more query.
        query_list = tuple(dict.fromkeys((*query_list, query_text)))
    query_text = " ; ".join(query_list) if len(query_list) > 1 else query_text or "".join(query_list)
    normalized_provider_key = str(provider_key or "").strip().casefold() or None
    normalized_exact_names = (
        tuple(
            dict.fromkeys(
                str(name or "").strip()
                for name in exact_names
                if str(name or "").strip()
            )
        )
        if isinstance(exact_names, (list, tuple))
        else ()
    )
    default_limit = (
        max(default_max_results, len(normalized_exact_names))
        if normalized_mode == "exact"
        # A few results for each step of a multi-query search.
        else max(default_max_results, SEARCH_TOOLS_RESULTS_PER_QUERY * len(query_list))
    )
    try:
        requested_max = int(max_results or default_limit)
    except (TypeError, ValueError):
        requested_max = default_max_results
    normalized_max = max(min_results, min(requested_max, hard_cap))
    search_pool_size = max(normalized_max * oversample_multiplier, min_search_pool_size)
    error: str | None = None
    if normalized_mode not in {"semantic", "provider", "exact"}:
        error = "mode must be semantic, provider, or exact"
    elif normalized_mode == "semantic" and not query_text:
        error = "query is required for semantic search"
    elif query_list and normalized_mode != "semantic":
        error = "queries is for semantic search only"
    elif too_many_queries:
        error = f"queries cannot contain more than {SEARCH_TOOLS_MAX_QUERIES} entries"
    elif normalized_mode == "provider":
        if not normalized_provider_key:
            error = "provider_key is required for provider search"
        elif not runtime_integration_setup_link(normalized_provider_key):
            error = "provider_key has an invalid format"
    elif normalized_mode == "exact":
        if not normalized_exact_names:
            error = "exact_names is required for exact search"
        elif len(normalized_exact_names) > hard_cap:
            error = f"exact_names cannot contain more than {hard_cap} names"
    return RuntimeSearchToolsRequest(
        mode=normalized_mode,
        query=query_text,
        provider_key=normalized_provider_key,
        exact_names=normalized_exact_names,
        max_results=normalized_max,
        search_pool_size=search_pool_size,
        error=error,
        queries=query_list if len(query_list) > 1 else (),
    )


def _runtime_empty_matches_suppression_hint(suppressed_mcp: list[dict]) -> str:
    """Reason-aware hint for the zero-matches path.

    Structured provider discovery produces distinct ``not_usable`` and
    ``unknown_server`` shapes that need different guidance than the generic
    scope-suppression case.
    """
    unknown = next(
        (item for item in suppressed_mcp if item.get("reason") == "unknown_server"),
        None,
    )
    if unknown is not None:
        similar = list(unknown.get("similar_servers") or [])
        closest = ", ".join(similar) if similar else "none found"
        return f"Unknown server key '{unknown.get('server_key')}'. Closest: {closest}."
    if suppressed_mcp and all(item.get("reason") == "not_usable" for item in suppressed_mcp):
        key = suppressed_mcp[0].get("server_key")
        return (
            f"'{key}' is not connected. Connect the integration under "
            "Settings → Integrations to browse its tools."
        )
    return (
        "MCP providers matched the search query but were not loaded "
        "because they do not match the user's current request."
    )


NOT_IN_SCOPE_REASON = "exists_not_bound_to_this_agent"
BLOCKED_BY_POLICY_REASON = "blocked_by_policy"
AVAILABLE_VIA_MANOR_REASON = "available_via_manor"


def runtime_not_in_scope_entries(
    *,
    requested_names: Iterable[str],
    registered_tool_names: Iterable[str],
    scoped_tool_names: Iterable[str],
    blocked_tool_names: Iterable[str] = (),
    can_acquire: bool = False,
    policy_blocks: Callable[[str], bool | None] | None = None,
    platform_routes: Mapping[str, dict] | None = None,
) -> list[dict]:
    """Describe exact-requested native tools that exist but are outside this run's scope.

    Information only: nothing here is loaded, granted or made callable. A
    policy-blocked tool is never offered for acquisition.
    """

    registered = {str(name) for name in registered_tool_names}
    scoped = {str(name) for name in scoped_tool_names}
    blocked = {str(name) for name in blocked_tool_names}
    entries: list[dict] = []
    seen: set[str] = set()
    for raw in requested_names:
        name = str(raw or "").strip().lower()
        if (
            not name
            or name in seen
            or name in scoped
            or name not in registered
            or name.startswith("mcp__")
        ):
            continue
        seen.add(name)
        # True: policy denies it. None: policy could not be evaluated, so
        # acquisition is not offered and no block is claimed.
        policy_verdict = policy_blocks(name) if policy_blocks is not None else False
        if name in blocked or policy_verdict is True:
            entries.append({
                "name": name,
                "reason": BLOCKED_BY_POLICY_REASON,
                "otherwise": (
                    "The current policy blocks this tool for this agent. Tell the user; "
                    "only they can change that policy."
                ),
            })
            continue
        route = (platform_routes or {}).get(name)
        if route is not None:
            # Already callable here through the in-scope manor tool.
            entries.append({"name": name, "reason": AVAILABLE_VIA_MANOR_REASON, "call_with": route})
            continue
        entry: dict = {"name": name, "reason": NOT_IN_SCOPE_REASON}
        if can_acquire and policy_verdict is False and runtime_tool_is_catalog_bindable(name):
            entry["acquire_with"] = {
                "tool": "acquire_capabilities",
                "arguments": {"action": "prepare", "catalog_ids": [f"tool:{name}"]},
            }
        entry["otherwise"] = "Ask the user to add it to this agent's configuration."
        entries.append(entry)
    return entries


def runtime_not_in_scope_hint(entries: Iterable[dict]) -> str:
    """One actionable sentence per out-of-scope tool."""

    sentences: list[str] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        if entry.get("reason") == AVAILABLE_VIA_MANOR_REASON:
            route = entry.get("call_with") or {}
            arguments = ", ".join(
                f"{key}={value!r}" for key, value in (route.get("arguments") or {}).items()
            )
            sentences.append(
                f"{name} is not bound as its own tool, but it is available here as "
                f"{route.get('tool')}({arguments})."
            )
        elif entry.get("reason") == BLOCKED_BY_POLICY_REASON:
            sentences.append(
                f"{name} exists but the current policy blocks it for this agent; "
                "tell the user it is unavailable here."
            )
        elif entry.get("acquire_with"):
            sentences.append(
                f"{name} exists but is not bound to this agent; acquire it with "
                f"acquire_capabilities(prepare, ['tool:{name}'])."
            )
        else:
            sentences.append(
                f"{name} exists but is not bound to this agent. "
                "Ask the user to add it to this agent's configuration."
            )
    return " ".join(sentences)


def runtime_search_tools_payload(
    *,
    matches: list[dict],
    query: str,
    mode: str = "semantic",
    provider_keys: Iterable[str] = (),
    suppressed_mcp: list[dict] | None = None,
    total_tool_count: int | None = None,
    servers: list[dict] | None = None,
    total_skill_count: int | None = None,
    not_in_scope: list[dict] | None = None,
) -> dict:
    suppressed_mcp = list(suppressed_mcp or [])
    not_in_scope = list(not_in_scope or [])
    normalized_provider_keys = tuple(
        dict.fromkeys(
            str(provider or "").strip().casefold()
            for provider in provider_keys
            if str(provider or "").strip()
        )
    )
    if not matches:
        tool_count = total_tool_count or 0
        hint = (
            f"No tools matched. {tool_count} tools available."
            if total_skill_count is None
            else (
                "No capabilities matched. "
                f"{tool_count} {'tool' if tool_count == 1 else 'tools'} and "
                f"{total_skill_count} "
                f"{'skill' if total_skill_count == 1 else 'skills'} available."
            )
        )
        payload: dict = {
            "ok": True,
            "matches": [],
            "mode": mode,
            "query": query,
            "loaded_tools": [],
            "hint": hint,
        }
        if normalized_provider_keys:
            payload["provider_keys"] = list(normalized_provider_keys)
        if mode == "exact":
            payload["hint"] = (
                f"{hint} Exact names do not use semantic matching; use semantic "
                "mode to search by capability description."
            )
        if suppressed_mcp:
            payload["suppressed_mcp"] = suppressed_mcp
            payload["hint"] = _runtime_empty_matches_suppression_hint(suppressed_mcp)
            setup_links = [
                runtime_integration_setup_link(provider)
                for provider in dict.fromkeys(
                    str(item.get("server_key") or "")
                    for item in suppressed_mcp if item.get("reason") == "not_usable"
                )
            ]
            if any(setup_links):
                payload["integration_setup"] = [link for link in setup_links if link]
        if not_in_scope:
            # The requested tool exists; "nothing matched" alone would be false.
            payload["not_in_scope"] = not_in_scope
            payload["hint"] = (
                f"{payload['hint']} {runtime_not_in_scope_hint(not_in_scope)}"
                if suppressed_mcp
                else runtime_not_in_scope_hint(not_in_scope)
            )
        return _runtime_structured_provider_failure(
            payload,
            provider_keys=normalized_provider_keys,
        )

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
        "ok": True,
        "matches": matches,
        "mode": mode,
        "query": query,
        # Several composite operations share one tool (manor): load it once.
        "loaded_tools": list(dict.fromkeys(
            match.get("name")
            for match in matches
            if match.get("name")
            and match.get("kind") != "skill"
            and match.get("available") is not False
        )),
    }
    if normalized_provider_keys:
        payload["provider_keys"] = list(normalized_provider_keys)
    if suppressed_mcp:
        payload["suppressed_mcp"] = suppressed_mcp
    if servers:
        payload["servers"] = servers
    mcp_options = runtime_mcp_provider_options(matches)
    if mcp_options:
        payload["mcp_options"] = mcp_options
    hints: list[str] = []
    if suppressed_mcp:
        hints.append(_runtime_empty_matches_suppression_hint(suppressed_mcp))
    if unavailable:
        payload["unavailable_mcp"] = unavailable
        setup_links = [
            runtime_integration_setup_link(provider)
            for provider in dict.fromkeys(
                str(item.get("server_key") or runtime_mcp_provider_from_tool_name(item.get("name") or "") or "")
                for item in unavailable
            )
        ]
        if any(setup_links):
            payload["integration_setup"] = [link for link in setup_links if link]
        hints.append(
            "MCP candidates without connected credentials are listed for transparency "
            "but are not loaded or callable. Connect the integration under Settings \u2192 "
            "Integrations, or choose a ready alternate path."
        )
    if not_in_scope:
        payload["not_in_scope"] = not_in_scope
        hints.append(runtime_not_in_scope_hint(not_in_scope))
    if hints:
        payload["hint"] = " ".join(hints)
    return _runtime_structured_provider_failure(
        payload,
        provider_keys=normalized_provider_keys,
    )


def _runtime_usable_match_names(
    matches: Iterable[dict],
    unavailable_providers: set[str],
) -> list[str]:
    """Names of matches the model can still call although some providers are unusable.

    Tools: callable unless flagged unavailable/not ready or served by an unusable
    MCP provider. Skills: usable only when every provider implied by their tools
    (or ``required_integrations``) is usable; a skill with no provider signal at
    all is treated as not usable so we never claim a false alternative.
    """
    usable: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        name = str(match.get("name") or "").strip()
        if not name:
            continue
        if match.get("kind") == "skill":
            providers = {
                provider.casefold()
                for provider in (
                    runtime_mcp_provider_from_tool_name(str(tool or ""))
                    for tool in (match.get("tools") or ())
                )
                if provider
            }
            providers |= {
                str(item.get("provider") or "").strip().casefold()
                for item in (match.get("required_integrations") or ())
                if isinstance(item, dict) and str(item.get("provider") or "").strip()
            }
            if providers:
                if providers & unavailable_providers:
                    continue
                usable.append(name)
                continue
            if match.get("available") is True or match.get("ready") is True:
                usable.append(name)
            continue
        if match.get("available") is False or match.get("ready") is False:
            continue
        provider = runtime_mcp_provider_from_tool_name(name)
        if provider and provider.casefold() in unavailable_providers:
            continue
        usable.append(name)
    return usable


def _runtime_structured_provider_failure(
    payload: dict,
    *,
    provider_keys: Iterable[str],
) -> dict:
    """Report provider-scoped discovery evidence, never end the parent run.

    Failure to discover one provider does not establish whether the user's
    capability is unavailable. The model chooses further discovery or setup;
    the ordinary execution boundary still owns permissions and effects.
    """

    requested = tuple(
        dict.fromkeys(
            str(provider or "").strip().casefold()
            for provider in provider_keys
            if str(provider or "").strip()
        )
    )
    if not requested:
        return payload
    unavailable = [
        provider
        for provider in requested
        if any(
            str(item.get("server_key") or "") == provider
            and item.get("reason") == "not_usable"
            for item in payload.get("suppressed_mcp") or ()
            if isinstance(item, dict)
        )
        or any(
            str(
                item.get("server_key")
                or runtime_mcp_provider_from_tool_name(str(item.get("name") or ""))
                or ""
            )
            == provider
            for item in payload.get("unavailable_mcp") or ()
            if isinstance(item, dict)
        )
    ]
    if not unavailable:
        return payload
    provider = unavailable[0]
    setup = runtime_integration_setup_link(provider)
    usable_names = _runtime_usable_match_names(
        payload.get("matches") or (),
        {str(item).casefold() for item in unavailable},
    )
    details = {
        "failure_scope": "provider_discovery",
        "provider_selection": "inferred" if payload.get("mode") == "semantic" else "explicit",
        "mode": payload.get("mode"),
        "query": payload.get("query"),
        "provider_keys": list(requested),
        "unavailable_provider_keys": unavailable,
        "callable_candidates": usable_names,
        "capability_availability": "not_established",
        "suppressed_mcp": payload.get("suppressed_mcp", []),
        "unavailable_mcp": payload.get("unavailable_mcp", []),
        **setup,
    }
    payload["details"] = details
    payload["unavailable_provider_keys"] = unavailable
    payload.update(setup)
    if usable_names:
        # Mark, do not abort: the model still has a callable path.
        existing_hint = str(payload.get("hint") or "").strip()
        alternatives = ", ".join(usable_names)
        payload["hint"] = " ".join(
            part for part in (
                existing_hint,
                f"{provider} is unavailable. Other invocable candidates: {alternatives}. "
                "Their suitability and dependencies must be assessed; they are not verified substitutes.",
            ) if part
        )
        return payload
    reason = str(payload.get("hint") or "").strip() or (
        f"No usable {provider} integration account is connected."
    )
    reason += (
        " This is a provider-discovery result, not proof that the requested capability "
        "is unavailable. No provider action was executed. Further discovery remains "
        "within the current tool scope. Discovery does not authorize replacing a "
        "provider explicitly required by the user."
    )
    # The catalog lookup succeeded. A candidate's missing connection is data,
    # not an execution failure or a reason to stop the parent loop.
    payload.update(ok=True, hint=reason)
    return payload


def runtime_finalize_search_tools_payload(
    *,
    matches: list[dict],
    request: RuntimeSearchToolsRequest,
    suppressed_mcp: list[dict] | None = None,
    total_tool_count: int | None = None,
    servers: list[dict] | None = None,
) -> dict:
    visible_matches = list(matches)[:request.max_results]
    return runtime_search_tools_payload(
        matches=visible_matches,
        mode=request.mode,
        query=request.query,
        provider_keys=(request.provider_key,) if request.provider_key else (),
        suppressed_mcp=suppressed_mcp,
        total_tool_count=total_tool_count,
        servers=servers,
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
        provider = runtime_mcp_provider_from_tool_name(tool_name)
        if self.restrict_social_tools and provider in SOCIAL_MCP_PROVIDERS:
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
    """Search every authorized tool; query text may rank but never gate it."""
    return RuntimeToolSearchScope(active_provider_scores={})
