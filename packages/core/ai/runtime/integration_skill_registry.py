"""Internal Integration-to-Skill routing registry.

Every static Integration catalog key has one route owner and one concrete
built-in child Skill. A catalog-only Skill may explain that no executable tool
surface exists, but it must never advertise actions the runtime cannot call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal


RouteStatus = Literal[
    "available",
    "catalog_only",
    "coming_soon",
    "legacy",
    "internal",
    "auxiliary",
    "browser_only",
]


@dataclass(frozen=True)
class IntegrationSkillRoute:
    provider_key: str
    parent_skill: str | None
    child_skill: str
    alternate_child_skills: tuple[str, ...]
    aliases: tuple[str, ...]
    chrome_fallback: bool = False
    status: RouteStatus = "available"


def _route(
    provider_key: str,
    parent_skill: str | None,
    child_skill: str,
    *aliases: str,
    chrome_fallback: bool = False,
    status: RouteStatus = "available",
    alternate_child_skills: tuple[str, ...] = (),
) -> IntegrationSkillRoute:
    canonical_aliases = (
        provider_key,
        provider_key.replace("_", " "),
        child_skill,
        *aliases,
    )
    return IntegrationSkillRoute(
        provider_key=provider_key,
        parent_skill=parent_skill,
        child_skill=child_skill,
        alternate_child_skills=alternate_child_skills,
        aliases=tuple(dict.fromkeys(alias.lower() for alias in canonical_aliases if alias)),
        chrome_fallback=chrome_fallback,
        status=status,
    )


# Keep this set in lockstep with packages.core.services.mcp_seed._MCP_CATALOG.
INTEGRATION_SKILL_ROUTES: dict[str, IntegrationSkillRoute] = {
    route.provider_key: route
    for route in (
        _route("gmail", "platform-communications", "mcp_gmail", "google mail", chrome_fallback=True),
        _route(
            "email",
            "platform-communications",
            "mcp_email",
            "imap",
            "smtp",
            alternate_child_skills=("mcp_gmail", "mcp_outlook"),
        ),
        _route(
            "google_calendar",
            "platform-productivity",
            "mcp_google_calendar",
            "google calendar",
            chrome_fallback=True,
        ),
        _route(
            "manor_mcp_calendar",
            "platform-productivity",
            "mcp_manor_mcp_calendar",
            "manor calendar",
        ),
        _route(
            "manor_mcp_minutes",
            "platform-productivity",
            "mcp_manor_mcp_minutes",
            "manor minutes",
            "meeting minutes",
        ),
        _route(
            "google_drive",
            "platform-productivity",
            "mcp_google_drive",
            "google drive",
            chrome_fallback=True,
        ),
        _route("linkedin", "platform-linkedin", "mcp_linkedin", "linked in", "领英", chrome_fallback=True),
        _route("github", "platform-development", "mcp_github", "git hub", chrome_fallback=True),
        _route(
            "twitter_x",
            "platform-social",
            "mcp_twitter_x",
            "twitter",
            "x.com",
            "tweet",
            "推特",
            chrome_fallback=True,
        ),
        _route("quickbooks", "platform-commerce", "mcp_quickbooks", "quick books", chrome_fallback=True),
        _route("stripe", "platform-commerce", "mcp_stripe", chrome_fallback=True),
        _route("paypal", "platform-commerce", "mcp_paypal", "pay pal", chrome_fallback=True),
        _route(
            "robinhood",
            "platform-market-data",
            "mcp_robinhood",
            "robin hood",
        ),
        _route(
            "slack",
            "platform-communications",
            "mcp_slack",
            chrome_fallback=True,
            status="catalog_only",
        ),
        _route(
            "notion",
            "platform-productivity",
            "mcp_notion",
            chrome_fallback=True,
        ),
        _route(
            "twilio",
            "platform-communications",
            "mcp_twilio",
            chrome_fallback=True,
        ),
        _route(
            "whatsapp",
            "platform-communications",
            "mcp_whatsapp",
            "whats app",
            chrome_fallback=True,
        ),
        _route(
            "webhook",
            "platform-communications",
            "mcp_webhook",
            "web hook",
        ),
        _route(
            "discord",
            "platform-communications",
            "mcp_discord",
            chrome_fallback=True,
        ),
        _route("telegram", "platform-communications", "mcp_telegram", "telegram bot"),
        _route(
            "wechat_personal",
            "platform-communications",
            "mcp_wechat_personal",
            "wechat personal",
            "personal wechat",
            "个人微信",
        ),
        _route(
            "wechat_official",
            "platform-communications",
            "mcp_wechat_official",
            "wechat official",
            "official account",
            "微信公众号",
            "公众号",
            chrome_fallback=True,
        ),
        _route("nango", "platform-productivity", "mcp_nango", "nango apps"),
        _route("replicate", "platform-media-research", "mcp_replicate", chrome_fallback=True),
        _route("elevenlabs", "platform-media-research", "mcp_elevenlabs", "eleven labs", chrome_fallback=True),
        _route("tavily", "platform-media-research", "mcp_tavily", chrome_fallback=True),
        _route(
            "alpaca_market_data",
            "platform-market-data",
            "mcp_alpaca_market_data",
            "alpaca market data",
        ),
        _route(
            "alpha_vantage",
            "platform-market-data",
            "mcp_alpha_vantage",
            "alpha vantage",
        ),
        _route(
            "twelve_data",
            "platform-market-data",
            "mcp_twelve_data",
            "twelve data",
        ),
        _route("jimeng", "platform-media-research", "mcp_jimeng", "即梦", chrome_fallback=True),
        _route(
            "producthunt",
            "platform-media-research",
            "mcp_producthunt",
            "product hunt",
            chrome_fallback=True,
        ),
        _route(
            "facebook",
            "platform-social",
            "mcp_facebook",
            "instagram",
            "facebook pages",
            chrome_fallback=True,
        ),
        _route("youtube", "platform-youtube", "mcp_youtube", "you tube", "油管", chrome_fallback=True),
        _route("tiktok", "platform-social", "mcp_tiktok", "tik tok", chrome_fallback=True),
        _route("shopify", "platform-commerce", "mcp_shopify", chrome_fallback=True),
        _route("woocommerce", "platform-commerce", "mcp_woocommerce", "woo commerce", chrome_fallback=True),
        _route("square", "platform-commerce", "mcp_square", "square payments", chrome_fallback=True),
        _route("tiktok_shop", "platform-commerce", "mcp_tiktok_shop", "tiktok shop", chrome_fallback=True),
        _route("amazon", "platform-commerce", "mcp_amazon", "amazon seller", "amazon sp-api", chrome_fallback=True),
        _route("outlook", "platform-communications", "mcp_outlook", "outlook mail", chrome_fallback=True),
        _route("onedrive", "platform-productivity", "mcp_onedrive", "one drive", chrome_fallback=True),
        _route(
            "ms_calendar",
            "platform-productivity",
            "mcp_ms_calendar",
            "microsoft calendar",
            "outlook calendar",
            chrome_fallback=True,
        ),
        _route("ms_teams", "platform-communications", "mcp_ms_teams", "microsoft teams", "teams chat", chrome_fallback=True),
        _route(
            "ms_excel",
            "platform-productivity",
            "mcp_ms_excel",
            "microsoft excel",
            "excel workbook",
            chrome_fallback=True,
        ),
    )
}


AUXILIARY_INTEGRATION_SKILL_ROUTES: dict[str, IntegrationSkillRoute] = {}


BROWSER_ONLY_PLATFORM_ROUTES: tuple[IntegrationSkillRoute, ...] = (
    _route(
        "douyin",
        "platform-social",
        "chrome",
        "抖音",
        status="browser_only",
    ),
    _route(
        "xiaohongshu",
        "platform-social",
        "chrome",
        "xhs",
        "rednote",
        "red note",
        "小红书",
        status="browser_only",
    ),
    _route("reddit", "platform-social", "chrome", status="browser_only"),
    _route("weibo", "platform-social", "chrome", "微博", status="browser_only"),
)


def _alias_present(text: str, alias: str) -> bool:
    if re.search(r"[a-z0-9]", alias):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text))
    return alias in text


def integration_skill_route_for_message(
    text: str | None,
) -> IntegrationSkillRoute | None:
    """Resolve the most specific named Integration in a user message."""

    lowered = str(text or "").strip().lower()
    if not lowered:
        return None
    routes = (
        *BROWSER_ONLY_PLATFORM_ROUTES,
        *INTEGRATION_SKILL_ROUTES.values(),
        *AUXILIARY_INTEGRATION_SKILL_ROUTES.values(),
    )
    matches = [
        (len(alias), route)
        for route in routes
        for alias in route.aliases
        if _alias_present(lowered, alias)
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: item[0])[1]


def integration_parent_skill_slugs() -> frozenset[str]:
    return frozenset(
        route.parent_skill
        for route in (
            *INTEGRATION_SKILL_ROUTES.values(),
            *AUXILIARY_INTEGRATION_SKILL_ROUTES.values(),
            *BROWSER_ONLY_PLATFORM_ROUTES,
        )
        if route.parent_skill
    )


def _normalized_skill_identifier(value: str | None) -> str:
    return re.sub(r"[-\s]+", "_", str(value or "").strip().lower())


@lru_cache(maxsize=128)
def integration_provider_keys_for_skill(
    slug: str | None,
    name: str | None = None,
) -> tuple[str, ...]:
    """Return static Integration providers owned by one concrete child Skill.

    Parent and alternate Skills are intentionally excluded. A broad platform
    Skill may cover many providers conceptually, but only the concrete child
    Skill is the provider's operational companion.
    """

    candidates = {
        normalized
        for value in (slug, name)
        if (normalized := _normalized_skill_identifier(value))
    }
    if not candidates:
        return ()
    return tuple(
        route.provider_key
        for route in INTEGRATION_SKILL_ROUTES.values()
        if _normalized_skill_identifier(route.child_skill) in candidates
    )
