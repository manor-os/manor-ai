from __future__ import annotations

import re
from typing import Iterable

_EXTERNAL_PLATFORM_ALIASES = (
    "xiaohongshu", "xhs", "rednote", "red note", "小红书",
    "linkedin", "linked in", "领英",
    "twitter", "x.com", "tweet", "推特",
    "facebook", "instagram", "ig",
    "wechat", "weixin", "微信", "公众号",
    "telegram", "whatsapp", "tiktok", "douyin", "抖音", "微博",
)
_EXTERNAL_ACTION_TERMS = (
    "publish", "send", "share", "comment", "like", "reply", "upload",
    "save to draft", "save draft",
    "发布", "发到", "发在", "发送", "发帖", "评论", "点赞", "转发", "上传",
    "保存到", "保存至", "存到", "存入",
)
_EXTERNAL_DRAFT_TERMS = (
    "draft", "caption", "copy", "creative", "image", "cover", "visual",
    "文案", "配图", "封面", "图片", "素材", "草稿", "写一篇", "写个", "生成",
)
_LOCAL_CODING_SKILL_SLUGS = {
    "local-coding-operations",
    "local_coding_operations",
}
_LOCAL_CODING_PROVIDER_ORDER = ("codex_cli", "claude_code", "gemini_cli", "aider", "cursor")
_LOCAL_CODING_PROVIDER_HINTS: dict[str, tuple[str, ...]] = {
    "claude_code": ("claude code", "claude_code", "claude cli", "本地claude", "本地 claude"),
    "codex_cli": ("codex cli", "codex_cli", "codex", "openai codex", "本地codex", "本地 codex"),
    "gemini_cli": ("gemini cli", "gemini_cli", "本地gemini", "本地 gemini"),
    "aider": ("aider", "本地aider", "本地 aider"),
    "cursor": ("cursor", "cursor cli", "本地cursor", "本地 cursor"),
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
    r"(?:~?/|/users/|downloads/|desktop/|documents/|[a-z0-9_.-]+/[a-z0-9_.-]+)",
    re.IGNORECASE,
)
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


def explicit_skill_reference(active_user_message: str | None, skill: str) -> bool:
    """Return True when the user explicitly named the requested skill."""
    if not active_user_message or not skill:
        return False
    text = active_user_message.lower()
    return any(variant and variant in text for variant in _skill_variants(skill))


def external_platform_action_intent(text: str | None) -> bool:
    if not text or runtime_approval_resume_intent(text):
        return False
    lowered = text.lower()
    has_platform = any(alias in lowered for alias in _EXTERNAL_PLATFORM_ALIASES)
    has_action = (
        any(term in lowered for term in _EXTERNAL_ACTION_TERMS)
        or bool(_ENGLISH_POST_TO_PLATFORM_RE.search(lowered))
        or bool(_CHINESE_EXTERNAL_ACTION_RE.search(text))
    )
    return has_platform and has_action


def external_platform_draft_intent(text: str | None) -> bool:
    if not text or external_platform_action_intent(text):
        return False
    lowered = text.lower()
    has_platform = any(alias in lowered for alias in _EXTERNAL_PLATFORM_ALIASES)
    has_draft_term = any(term in lowered for term in _EXTERNAL_DRAFT_TERMS)
    return has_platform and has_draft_term


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
    has_path = bool(_LOCAL_CODING_PATH_RE.search(lowered))
    has_code_file = bool(_LOCAL_CODE_FILE_RE.search(text))
    return (has_local_hint or has_path) and (has_code_file or has_path)


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
    if external_platform_action_intent(active_user_message):
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
        (runtime_skill_relevance_score(skill, active_user_message), index, skill)
        for index, skill in enumerate(items)
    ]
    if not any(score > 0 for score, _, _ in scored):
        return items
    return [skill for _, _, skill in sorted(scored, key=lambda item: (-item[0], item[1]))]


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
    return rank_skills_for_runtime_turn(
        items,
        active_user_message=active_user_message,
    )
