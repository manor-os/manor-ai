from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.blueprints.installer import InstallMode, install_blueprint
from packages.core.blueprints.solo_company import get_solo_company_blueprint
from packages.core.models.base import generate_ulid
from packages.core.models.document import Document, DocumentGroup, DocumentGroupMember
from packages.core.models.scheduler import ScheduledJob
from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowRun
from packages.core.services.embedding_service import hybrid_search
from packages.core.services.workflow_service import (
    execute_workflow_step,
    start_workflow_from_binding,
    validate_workflow_steps,
)


BLUEPRINT_SLUG = "solo-content-distribution-studio-v1"
CONTENT_WORKFLOWS = {
    "opc-generate-topic-from-knowledge-v1",
    "opc-write-article-from-topic-v1",
    "opc-create-image-from-topic-v1",
    "opc-create-video-from-topic-v1",
}
PUBLISH_WORKFLOWS = {
    "opc-publish-linkedin-v1",
    "opc-publish-medium-v1",
    "opc-submit-hacker-news-v1",
    "opc-publish-wechat-official-v1",
    "opc-publish-x-v1",
    "opc-publish-reddit-v1",
}
REPLY_WORKFLOW = "opc-reply-platform-comments-v1"


def _workflow_map() -> dict[str, dict]:
    payload = get_solo_company_blueprint(BLUEPRINT_SLUG)
    return {workflow["slug"]: workflow for workflow in payload["recipe"]["workflows"]}


def test_opc_blueprint_exposes_independent_research_publish_and_reply_atoms() -> None:
    workflows = _workflow_map()
    atomic = CONTENT_WORKFLOWS | PUBLISH_WORKFLOWS | {REPLY_WORKFLOW}

    assert atomic < set(workflows)
    assert all(workflows[slug]["trigger_type"] == "mcp" for slug in atomic)
    assert all(workflows[slug]["binding_config"]["chat_entrypoint"]["wait_bridge"] is True for slug in atomic)
    assert all(
        all(step.get("type") not in {"subworkflow", "foreach_subworkflow"} for step in workflows[slug]["steps"])
        for slug in atomic
    )
    assert all(
        next(
            item
            for item in workflows[slug]["run_inputs"]
            if item["key"] == "run_context"
        )["hidden"] is True
        for slug in atomic
    )

    topic = workflows["opc-generate-topic-from-knowledge-v1"]
    research = next(step for step in topic["steps"] if step["id"] == "research_current_platform_signals")
    assert research["tool"] == "web_search"
    assert research["depends_on"] == ["retrieve_topic_knowledge"]
    topic_draft = next(step for step in topic["steps"] if step["id"] == "draft_topic_brief")
    assert topic_draft["depends_on"] == [research["id"]]
    assert "public_signal_refs" in topic_draft["output_schema"]["required"]

    daily = workflows["opc-daily-content-dispatcher-v1"]
    daily_research = next(step for step in daily["steps"] if step["id"] == "research_daily_platform_signals")
    assert daily_research["tool"] == "web_search"


def test_opc_atomic_flows_prefill_only_from_approved_upstream_results() -> None:
    workflows = _workflow_map()
    topic_contract = {
        "source": "workflow_result",
        "workflow_slug": "opc-generate-topic-from-knowledge-v1",
        "terminal_step_id": "topic_approved",
    }
    article_contract = {
        "source": "workflow_result",
        "workflow_slug": "opc-write-article-from-topic-v1",
        "terminal_step_id": "article_approved",
    }

    for slug in (
        "opc-write-article-from-topic-v1",
        "opc-create-image-from-topic-v1",
        "opc-create-video-from-topic-v1",
    ):
        inputs = {item["key"]: item for item in workflows[slug]["run_inputs"]}
        assert inputs["topic_brief"]["prefill"] == topic_contract
        assert set(inputs["topic_brief"]["schema"]["required"]) == {
            "topic_id", "title", "thesis", "audience",
        }

    for slug in ("opc-create-image-from-topic-v1", "opc-create-video-from-topic-v1"):
        inputs = {item["key"]: item for item in workflows[slug]["run_inputs"]}
        assert inputs["article_context"]["prefill"] == article_contract

    expected_paths = {
        "source_title": "title",
        "source_body": "markdown",
        "canonical_url": "canonical_url",
    }
    for slug in PUBLISH_WORKFLOWS:
        inputs = {item["key"]: item for item in workflows[slug]["run_inputs"]}
        for key, path in expected_paths.items():
            assert inputs[key]["prefill"] == {**article_contract, "path": path}

    reply_inputs = {
        item["key"]: item
        for item in workflows[REPLY_WORKFLOW]["run_inputs"]
    }
    assert reply_inputs["max_replies"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 20,
    }


def test_opc_platform_publishers_use_exact_approval_and_chrome_verification() -> None:
    workflows = _workflow_map()

    x_flow = workflows["opc-publish-x-v1"]
    assert any(
        step.get("tool") == "mcp__twitter_x__create_tweet" and step.get("on_error") == "continue"
        for step in x_flow["steps"]
    )
    reddit_flow = workflows["opc-publish-reddit-v1"]
    assert not any(step.get("kind") == "tool_call" for step in reddit_flow["steps"])

    service_by_workflow = {
        "opc-publish-linkedin-v1": "distribution.linkedin.chrome",
        "opc-publish-medium-v1": "distribution.medium.chrome",
        "opc-submit-hacker-news-v1": "distribution.hacker_news.chrome",
        "opc-publish-wechat-official-v1": "distribution.wechat.chrome",
        "opc-publish-x-v1": "distribution.x.chrome",
        "opc-publish-reddit-v1": "distribution.reddit.chrome",
    }
    for slug, service_key in service_by_workflow.items():
        flow = workflows[slug]
        approvals = [step for step in flow["steps"] if step.get("kind") == "hitl_approval"]
        assert len(approvals) == 1
        approval = approvals[0]
        assert approval["review"]
        assert approval["allow_always"] is True
        assert "always_approve" in approval["approval_values"]
        assert approval["approval_action_key"]

        chrome_step = next(step for step in flow["steps"] if step.get("service_key") == service_key)
        required = set(chrome_step["output_schema"]["required"])
        assert {"published", "verified", "publication_url", "blocker"} <= required
        success_gate = next(
            step
            for step in flow["steps"]
            if chrome_step["id"] in (step.get("depends_on") or []) and step.get("type") == "condition"
        )
        assert "published == true" in success_gate["config"]["expression"]
        assert "verified == true" in success_gate["config"]["expression"]


def test_opc_comment_reply_flow_is_owned_inbound_approval_first_and_non_engagement() -> None:
    payload = get_solo_company_blueprint(BLUEPRINT_SLUG)
    reply_flow = _workflow_map()[REPLY_WORKFLOW]
    steps = {step["id"]: step for step in reply_flow["steps"]}

    assert steps["inspect_inbound_comments"]["service_key"] == "distribution.comments.chrome"
    assert "read only" in steps["inspect_inbound_comments"]["input"].lower()
    assert "belongs to the signed-in operator" in steps["inspect_inbound_comments"]["input"]
    assert steps["draft_comment_replies"]["service_key"] == "content.comment_reply"
    approval = steps["approve_comment_replies"]
    assert approval["review"] == "{{reply_packet}}"
    assert approval["approval_action_key"] == "social_comment.reply"
    assert steps["comment_replies_approved_gate"]["false_next"] == ["comment_replies_changes_requested"]
    assert steps["comment_replies_changes_requested"]["type"] == "end"

    publish_prompt = steps["publish_comment_replies"]["input"].lower()
    assert "never vote, react, follow, request engagement" in publish_prompt
    assert "exact approved replies" in publish_prompt
    never_allow = set(payload["policy"]["governance"]["never_allow_actions"])
    assert {"hacker_news.vote_*", "social.mass_comment", "social.unsolicited_comment"} <= never_allow


def test_daily_slot_router_supports_all_six_publishers() -> None:
    workflows = _workflow_map()
    slot = workflows["opc-execute-platform-slot-v1"]
    referenced = {step["config"]["workflow_id"] for step in slot["steps"] if step.get("type") == "subworkflow"}
    assert CONTENT_WORKFLOWS | PUBLISH_WORKFLOWS == referenced

    route = next(step for step in slot["steps"] if step["id"] == "route_platform")
    expressions = {case["expression"] for case in route["config"]["cases"]}
    assert expressions == {
        "slot.platform == 'linkedin'",
        "slot.platform == 'medium'",
        "slot.platform == 'hacker_news'",
        "slot.platform == 'wechat_official'",
        "slot.platform == 'x'",
        "slot.platform == 'reddit'",
    }


def test_post_install_checks_cover_every_workspace_binding_and_schedule() -> None:
    payload = get_solo_company_blueprint(BLUEPRINT_SLUG)
    checks = payload["policy"]["post_install_checks"]
    checked_workflows = {
        check["workflow_slug"]
        for check in checks
        if check["kind"] == "workflow_present"
    }
    checked_jobs = {
        check["job_id"]
        for check in checks
        if check["kind"] == "cron_scheduled"
    }

    assert checked_workflows == {
        workflow["slug"]
        for workflow in payload["recipe"]["workflows"]
        if not workflow.get("internal")
    }
    assert checked_jobs == {
        job["job_id"]
        for job in payload["recipe"]["scheduled_jobs"]
    }


@pytest.mark.asyncio
async def test_opc_blueprint_installs_all_workflows_as_runnable_graphs(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    payload = get_solo_company_blueprint(BLUEPRINT_SLUG)
    installed = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.SIMULATE,
        governance_preset="standard",
    )
    await db_session.commit()

    assert [todo for todo in installed.todos if todo.blocking] == []
    assert len(installed.workflow_binding_ids) == 12

    knowledge_pack = payload["embedded"]["knowledge_packs"][0]
    group = (await db_session.execute(
        select(DocumentGroup).where(
            DocumentGroup.entity_id == entity_id,
            DocumentGroup.workspace_id == installed.workspace_id,
            DocumentGroup.name == knowledge_pack["title"],
        )
    )).scalar_one()
    knowledge_documents = list((await db_session.execute(
        select(Document)
        .join(DocumentGroupMember, DocumentGroupMember.document_id == Document.id)
        .where(DocumentGroupMember.group_id == group.id)
    )).scalars().all())
    assert {document.name for document in knowledge_documents} == {
        starter["path"] for starter in knowledge_pack["starter_documents"]
    }
    assert all(document.metadata_.get("content_text") for document in knowledge_documents)
    async def no_embedding_provider():
        return None

    monkeypatch.setattr(
        "packages.core.services.embedding_service._resolve_embedding_config",
        no_embedding_provider,
    )
    knowledge_hits = await hybrid_search(
        db_session,
        entity_id,
        "approval publishing policy",
        workspace_id=installed.workspace_id,
    )
    assert any(hit["name"] == "platform-rules/publishing-policy.md" for hit in knowledge_hits)

    definitions = list(
        (
            await db_session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.entity_id == entity_id,
                )
            )
        )
        .scalars()
        .all()
    )
    definitions = [row for row in definitions if row.name in _workflow_map()]
    assert len(definitions) == 13
    for definition in definitions:
        validation = validate_workflow_steps(definition.steps)
        assert validation["valid"], (definition.name, validation)
        assert validation["entry_step_id"] == "start"

    bindings = list(
        (
            await db_session.execute(
                select(WorkflowBinding).where(WorkflowBinding.id.in_(installed.workflow_binding_ids))
            )
        )
        .scalars()
        .all()
    )
    assert {binding.trigger_type for binding in bindings} == {"mcp"}

    daily_job = (
        await db_session.execute(
            select(ScheduledJob).where(
                ScheduledJob.entity_id == entity_id,
                ScheduledJob.job_id.like("opc-daily-content-dispatch-%"),
            )
        )
    ).scalar_one()
    assert daily_job.execution_type == "workflow"
    assert daily_job.execution_target["workflow_slug"] == ("opc-daily-content-dispatcher-v1")


@pytest.mark.asyncio
async def test_every_opc_atom_runs_to_review_before_any_public_side_effect(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_id = generate_ulid()
    payload = get_solo_company_blueprint(BLUEPRINT_SLUG)
    installed = await install_blueprint(
        db_session,
        entity_id=entity_id,
        payload=payload,
        mode=InstallMode.LIVE,
        governance_preset="standard",
    )
    await db_session.commit()

    packets = {
        "content.strategy": {
            "topic_id": "topic-1",
            "title": "A grounded topic",
            "thesis": "The thesis",
            "audience": "independent founders",
            "objective": "create qualified demand",
            "content_pillar": "founder insight",
            "key_questions": ["What changed?"],
            "knowledge_queries": ["verified product facts"],
            "knowledge_refs": ["products/product-facts.md"],
            "public_signal_refs": ["https://example.com/current-signal"],
            "evidence_gaps": [],
            "desired_formats": ["article", "image", "video"],
            "target_platforms": ["linkedin", "medium", "x", "reddit"],
        },
        "content.article": {
            "topic_id": "topic-1",
            "title": "Canonical article",
            "deck": "A factual deck",
            "markdown": "# Article\n\nBody",
            "summary": "Summary",
            "cta": "Discuss the tradeoff",
            "knowledge_refs": ["products/product-facts.md"],
            "citations": ["products/product-facts.md"],
            "evidence_gaps": [],
            "visual_directions": [],
            "canonical_url": "https://example.com/article",
        },
        "content.image": {
            "topic_id": "topic-1",
            "objective": "Explain the workflow",
            "generation_prompt": "A factual diagram",
            "negative_constraints": [],
            "composition": "Landscape",
            "on_image_text": "",
            "alt_text": "Diagram",
            "knowledge_refs": ["brand/visual-style.md"],
            "evidence_gaps": [],
        },
        "content.video": {
            "topic_id": "topic-1",
            "hook": "One topic, many formats",
            "narration": "Factual narration",
            "scenes": [{"scene_id": "one"}],
            "generation_prompt": "A concise workflow video",
            "caption": "Workflow",
            "cta": "Discuss",
            "knowledge_refs": ["brand/visual-style.md"],
            "evidence_gaps": [],
            "listed_side_effects": ["generation capacity"],
        },
        "content.linkedin": {
            "post_text": "LinkedIn post",
            "article_title": "Canonical article",
            "article_description": "Description",
            "canonical_url": "https://example.com/article",
            "media_kind": "link",
            "media_source": "",
            "alt_text": "Article",
        },
        "content.medium": {
            "title": "Canonical article",
            "subtitle": "Subtitle",
            "markdown": "# Article\n\nBody",
            "tags": ["ai"],
            "canonical_url": "https://example.com/article",
            "handoff_steps": ["Publish"],
        },
        "content.hacker_news": {
            "eligible": True,
            "reason": "Original technical source",
            "title": "Canonical article",
            "url": "https://example.com/article",
            "submission_kind": "story",
            "manual_steps": ["Submit"],
        },
        "content.wechat": {
            "cover_source": "approved-assets/cover.png",
            "articles": [
                {
                    "title": "标题",
                    "author": "Founder",
                    "digest": "摘要",
                    "content": "<p>正文</p>",
                    "content_source_url": "https://example.com/article",
                    "thumb_media_id": "cover-id",
                    "need_open_comment": 1,
                    "only_fans_can_comment": 0,
                }
            ],
        },
        "content.x": {
            "post_text": "A factual X post https://example.com/article",
            "canonical_url": "https://example.com/article",
            "character_count": 50,
        },
        "content.reddit": {
            "subreddit": "startups",
            "post_kind": "link",
            "title": "Canonical article",
            "body": "Useful context",
            "canonical_url": "https://example.com/article",
            "flair": "Discussion",
            "affiliation_disclosure": "I am affiliated with this project.",
        },
        "distribution.comments.chrome": {
            "platform": "reddit",
            "publication_url": "https://reddit.com/r/startups/1",
            "owned_by_operator": True,
            "candidates": [{"comment_id": "c1", "author": "reader", "text": "How?"}],
            "evidence": ["Signed-in author matches"],
            "blocker": "",
        },
        "content.comment_reply": {
            "platform": "reddit",
            "publication_url": "https://reddit.com/r/startups/1",
            "replies": [
                {
                    "comment_id": "c1",
                    "comment_author": "reader",
                    "comment_excerpt": "How?",
                    "reply_text": "Here is the verified detail.",
                }
            ],
            "skipped": [],
        },
    }

    async def fake_rag_step(self, step, variables, entity_id, user_id="", runtime_context=None):
        del self, variables, entity_id, user_id
        assert runtime_context and runtime_context.get("workspace_id")
        return {
            "status": "completed",
            "output": [{"path": "products/product-facts.md", "text": "Verified fact"}],
            "output_var": step["config"]["output_var"],
        }

    async def fake_agent_step(
        self,
        step,
        variables,
        entity_id,
        user_id="",
        runtime_context=None,
        db=None,
    ):
        del self, variables, entity_id, user_id, runtime_context, db
        service_key = step["config"]["service_key"]
        return {
            "status": "completed",
            "output": packets[service_key],
            "output_var": step["config"]["output_var"],
        }

    async def fake_tool_step(self, step, variables, entity_id, user_id="", runtime_context=None):
        del self, variables, entity_id, user_id, runtime_context
        assert step["config"]["tool"] == "web_search"
        return {
            "status": "completed",
            "output": {"results": [{"title": "Signal", "url": "https://example.com/current-signal"}]},
            "output_var": step["config"]["output_var"],
            "tools_used": ["web_search"],
        }

    async def fail_if_media_runs(*args, **kwargs):
        del args, kwargs
        raise AssertionError("A pre-approval run must not generate media")

    monkeypatch.setattr(WorkflowRunner, "_execute_rag_step", fake_rag_step)
    monkeypatch.setattr(WorkflowRunner, "_execute_agent_step", fake_agent_step)
    monkeypatch.setattr(WorkflowRunner, "_execute_tool_step", fake_tool_step)
    monkeypatch.setattr(WorkflowRunner, "_execute_media_step", fail_if_media_runs)

    bindings = list(
        (
            await db_session.execute(
                select(WorkflowBinding).where(WorkflowBinding.id.in_(installed.workflow_binding_ids))
            )
        )
        .scalars()
        .all()
    )
    definitions = {
        workflow.id: workflow
        for workflow in (
            await db_session.execute(select(WorkflowDefinition).where(WorkflowDefinition.entity_id == entity_id))
        )
        .scalars()
        .all()
    }

    common_inputs = {
        "run_context": {"plan_id": "plan-1", "slot_id": "slot-1"},
        "content_goal": "create qualified demand",
        "audience": "founders",
        "content_pillar": "workflow reliability",
        "knowledge_query": "verified facts",
        "desired_formats": "article,image,video",
        "target_platforms": "linkedin,medium,hacker_news,wechat_official,x,reddit",
        "topic_brief": packets["content.strategy"],
        "article_context": packets["content.article"],
        "target_length": "1200 words",
        "source_title": "Canonical article",
        "source_body": "Detailed source body",
        "canonical_url": "https://example.com/article",
        "cta": "Discuss",
        "tags": "ai,startups",
        "media_kind": "link",
        "media_source": "",
        "linkedin_author_urn": "urn:li:person:test",
        "submission_kind": "story",
        "author_name": "Founder",
        "thumb_media_id": "cover-id",
        "cover_source": "approved-assets/cover.png",
        "image_size": "1536x1024",
        "duration_seconds": 15,
        "aspect_ratio": "16:9",
        "subreddit": "startups",
        "post_kind": "link",
        "flair": "Discussion",
        "affiliation_disclosure": "I am affiliated with this project.",
        "platform": "reddit",
        "publication_url": "https://reddit.com/r/startups/1",
        "reply_goal": "Answer questions",
        "max_replies": 3,
    }
    expected_approval = {
        "opc-generate-topic-from-knowledge-v1": "approve_topic",
        "opc-write-article-from-topic-v1": "approve_article",
        "opc-create-image-from-topic-v1": "approve_image_brief",
        "opc-create-video-from-topic-v1": "approve_video_brief",
        "opc-publish-linkedin-v1": "approve_linkedin",
        "opc-publish-medium-v1": "approve_medium",
        "opc-submit-hacker-news-v1": "approve_hn",
        "opc-publish-wechat-official-v1": "approve_wechat",
        "opc-publish-x-v1": "approve_x",
        "opc-publish-reddit-v1": "approve_reddit",
        REPLY_WORKFLOW: "approve_comment_replies",
    }

    paused_runs: dict[str, WorkflowRun] = {}
    for binding in bindings:
        workflow = definitions[binding.workflow_id]
        if workflow.name not in expected_approval:
            continue
        run = await start_workflow_from_binding(
            db_session,
            binding,
            variables=common_inputs,
            trigger_data=common_inputs,
            trigger_source="workflow_atom_test",
        )
        for _ in range(10):
            if run.status != "running":
                break
            await execute_workflow_step(db_session, run.id, entity_id)
            await db_session.refresh(run)
        assert run.status == "paused", (workflow.name, run.status, run.step_results)
        assert run.current_step_id == expected_approval[workflow.name]
        assert all(set(result.get("tools_used") or []) <= {"web_search"} for result in run.step_results.values())
        paused_runs[workflow.name] = run

    assert set(paused_runs) == set(expected_approval)

    for slug, run in paused_runs.items():
        workflow = definitions[run.workflow_id]
        approval_id = expected_approval[slug]
        approval_step = next(step for step in workflow.steps if step["id"] == approval_id)
        response_variable = approval_step["config"]["response_variable"]
        run.variables = {**(run.variables or {}), response_variable: "cancel"}
        run.step_results = {
            **(run.step_results or {}),
            approval_id: {
                **dict((run.step_results or {}).get(approval_id) or {}),
                "status": "completed",
                "decision": "cancel",
                "approved": False,
            },
        }
        run.status = "running"
        await db_session.flush()
        for _ in range(4):
            if run.status != "running":
                break
            await execute_workflow_step(db_session, run.id, entity_id)
            await db_session.refresh(run)
        assert run.status == "completed", (slug, run.status, run.step_results)
        assert all(set(result.get("tools_used") or []) <= {"web_search"} for result in run.step_results.values())
