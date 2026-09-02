from __future__ import annotations

import re
from typing import Iterable

from packages.core.ai.runtime.chrome_routing import detect_chrome_local_browser_route
from packages.core.ai.runtime.integration_skill_registry import (
    IntegrationSkillRoute,
    integration_skill_route_for_message,
)
from packages.core.ai.runtime.skill_invocation_policy import (
    retain_required_skill_invocation_policies,
    trusted_skill_invocation_policy,
)

_EXTERNAL_PLATFORM_ALIASES = (
    "xiaohongshu", "xhs", "rednote", "red note", "小红书",
    "linkedin", "linked in", "领英",
    "twitter", "x.com", "tweet", "推特",
    "youtube", "you tube", "youtu.be", "youtube.com", "yt", "油管",
    "facebook", "instagram", "ig", "reddit",
    "wechat", "weixin", "微信", "公众号",
    "telegram", "whatsapp", "tiktok", "douyin", "抖音", "微博",
)
_YOUTUBE_PLATFORM_ALIASES = ("youtube", "you tube", "youtu.be", "youtube.com", "yt", "油管")
_LINKEDIN_PLATFORM_ALIASES = ("linkedin", "linked in", "领英")
_SOCIAL_CHROME_PLATFORM_ALIASES = (
    "xiaohongshu", "xhs", "rednote", "red note", "小红书",
    "twitter", "x.com", "tweet", "推特",
    "facebook", "instagram", "ig", "reddit",
    "tiktok", "douyin", "抖音", "微博",
)
_PLATFORM_READ_OPERATION_TERMS = (
    "search", "find", "read", "browse", "view", "inspect", "research", "compare",
    "list", "show", "summarize", "recent", "details", "subscriber", "metrics", "stats",
    "insights", "analytics", "timeline", "feed", "profile",
    "搜索", "搜寻", "查找", "读取", "浏览", "查看", "研究", "对比", "比较", "列出", "展示",
    "总结", "详情", "订阅者", "数据", "统计", "洞察", "时间线", "动态", "主页", "资料",
)
_EXTERNAL_ACTION_TERMS = (
    "publish", "send", "share", "comment", "like", "reply", "react to", "upload",
    "follow", "retweet", "repost",
    "save to draft", "save draft", "draft box", "draftbox",
    "发布", "发到", "发在", "发送", "发帖", "评论", "点赞", "关注", "转发", "上传",
    "保存到", "保存至", "存到", "存入", "草稿箱",
)
_EXTERNAL_DRAFT_TERMS = (
    "draft", "caption", "copy", "creative", "image", "cover", "visual",
    "文案", "配图", "封面", "图片", "素材", "草稿", "写一篇", "写个", "生成",
)
_INTEGRATION_OPERATION_TERMS = (
    "use", "run", "create", "get", "fetch", "send", "post", "publish", "upload",
    "download", "update", "edit", "delete", "remove", "add", "list", "search",
    "find", "read", "view", "inspect", "research", "compare", "sync", "import",
    "export", "connect", "call", "message", "generate", "invoice", "charge",
    "refund", "pay", "schedule", "book", "save", "retrieve", "analyze", "query",
    "check", "review", "fix", "refactor", "test", "comment", "like", "follow", "share",
    "使用", "运行", "创建", "获取", "拉取", "发送", "发帖", "发布", "上传", "下载",
    "更新", "编辑", "删除", "移除", "添加", "列出", "搜索", "查找", "读取", "查看",
    "检查", "研究", "对比", "比较", "同步", "导入", "导出", "连接", "调用", "消息",
    "生成", "账单", "付款", "退款", "支付", "安排", "预订", "保存", "查询", "分析",
    "修复", "重构", "测试", "评论", "点赞", "关注", "分享",
)
_LOCAL_CODING_SKILL_SLUGS = {
    "local-coding-operations",
    "local_coding_operations",
    "platform-development",
    "platform_development",
    "mcp-aider",
    "mcp-claude-code",
    "mcp-codex-cli",
    "mcp-continue-cli",
    "mcp-cursor-cli",
    "mcp-gemini-cli",
}
_PRESENTATION_SKILL_SLUGS = {
    "pptx",
    "presentation",
    "presentations",
    "slides",
}
_PRESENTATION_ARTIFACT_RE = re.compile(
    r"(?:\bpowerpoint\b|(?<![a-z0-9])pptx?(?![a-z0-9])|"
    r"\bslide\s+deck\b|\bpresentation\s+deck\b|\bpresentations?\b|"
    r"\bslides?\b|演示文稿|幻灯片|演示模板|演示模版)",
    re.IGNORECASE,
)
_PRESENTATION_ARTIFACT_ACTION_RE = re.compile(
    r"(?:\b(?:create|make|generate|build|design|produce|prepare|write|edit|"
    r"revise|update|remix|improve|refine|export|save|render|open|review|"
    r"inspect|read|compare|repair|fix|resume|continue|validate|checkpoint)\w*\b|"
    r"创建|生成|制作|设计|做一套|做一份|编辑|修改|调整|优化|重做|导出|保存|"
    r"打开|查看|检查|比较|对比|修复|继续|续跑|恢复|验收)",
    re.IGNORECASE,
)
_PRESENTATION_STAGE_RE = re.compile(
    r"(?:"
    r"\bp\d{2}\b[^.\n]{0,160}\bcheckpoint\b|"
    r"\bcheckpoint\b[^.\n]{0,160}\bp\d{2}\b|"
    r"(?:^|[/\\])slide[_-]\d{2}\.svg\b|"
    r"/svg_output/[^\s]+\.svg\b|"
    r"\b(?:native_charts|layout_plan)\.json\b"
    r")",
    re.IGNORECASE,
)
_PRESENTATION_SANDBOX_RESUME_RE = re.compile(
    r"(?:/skill/projects/|pptx_pipeline\.py\s+(?:resume|run|checkpoint|"
    r"revalidate-checkpoints)|\bsandbox\s+[a-z0-9-]{6,})",
    re.IGNORECASE,
)
_PRESENTATION_FRESH_CREATION_RE = re.compile(
    r"(?:\b(?:create|make|generate|build|design|produce|prepare)\w*\b|"
    r"创建|生成|制作|设计|做一套|做一份)",
    re.IGNORECASE,
)
_PRESENTATION_EXPLICIT_FRESH_PROJECT_RE = re.compile(
    r"(?:"
    r"\b(?:new\s+fresh|fresh\s+new|brand[- ]new)\b[^.\n]{0,80}"
    r"\b(?:project|deck|presentation|powerpoint|pptx|slides?)\b|"
    r"\b(?:project|deck|presentation|powerpoint|pptx|slides?)\b[^.\n]{0,80}"
    r"\b(?:new\s+fresh|fresh\s+new|brand[- ]new)\b|"
    r"(?:全新|重新新建)[^。\n]{0,40}(?:项目|演示文稿|幻灯片|PPTX?|pptx?)"
    r")",
    re.IGNORECASE,
)
_PRESENTATION_DELIVERY_ACTION_RE = re.compile(
    r"(?:\b(?:create|make|generate|build|design|produce|prepare|write|edit|"
    r"revise|update|remix|improve|refine|export|save|render|repair|fix|"
    r"resume|continue|validate)\w*\b|创建|生成|制作|设计|做一套|做一份|编辑|"
    r"修改|调整|优化|重做|导出|保存|修复|继续|续跑|恢复|验收)",
    re.IGNORECASE,
)
_CHROME_SKILL_SLUGS = {"chrome"}
# The marketplace publisher is a browser-capable YouTube route, not a writing
# skill. Keep both persisted spellings alongside the platform umbrella so the
# runtime can retain it as a safe parallel handoff candidate.
_YOUTUBE_PLATFORM_SKILL_SLUGS = {
    "platform-youtube",
    "platform_youtube",
    "youtube-studio-publisher",
    "youtube_studio_publisher",
}
_YOUTUBE_MCP_SKILL_SLUGS = {"mcp-youtube", "mcp_youtube", "youtube"}
_YOUTUBE_ROUTE_SKILL_SLUGS = _YOUTUBE_PLATFORM_SKILL_SLUGS | _YOUTUBE_MCP_SKILL_SLUGS
_LINKEDIN_PLATFORM_SKILL_SLUGS = {"platform-linkedin", "platform_linkedin"}
_LINKEDIN_MCP_SKILL_SLUGS = {"mcp-linkedin", "mcp_linkedin", "linkedin"}
_LINKEDIN_ROUTE_SKILL_SLUGS = _LINKEDIN_PLATFORM_SKILL_SLUGS | _LINKEDIN_MCP_SKILL_SLUGS
_SOCIAL_PLATFORM_SKILL_SLUGS = {"platform-social", "platform_social"}
_LINKEDIN_BROWSER_ACTION_TERMS = (
    "search", "find", "look up", "lookup", "open", "read", "view", "research", "compare",
    "搜索", "搜寻", "查找", "打开", "查看", "读取", "研究", "对比", "比较",
)
_LINKEDIN_BROWSER_OBJECT_TERMS = (
    "people", "person", "profile", "profiles", "company", "companies", "job", "jobs",
    "candidate", "candidates", "recruiter", "recruiters", "network",
    "人员", "人选", "人才", "个人资料", "档案", "主页", "公司", "企业", "职位", "工作", "招聘", "人脉",
)
_LINKEDIN_NETWORK_ACTION_TERMS = (
    "connect", "connection", "connection request", "invite", "invitation", "follow", "message", "dm",
    "加人", "连接", "好友", "邀请", "关注", "消息", "私信",
)
_LOCAL_CODING_PROVIDER_ORDER = (
    "codex_cli",
    "claude_code",
    "gemini_cli",
    "aider",
    "cursor",
    "continue_cli",
)
_LOCAL_CODING_PROVIDER_HINTS: dict[str, tuple[str, ...]] = {
    "claude_code": ("claude code", "claude_code", "claude cli", "本地claude", "本地 claude"),
    "codex_cli": ("codex cli", "codex_cli", "codex", "openai codex", "本地codex", "本地 codex"),
    "gemini_cli": ("gemini cli", "gemini_cli", "本地gemini", "本地 gemini"),
    "aider": ("aider", "本地aider", "本地 aider"),
    "cursor": ("cursor", "cursor cli", "本地cursor", "本地 cursor"),
    "continue_cli": (
        "continue cli",
        "continue_cli",
        "continue.dev",
        "本地continue",
        "本地 continue",
    ),
}
_LOCAL_CODING_PROVIDER_TERMS = (
    "codex cli", "codex_cli", "codex", "claude code", "claude_code",
    "coding cli", "code cli", "编程cli", "编程 cli", "本地codex", "本地 codex",
    "本地claude", "本地 claude",
)
_LOCAL_CODING_ACTION_TERMS = (
    "edit", "append", "modify", "change", "review", "refactor", "fix", "test",
    "写入", "追加", "加上", "修改", "改", "编辑", "审查", "评审", "重构", "修复", "测试",
)
_LOCAL_CODING_HINT_TERMS = (
    "local repo", "local repository", "local project", "local directory",
    "本地项目", "本地目录", "本地仓库", "本地文件", "项目目录", "代码仓库",
)
_LOCAL_CODING_PATH_RE = re.compile(
    r"(?:~/|(?:^|[\s(\"'`])/(?:[a-z0-9_.-]+/)+[a-z0-9_.-]+|"
    r"downloads/|desktop/|documents/|(?:\./|\.\./)|[a-z]:[\\/])",
    re.IGNORECASE,
)
_REMOTE_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_KNOWLEDGE_PATH_RE = re.compile(r"\bknowledge(?:[/\\][^\s]*)?", re.IGNORECASE)
_LOCAL_CODE_FILE_RE = re.compile(
    r"\.(?:md|go|py|js|jsx|ts|tsx|json|yaml|yml|toml|rs|java|kt|swift|rb|php|"
    r"c|cc|cpp|h|hpp|css|scss|html|vue|svelte|sql|sh)\b",
    re.IGNORECASE,
)
_ENGLISH_POST_TO_PLATFORM_RE = re.compile(
    r"\bpost(?:ing)?\b.{0,40}\b(?:to|on|onto|in)\b"
)
_CHINESE_EXTERNAL_ACTION_RE = re.compile(
    r"(?:发布|发到|发在|发送|发帖|上传|评论|点赞|转发|回复|"
    r"发(?!现|生|明|起|热|酵)(?:一|个|条)?)"
    r".{0,12}(?:小红书|微博|抖音|微信|公众号|领英|推特|脸书)"
)
_RUNTIME_APPROVAL_APPROVED_PREFIX = "[Runtime approval approved]"
_SKILL_RELEVANCE_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]*|[\u4e00-\u9fff]+", re.IGNORECASE)


def runtime_approval_resume_intent(text: str | None) -> bool:
    return bool(text and text.strip().startswith(_RUNTIME_APPROVAL_APPROVED_PREFIX))


def _skill_variants(slug: str | None, name: str | None = None) -> set[str]:
    values = {str(v).strip().lower() for v in (slug, name) if str(v or "").strip()}
    variants = set(values)
    for value in values:
        variants.add(value.replace("_", "-"))
        variants.add(value.replace("-", "_"))
        variants.add(value.replace(" ", "-"))
        variants.add(value.replace(" ", "_"))
    return variants


def is_local_coding_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_LOCAL_CODING_SKILL_SLUGS))


def is_presentation_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_PRESENTATION_SKILL_SLUGS))


def is_chrome_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_CHROME_SKILL_SLUGS))


def is_youtube_platform_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_YOUTUBE_PLATFORM_SKILL_SLUGS))


def is_youtube_mcp_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_YOUTUBE_MCP_SKILL_SLUGS))


def is_youtube_route_skill(slug: str | None, name: str | None = None) -> bool:
    variants = _skill_variants(slug, name)
    return bool(variants.intersection(_YOUTUBE_ROUTE_SKILL_SLUGS | _CHROME_SKILL_SLUGS))


def is_linkedin_platform_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_LINKEDIN_PLATFORM_SKILL_SLUGS))


def is_linkedin_mcp_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_LINKEDIN_MCP_SKILL_SLUGS))


def is_linkedin_route_skill(slug: str | None, name: str | None = None) -> bool:
    variants = _skill_variants(slug, name)
    return bool(
        variants.intersection(
            _LINKEDIN_ROUTE_SKILL_SLUGS | _CHROME_SKILL_SLUGS
        )
    )


def is_social_platform_skill(slug: str | None, name: str | None = None) -> bool:
    return bool(_skill_variants(slug, name).intersection(_SOCIAL_PLATFORM_SKILL_SLUGS))


def is_social_platform_mcp_skill(
    active_user_message: str | None,
    slug: str | None,
    name: str | None = None,
) -> bool:
    route = integration_skill_route_for_message(active_user_message)
    if not route or route.parent_skill != "platform-social":
        return False
    variants = _skill_variants(slug, name)
    child_variants = _skill_variants(route.child_skill, route.child_skill)
    return route.child_skill != "chrome" and bool(variants.intersection(child_variants))


def is_social_platform_route_skill(
    active_user_message: str | None,
    slug: str | None,
    name: str | None = None,
) -> bool:
    return bool(
        is_chrome_skill(slug, name)
        or is_social_platform_skill(slug, name)
        or is_social_platform_mcp_skill(active_user_message, slug, name)
    )


def is_integration_parent_skill(
    route: IntegrationSkillRoute | None,
    slug: str | None,
    name: str | None = None,
) -> bool:
    if route is None or not route.parent_skill:
        return False
    return bool(
        _skill_variants(slug, name).intersection(
            _skill_variants(route.parent_skill, route.parent_skill)
        )
    )


def is_integration_child_skill(
    route: IntegrationSkillRoute | None,
    slug: str | None,
    name: str | None = None,
) -> bool:
    if route is None:
        return False
    expected = (route.child_skill, *route.alternate_child_skills)
    expected_variants = set().union(
        *(_skill_variants(child, child) for child in expected)
    )
    return bool(_skill_variants(slug, name).intersection(expected_variants))


def is_named_integration_route_skill(
    active_user_message: str | None,
    slug: str | None,
    name: str | None = None,
) -> bool:
    route = integration_skill_route_for_message(active_user_message)
    return bool(
        is_integration_parent_skill(route, slug, name)
        or is_integration_child_skill(route, slug, name)
        or (
            route
            and route.parent_skill == "platform-youtube"
            and is_youtube_route_skill(slug, name)
        )
        or (route and route.chrome_fallback and is_chrome_skill(slug, name))
    )


def explicit_skill_reference(active_user_message: str | None, skill: str) -> bool:
    """Return True when the user explicitly named the requested skill."""
    if not active_user_message or not skill:
        return False
    text = active_user_message.lower()
    return any(variant and variant in text for variant in _skill_variants(skill))


def _contains_platform_alias(text: str, aliases: Iterable[str]) -> bool:
    lowered = text.lower()
    for alias in aliases:
        normalized = alias.lower()
        if re.search(r"[a-z0-9]", normalized):
            pattern = rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])"
            if re.search(pattern, lowered):
                return True
        elif normalized in lowered:
            return True
    return False


def external_platform_action_intent(text: str | None) -> bool:
    if not text or runtime_approval_resume_intent(text):
        return False
    lowered = text.lower()
    has_platform = _contains_platform_alias(lowered, _EXTERNAL_PLATFORM_ALIASES)
    has_action = (
        any(term in lowered for term in _EXTERNAL_ACTION_TERMS)
        or bool(_ENGLISH_POST_TO_PLATFORM_RE.search(lowered))
        or bool(_CHINESE_EXTERNAL_ACTION_RE.search(text))
    )
    return has_platform and has_action


def youtube_platform_action_intent(text: str | None) -> bool:
    if not text or runtime_approval_resume_intent(text):
        return False
    lowered = text.lower()
    has_platform = _contains_platform_alias(lowered, _YOUTUBE_PLATFORM_ALIASES)
    if not has_platform:
        return False
    if external_platform_draft_intent(text):
        return False
    return (
        any(term in lowered for term in _EXTERNAL_ACTION_TERMS)
        or _contains_platform_alias(lowered, _PLATFORM_READ_OPERATION_TERMS)
        or bool(_ENGLISH_POST_TO_PLATFORM_RE.search(lowered))
    )


def external_platform_draft_intent(text: str | None) -> bool:
    if not text or external_platform_action_intent(text):
        return False
    lowered = text.lower()
    has_platform = _contains_platform_alias(lowered, _EXTERNAL_PLATFORM_ALIASES)
    has_draft_term = any(term in lowered for term in _EXTERNAL_DRAFT_TERMS)
    return has_platform and has_draft_term


def linkedin_networking_operation_intent(text: str | None) -> bool:
    """Detect LinkedIn people research, connection, follow, and messaging work."""
    if not text or runtime_approval_resume_intent(text):
        return False
    lowered = text.lower()
    if not _contains_platform_alias(lowered, _LINKEDIN_PLATFORM_ALIASES):
        return False
    if external_platform_draft_intent(text):
        return False
    has_browser_action = _contains_platform_alias(
        lowered, _LINKEDIN_BROWSER_ACTION_TERMS
    )
    has_browser_object = _contains_platform_alias(
        lowered, _LINKEDIN_BROWSER_OBJECT_TERMS
    )
    has_network_action = _contains_platform_alias(
        lowered, _LINKEDIN_NETWORK_ACTION_TERMS
    )
    return (has_browser_action and has_browser_object) or has_network_action


def linkedin_content_action_intent(text: str | None) -> bool:
    """Detect LinkedIn publishing and engagement, excluding networking actions."""
    if not text or not external_platform_action_intent(text):
        return False
    return bool(
        _contains_platform_alias(text.lower(), _LINKEDIN_PLATFORM_ALIASES)
        and not linkedin_networking_operation_intent(text)
    )


def linkedin_platform_operation_intent(text: str | None) -> bool:
    """Detect LinkedIn API actions and browser-only research/networking work."""
    return bool(
        linkedin_content_action_intent(text)
        or linkedin_networking_operation_intent(text)
    )


def social_platform_action_intent(text: str | None) -> bool:
    """Detect social reads or actions that may use a platform MCP or Chrome."""
    if not text or runtime_approval_resume_intent(text):
        return False
    lowered = text.lower()
    if not _contains_platform_alias(lowered, _SOCIAL_CHROME_PLATFORM_ALIASES):
        return False
    has_read_operation = _contains_platform_alias(
        lowered,
        _PLATFORM_READ_OPERATION_TERMS,
    )
    if external_platform_draft_intent(text) and not has_read_operation:
        return False
    return bool(
        external_platform_action_intent(text)
        or has_read_operation
    )


def named_integration_operation_intent(text: str | None) -> bool:
    if not text or runtime_approval_resume_intent(text):
        return False
    route = integration_skill_route_for_message(text)
    if route is None:
        return False
    lowered = text.lower()
    if route.parent_skill in {
        "platform-linkedin",
        "platform-social",
        "platform-youtube",
    }:
        return bool(
            external_platform_action_intent(text)
            or _contains_platform_alias(lowered, _PLATFORM_READ_OPERATION_TERMS)
        )
    return _contains_platform_alias(lowered, _INTEGRATION_OPERATION_TERMS)


def local_coding_cli_intent(text: str | None) -> bool:
    if not text or runtime_approval_resume_intent(text):
        return False
    lowered = text.lower()
    has_provider = any(term in lowered for term in _LOCAL_CODING_PROVIDER_TERMS)
    if has_provider:
        return True
    has_action = any(term in lowered for term in _LOCAL_CODING_ACTION_TERMS) or any(
        term in text for term in _LOCAL_CODING_ACTION_TERMS
    )
    if not has_action:
        return False
    has_local_hint = (
        "本地" in text
        or "local" in lowered
        or any(term in lowered for term in _LOCAL_CODING_HINT_TERMS)
    )
    path_candidate = _REMOTE_URL_RE.sub(" ", lowered)
    path_candidate = _KNOWLEDGE_PATH_RE.sub(" ", path_candidate)
    has_path = bool(_LOCAL_CODING_PATH_RE.search(path_candidate))
    has_code_file = bool(_LOCAL_CODE_FILE_RE.search(text))
    return (has_local_hint or has_path) and (has_code_file or has_path)


def presentation_artifact_intent(text: str | None) -> bool:
    """Return whether the latest turn requests a PowerPoint artifact workflow.

    Generic ranking is intentionally insufficient here: deck topics frequently
    contain words such as "platform" or "development", which can otherwise
    outrank the built-in PPTX workflow. Keep explicit local coding requests on
    their dedicated route, but deterministically recognize ordinary create/edit
    language for PowerPoint, PPTX, presentations, slides, and slide decks.
    """

    if not text or runtime_approval_resume_intent(text):
        return False
    stage_resume = bool(_PRESENTATION_STAGE_RE.search(text))
    if (
        local_coding_cli_intent(text)
        and not _PRESENTATION_SANDBOX_RESUME_RE.search(text)
        and not stage_resume
    ):
        return False
    return bool(
        (_PRESENTATION_ARTIFACT_RE.search(text) or stage_resume)
        and _PRESENTATION_ARTIFACT_ACTION_RE.search(text)
    )


def presentation_delivery_intent(text: str | None) -> bool:
    """Return whether the turn asks to create or modify a deliverable deck."""

    if not presentation_artifact_intent(text):
        return False
    return bool(_PRESENTATION_DELIVERY_ACTION_RE.search(str(text)))


def presentation_fresh_creation_intent(text: str | None) -> bool:
    """Return True when a turn starts a new deck rather than resuming one."""

    if not presentation_artifact_intent(text):
        return False
    value = str(text or "")
    # Strong fresh-project wording is authoritative even when the same sentence
    # says "do not reuse or edit the prior project".  Treating the negated
    # word "edit" as a resume request caused a delivered deck checkpoint to be
    # restored into an explicitly fresh benchmark run.
    if (
        _PRESENTATION_EXPLICIT_FRESH_PROJECT_RE.search(value)
        and not _PRESENTATION_SANDBOX_RESUME_RE.search(value)
    ):
        return True
    return bool(
        _PRESENTATION_FRESH_CREATION_RE.search(value)
        and not _PRESENTATION_SANDBOX_RESUME_RE.search(value)
        and not re.search(
            r"(?:\b(?:resume|continue|repair|fix|revise|update)\w*\b|"
            r"\bedit(?:ed|ing)?\b|"
            r"继续|续跑|恢复|修复|修改|编辑)",
            value,
            re.IGNORECASE,
        )
    )


def local_coding_provider_route(text: str | None) -> tuple[str, ...]:
    """Return local coding providers that match the active user request."""
    if not text or runtime_approval_resume_intent(text):
        return ()
    lowered = text.lower()
    explicit: list[str] = []
    for provider in _LOCAL_CODING_PROVIDER_ORDER:
        if any(hint in lowered for hint in _LOCAL_CODING_PROVIDER_HINTS.get(provider, ())):
            explicit.append(provider)
    if explicit:
        return tuple(explicit)
    if local_coding_cli_intent(text):
        return ("codex_cli", "claude_code")
    return ()


def should_route_external_action_to_integration(
    *,
    active_user_message: str | None,
    skill: str,
    manual_skill_selected: bool,
) -> bool:
    """Return True when an accidental skill route should yield to integrations."""
    if manual_skill_selected or explicit_skill_reference(active_user_message, skill):
        return False
    if named_integration_operation_intent(active_user_message):
        return not is_named_integration_route_skill(
            active_user_message,
            skill,
            skill,
        )
    if youtube_platform_action_intent(active_user_message) and is_youtube_route_skill(skill):
        return False
    if linkedin_platform_operation_intent(active_user_message):
        return not is_linkedin_route_skill(skill, skill)
    if social_platform_action_intent(active_user_message):
        return not is_social_platform_route_skill(active_user_message, skill, skill)
    if external_platform_action_intent(active_user_message):
        if youtube_platform_action_intent(active_user_message):
            return not (
                is_youtube_platform_skill(skill, skill)
                or is_youtube_mcp_skill(skill, skill)
                or is_chrome_skill(skill, skill)
            )
        return True
    return False


def skill_slug_and_name(skill) -> tuple[str, str]:
    slug = str(getattr(skill, "slug", "") or getattr(skill, "name", "") or "")
    name = str(
        getattr(skill, "name", "")
        or getattr(skill, "display_name", "")
        or getattr(skill, "slug", "")
        or ""
    )
    display = str(getattr(skill, "display_name", "") or "")
    return slug, display or name


def _skill_text_values(skill) -> tuple[str, ...]:
    values: list[str] = []
    for attr in (
        "slug",
        "name",
        "display_name",
        "description",
        "category",
        "output_format",
    ):
        raw = getattr(skill, attr, None)
        if raw:
            values.append(str(raw))
    raw_tags = getattr(skill, "tags", None) or []
    if isinstance(raw_tags, str):
        values.append(raw_tags)
    else:
        values.extend(str(tag) for tag in raw_tags if str(tag or "").strip())
    metadata = getattr(skill, "metadata", None) or {}
    if isinstance(metadata, dict):
        for key in ("category", "output_format", "tags"):
            raw = metadata.get(key)
            if isinstance(raw, (list, tuple, set)):
                values.extend(str(item) for item in raw if str(item or "").strip())
            elif raw:
                values.append(str(raw))
    return tuple(value.strip() for value in values if value and value.strip())


def _skill_relevance_terms(text: str | None) -> set[str]:
    if not text:
        return set()
    terms: set[str] = set()
    for match in _SKILL_RELEVANCE_TOKEN_RE.finditer(text.lower()):
        token = match.group(0).strip("._+-")
        if not token:
            continue
        terms.add(token)
        for part in re.split(r"[-_./+]+", token):
            if part:
                terms.add(part)
    return terms


def runtime_skill_relevance_score(skill, active_user_message: str | None) -> int:
    """Score a skill against the latest user request using only catalog text."""

    query = str(active_user_message or "").strip().lower()
    if not query:
        return 0
    slug, name = skill_slug_and_name(skill)
    variants = _skill_variants(slug, name)
    values = _skill_text_values(skill)
    searchable = " \n".join(values).lower()
    score = 0

    for variant in variants:
        if variant and variant in query:
            score += 120

    for term in _skill_relevance_terms(query):
        if len(term) < 2:
            continue
        if term in variants:
            score += 80
        elif term in searchable:
            score += 20 + min(len(term), 20)

    return score


def rank_skills_for_runtime_turn(
    skills: Iterable,
    *,
    active_user_message: str | None,
) -> list:
    """Rank visible skill descriptors without product/domain-specific mappings."""

    items = list(skills or [])
    scored = [
        (
            trusted_skill_invocation_policy(skill) is not None,
            runtime_skill_relevance_score(skill, active_user_message),
            index,
            skill,
        )
        for index, skill in enumerate(items)
    ]
    if not any(pinned or score > 0 for pinned, score, _, _ in scored):
        return items
    return [
        skill
        for _, _, _, skill in sorted(
            scored,
            key=lambda item: (-int(item[0]), -item[1], item[2]),
        )
    ]


def filter_skills_for_runtime_turn(
    skills: Iterable,
    *,
    active_user_message: str | None,
    manual_skill_selected: bool = False,
) -> list:
    """Filter accidental skill candidates using runtime turn intent."""
    items = list(skills or [])
    if manual_skill_selected:
        return items
    if presentation_artifact_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_presentation_skill(*skill_slug_and_name(skill))
        ]
        return retain_required_skill_invocation_policies(items, selected)
    if detect_chrome_local_browser_route(active_user_message):
        selected = [
            skill for skill in items
            if is_chrome_skill(*skill_slug_and_name(skill))
            or (
                youtube_platform_action_intent(active_user_message)
                and (
                    is_youtube_platform_skill(*skill_slug_and_name(skill))
                    or is_youtube_mcp_skill(*skill_slug_and_name(skill))
                )
            )
            or (
                linkedin_platform_operation_intent(active_user_message)
                and (
                    is_linkedin_platform_skill(*skill_slug_and_name(skill))
                    or is_linkedin_mcp_skill(*skill_slug_and_name(skill))
                )
            )
            or (
                social_platform_action_intent(active_user_message)
                and (
                    is_social_platform_skill(*skill_slug_and_name(skill))
                    or is_social_platform_mcp_skill(
                        active_user_message, *skill_slug_and_name(skill)
                    )
                )
            )
            or (
                named_integration_operation_intent(active_user_message)
                and (
                    is_integration_parent_skill(
                        integration_skill_route_for_message(active_user_message),
                        *skill_slug_and_name(skill),
                    )
                    or is_integration_child_skill(
                        integration_skill_route_for_message(active_user_message),
                        *skill_slug_and_name(skill),
                    )
                )
            )
        ]
        return retain_required_skill_invocation_policies(items, selected)
    if youtube_platform_action_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_youtube_platform_skill(*skill_slug_and_name(skill))
            or is_youtube_mcp_skill(*skill_slug_and_name(skill))
            or is_chrome_skill(*skill_slug_and_name(skill))
        ]
        return retain_required_skill_invocation_policies(items, selected)
    if linkedin_platform_operation_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_linkedin_platform_skill(*skill_slug_and_name(skill))
            or is_linkedin_mcp_skill(*skill_slug_and_name(skill))
            or is_chrome_skill(*skill_slug_and_name(skill))
        ]
        return retain_required_skill_invocation_policies(items, selected)
    if social_platform_action_intent(active_user_message):
        selected = [
            skill
            for skill in items
            if is_social_platform_route_skill(
                active_user_message, *skill_slug_and_name(skill)
            )
        ]
        # A social publish request can still need a content skill to prepare
        # the draft when this workspace has no platform route installed. The
        # invoke boundary separately blocks accidental external publishing,
        # so do not hide all ordinary writing candidates at discovery time.
        if not selected:
            return items
        return retain_required_skill_invocation_policies(items, selected)
    if local_coding_cli_intent(active_user_message):
        # Keep the local coding umbrella together with connected MCP guidance
        # packs.  The renderer applies the per-pack availability gate, so an
        # unconnected provider is still omitted without hiding a connected
        # provider that can assist the coding task.
        selected = [
            skill
            for skill in items
            if is_local_coding_skill(*skill_slug_and_name(skill))
            or str(skill_slug_and_name(skill)[0] or "").startswith(("mcp_", "mcp-"))
        ]
        return retain_required_skill_invocation_policies(items, selected)
    if named_integration_operation_intent(active_user_message):
        route = integration_skill_route_for_message(active_user_message)
        selected = [
            skill
            for skill in items
            if is_integration_parent_skill(route, *skill_slug_and_name(skill))
            or is_integration_child_skill(route, *skill_slug_and_name(skill))
            or (
                route is not None
                and route.chrome_fallback
                and is_chrome_skill(*skill_slug_and_name(skill))
            )
        ]
        return retain_required_skill_invocation_policies(items, selected)
    return rank_skills_for_runtime_turn(
        items,
        active_user_message=active_user_message,
    )
