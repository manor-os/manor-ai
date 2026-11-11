from datetime import UTC, datetime
import json
from zoneinfo import ZoneInfo

import pytest

from packages.core.models.workspace import Workspace
from packages.core.models.workspace_stat import WorkspaceStat
from packages.core.ai.tools.stats_tools import _selected_stats
from packages.core.stats.publication_reporting import (
    compare_publication_stats,
    list_publication_observations,
)
from packages.core.stats.service import collect_stat, create_stat, record_observation

pytestmark = pytest.mark.oss_regression


async def _observe(
    db, stat, *, subject, value, source, observed_at, sample_key, definition="", availability="available"
):
    return await record_observation(
        db,
        stat,
        value=value,
        source=source,
        observed_at=datetime.fromisoformat(observed_at),
        idempotency_key=sample_key,
        evidence={
            "subject": subject,
            "precision": "exact",
            "definition": definition,
            "availability": availability,
            **({"gaps": ["metric_unavailable"]} if value is None else {}),
        },
    )


async def test_stats_tools_enforce_actor_workspace_and_lifecycle_at_entry(db_session, monkeypatch):
    from packages.core.ai.tools.stats_tools import get_tools
    from packages.core.models.base import generate_ulid
    from packages.core.models.user import Entity, User
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services import public_social_metrics

    entity_id, user_id = generate_ulid(), generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="Publication tools")
    user = User(
        id=user_id,
        entity_id=entity_id,
        email=f"{user_id}@test.com",
        password_hash="unused",
        role="member",
        status="active",
    )
    db_session.add_all([Entity(id=entity_id, name="Statistics actor"), user, workspace])
    await db_session.flush()
    member = WorkspaceStaff(workspace_id=workspace.id, user_id=user_id, role="owner", status="active")
    db_session.add(member)
    await db_session.commit()
    handlers = {schema["function"]["name"]: handler for schema, handler in get_tools()}
    context = dict(entity_id=entity_id, workspace_id=workspace.id, _user_id_from_context=user_id)
    definition = dict(
        key="release.views",
        name="Release views",
        collector_type="public_post",
        collector_config={
            "platform": "youtube",
            "public_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "external_post_id": "abcdefghijk",
            "metric_key": "views",
            "subject": {"kind": "web_page", "id": "release"},
        },
    )
    missing_actor = json.loads(
        await handlers["configure_workspace_stat"](entity_id=entity_id, workspace_id=workspace.id, **definition)
    )
    assert missing_actor.get("error")
    configured = json.loads(await handlers["configure_workspace_stat"](
        **context, **definition, freshness_limit_seconds=21600,
    ))
    assert configured["created"] is True
    assert configured["freshness_limit_seconds"] == 21600
    again = json.loads(await handlers["configure_workspace_stat"](**context, **definition))
    assert again["stat_id"] == configured["stat_id"]
    assert again["created"] is False
    assert again["freshness_limit_seconds"] == 21600
    reconciled = json.loads(await handlers["configure_workspace_stat"](
        **context, **{
            **definition,
            "key": "recovered.views",
            "name": "Recovered views",
            "freshness_limit_seconds": None,
        },
    ))
    assert reconciled["stat_id"] == configured["stat_id"]
    assert reconciled["created"] is False
    assert reconciled["freshness_limit_seconds"] == 21600
    assert reconciled["requested_freshness_limit_seconds"] is None
    assert reconciled["operational_policy_mismatch"] == {
        "field": "freshness_limit_seconds",
        "actual": 21600,
        "requested": None,
        "update_required": True,
    }
    for invalid_freshness_limit in (59, True):
        invalid_freshness = json.loads(await handlers["configure_workspace_stat"](
            **context, **{
                **definition,
                "key": f"invalid-{invalid_freshness_limit}.views",
                "freshness_limit_seconds": invalid_freshness_limit,
            },
        ))
        assert invalid_freshness["error"] == "workspace_stat_invalid"
    conflicting = json.loads(await handlers["configure_workspace_stat"](
        **context, **{**definition, "window": "rolling_7d"},
    ))
    assert conflicting.get("error")
    other_subject = json.loads(await handlers["configure_workspace_stat"](
        **context,
        **{**definition, "key": "other.views", "collector_config": {
            **definition["collector_config"], "subject": {"kind": "web_page", "id": "other"},
        }},
    ))
    assert other_subject["created"] is True
    assert other_subject["stat_id"] != configured["stat_id"]
    # An unrelated manual Stat is reused when a later caller only rewords its display text.
    manual = dict(
        key="newsletter.signups", name="Newsletter signups", collector_type="manual",
        collector_config={"subject": {"kind": "web_page", "id": "newsletter"}},
    )
    first_manual = json.loads(await handlers["configure_workspace_stat"](
        **context, **manual, description="Signups from the footer form",
    ))
    reworded = json.loads(await handlers["configure_workspace_stat"](
        **context, **{**manual, "name": "Signups (newsletter)"}, description="Footer form signups",
    ))
    assert reworded["stat_id"] == first_manual["stat_id"]
    assert reworded["created"] is False
    assert reworded["revision"] == first_manual["revision"]
    assert json.loads(await handlers["configure_workspace_stat"](
        **context, **{**manual, "collector_config": {"subject": {"kind": "web_page", "id": "pricing"}}},
    )).get("error")

    # A Workflow records its own measurements on manual Stats, configuring a missing one on first use.
    recorded = json.loads(await handlers["record_workspace_stat_observations"](**context, observations=[
        {"key": "newsletter.signups", "value": 4, "idempotency_key": "batch-1:signups"},
        {"key": "newsletter.unsubscribes", "value": None, "gaps": ["hidden"], "idempotency_key": "batch-1:unsub",
         "definition": {"name": "Unsubscribes", "collector_config": {"subject": {"kind": "web_page", "id": "newsletter"}}}},
    ]))
    assert [(row["key"], row["value"]) for row in recorded["observations"]] == [
        ("newsletter.signups", 4.0), ("newsletter.unsubscribes", None)]
    replay = json.loads(await handlers["record_workspace_stat_observations"](**context, observations=[
        {"key": "newsletter.signups", "value": 4, "idempotency_key": "batch-1:signups"}]))
    assert replay["observations"][0]["observation_id"] == recorded["observations"][0]["observation_id"]
    for bad in ([{"key": "newsletter.bounces", "value": 1, "idempotency_key": "x"}],
                [{"key": str(definition["key"]), "value": 1, "idempotency_key": "x"}]):
        assert json.loads(await handlers["record_workspace_stat_observations"](**context, observations=bad)).get("error")
    # Deleting takes one family; a selector that could sweep the Workspace is refused.
    assert json.loads(await handlers["delete_workspace_stats"](**context, key_prefix="news")).get("error")
    assert json.loads(await handlers["delete_workspace_stats"](**context)).get("error")
    for literal_prefix in ("ne%.", "newsletter._"):
        selected, cursor = await _selected_stats(
            db_session, entity_id=entity_id, workspace_id=workspace.id, params={"key_prefix": literal_prefix},
        )
        assert selected == [] and cursor is None
        assert json.loads(await handlers["delete_workspace_stats"](**context, key_prefix=literal_prefix))["deleted"] == 0
    assert json.loads(await handlers["delete_workspace_stats"](**context, key_prefix="newsletter."))["deleted"] == 2

    public_fetches = 0

    async def public_snapshot(**_kwargs):
        nonlocal public_fetches
        public_fetches += 1
        return {
            "parser_version": "fixture-v1",
            "metrics": {
                "views": {
                    "value": 17,
                    "availability": "available",
                    "precision": "exact",
                    "provider_metric_name": "viewCount",
                }
            },
            "evidence": {"url": definition["collector_config"]["public_url"]},
        }

    monkeypatch.setattr(public_social_metrics, "collect_public_post_metrics", public_snapshot)
    # A replay is keyed to the minute it samples; pin the clock so the replay
    # cannot cross a minute boundary and sample a new bucket.
    sampled_at = datetime.now(UTC).replace(second=30, microsecond=0)
    monkeypatch.setattr("packages.core.stats.service._utcnow", lambda: sampled_at)
    collected = json.loads(await handlers["collect_workspace_stats"](**context, stat_ids=[configured["stat_id"]]))
    assert len(collected["observations"]) == 1
    assert collected["observations"][0]["value"] == 17
    assert collected["observations"][0]["source"] == "public_post"
    assert collected["observations"][0]["evidence"]["availability"] == "available"
    replayed_collection = json.loads(
        await handlers["collect_workspace_stats"](**context, stat_ids=[configured["stat_id"]])
    )
    assert replayed_collection["observations"] == collected["observations"]
    assert public_fetches == 1

    async def failed_snapshot(**_kwargs):
        raise ValueError("public page unavailable")

    monkeypatch.setattr(public_social_metrics, "collect_public_post_metrics", failed_snapshot)
    failed = json.loads(await handlers["collect_workspace_stats"](**context, stat_ids=[other_subject["stat_id"]]))
    assert failed["observations"][0]["value"] is None
    assert failed["observations"][0]["source"] == "collector_error"
    assert failed["observations"][0]["evidence"]["error"] == "public page unavailable"
    member.role = "viewer"
    await db_session.commit()
    denied_write = json.loads(await handlers["collect_workspace_stats"](**context, stat_ids=[configured["stat_id"]]))
    assert denied_write.get("error")
    report = json.loads(
        await handlers["query_workspace_stat_observations"](
            **context,
            stat_ids=[configured["stat_id"]],
            window_start="2020-01-01T00:00:00Z",
            window_end="2030-01-01T00:00:00Z",
        )
    )
    assert report["stats"][0]["latest_value"] == 17
    weekly = json.loads(
        await handlers["query_workspace_stat_observations"](
            **context,
            subject=definition["collector_config"]["subject"],
            window="previous_calendar_week",
            timezone="America/Los_Angeles",
            limit=1,
        )
    )
    local_start = datetime.fromisoformat(weekly["window"]["start"]).astimezone(
        ZoneInfo("America/Los_Angeles")
    )
    iso_year, iso_week, _ = local_start.date().isocalendar()
    assert weekly["reporting_period_key"] == f"{iso_year}-W{iso_week:02d}"
    member.role = "owner"
    workspace.status = "paused"
    await db_session.commit()
    paused_collection = json.loads(await handlers["collect_workspace_stats"](
        **context, stat_ids=[configured["stat_id"]],
    ))
    assert not paused_collection.get("error")
    assert paused_collection["observations"][0]["stat_id"] == configured["stat_id"]
    workspace.status = "active"
    workspace.deleted_at = datetime.now(UTC)
    await db_session.commit()
    assert json.loads(await handlers["configure_workspace_stat"](**context, **definition)).get("error")


async def test_generic_publication_stat_preserves_source_and_compares_exact_interval(db_session, monkeypatch):
    from packages.core.ai.tools import stats_tools

    class ReportMonday(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 12, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(stats_tools, "datetime", ReportMonday)
    start, end, period = stats_tools._window({
        "window": "previous_calendar_week", "timezone": "Asia/Shanghai",
    })
    assert (start, end, period) == (
        "2026-10-04T16:00:00+00:00", "2026-10-11T16:00:00+00:00", "2026-W41",
    )
    workspace = Workspace(entity_id="publication-stat-entity", name="Publisher")
    db_session.add(workspace)
    await db_session.flush()
    stat = await create_stat(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        key="website.release.views",
        name="Release page views",
        collector_type="manual",
        collector_config={"subject": {"kind": "web_page", "id": "release-1"}},
        install_schedule=False,
    )
    subject = {"kind": "web_page", "id": "release-1"}
    await _observe(
        db_session,
        stat,
        subject=subject,
        value=100,
        source="public_page",
        observed_at="2026-10-04T15:59:59+00:00",
        sample_key="baseline",
        definition="page-v1",
    )
    await _observe(
        db_session,
        stat,
        subject=subject,
        value=140,
        source="public_page",
        observed_at="2026-10-07T00:00:00+00:00",
        sample_key="current",
        definition="page-v1",
    )
    await _observe(
        db_session, stat, subject=subject, value=160, source="public_page",
        observed_at="2026-10-11T15:59:59+00:00", sample_key="week-close",
        definition="page-v1",
    )
    await _observe(
        db_session, stat, subject=subject, value=999, source="public_page",
        observed_at="2026-10-11T16:00:00+00:00", sample_key="next-week",
        definition="page-v1",
    )
    rows = await list_publication_observations(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        stat_ids=[stat.id],
        window_end=end,
    )
    report = compare_publication_stats(
        [stat],
        rows,
        window_start=start,
        window_end=end,
    )

    item = report["stats"][0]
    assert item["latest_value"] == 160.0
    assert item["exact_delta"] == 60.0
    assert item["source"] == "public_page"
    assert item["evidence"]["subject"] == subject


async def test_generic_publication_stat_does_not_fabricate_a_delta(db_session):
    workspace = Workspace(entity_id="publication-stat-second", name="Publisher")
    db_session.add(workspace)
    await db_session.flush()
    stat = await create_stat(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        key="newsletter.issue.opens",
        name="Issue opens",
        collector_type="manual",
        collector_config={"subject": {"kind": "newsletter_issue", "id": "issue-1"}},
        install_schedule=False,
    )
    await _observe(
        db_session,
        stat,
        subject={"kind": "newsletter_issue", "id": "issue-1"},
        value=100,
        source="provider_api",
        observed_at="2026-10-05T00:00:00+00:00",
        sample_key="baseline",
        definition="provider-v1",
    )
    rows = await list_publication_observations(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        stat_ids=[stat.id],
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )
    item = compare_publication_stats(
        [stat],
        rows,
        window_start=datetime(2026, 10, 6, tzinfo=UTC),
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )["stats"][0]

    assert item["exact_delta"] is None
    assert item["delta_reason"] == "no_current_period_observation"


def test_generic_report_distinguishes_no_observation_from_unavailable_value():
    stat = WorkspaceStat(id="stat-no-observation", key="release.views", name="Release views",
                         entity_id="entity", workspace_id="workspace", collector_type="public_post")
    item = compare_publication_stats(
        [stat], [],
        window_start=datetime(2026, 10, 6, tzinfo=UTC),
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )["stats"][0]
    assert item["latest_value"] is None
    assert item["delta_reason"] == "no_observation"


async def test_generic_report_rejects_unavailable_and_source_switched_baselines(db_session):
    workspace = Workspace(entity_id="publication-stat-third", name="Publisher")
    db_session.add(workspace)
    await db_session.flush()
    subject = {"kind": "podcast_episode", "id": "episode-1"}
    stat = await create_stat(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        key="podcast.episode.plays",
        name="Episode plays",
        collector_type="manual",
        collector_config={"subject": subject},
        install_schedule=False,
    )
    await _observe(
        db_session,
        stat,
        subject=subject,
        value=100,
        source="provider_a",
        observed_at="2026-10-05T00:00:00+00:00",
        sample_key="a-baseline",
        definition="v1",
    )
    await _observe(
        db_session,
        stat,
        subject=subject,
        value=120,
        source="provider_b",
        observed_at="2026-10-07T00:00:00+00:00",
        sample_key="b-current",
        definition="v1",
    )
    rows = await list_publication_observations(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        stat_ids=[stat.id],
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )
    switched = compare_publication_stats(
        [stat],
        rows,
        window_start=datetime(2026, 10, 6, tzinfo=UTC),
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )["stats"][0]
    assert switched["delta_reason"] == "missing_comparable_baseline"
    await _observe(
        db_session,
        stat,
        subject=subject,
        value=None,
        source="provider_b",
        observed_at="2026-10-08T00:00:00+00:00",
        sample_key="b-unavailable",
        availability="unavailable",
    )
    rows = await list_publication_observations(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        stat_ids=[stat.id],
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )
    item = compare_publication_stats(
        [stat],
        rows,
        window_start=datetime(2026, 10, 6, tzinfo=UTC),
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )["stats"][0]

    assert item["latest_value"] is None
    assert item["exact_delta"] is None
    assert item["delta_reason"] == "unavailable"

    await _observe(
        db_session,
        stat,
        subject=subject,
        value=130,
        source="provider_a",
        observed_at="2026-10-09T00:00:00+00:00",
        sample_key="a-returned",
        definition="v1",
    )
    rows = await list_publication_observations(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        stat_ids=[stat.id],
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )
    returned = compare_publication_stats(
        [stat],
        rows,
        window_start=datetime(2026, 10, 6, tzinfo=UTC),
        window_end=datetime(2026, 10, 13, tzinfo=UTC),
    )["stats"][0]
    assert returned["exact_delta"] == 30.0


async def test_failed_generic_collector_records_nullable_observation(db_session, monkeypatch):
    from packages.core.services import public_social_metrics
    from packages.core.stats.service import StatError, update_stat

    workspace = Workspace(entity_id="publication-stat-fourth", name="Publisher")
    db_session.add(workspace)
    await db_session.flush()
    definition = dict(
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        key="web.article.views",
        name="Article views",
        collector_type="public_post",
        collector_config={
            "platform": "instagram",
            "public_url": "https://www.instagram.com/reel/shortcode/",
            "external_post_id": "shortcode",
            "metric_key": "views",
        },
        install_schedule=False,
    )
    for invalid_config in (
        {"platform": "instagram", "metric": "views", "published_url": "https://www.instagram.com/reel/shortcode/"},
        {**definition["collector_config"], "external_post_id": "123456"},
        {**definition["collector_config"], "metric_key": "unknown_metric"},
    ):
        with pytest.raises(StatError):
            await create_stat(db_session, **{**definition, "collector_config": invalid_config})
    stat = await create_stat(db_session, **definition)
    with pytest.raises(StatError):
        await update_stat(db_session, stat, collector_config={"metric": "views"})
    assert stat.collector_config == definition["collector_config"]

    async def failed_snapshot(**_kwargs):
        raise ValueError("public page unavailable")

    monkeypatch.setattr(public_social_metrics, "collect_public_post_metrics", failed_snapshot)

    observation = await collect_stat(db_session, stat, observed_at=datetime(2026, 10, 7, tzinfo=UTC))

    assert observation.value is None
    assert observation.source == "collector_error"
    assert observation.evidence["gaps"] == ["collector_error"]
    assert observation.evidence["error"] == "public page unavailable"
    assert stat.current_value is None
    assert stat.last_collection_status == "error"
    assert stat.last_collection_error == "public page unavailable"


async def test_subject_stat_pagination_traverses_more_than_twenty_without_cross_workspace_rows(db_session):
    workspace = Workspace(entity_id="publication-pages", name="Publisher")
    other = Workspace(entity_id="publication-pages", name="Other")
    db_session.add_all([workspace, other])
    await db_session.flush()
    subject = {"kind": "web_page", "id": "release"}
    for index in range(21):
        await create_stat(
            db_session,
            entity_id=workspace.entity_id,
            workspace_id=workspace.id,
            key=f"release.metric.{index}",
            name=f"Metric {index}",
            collector_type="manual",
            collector_config={"subject": subject},
            install_schedule=False,
        )
    await create_stat(
        db_session,
        entity_id=other.entity_id,
        workspace_id=other.id,
        key="release.metric.other",
        name="Other",
        collector_type="manual",
        collector_config={"subject": subject},
        install_schedule=False,
    )

    first, cursor = await _selected_stats(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        params={"subject": subject, "limit": 20},
    )
    second, last_cursor = await _selected_stats(
        db_session,
        entity_id=workspace.entity_id,
        workspace_id=workspace.id,
        params={"subject": subject, "limit": 20, "after_id": cursor},
    )

    assert len(first) == 20
    assert len(second) == 1
    assert cursor and last_cursor is None
    assert {row.workspace_id for row in [*first, *second]} == {workspace.id}
