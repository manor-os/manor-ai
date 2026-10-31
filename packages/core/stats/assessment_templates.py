"""Ready-made AI-assessed audience metrics a Workspace can track.

Each template becomes an ordinary Workspace stat whose value an agent records
from evidence on a schedule (see ``review_setup.configure_review``). Rubrics
are platform-neutral: the assessing agent reads whichever platforms the
Workspace publishes to through its connected integrations or Chrome.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.core.stats.service import StatError, create_stat

CADENCE_SECONDS = {"daily": 86400, "weekly": 604800}

_EVIDENCE = (
    " Read the current number on the platforms this Workspace publishes to, using its connected integrations "
    "or the Chrome extension. Cite evidence a person can check: a Chrome screenshot of the page showing the number, "
    "or a short evidence note in this Workspace's Knowledge (platform, page URL, the number and when you read it). "
    "If several platforms apply, add them up and "
    "name each platform and its number in the reasoning. When no platform can be read, record insufficient_evidence "
    "with the concrete gap (for example which account is not connected); never record zero for a number you could not read."
)


@dataclass(frozen=True)
class AssessmentTemplate:
    key: str
    names: dict[str, str]
    units: dict[str, str]
    rubric: str

    def name(self, locale: str | None) -> str:
        return self.names.get(_language(locale), self.names["en"])

    def unit(self, locale: str | None) -> str:
        return self.units.get(_language(locale), self.units["en"])


def _language(locale: str | None) -> str:
    return str(locale or "en").split("-")[0].lower()


AUDIENCE_TEMPLATES: tuple[AssessmentTemplate, ...] = (
    AssessmentTemplate(
        key="latest_video_views",
        names={"en": "Latest video views", "zh": "最新视频播放量", "es": "Vistas del último video"},
        units={"en": "views", "zh": "次", "es": "vistas"},
        rubric="Views of the most recently published video." + _EVIDENCE,
    ),
    AssessmentTemplate(
        key="latest_video_comments",
        names={"en": "Latest video comments", "zh": "最新视频评论数", "es": "Comentarios del último video"},
        units={"en": "comments", "zh": "条", "es": "comentarios"},
        rubric="Comments on the most recently published video." + _EVIDENCE,
    ),
    AssessmentTemplate(
        key="latest_video_likes",
        names={"en": "Latest video likes", "zh": "最新视频点赞数", "es": "Me gusta del último video"},
        units={"en": "likes", "zh": "个", "es": "me gusta"},
        rubric="Likes on the most recently published video." + _EVIDENCE,
    ),
    AssessmentTemplate(
        key="followers",
        names={"en": "Followers", "zh": "粉丝 / 订阅数", "es": "Seguidores"},
        units={"en": "followers", "zh": "人", "es": "seguidores"},
        rubric="Followers or subscribers of this Workspace's accounts or channels." + _EVIDENCE,
    ),
    AssessmentTemplate(
        key="active_users",
        names={"en": "Active users (7 days)", "zh": "活跃用户数（7 天）", "es": "Usuarios activos (7 días)"},
        units={"en": "users", "zh": "人", "es": "usuarios"},
        rubric="Users or members active in the last 7 days, as reported by the product, community or analytics "
               "platform this Workspace uses." + _EVIDENCE,
    ),
)
_TEMPLATES_BY_KEY = {template.key: template for template in AUDIENCE_TEMPLATES}


def list_assessment_templates(locale: str | None) -> list[dict[str, Any]]:
    return [{"key": t.key, "name": t.name(locale), "unit": t.unit(locale)} for t in AUDIENCE_TEMPLATES]


async def add_assessed_metrics(
    db: Any, *, workspace: Any, user: Any, template_keys: list[str],
    service_key: str | None = None, cadence: str = "daily",
) -> dict[str, Any]:
    """Create the chosen audience stats and have an agent assess them on a schedule.

    Caller owns the commit. Existing stats with the same key are reused.
    """
    from sqlalchemy import select

    from packages.core.models.workspace import AgentSubscription
    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.stats.review_setup import COUNT_SCALE, _existing_review, configure_review

    unknown = [key for key in template_keys if key not in _TEMPLATES_BY_KEY]
    if not template_keys or unknown:
        raise StatError(f"unknown assessment templates: {', '.join(unknown) or 'none chosen'}")
    if cadence not in CADENCE_SECONDS:
        raise StatError("cadence must be daily or weekly")

    if not service_key:
        existing = await _existing_review(db, workspace)
        if existing is not None:
            producer = next((step.get("config") or {} for step in existing[0].steps or [] if step.get("type") == "agent"), {})
            service_key = str(producer.get("service_key") or "")
        else:
            services = sorted({row.service_key for row in (await db.execute(select(AgentSubscription).where(
                AgentSubscription.entity_id == workspace.entity_id, AgentSubscription.workspace_id == workspace.id,
                AgentSubscription.status == "active",
            ))).scalars() if row.service_key})
            if len(services) != 1:
                raise StatError("choose which service assesses these metrics: " + (", ".join(services) or "no service has an agent"))
            service_key = services[0]

    locale = getattr(user, "locale", None)
    existing_stats = {stat.key: stat for stat in (await db.execute(select(WorkspaceStat).where(
        WorkspaceStat.entity_id == workspace.entity_id, WorkspaceStat.workspace_id == workspace.id,
        WorkspaceStat.key.in_(template_keys),
    ))).scalars()}
    for key in template_keys:
        if key in existing_stats:
            continue
        template = _TEMPLATES_BY_KEY[key]
        await create_stat(
            db, entity_id=workspace.entity_id, workspace_id=workspace.id, key=key,
            name=template.name(locale), unit=template.unit(locale), value_type="number",
            collector_type="manual", origin="user", install_schedule=False,
        )
    configured = await configure_review(
        db, workspace=workspace, user_id=user.id, service_key=service_key,
        rubrics={key: {"rubric": _TEMPLATES_BY_KEY[key].rubric, "scale": COUNT_SCALE} for key in template_keys},
        every_seconds=CADENCE_SECONDS[cadence],
    )
    return {**configured, "service_key": service_key}
