import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from auth_helpers import register_user_and_get_token
from packages.core.services.workspace_app_view import (
    normalize_app_view_config,
    project_workspace_app_view,
    validate_app_view_data_contract,
)
from packages.core.services.workspace_operation_service import _apply_single_patch
from packages.core.ai.tools import workspace_arch_tools


def _workspace(config=None):
    model = {
        "services": [
            {"key": "customer_support", "name": "Handle customer questions", "description": "Respond with the workspace knowledge."},
        ],
    }
    if config is not None:
        model["app_view"] = config
    return SimpleNamespace(
        id="workspace-1", name="Support Desk", primary_work="Help customers",
        operating_context="", status="active", operating_model=model,
        cover_image_url="https://example.com/cover.jpg",
    )


def _flow():
    return SimpleNamespace(
        binding_id="binding-1", workflow_id="workflow-1",
        title="Triage a request", description="Review and categorize a request",
        placeholder="", run_inputs=({"key": "request", "label": "Request", "required": True},),
    )


def test_app_view_uses_live_capability_names_and_removes_missing_flows():
    initial = project_workspace_app_view(_workspace(), [_flow()])
    assert initial["hero"] == "workflow:binding-1"
    assert initial["cover_image_url"] == "https://example.com/cover.jpg"
    assert initial["cover_source"] == "workspace"
    assert initial["config"]["visual"] == "auto"
    assert initial["config"]["layout"] == "feature"
    assert [item["title"] for item in initial["actions"]] == [
        "Triage a request", "Handle customer questions",
    ]

    changed = project_workspace_app_view(_workspace({
        "version": 1,
        "hero": "service:customer_support",
        "order": ["service:customer_support"],
        "labels": {"service:customer_support": "Talk to customers"},
    }), [])
    assert changed["hero"] == "service:customer_support"
    assert changed["actions"] == [{
        "key": "service:customer_support", "kind": "chat",
        "title": "Talk to customers",
        "source_title": "Handle customer questions",
        "source_description": "Respond with the workspace knowledge.",
        "description": "Respond with the workspace knowledge.",
        "prompt": "Handle customer questions",
        "inputs": [],
    }]


def test_app_view_uses_starter_cover_only_when_workspace_has_none():
    workspace = _workspace()
    workspace.cover_image_url = None
    view = project_workspace_app_view(workspace, [], starter_cover_url="/blueprints/covers/starter.webp")
    assert view["cover_image_url"] == "/blueprints/covers/starter.webp"
    assert view["cover_source"] == "starter"


def test_hidden_project_schedule_does_not_resurface_as_an_app_action():
    workspace = _workspace({
        "data_sources": {
            "projects": {"type": "workflow_projects", "project_type": "course", "producer_workflows": ["workflow_definition:course"]},
            "jobs": {"type": "owned_automations", "parent": "projects"},
        },
        "components": [{"key": "course", "type": "data_view", "context": {"kind": "project", "source": "projects"},
                        "blocks": [{"key": "schedule", "type": "progress", "source": "jobs"}],
                        "display": {"hidden": True, "hidden_blocks": ["schedule"]}}],
    })
    jobs = [SimpleNamespace(job_id="internal", name="Collect course data", enabled=True, last_status=None, owner_project_id="course"),
            SimpleNamespace(job_id="general", name="Daily summary", enabled=True, last_status=None, owner_project_id=None)]
    view = project_workspace_app_view(workspace, [_flow()], jobs)
    assert view["hero"] == "workflow:binding-1"
    assert "automation:internal" not in {item["key"] for item in view["actions"]}
    assert "automation:general" in {item["key"] for item in view["actions"]}
    assert {item["job_id"] for item in view["automation_options"]} == {"internal", "general"}


def test_the_app_never_lists_how_the_workspace_runs_itself():
    from packages.core.services.workspace_app_view import app_lists_scheduled_job

    def job(job_id, execution_type="agent", target=None):
        return SimpleNamespace(job_id=job_id, execution_type=execution_type, execution_target=target or {})

    assert app_lists_scheduled_job(job("daily-digest"))
    for platform_job in (job("sr:workspace-1"), job("oe:workspace-1"), job("cie:workspace-1"),
                         job("measure", "goal_measurement"), job("idea", target={"managed_by": "agent_initiative"})):
        assert not app_lists_scheduled_job(platform_job)


def test_motion_cover_keeps_its_source_image_binding():
    view = project_workspace_app_view(_workspace({
        "visual": "motion",
        "motion_cover_url": "/media/animated-cover.webm",
        "motion_cover_source_url": "https://example.com/cover.jpg",
    }), [])
    assert view["config"]["motion_cover_url"] == "/media/animated-cover.webm"
    assert view["config"]["motion_cover_source_url"] == view["cover_image_url"]


def test_motion_cover_update_persists_video_and_source_together():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update",
        "payload": {
            "visual": "motion",
            "motion_cover_url": "/media/animated-cover.webm",
            "motion_cover_source_url": "https://example.com/cover.jpg",
        },
    })
    config = updated["operating_model"]["app_view"]
    assert config["motion_cover_url"] == "/media/animated-cover.webm"
    assert config["motion_cover_source_url"] == "https://example.com/cover.jpg"


def test_app_cover_travels_with_the_config_under_the_workspace_cover():
    config = {
        "cover_image_url": "/blueprints/motion/wave.webp",
        "visual": "motion",
        "motion_cover_url": "/blueprints/motion/wave.mp4",
        "motion_cover_source_url": "/blueprints/motion/wave.webp",
    }
    bare = _workspace(config)
    bare.cover_image_url = None
    view = project_workspace_app_view(bare, [], starter_cover_url="/blueprints/covers/listing.webp")
    # The motion cover plays over the picture it was made for, not the Marketplace artwork.
    assert (view["cover_image_url"], view["cover_source"]) == ("/blueprints/motion/wave.webp", "app")
    assert view["config"]["motion_cover_source_url"] == view["cover_image_url"]
    own = project_workspace_app_view(_workspace(config), [], starter_cover_url="/blueprints/covers/listing.webp")
    assert (own["cover_image_url"], own["cover_source"]) == ("https://example.com/cover.jpg", "workspace")
    plain = _workspace({})
    plain.cover_image_url = None
    starter = project_workspace_app_view(plain, [], starter_cover_url="/blueprints/covers/listing.webp")
    assert (starter["cover_image_url"], starter["cover_source"]) == ("/blueprints/covers/listing.webp", "starter")
    assert "cover_image_url" not in starter["config"]

    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {"op": "app_view.update", "payload": {"cover_image_url": "/blueprints/motion/wave.webp"}})
    assert updated["operating_model"]["app_view"]["cover_image_url"] == "/blueprints/motion/wave.webp"
    cleared = _apply_single_patch(updated, {"op": "app_view.update", "payload": {"cover_image_url": None}})
    assert "cover_image_url" not in cleared["operating_model"]["app_view"]


def test_app_theme_is_normalized_and_set_or_cleared_by_the_operation():
    config = normalize_app_view_config({"theme": {"accent": "#7C5CFF", "surface": "deep", "radius": "sharp"}})
    assert config["theme"] == {"accent": "#7c5cff", "surface": "deep", "radius": "sharp"}
    assert "theme" not in normalize_app_view_config({"theme": {}})

    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {"op": "app_view.update", "payload": {"theme": {"surface": "tinted"}}})
    assert updated["operating_model"]["app_view"]["theme"] == {"surface": "tinted"}
    cleared = _apply_single_patch(updated, {"op": "app_view.update", "payload": {"theme": None}})
    assert "theme" not in cleared["operating_model"]["app_view"]


def test_service_form_is_saved_and_projected_as_guided_inputs():
    fields = [
        {"key": "song", "label": "Upload a song", "type": "file", "required": True},
        {"key": "release_date", "label": "Release date", "type": "text", "required": True,
         "placeholder": "YYYY-MM-DD"},
    ]
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update",
        "payload": {"forms": {"service:customer_support": fields}},
    })
    view = project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": updated["operating_model"]}), [],
    )
    assert [field["key"] for field in view["actions"][0]["inputs"]] == ["song", "release_date"]
    assert view["actions"][0]["inputs"][0]["type"] == "file"

    removed = _apply_single_patch(updated, {
        "op": "app_view.update", "payload": {"forms": {"service:customer_support": None}},
    })
    assert removed["operating_model"]["app_view"]["forms"] == {}


def test_app_view_copy_and_input_contract_follow_saved_presentation_and_live_flow():
    flow = _flow()
    flow.run_inputs = ({"key": "request", "label": "Request", "type": "json",
                        "required": True, "placeholder": "Paste a request",
                        "schema": {"type": "object", "properties": {"goal": {"type": "string"}}}},)
    view = project_workspace_app_view(_workspace({
        "eyebrow": "YOUR STUDIO",
        "title": "Release studio",
        "description": "Prepare and review a release.",
        "hero": "workflow:binding-1",
        "descriptions": {"workflow:binding-1": "Start with a request."},
    }), [flow])
    assert view["title"] == "Release studio"
    assert view["eyebrow"] == "YOUR STUDIO"
    assert view["description"] == "Prepare and review a release."
    assert view["actions"][0]["description"] == "Start with a request."
    assert view["actions"][0]["inputs"] == [
        {"key": "request", "label": "Request", "type": "json",
         "required": True, "placeholder": "Paste a request",
         "schema": {"type": "object", "properties": {"goal": {"type": "string"}}}},
    ]
    assert view["actions"][0]["binding_id"] == "binding-1"


def test_app_page_reads_in_the_viewers_language_and_saves_as_authored():
    config = {
        "title": "Video Studio", "labels": {"workflow:binding-1": "Make a video"},
        "targets": {"workflow:binding-1": "workflow:binding-1"},
        "i18n": {"zh-CN": {
            "title": "视频工作室", "labels": {"workflow:binding-1": "做视频"},
            # A translation never retargets an entry or adds text the page lacks.
            "targets": {"workflow:binding-1": "workflow:other"}, "eyebrow": "工作室",
        }},
    }
    stored = normalize_app_view_config(config)
    assert stored["i18n"] == {"zh": {"title": "视频工作室", "labels": {"workflow:binding-1": "做视频"}}}

    chinese = project_workspace_app_view(_workspace(config), [_flow()], locale="zh")
    assert chinese["title"] == "视频工作室"
    assert chinese["actions"][0]["title"] == "做视频"
    assert chinese["actions"][0]["binding_id"] == "binding-1"
    # The settings page saves what it was given, so it gets the authored page.
    assert chinese["config"]["title"] == "Video Studio"
    assert chinese["config"]["i18n"] == stored["i18n"]

    english = project_workspace_app_view(_workspace(config), [_flow()], locale="en")
    assert english["title"] == "Video Studio"
    assert english["actions"][0]["title"] == "Make a video"

    with pytest.raises(ValueError, match="i18n"):
        normalize_app_view_config({"i18n": ["zh"]})


def test_app_entry_icons_and_chosen_metrics_reach_the_page_and_cross_a_blueprint():
    from packages.core.blueprints.app_view import remap_app_view

    # A config without either keeps its normalized form, so stored pages need no rewrite.
    assert "icons" not in normalize_app_view_config({"sections": [{"block": "metrics"}]})
    assert "items" not in normalize_app_view_config({"sections": [{"block": "metrics"}]})["sections"][0]

    view = project_workspace_app_view(_workspace({
        "icons": {"workflow:binding-1": "music"},
        "sections": [{"block": "actions"}, {"block": "metrics", "items": ["goal:revenue", "stat:orders"]}],
    }), [_flow()])
    assert view["actions"][0]["icon"] == "music"
    assert all("icon" not in action for action in view["actions"][1:])
    assert view["sections"][1]["items"] == ["goal:revenue", "stat:orders"]

    exported = remap_app_view(
        {"icons": {"workflow:binding-1": "music"}, "sections": [{"block": "metrics", "items": ["stat:orders"]}]},
        workflows={"binding-1": "make-song"}, agents={}, automations={}, stat_keys={"orders"},
    )
    assert exported["icons"] == {"workflow:make-song": "music"}
    with pytest.raises(ValueError, match="Stats that are not included"):
        remap_app_view({"sections": [{"block": "metrics", "items": ["stat:orders"]}]},
                       workflows={}, agents={}, automations={}, stat_keys=set())

    updated = _apply_single_patch({"operating_model": {"app_view": {"icons": {"workflow:binding-1": "music"}}}}, {
        "op": "app_view.update", "payload": {"icons": {"workflow:binding-1": None, "automation:job": "clock"}},
    })
    assert updated["operating_model"]["app_view"]["icons"] == {"automation:job": "clock"}


def test_a_flows_rules_across_inputs_reach_its_app_form_and_entries_that_start_it():
    rule = {"anyOf": [{"required": ["request"]}], "x-message": "Say what you need."}
    flow = _flow()
    flow.input_rules = (rule,)
    view = project_workspace_app_view(_workspace({
        "entries": {"action:urgent": {"title": "Urgent request"}},
        "targets": {"action:urgent": "workflow:binding-1"},
    }), [flow])
    actions = {action["key"]: action for action in view["actions"]}
    assert actions["workflow:binding-1"]["input_rules"] == [rule]
    assert actions["action:urgent"]["input_rules"] == [rule]
    # A Flow without rules keeps its form as it was.
    assert "input_rules" not in project_workspace_app_view(_workspace(), [_flow()])["actions"][0]


def test_the_view_a_workspace_opens_as_crosses_a_blueprint_and_can_be_cleared():
    from packages.core.blueprints.app_view import remap_app_view

    assert "open_as" not in normalize_app_view_config({})
    assert remap_app_view({"open_as": "app"}, workflows={}, agents={}, automations={})["open_as"] == "app"
    with pytest.raises(ValueError, match="open_as"):
        normalize_app_view_config({"open_as": "fullscreen"})

    updated = _apply_single_patch({"operating_model": {"app_view": {}}},
                                  {"op": "app_view.update", "payload": {"open_as": "app"}})
    assert updated["operating_model"]["app_view"]["open_as"] == "app"
    cleared = _apply_single_patch(updated, {"op": "app_view.update", "payload": {"open_as": None}})
    assert "open_as" not in cleared["operating_model"]["app_view"]


def test_app_page_translations_follow_its_entries_through_a_blueprint():
    from packages.core.blueprints.app_view import remap_app_view

    exported = remap_app_view(
        {"labels": {"workflow:binding-1": "Make"}, "i18n": {"zh": {"labels": {"workflow:binding-1": "制作"}}}},
        workflows={"binding-1": "make-video"}, agents={}, automations={},
    )
    assert exported["i18n"] == {"zh": {"labels": {"workflow:make-video": "制作"}}}


def test_app_view_targets_live_flow_agent_and_automation():
    alternate = SimpleNamespace(
        binding_id="binding-2", workflow_id="workflow-2", title="Publish a release",
        description="Prepare the final package", placeholder="", run_inputs=(),
    )
    job = SimpleNamespace(job_id="job-1", name="Weekly release check", enabled=True, last_status="succeeded")
    view = project_workspace_app_view(_workspace({
        "targets": {
            "workflow:binding-1": "binding-2",
            "service:customer_support": "agent-2",
            "automation:job-1": "job-1",
        },
    }), [_flow(), alternate], [job], [
        {"id": "agent-1", "name": "Default helper", "service_key": "customer_support"},
        {"id": "agent-2", "name": "Release advisor", "service_key": ""},
    ])
    assert view["actions"][0]["binding_id"] == "binding-2"
    assert view["actions"][0]["inputs"] == []
    assert [option["binding_id"] for option in view["workflow_options"]] == ["binding-1", "binding-2"]
    assert view["actions"][2]["agent_id"] == "agent-2"
    assert view["actions"][2]["agent_name"] == "Release advisor"
    assert view["actions"][2]["default_agent_id"] == "agent-1"
    assert view["actions"][3]["kind"] == "automation"
    assert view["actions"][3]["job_id"] == "job-1"


def test_automation_explains_its_live_process_and_schedule():
    job = SimpleNamespace(
        job_id="job-1", name="Weekly request review", enabled=False,
        last_status=None, job_type="interval", schedule_kind="every",
        every_seconds=604800, run_at=None, cron_expr=None, timezone="UTC",
        payload_message=None,
        execution_target={"binding_id": "binding-1", "workflow_id": "workflow-1"},
    )
    view = project_workspace_app_view(_workspace(), [_flow()], [job])
    action = next(item for item in view["available_actions"] if item["kind"] == "automation")
    assert action["description"] == "Review and categorize a request"
    assert action["target_title"] == "Triage a request"
    assert action["target_workflow_id"] == "workflow-1"
    assert action["every_seconds"] == 604800
    assert action["enabled"] is False


def test_unarranged_page_leaves_out_what_a_flow_already_covers():
    def job(job_id, target):
        return SimpleNamespace(
            job_id=job_id, name=job_id, enabled=True, last_status=None, job_type="interval",
            schedule_kind="every", every_seconds=86400, run_at=None, cron_expr=None, timezone="UTC",
            payload_message=None, execution_target=target,
        )
    jobs = [
        job("schedules-the-flow", {"binding_id": "binding-1", "workflow_id": "workflow-1"}),
        job("weekly-review", {"service_key": "customer_support"}),
        job("assessment", {"workflow_slug": "workspace-evidence-assessment"}),
    ]
    view = project_workspace_app_view(
        _workspace(), [_flow()], jobs,
        flow_service_keys={"customer_support"}, plumbing_keys={"automation:assessment"},
    )
    # The service runs inside the flow, one automation only schedules the flow,
    # and the assessment refreshes the tracking tiles; the rest stays.
    assert [action["key"] for action in view["actions"]] == ["workflow:binding-1", "automation:weekly-review"]
    assert view["config"]["hidden"] == [
        "service:customer_support", "automation:schedules-the-flow", "automation:assessment",
    ]
    assert len(view["available_actions"]) == 5

    # An entry named in order or hidden keeps its explicit place; the rest
    # still follow the default.
    arranged = project_workspace_app_view(
        _workspace({"order": ["service:customer_support"], "hidden": ["automation:weekly-review"]}), [_flow()], jobs,
        flow_service_keys={"customer_support"}, plumbing_keys={"automation:assessment"},
    )
    assert [action["key"] for action in arranged["actions"]] == ["service:customer_support", "workflow:binding-1"]
    assert arranged["config"]["hidden"] == [
        "automation:weekly-review", "automation:schedules-the-flow", "automation:assessment",
    ]

    # A page is never left empty.
    alone = project_workspace_app_view(_workspace(), [], [], flow_service_keys={"customer_support"})
    assert [action["key"] for action in alone["actions"]] == ["service:customer_support"]


def test_app_view_target_patch_can_be_removed_without_changing_service():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update", "payload": {"targets": {"service:customer_support": "agent-2"}},
    })
    assert updated["operating_model"]["app_view"]["targets"] == {"service:customer_support": "agent-2"}
    assert updated["operating_model"]["services"] == state["operating_model"]["services"]
    removed = _apply_single_patch(updated, {
        "op": "app_view.update", "payload": {"targets": {"service:customer_support": None}},
    })
    assert removed["operating_model"]["app_view"]["targets"] == {}


def test_app_view_operation_changes_presentation_without_changing_service():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {
        "op": "app_view.update",
        "payload": {"title": "Help desk", "description": "Ask a question.",
                    "labels": {"service:customer_support": "Customer help"},
                    "descriptions": {"service:customer_support": "Get an answer."},
                    "visual": "motion", "layout": "cover"},
    })
    assert updated["operating_model"]["services"][0]["name"] == "Handle customer questions"
    assert updated["operating_model"]["app_view"]["labels"] == {
        "service:customer_support": "Customer help",
    }
    assert updated["operating_model"]["app_view"]["visual"] == "motion"
    assert updated["operating_model"]["app_view"]["layout"] == "cover"
    assert updated["operating_model"]["app_view"]["title"] == "Help desk"
    assert updated["operating_model"]["app_view"]["descriptions"] == {
        "service:customer_support": "Get an answer.",
    }
    assert project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": updated["operating_model"]}),
        [],
    )["actions"][0]["title"] == "Customer help"

    translated = _apply_single_patch(updated, {
        "op": "app_view.update",
        "payload": {"i18n": {"zh": {"title": "客服台", "labels": {"service:customer_support": "客户帮助"}}}},
    })
    assert project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": translated["operating_model"]}),
        [], locale="zh",
    )["actions"][0]["title"] == "客户帮助"
    removed = _apply_single_patch(translated, {"op": "app_view.update", "payload": {"i18n": {"zh": None}}})
    assert "i18n" not in removed["operating_model"]["app_view"]

    restored = _apply_single_patch(updated, {
        "op": "app_view.update",
        "payload": {"labels": {"service:customer_support": None}},
    })
    assert project_workspace_app_view(
        SimpleNamespace(**{**vars(_workspace()), "operating_model": restored["operating_model"]}),
        [],
    )["actions"][0]["title"] == "Handle customer questions"


@pytest.mark.asyncio
async def test_architect_rejects_legacy_tool_wrapper_for_project_workflow(monkeypatch):
    draft = SimpleNamespace(fields={})
    monkeypatch.setattr(workspace_arch_tools, "_load_draft", AsyncMock(return_value=draft))
    result = await workspace_arch_tools._propose_workflow(
        None, entity_id="entity-1", draft_id="draft-1", slug="deliveries", name="Deliveries",
        steps=[{"id": "create", "type": "tool", "config": {"tool": "manor"}}],
    )
    assert json.loads(result)["ok"] is False
    assert "workflow_project" in json.loads(result)["error"]


@pytest.mark.parametrize("config", [
    {"hero": "https://example.com"},
    {"order": ["service:one", "service:one"]},
    {"labels": {"service:one": ""}},
    {"title": ""},
    {"eyebrow": "x" * 61},
    {"description": "x" * 601},
    {"descriptions": {"service:one": "x" * 241}},
    {"forms": {"unknown:one": [{"key": "x", "label": "X", "type": "text"}]}},
    {"forms": {"service:one": [{"key": "x", "label": "X", "type": "file"}, {"key": "x", "label": "Y", "type": "text"}]}},
    {"forms": {"service:one": [{"key": "x", "label": "X", "type": "select"}]}},
    {"visual": "video"},
    {"motion_cover_url": "https://example.com/animated.mp4"},
    {"motion_cover_url": "javascript:alert(1)", "motion_cover_source_url": "/cover.png"},
    {"motion_cover_url": 42, "motion_cover_source_url": "/cover.png"},
    {"cover_image_url": "javascript:alert(1)"},
    {"theme": {"accent": "teal"}},
    {"theme": {"surface": "glass"}},
    {"theme": {"radius": "round", "font": "Comic Sans"}},
    {"layout": "dashboard"},
    {"layout": "compact"},
    {"targets": {"service:one": "../other-workspace"}},
    {"components": "arbitrary UI"},
    {"sections": [{"block": "dashboard"}]},
    {"sections": [{"block": "outputs", "source": "folder:/orders"}]},
    {"sections": [{"block": "outputs", "view": "board"}]},
    {"sections": [{"block": "outputs", "limit": 500}]},
    {"sections": [{"block": "actions", "source": "workspace"}]},
    {"sections": [{"block": "queue", "source": "approvals:all"}]},
    {"sections": [{"block": "queue"}, {"id": "again", "block": "queue"}]},
    {"sections": [{"id": "same", "block": "outputs"}, {"id": "same", "block": "outputs"}]},
    {"sections": [{"block": "outputs"}] * 9},
    {"sections": [{"block": "metrics", "view": "list"}]},
    {"sections": [{"block": "metrics", "limit": 13}]},
    {"sections": [{"block": "metrics", "source": "workflow:binding-1"}]},
    {"sections": [{"block": "metrics", "items": ["orders"]}]},
    {"sections": [{"block": "metrics", "items": ["stat:orders", "stat:orders"]}]},
    {"sections": [{"block": "outputs", "items": ["stat:orders"]}]},
    {"icons": {"workflow:binding-1": "unicorn"}},
    {"icons": {"not-a-key": "music"}},
])
def test_app_view_rejects_untrusted_layout_or_action_references(config):
    with pytest.raises(ValueError):
        normalize_app_view_config(config)


def test_app_action_inputs_keep_the_steps_their_form_is_paged_by():
    flow = SimpleNamespace(
        binding_id="binding-1", workflow_id="workflow-1", title="Launch", description="Start",
        placeholder="", run_inputs=(
            {"key": "song_title", "label": "Song title", "required": True, "step": "song", "step_label": "Song"},
            {"key": "media_files", "label": "Media", "type": "json", "required": True,
             "step": "materials", "step_label": "Materials"},
        ),
    )
    view = project_workspace_app_view(_workspace(), [flow])
    action = next(row for row in view["actions"] if row["key"] == "workflow:binding-1")
    assert [(item["key"], item["step"], item["step_label"]) for item in action["inputs"]] == [
        ("song_title", "song", "Song"), ("media_files", "materials", "Materials"),
    ]


@pytest.mark.parametrize("key", ["service:customer_support", "workflow:binding-1", "automation:job"])
@pytest.mark.parametrize("target,kind", [("agent:agent", "chat"), ("agent:default", "chat"),
                                        ("workflow:binding-1", "workflow"), ("automation:job", "automation")])
def test_every_app_entry_can_choose_each_runner_and_uses_its_input_contract(key, target, kind):
    fields = [{"key": "brief", "label": "Brief", "type": "text"}]
    job = SimpleNamespace(job_id="job", name="Review", enabled=False, last_status="succeeded",
                          payload_message="Review the release", schedule_kind="cron", cron_expr="0 9 * * *")
    config = {"targets": {key: target}, "forms": {key: fields}, "labels": {key: "Custom entry"}}
    view = project_workspace_app_view(_workspace(config), [_flow()], [job], [{"id": "agent", "name": "Writer"}])
    action = next(row for row in view["actions"] if row["key"] == key)
    assert action["kind"] == kind
    assert action["title"] == "Custom entry"
    assert action["available"] is True
    if kind == "chat":
        assert action["inputs"][0]["key"] == "brief"
        assert action.get("agent_id") == ("agent" if target == "agent:agent" else None)
        assert not action.get("binding_id") and not action.get("job_id")
    elif kind == "workflow":
        assert action["binding_id"] == "binding-1"
        assert action["inputs"][0]["key"] == "request"
        assert not action.get("agent_id") and not action.get("job_id")
    else:
        assert action["job_id"] == "job"
        assert action["enabled"] is False
        assert action["instructions"] == "Review the release"
        assert action["cron_expr"] == "0 9 * * *"
        assert action["inputs"] == []
        assert not action.get("agent_id") and not action.get("binding_id")


def test_app_sections_default_page_and_removed_flows_are_unavailable():
    default = project_workspace_app_view(_workspace(), [_flow()])
    assert [(s["id"], s["block"], s["source"], s["implicit"]) for s in default["sections"]] == [
        ("connections", "connections", None, True),
        ("queue", "queue", "approvals:pending", True),
        ("actions", "actions", None, True),
        ("outputs", "outputs", "workspace", True),
    ]
    assert default["config"]["sections"] is None

    configured = project_workspace_app_view(_workspace({"sections": [
        {"id": "reports", "block": "outputs", "source": "workflow:binding-1", "view": "gallery", "limit": 12},
        {"id": "gone", "block": "outputs", "source": "workflow:removed"},
        {"block": "actions"},
    ]}), [_flow()])
    # Functions come before outputs whatever order they were written in.
    assert [(s["id"], s["available"], s["source_title"], s["implicit"]) for s in configured["sections"]] == [
        ("actions", True, None, False),
        ("reports", True, "Triage a request", False),
        ("gone", False, None, False),
    ]
    assert configured["sections"][1]["view"] == "gallery" and configured["sections"][1]["limit"] == 12
    assert configured["sections"][2]["limit"] == 8


def test_app_view_update_replaces_and_resets_sections():
    updated = _apply_single_patch({"operating_model": {}}, {
        "op": "app_view.update",
        "payload": {"sections": [{"block": "outputs", "view": "gallery"}, {"block": "actions"}]},
    })
    assert [s["id"] for s in updated["operating_model"]["app_view"]["sections"]] == ["actions", "outputs"]
    kept = _apply_single_patch(updated, {"op": "app_view.update", "payload": {"title": "Studio"}})
    assert kept["operating_model"]["app_view"]["sections"] == updated["operating_model"]["app_view"]["sections"]
    reset = _apply_single_patch(kept, {"op": "app_view.update", "payload": {"sections": None}})
    assert reset["operating_model"]["app_view"]["sections"] is None


def test_a_section_title_heads_it_and_is_kept_only_when_set():
    view = normalize_app_view_config({"sections": [
        {"id": "decks", "block": "outputs", "source": "workflow:binding-1", "title": "  Decks "}, {"block": "actions"},
    ]})
    assert [section.get("title") for section in view["sections"]] == [None, "Decks"]
    assert "title" not in view["sections"][0]
    projected = project_workspace_app_view(_workspace({"sections": view["sections"]}), [_flow()])
    assert next(s for s in projected["sections"] if s["id"] == "decks")["title"] == "Decks"
    for title in ("", "x" * 61, 3):
        with pytest.raises(ValueError, match="section titles"):
            normalize_app_view_config({"sections": [{"block": "outputs", "title": title}]})


@pytest.mark.parametrize("target,kind", [("agent:missing", "chat"), ("workflow:missing", "workflow"),
                                        ("automation:missing", "automation")])
def test_missing_typed_targets_never_fall_back_to_the_original_automation(target, kind):
    job = SimpleNamespace(job_id="job", name="Review", enabled=True, last_status=None)
    view = project_workspace_app_view(_workspace({"targets": {"automation:job": target}}), [_flow()], [job])
    action = next(row for row in view["actions"] if row["key"] == "automation:job")
    assert action["kind"] == kind
    assert action["available"] is False
    assert not action.get("agent_id") and not action.get("binding_id") and not action.get("job_id")
def test_list_workspace_chat_entrypoints_accepts_unpublished_flag():
    import inspect
    from packages.core.services.workspace_workflow_router import list_workspace_chat_entrypoints

    params = inspect.signature(list_workspace_chat_entrypoints).parameters
    assert params["include_unpublished"].default is False


async def _seed_chat_entrypoint(
    db,
    *,
    entity_id: str,
    workspace_id: str,
    title: str,
    binding_status: str = "active",
    enabled: bool = True,
    definition_status: str = "active",
    is_active: bool = True,
):
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition

    workflow = WorkflowDefinition(
        entity_id=entity_id,
        workspace_id=workspace_id,
        name=title,
        steps=[],
        is_active=is_active,
        status=definition_status,
    )
    db.add(workflow)
    await db.flush()
    binding = WorkflowBinding(
        entity_id=entity_id,
        workflow_id=workflow.id,
        workspace_id=workspace_id,
        name=title,
        trigger_type="manual",
        enabled=enabled,
        status=binding_status,
        config={"chat_entrypoint": {"enabled": True, "title": title}},
    )
    db.add(binding)
    await db.flush()
    return binding


@pytest.mark.asyncio
async def test_unpublished_chat_entrypoints_are_opt_in_and_never_launchable(db_session):
    from packages.core.models.base import generate_ulid
    from packages.core.models.workspace import Workspace
    from packages.core.services.workspace_workflow_router import (
        get_workspace_chat_entrypoint,
        list_workspace_chat_entrypoints,
    )

    entity_id = generate_ulid()
    workspace = Workspace(entity_id=entity_id, name="App View Flows", status="active")
    db_session.add(workspace)
    await db_session.flush()

    published = await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id, title="Published flow",
    )
    paused = await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Paused flow", binding_status="paused",
    )
    draft = await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Draft flow", binding_status="draft",
    )
    await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Retired flow", binding_status="inactive",
    )
    await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Disabled flow", enabled=False,
    )
    await _seed_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id,
        title="Inactive definition", is_active=False, definition_status="inactive",
    )
    await db_session.commit()

    published_ids = {
        item.binding_id
        for item in await list_workspace_chat_entrypoints(
            db_session, entity_id=entity_id, workspace_id=workspace.id,
        )
    }
    unpublished_ids = {
        item.binding_id
        for item in await list_workspace_chat_entrypoints(
            db_session, entity_id=entity_id, workspace_id=workspace.id,
            include_unpublished=True,
        )
    }

    assert published_ids == {published.id}
    assert unpublished_ids == {published.id, paused.id, draft.id}
    assert await get_workspace_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id, binding_id=paused.id,
    ) is None
    assert await get_workspace_chat_entrypoint(
        db_session, entity_id=entity_id, workspace_id=workspace.id, binding_id=published.id,
    ) is not None


@pytest.mark.asyncio
async def test_workspace_app_view_endpoint_lists_without_500(client):
    registration_response = await register_user_and_get_token(client, {
        "username": "app_view_unpublished_listing",
        "email": "app_view_unpublished_listing@test.com",
        "password": "pass123456789",
        "entity_name": "App View Unpublished Listing",
    })
    registration = registration_response.json()
    headers = {"Authorization": f"Bearer {registration['access_token']}"}
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={
        "name": "App View Workspace",
    })).json()
    workflow = (await client.post("/api/v1/workflows", headers=headers, json={
        "name": "Draft app action",
        "steps": [
            {"id": "trigger", "type": "trigger", "name": "Start", "config": {}, "next": ["end"]},
            {"id": "end", "type": "end", "name": "Done", "config": {}, "next": []},
        ],
    })).json()
    created = await client.post("/api/v1/workflows/bindings", headers=headers, json={
        "workflow_id": workflow["id"],
        "workspace_id": workspace["id"],
        "trigger_type": "manual",
        "config": {"chat_entrypoint": {"enabled": True, "title": "Draft app action"}},
    })
    assert created.status_code == 201, created.text
    updated = await client.put(
        f"/api/v1/workflows/bindings/{created.json()['id']}",
        headers=headers,
        json={"status": "paused"},
    )
    assert updated.status_code == 200, updated.text
    binding = updated.json()

    response = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/app-view",
        headers=headers,
    )
    listed = await client.get(
        f"/api/v1/workspaces/{workspace['id']}/chat/entrypoints",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    action_keys = {action["key"] for action in response.json()["actions"]}
    assert f"workflow:{binding['id']}" in action_keys
    assert listed.status_code == 200, listed.text
    assert listed.json() == []


def test_data_view_contract_is_optional_but_rejects_unbound_or_incompatible_reads():
    config = normalize_app_view_config({
        "data_sources": {
            "campaigns": {
                "type": "workflow_projects", "project_type": "campaign", "schema_versions": [1],
                "producer_workflows": ["workflow_definition:campaign_flow"],
                "launch_workflows": [{"workflow": "workflow:campaign_launch", "project_steps": ["create_campaign"]}],
                "fields": [["status"], ["document_id"]],
            },
            "deliverables": {
                "type": "project_documents", "parent": "campaigns", "document_path": ["document_id"],
                "document_role": "deliverable", "fields": [],
            },
            "health": {"type": "workspace_stats", "stat_keys": ["customer.health"], "fields": []},
        },
        "components": [{
            "type": "data_view", "schema_version": 1, "key": "results", "title": "Results",
            "context": {"kind": "project", "source": "campaigns"},
            "blocks": [
                {"key": "progress", "type": "progress", "source": "campaigns"},
                {"key": "files", "type": "documents", "source": "deliverables"},
                {"key": "health", "type": "metrics", "source": "health"},
            ],
        }],
    })
    workflow = {"slug": "campaign_flow", "steps": [{"type": "workflow_project", "config": {
        "project_type": "campaign", "state_schema": {"type": "object", "properties": {
            "status": {"type": "string"}, "document_id": {"type": "string"},
            "publishing": {"type": "object", "properties": {"run_id": {"type": "string"}}},
        }},
    }}]}
    catalog = {"workflow_definitions": {"campaign_flow"}, "workflow_bindings": {"campaign_launch"},
               "stat_keys": {"customer.health"}, "workflows": [workflow]}
    validate_app_view_data_contract(config["data_sources"], config["components"], catalog=catalog)
    config["data_sources"]["campaigns"]["execution_run_id_path"] = ["publishing", "run_id"]
    normalized = normalize_app_view_config(config)
    validate_app_view_data_contract(normalized["data_sources"], normalized["components"], catalog=catalog)
    config["data_sources"]["campaigns"]["execution_run_id_path"] = ["publishing", "unknown"]
    with pytest.raises(ValueError, match="execution_run_id_path"):
        validate_app_view_data_contract(config["data_sources"], config["components"], catalog=catalog)
    config["data_sources"]["campaigns"].pop("execution_run_id_path")
    with pytest.raises(ValueError, match="unavailable workflow definition"):
        validate_app_view_data_contract(config["data_sources"], config["components"], catalog={
            **catalog, "workflow_definitions": set(),
        })
    bad_components = [{**config["components"][0], "blocks": [
        {"key": "wrong", "type": "metrics", "source": "deliverables", "limit": 20,
         "fields": [], "metric_keys": [], "title": None},
    ]}]
    with pytest.raises(ValueError, match="incompatible source type"):
        validate_app_view_data_contract(config["data_sources"], bad_components, catalog=catalog)


def test_data_view_display_normalizes_defaults_and_complete_groups():
    source = {
        "type": "workflow_projects", "project_type": "campaign", "fields": [],
        "producer_workflows": ["workflow_definition:campaign"],
    }
    config = normalize_app_view_config({
        "data_sources": {"campaigns": source},
        "components": [{
            "type": "data_view", "schema_version": 1, "key": "campaign_status",
            "context": {"kind": "project", "source": "campaigns"},
            "blocks": [
                {"key": "metrics", "type": "progress", "source": "campaigns"},
                {"key": "published", "type": "records", "source": "campaigns"},
                {"key": "documents", "type": "documents", "source": "campaigns"},
            ],
            "display": {
                "layout": "tabs",
                "groups": [
                    {"key": "overview", "title": "Overview", "blocks": ["metrics", "published"]},
                    {"key": "assets", "title": "Assets", "blocks": ["documents"]},
                ],
                "hidden_blocks": ["documents"],
            },
        }],
    })
    assert config["components"][0]["display"] == {
        "layout": "tabs",
        "groups": [
            {"key": "overview", "title": "Overview", "blocks": ["metrics", "published"]},
            {"key": "assets", "title": "Assets", "blocks": ["documents"]},
        ],
        "hidden": False,
        "hidden_blocks": ["documents"],
    }

    default_config = normalize_app_view_config({
        "data_sources": {"campaigns": source},
        "components": [{
            "type": "data_view", "schema_version": 1, "key": "default_view",
            "context": {"kind": "project", "source": "campaigns"},
            "blocks": [{"key": "metrics", "type": "progress", "source": "campaigns"}],
        }],
    })
    assert default_config["components"][0]["display"] == {
        "layout": "stack", "groups": [], "hidden": False, "hidden_blocks": [],
    }


@pytest.mark.parametrize("display", [
    "tabs",
    {"unknown": True},
    {"layout": "grid"},
    {"hidden": 1},
    {"hidden_blocks": ["unknown"]},
    {"hidden_blocks": ["metrics", "metrics"]},
    {"layout": "tabs"},
    {"groups": [{"key": "overview", "title": "Overview", "blocks": ["metrics", "metrics"]}]},
    {"groups": [{"key": "overview", "title": "Overview", "blocks": ["unknown"]}]},
    {"groups": [{"key": "overview", "title": "Overview", "blocks": ["metrics"]}]},
])
def test_data_view_display_rejects_invalid_or_incomplete_references(display):
    with pytest.raises(ValueError):
        normalize_app_view_config({
            "data_sources": {
                "campaigns": {"type": "workflow_projects", "project_type": "campaign", "fields": [],
                              "producer_workflows": ["workflow_definition:campaign"]},
            },
            "components": [{
                "type": "data_view", "schema_version": 1, "key": "campaign_status",
                "context": {"kind": "project", "source": "campaigns"},
                "blocks": [
                    {"key": "metrics", "type": "progress", "source": "campaigns"},
                    {"key": "documents", "type": "documents", "source": "campaigns"},
                ],
                "display": display,
            }],
        })


def test_app_view_update_persists_data_view_display_contract():
    state = {"operating_model": _workspace().operating_model}
    updated = _apply_single_patch(state, {"op": "app_view.update", "payload": {
        "data_sources": {
            "campaigns": {"type": "workflow_projects", "project_type": "campaign", "fields": [],
                          "producer_workflows": ["workflow_definition:campaign"]},
        },
        "components": [{
            "type": "data_view", "schema_version": 1, "key": "campaign_status",
            "context": {"kind": "project", "source": "campaigns"},
            "blocks": [{"key": "metrics", "type": "progress", "source": "campaigns"}],
            "display": {"hidden": True},
        }],
    }})
    assert updated["operating_model"]["app_view"]["components"][0]["display"] == {
        "layout": "stack", "groups": [], "hidden": True, "hidden_blocks": [],
    }


def test_project_data_source_requires_a_producer_contract():
    with pytest.raises(ValueError, match="require producer_workflows"):
        normalize_app_view_config({"data_sources": {
            "projects": {"type": "workflow_projects", "project_type": "client_delivery", "fields": []},
        }})


def test_project_source_normalizes_stage_datetime_order_and_rollup_contract():
    config = normalize_app_view_config({"data_sources": {
        "courses": {"type": "workflow_projects", "project_type": "course", "fields": [["due_at"]],
                    "stages": ["ready", "scheduled", "ready"],
                    "order": {"field": ["due_at"], "type": "datetime", "direction": "asc"},
                    "producer_workflows": ["workflow_definition:course"]},
        "lessons": {"type": "related_projects", "project_type": "lesson", "parent": "courses",
                    "parent_state_path": ["course_id"], "fields": [["due_at"]],
                    "stages": ["scheduled", "ready"],
                    "order": {"field": ["due_at"], "type": "datetime", "direction": "asc"},
                    "producer_workflows": ["workflow_definition:lesson"]},
    }, "components": [{"type": "data_view", "key": "course_plan", "context": {"kind": "project", "source": "courses"},
                         "blocks": [{"key": "rollup", "type": "project_rollup", "source": "lessons",
                                     "count_by_stage": True, "first": {"stages": ["scheduled", "ready"]}},
                                    {"key": "progress", "type": "progress", "source": "lessons", "fields": [
                                        {"label": "Due", "field": ["due_at"], "format": "date"},
                                    ]}]}]})
    assert config["data_sources"]["lessons"]["stages"] == ["scheduled", "ready"]
    assert config["data_sources"]["lessons"]["order"]["field"] == ["due_at"]
    assert config["components"][0]["blocks"][0]["first"]["stages"] == ["scheduled", "ready"]
    assert config["components"][0]["blocks"][1]["fields"] == [
        {"label": "Due", "field": ["due_at"], "format": "date", "hide_when_empty": False},
    ]


@pytest.mark.parametrize("source_stages,date_field", [(["scheduled"], ["due_at"]), (None, ["other_at"])])
def test_calendar_display_requires_an_all_stage_source_and_its_ordered_date(source_stages, date_field):
    with pytest.raises(ValueError, match="calendar display references"):
        normalize_app_view_config({"data_sources": {
            "courses": {"type": "workflow_projects", "project_type": "course", "fields": [["title"]],
                        "producer_workflows": ["workflow_definition:course"]},
            "reminders": {"type": "related_projects", "project_type": "reminder", "parent": "courses",
                          "parent_state_path": ["course_id"], "fields": [["due_at"]],
                          **({"stages": source_stages} if source_stages is not None else {}),
                          "order": {"field": ["due_at"], "type": "datetime", "direction": "asc"},
                          "producer_workflows": ["workflow_definition:reminder"]},
        }, "components": [{"type": "data_view", "key": "course_calendar", "context": {"kind": "project", "source": "courses"},
                             "blocks": [
                                 {"key": "calendar", "type": "progress", "source": "reminders"},
                                 {"key": "rollup", "type": "project_rollup", "source": "reminders", "count_by_stage": True},
                                 {"key": "context", "type": "summary", "source": "courses", "fields": [
                                     {"label": "Title", "field": ["title"]},
                                 ]},
                             ], "display": {"calendar": {
                                 "block": "calendar", "date_field": date_field, "rollup_block": "rollup",
                                 "context_block": "context", "attention_stages": [], "collection_title": "Courses",
                             }}}]})

def test_workspace_progress_lists_projects_with_labeled_fields():
    """A Workspace-level list of projects reads like a selected project's fields."""
    sources = {
        "creators": {"type": "workflow_projects", "project_type": "creator", "fields": [["profile_url"], ["priority"]],
                     "producer_workflows": ["workflow_definition:creator"]},
        "jobs": {"type": "owned_automations", "parent": "creators"},
    }
    fields = [{"label": "Profile", "field": ["profile_url"], "format": "link", "hide_when_empty": True},
              {"label": "Priority", "field": ["priority"], "labels": {"high": "High"}, "hide_when_empty": True}]
    config = normalize_app_view_config({"data_sources": sources, "components": [{
        "type": "data_view", "key": "tracked", "context": {"kind": "workspace"},
        "blocks": [{"key": "creators", "type": "progress", "source": "creators", "fields": fields}]}]})
    assert config["components"][0]["blocks"][0]["fields"] == [
        {"label": "Profile", "field": ["profile_url"], "format": "link", "hide_when_empty": True},
        {"label": "Priority", "field": ["priority"], "format": "text", "hide_when_empty": True, "labels": {"high": "High"}},
    ]
    with pytest.raises(ValueError, match="progress structured fields require a project source"):
        normalize_app_view_config({"data_sources": sources, "components": [{
            "type": "data_view", "key": "tracked", "context": {"kind": "workspace"},
            "blocks": [{"key": "jobs", "type": "progress", "source": "jobs", "fields": fields[:1]}]}]})


def test_training_delivery_presentation_contract_is_portable_and_bounded():
    config = normalize_app_view_config({
        "layout": "feature",
        "data_sources": {
            "training": {
                "type": "workflow_projects", "project_type": "training_delivery",
                "fields": [["title"]], "producer_workflows": ["workflow_definition:training_delivery"],
            },
            "deliveries": {
                "type": "related_projects", "project_type": "training_delivery_item", "parent": "training",
                "parent_state_path": ["training_id"], "fields": [["title"]],
                "producer_workflows": ["workflow_definition:training_delivery_item"],
            },
        },
        "components": [{
            "type": "data_view", "key": "training_status", "context": {"kind": "project", "source": "training"},
            "blocks": [
                {"key": "delivery_rollup", "type": "project_rollup", "source": "deliveries", "count_by_stage": True,
                 "completion": {"stages": ["delivered"], "label": "Delivered"},
                 "stage_labels": {"ready": "Materials ready", "leased": "Work in progress"},
                 "empty_message": "No training deliveries have been scheduled."},
                {"key": "delivery_progress", "type": "progress", "source": "deliveries",
                 "stage_labels": {"ready": "Materials ready", "leased": "Work in progress"},
                 "empty_message": "No delivery work is available yet."},
            ],
        }],
    })
    rollup, progress = config["components"][0]["blocks"]
    assert config["layout"] == "feature"
    assert rollup["completion"] == {"stages": ["delivered"], "label": "Delivered"}
    assert rollup["stage_labels"] == {"ready": "Materials ready", "leased": "Work in progress"}
    assert progress["empty_message"] == "No delivery work is available yet."

    invalid = {**config, "components": [{**config["components"][0], "blocks": [
        {**rollup, "completion": {"stages": [], "label": "Delivered"}},
        progress,
    ]}]}
    with pytest.raises(ValueError, match="completion"):
        normalize_app_view_config(invalid)

    invalid["components"][0]["blocks"][0] = {
        **rollup, "count_by_stage": False, "first": {"stages": ["ready"]},
    }
    with pytest.raises(ValueError, match="completion requires count_by_stage"):
        normalize_app_view_config(invalid)

    invalid["components"][0]["blocks"][0] = {**rollup, "type": "records", "stage_labels": None}
    with pytest.raises(ValueError, match="completion"):
        normalize_app_view_config(invalid)


def test_a_packs_block_follows_a_plan_and_offers_one_action_per_pack():
    sources = {
        "series": {"type": "workflow_projects", "project_type": "series", "fields": [["title"]],
                   "producer_workflows": ["workflow_definition:make"]},
        "plan": {"type": "project_records", "parent": "series", "record_path": ["episodes"]},
        "results": {"type": "project_files", "parent": "series", "results_only": True},
    }
    packs = {"key": "episodes", "type": "packs", "source": "results", "plan": "plan",
             "item_action": {"action": "action:series-remake", "input": "episode_id", "label": " Remake ",
                             "stages": ["made"]},
             "plan_action": {"action": "action:series-make", "label": "Make this episode",
                             "inputs": {"series_id": "parent", "episode_code": "code"}}}

    def config(block):
        return {"data_sources": sources, "components": [{"type": "data_view", "key": "series",
                "context": {"kind": "project", "source": "series"}, "blocks": [block]}]}

    block = normalize_app_view_config(config(packs))["components"][0]["blocks"][0]
    assert block["plan"] == "plan"
    assert block["item_action"] == {"action": "action:series-remake", "input": "episode_id", "label": "Remake",
                                    "stages": ["made"]}
    assert block["plan_action"]["inputs"] == {"series_id": "parent", "episode_code": "code"}
    # The plan is a project's records, and only packs follow one or carry an action.
    for bad in ({**packs, "plan": "results"}, {**packs, "item_action": {"action": "remake", "input": "x", "label": "R"}},
                {**packs, "type": "documents"},
                {**packs, "plan_action": {**packs["plan_action"], "inputs": {"series_id": "title"}}},
                {key: value for key, value in packs.items() if key != "plan"}):
        with pytest.raises(ValueError, match="plan|item_action|plan_action"):
            normalize_app_view_config(config(bad))


def test_document_source_requires_a_string_typed_schema_field():
    config = normalize_app_view_config({"data_sources": {
        "projects": {"type": "workflow_projects", "project_type": "client_delivery", "fields": [],
                     "producer_workflows": ["workflow_definition:delivery"]},
        "documents": {"type": "project_documents", "parent": "projects", "document_path": ["artifact"]},
    }})
    workflow = {"slug": "delivery", "steps": [{"type": "workflow_project", "config": {
        "project_type": "client_delivery", "state_schema": {"properties": {"artifact": {"type": "object"}}},
    }}]}
    with pytest.raises(ValueError, match="string-typed"):
        validate_app_view_data_contract(config["data_sources"], [], catalog={
            "workflow_definitions": {"delivery"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [workflow],
        })


def test_record_metrics_and_display_require_declared_project_ancestry():
    config = normalize_app_view_config({"data_sources": {
        "courses": {"type": "workflow_projects", "project_type": "course", "fields": [["title"]],
                    "label_fields": [["title"]],
                    "producer_workflows": ["workflow_definition:course"]},
        "lessons": {"type": "related_projects", "project_type": "lesson", "parent": "courses",
                    "parent_state_path": ["course_id"], "fields": [["video_title"]],
                    "producer_workflows": ["workflow_definition:lesson"]},
        "publications": {"type": "project_records", "parent": "lessons", "record_path": ["publication"],
                         "record_schema": "publication-receipt/v1", "fields": []},
        "results": {"type": "publication_stats", "parent": "publications", "subject": "courses",
                    "metric_keys": ["views"], "fields": []},
    }, "components": [{"type": "data_view", "schema_version": 1, "key": "course", "context": {"kind": "project", "source": "courses"},
        "visibility": {"block": "published", "when": "has_observations"},
        "blocks": [{"key": "published", "type": "records", "source": "publications", "metrics_source": "results",
                    "record_display": {"title": {"source": "lessons", "field": ["video_title"]},
                                       "fields": [{"label": "Course", "source": "courses", "field": ["title"]}]}}]}]})
    workflows = [
        {"slug": "course", "steps": [{"config": {
            "project_type": "course", "state_schema": {"properties": {"title": {"type": "string"}}},
        }}]},
        {"slug": "lesson", "steps": [{"config": {
            "project_type": "lesson", "state_schema": {"properties": {
                "course_id": {"type": "string"}, "video_title": {"type": "string"}, "publication": {"type": "object"},
            }},
        }}]},
    ]
    validate_app_view_data_contract(config["data_sources"], config["components"], catalog={
        "workflow_definitions": {"course", "lesson"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": workflows,
    })
    # The default collector is implicit, so existing configs round-trip unchanged.
    assert "collector_types" not in config["data_sources"]["results"]
    config["data_sources"]["results"]["collector_types"] = ["manual", "manual"]
    assert normalize_app_view_config(config)["data_sources"]["results"]["collector_types"] == ["manual"]
    for invalid in ([], ["agent"], "manual"):
        config["data_sources"]["results"]["collector_types"] = invalid
        with pytest.raises(ValueError, match="collector_types"):
            normalize_app_view_config(config)
    config["data_sources"]["results"].pop("collector_types")
    component = config["components"][0]
    assert component["visibility"] == {"block": "published", "when": "has_observations"}
    component["blocks"][0]["metrics_display"] = "configured"
    assert normalize_app_view_config(config)["components"][0]["blocks"][0]["metrics_display"] == "configured"
    for invalid in ("observed_only", {}, True):
        component["blocks"][0]["metrics_display"] = invalid
        with pytest.raises(ValueError, match="metrics_display"):
            normalize_app_view_config(config)
    component["blocks"][0]["metrics_display"] = "configured"
    component["visibility"]["block"] = "missing"
    with pytest.raises(ValueError, match="project records block"):
        normalize_app_view_config(config)
    component["visibility"]["block"] = "published"
    component["context"]["kind"] = "workspace"
    with pytest.raises(ValueError, match="project context"):
        normalize_app_view_config(config)
    component["context"]["kind"] = "project"
    config["data_sources"]["unrelated"] = {**config["data_sources"]["courses"], "project_type": "unrelated"}
    component["context"]["source"] = "unrelated"
    with pytest.raises(ValueError, match="selected project's records"):
        normalize_app_view_config(config)
    component["context"]["source"] = "courses"
    del config["data_sources"]["unrelated"]
    config["data_sources"]["courses"]["label_fields"] = [["private_notes"]]
    with pytest.raises(ValueError, match="label_fields must be declared"):
        normalize_app_view_config(config)
    config["data_sources"]["courses"]["label_fields"] = [["title"]]
    workflows[0]["steps"][0]["config"]["state_schema"]["properties"]["title"]["type"] = "object"
    with pytest.raises(ValueError, match="label_fields require string-typed"):
        validate_app_view_data_contract(config["data_sources"], config["components"], catalog={
            "workflow_definitions": {"course", "lesson"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": workflows,
        })
    workflows[0]["steps"][0]["config"]["state_schema"]["properties"]["title"]["type"] = "string"
    config["components"][0]["blocks"][0]["metrics_source"] = "courses"
    with pytest.raises(ValueError, match="metrics_source"):
        validate_app_view_data_contract(config["data_sources"], config["components"], catalog={
            "workflow_definitions": {"course", "lesson"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": workflows,
        })
    block = config["components"][0]["blocks"][0]
    block["metrics_source"] = "results"
    for title, message in [
        ({"source": "publications", "field": ["video_title"]}, "not a record ancestor"),
        ({"source": "lessons", "field": ["private_notes"]}, "not declared"),
    ]:
        block["record_display"]["title"] = title
        with pytest.raises(ValueError, match=message):
            normalize_app_view_config(config)


def test_record_media_and_visibility_support_non_publication_projects():
    from copy import deepcopy

    config = normalize_app_view_config({"data_sources": {
        "inspections": {"type": "workflow_projects", "project_type": "inspection", "fields": [["title"], ["outcome"]],
                        "producer_workflows": ["workflow_definition:inspect"]},
        "photos": {"type": "project_documents", "parent": "inspections", "document_path": ["photo"]},
        "other": {"type": "workflow_projects", "project_type": "other", "fields": [],
                  "producer_workflows": ["workflow_definition:other"]},
    }, "components": [{"type": "data_view", "key": "inspections", "context": {"kind": "project", "source": "inspections"},
        "visibility": {"block": "checks", "when": "has_records"},
        "blocks": [{"key": "checks", "type": "records", "source": "inspections", "record_display": {
            "title": {"source": "inspections", "field": ["title"]},
            "fields": [{"label": "Outcome", "source": "inspections", "field": ["outcome"], "labels": {"pass": "Passed"}}],
            "media": [{"source": "photos", "label": "Inspection photo"}],
        }}]}]})
    assert config["components"][0]["blocks"][0]["record_display"]["media"] == [{"source": "photos", "label": "Inspection photo"}]
    invalid = deepcopy(config)
    invalid["data_sources"]["photos"]["parent"] = "other"
    with pytest.raises(ValueError, match="not owned by a record ancestor"):
        normalize_app_view_config(invalid)
    invalid = deepcopy(config)
    invalid["components"][0]["visibility"]["when"] = "has_observations"
    with pytest.raises(ValueError, match="publication records block"):
        normalize_app_view_config(invalid)
    invalid = deepcopy(config)
    invalid["components"][0]["blocks"][0]["record_display"]["media"] = [{"source": "inspections"}]
    with pytest.raises(ValueError, match="document sources"):
        normalize_app_view_config(invalid)
    invalid = deepcopy(config)
    invalid["data_sources"]["results"] = {"type": "publication_stats", "parent": "inspections", "subject": "inspections", "metric_keys": ["views"]}
    invalid["components"][0]["blocks"][0]["metrics_source"] = "results"
    with pytest.raises(ValueError, match="incompatible metrics_source"):
        normalize_app_view_config(invalid)


def test_project_summary_is_scoped_to_the_selected_declared_project_fields():
    source = {
        "type": "workflow_projects", "project_type": "product_launch", "schema_versions": [1],
        "producer_workflows": ["workflow_definition:product_launch"], "launch_workflows": [],
        "fields": [["launch", "status"], ["launch", "date"], ["portal_links"], ["tracking", "ends_at"]],
    }
    config = normalize_app_view_config({"data_sources": {"launches": source}, "components": [{
        "type": "data_view", "schema_version": 1, "key": "launch_context",
        "context": {"kind": "project", "source": "launches"},
        "blocks": [{"key": "summary", "type": "summary", "source": "launches", "fields": [
            {"label": "Launch status", "field": ["launch", "status"], "format": "status", "labels": {"planned": "Planned"}},
            {"label": "Launch date", "field": ["launch", "date"], "format": "date", "hide_when_empty": True},
            {"label": "Customer portals", "field": ["portal_links"], "format": "link_list", "hide_when_empty": True},
            {"label": "Tracking through", "field": ["tracking", "ends_at"], "format": "date"},
        ]}],
    }]})
    workflow = {"slug": "product_launch", "steps": [{"type": "workflow_project", "config": {
        "project_type": "product_launch", "state_schema": {"type": "object", "properties": {
            "launch": {"type": "object", "properties": {"status": {"type": "string"}, "date": {"type": "string"}}},
            "portal_links": {"type": "array"},
            "tracking": {"type": "object", "properties": {"ends_at": {"type": "string"}}},
        }},
    }}]}
    catalog = {"workflow_definitions": {"product_launch"}, "workflow_bindings": set(), "stat_keys": set(), "workflows": [workflow]}
    validate_app_view_data_contract(config["data_sources"], config["components"], catalog=catalog)
    assert config["components"][0]["blocks"][0]["fields"][0]["labels"] == {"planned": "Planned"}

    config["components"][0]["blocks"][0]["fields"][0]["field"] = ["private_notes"]
    with pytest.raises(ValueError, match="must be declared"):
        normalize_app_view_config(config)
    config["components"][0]["blocks"][0]["fields"][0]["field"] = ["launch", "status"]

    # A provider field shows its account from declared fallback paths only.
    provider_field = {"label": "Channel", "field": ["launch", "status"], "format": "provider",
                      "secondary": [["launch", "date"], ["tracking", "ends_at"]]}
    config["components"][0]["blocks"][0]["fields"].append(provider_field)
    assert normalize_app_view_config(config)["components"][0]["blocks"][0]["fields"][-1]["secondary"] == [
        ["launch", "date"], ["tracking", "ends_at"],
    ]
    provider_field["secondary"] = [["private_notes"]]
    with pytest.raises(ValueError, match="must be declared"):
        normalize_app_view_config(config)
    config["components"][0]["blocks"][0]["fields"].pop()

    config["components"][0]["blocks"][0]["fields"][0]["field"] = ["launch", "status"]
    config["components"][0]["context"]["kind"] = "workspace"
    with pytest.raises(ValueError, match="selected project source"):
        normalize_app_view_config(config)
@pytest.mark.asyncio
async def test_app_outputs_section_lists_workspace_runs_with_only_openable_files(client):
    import packages.core.database as db_module
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.user import User
    from packages.core.models.workflow import WorkflowRun, WorkflowTemplateInstallation
    from packages.core.models.workspace import WorkspaceStaff
    from packages.core.services.auth_service import create_access_token, hash_password

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_outputs_owner", "email": "app_outputs_owner@test.com",
        "password": "pass123", "entity_name": "App Outputs",
    })).json()
    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=owner_headers)).json()
    entity_id = me["entity_id"]
    workspace = (await client.post("/api/v1/workspaces", headers=owner_headers, json={"name": "Reports"})).json()
    other = (await client.post("/api/v1/workspaces", headers=owner_headers, json={"name": "Elsewhere"})).json()

    shared, private, trashed, recipe = (generate_ulid() for _ in range(4))
    async with db_module.async_session() as session:
        member = User(entity_id=entity_id, email="app_outputs_member@test.com", display_name="Member",
                      password_hash=hash_password("pass123"), role="member", status="active")
        session.add(member)
        await session.flush()
        session.add(WorkspaceStaff(workspace_id=workspace["id"], user_id=member.id, role="viewer", status="active"))
        session.add_all([
            Document(id=shared, entity_id=entity_id, name="Weekly.docx", fs_path="reports/weekly.docx",
                     file_type="docx", source="upload", visibility="entity", owner_id=me["id"]),
            Document(id=private, entity_id=entity_id, name="Private.docx", fs_path="reports/private.docx",
                     file_type="docx", source="upload", visibility="private", owner_id=me["id"]),
            Document(id=trashed, entity_id=entity_id, name="Old.docx", fs_path="reports/old.docx",
                     file_type="docx", source="upload", visibility="entity", owner_id=me["id"], is_trashed=True),
            # A file made on the way (an edit recipe) is not an output.
            Document(id=recipe, entity_id=entity_id, name="final.video-edit.json", fs_path="reports/final.video-edit.json",
                     file_type="json", source="ai_generated", visibility="entity", owner_id=me["id"],
                     metadata_={"artifact": {"role": "editor_recipe"}}),
        ])
        files = [{"document_id": doc_id, "name": "ignored"} for doc_id in (shared, private, trashed, recipe)]
        # A Blueprint-internal Workflow is hidden from the App, and so are its runs.
        internal_workflow = generate_ulid()
        session.add(WorkflowTemplateInstallation(
            entity_id=entity_id, template_id="bp", workflow_id=internal_workflow,
            source_type="workspace_blueprint", installation_metadata={"internal": True},
        ))
        session.add(WorkflowRun(workflow_id=internal_workflow, entity_id=entity_id, workspace_id=workspace["id"],
                                status="completed", trigger_data={"_workflow_name": "Internal sync"}))
        report_run_id = generate_ulid()
        session.add_all([
            WorkflowRun(id=report_run_id, workflow_id=generate_ulid(), entity_id=entity_id, workspace_id=workspace["id"],
                        binding_id=generate_ulid(), status="completed", trigger_source="manual",
                        variables={"__result": {"files": files}}, trigger_data={"_workflow_name": "Weekly report"}),
            WorkflowRun(workflow_id=generate_ulid(), entity_id=entity_id, workspace_id=other["id"],
                        status="completed", trigger_data={"_workflow_name": "Elsewhere run"}),
        ])
        await session.commit()
        member_headers = {"Authorization": f"Bearer {create_access_token(member.id, entity_id, 'member')}"}

    endpoint = f"/api/v1/workspaces/{workspace['id']}/app-view/sections"
    as_owner = await client.get(f"{endpoint}/outputs", headers=owner_headers)
    assert as_owner.status_code == 200, as_owner.text
    [run] = as_owner.json()["items"]
    assert run["title"] == "Weekly report" and run["status"] == "completed"
    assert [(f["document_id"], f["name"]) for f in run["files"]] == [(shared, "Weekly.docx"), (private, "Private.docx")]
    assert run["file_count"] == 2

    as_member = await client.get(f"{endpoint}/outputs", headers=member_headers)
    assert as_member.status_code == 200, as_member.text
    assert [f["document_id"] for f in as_member.json()["items"][0]["files"]] == [shared]

    # Once the run changes, its summary projection carries the delivered files
    # and publications; file access is still checked per viewer.
    async with db_module.async_session() as session:
        report = await session.get(WorkflowRun, report_run_id)
        report.step_results = {"publish": {"status": "completed", "output": {
            "schema_version": "publication-receipt/v1", "platform": "linkedin", "title": "Weekly report",
            "published_url": "https://www.linkedin.com/feed/update/9", "verification_status": "verified",
        }}}
        await session.commit()
    projected = (await client.get(f"{endpoint}/outputs", headers=member_headers)).json()["items"][0]
    assert [f["document_id"] for f in projected["files"]] == [shared]
    assert projected["publications"] == [{
        "platform": "linkedin", "title": "Weekly report",
        "published_url": "https://www.linkedin.com/feed/update/9", "verification_status": "verified",
    }]

    assert (await client.get(f"{endpoint}/actions", headers=owner_headers)).status_code == 404
    assert (await client.get(f"{endpoint}/unknown", headers=owner_headers)).status_code == 404
    outsider = (await client.post("/api/v1/auth/register", json={
        "username": "app_outputs_outsider", "email": "app_outputs_outsider@test.com",
        "password": "pass123", "entity_name": "Outsider",
    })).json()
    outsider_headers = {"Authorization": f"Bearer {outsider['access_token']}"}
    assert (await client.get(f"{endpoint}/outputs", headers=outsider_headers)).status_code == 404


@pytest.mark.asyncio
async def test_app_metrics_section_follows_the_viewers_stat_order_and_attaches_goals(client):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    import packages.core.database as db_module
    from packages.core.models.base import generate_ulid
    from packages.core.models.goal import Goal, GoalMeasurement
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workflow import WorkflowBinding, WorkflowRun
    from packages.core.models.workspace_stat import WorkspaceStat, WorkspaceStatObservation

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_metrics_owner", "email": "app_metrics_owner@test.com",
        "password": "pass123", "entity_name": "App Metrics",
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    entity_id = (await client.get("/api/v1/auth/me", headers=headers)).json()["entity_id"]
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Shop"})).json()
    now = datetime.now(timezone.utc)

    def stat(key: str, value: float, status: str = "active") -> WorkspaceStat:
        return WorkspaceStat(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace["id"], key=key, name=key,
                             collector_type="manual", status=status, current_value=Decimal(str(value)),
                             current_value_updated_at=now)

    orders, refunds, hidden, archived = stat("orders", 128), stat("refunds", 2.5), stat("hidden", 1), stat("old", 9, "archived")
    # An agent-reviewed stat whose assessment Workflow is running right now.
    views = stat("views", 12480)
    views.collector_type, views.collector_config = "agent_review", {"workflow_slug": "audience-review"}
    views.last_collection_status = "insufficient_evidence"
    views.last_assessment = {"collector_revision": 1, "evidence": {"gaps": ["TikTok is not connected"]}}
    async with db_module.async_session() as session:
        workflow_id, binding_id, next_run = generate_ulid(), generate_ulid(), now + timedelta(hours=4)
        session.add(WorkflowBinding(id=binding_id, entity_id=entity_id, workflow_id=workflow_id, workspace_id=workspace["id"],
                                    config={"workspace_blueprint_workflow_slug": "audience-review"}))
        session.add(ScheduledJob(job_id=f"review-{binding_id}", entity_id=entity_id, workspace_id=workspace["id"],
                                 execution_type="workflow", execution_target={"binding_id": binding_id},
                                 enabled=True, next_run_at=next_run))
        session.add(WorkflowRun(workflow_id=workflow_id, entity_id=entity_id, workspace_id=workspace["id"],
                                binding_id=binding_id, status="running"))
        session.add_all([orders, refunds, hidden, archived, views])
        session.add_all([WorkspaceStatObservation(
            id=generate_ulid(), stat_id=orders.id, entity_id=entity_id, workspace_id=workspace["id"],
            value=Decimal(value), observed_at=now - timedelta(days=age), source="manual", idempotency_key=f"o{age}",
        ) for age, value in ((2, "90"), (1, "110"), (0, "128"))])
        primary = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace["id"], stat_id=refunds.id,
                       title="Refunds under 2%", metric_key="refunds", target_value=Decimal("2"), priority=1,
                       pace_status="behind")
        secondary = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace["id"], stat_id=refunds.id,
                         title="Refunds under 3%", metric_key="refunds", target_value=Decimal("3"), priority=5)
        revenue = Goal(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace["id"], title="Revenue", goal_key="revenue",
                       metric_key="revenue", target_value=Decimal("300000"), current_value=Decimal("186000"))
        session.add_all([primary, secondary, revenue, GoalMeasurement(
            goal_id=revenue.id, measured_at=now, value=Decimal("186000"), source="manual")])
        await session.commit()

    # The page shows tracked numbers only when its config asks for them.
    endpoint = f"/api/v1/workspaces/{workspace['id']}/app-view/sections/metrics"
    assert (await client.get(endpoint, headers=headers)).status_code == 404
    async with db_module.async_session() as session:
        from packages.core.models.workspace import Workspace
        stored = await session.get(Workspace, workspace["id"])
        stored.operating_model = {**(stored.operating_model or {}), "app_view": {"sections": [{"block": "metrics"}]}}
        await session.commit()
    quick_view = await client.put(f"/api/v1/workspaces/{workspace['id']}/stats/quick-view", headers=headers, json={
        "ordered_stat_ids": [refunds.id, orders.id], "hidden_stat_ids": [hidden.id],
    })
    assert quick_view.status_code == 200, quick_view.text
    response = await client.get(f"/api/v1/workspaces/{workspace['id']}/app-view/sections/metrics", headers=headers)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(item["kind"], item["title"]) for item in items] == [
        ("stat", "refunds"), ("stat", "orders"), ("stat", "views"), ("goal", "Revenue"),
    ]
    assert items[0]["goal"]["title"] == "Refunds under 2%" and items[0]["goal"]["pace_status"] == "behind"
    assert [point["value"] for point in items[1]["series"]] == [90, 110, 128]
    # The assessment Workflow's phase wins, exactly as on the Stats page.
    stats_page = (await client.get(f"/api/v1/workspaces/{workspace['id']}/stats", headers=headers)).json()["items"]
    assert items[2]["freshness_status"] == "assessing" == next(s for s in stats_page if s["key"] == "views")["freshness_status"]
    assert items[2]["collector_type"] == "agent_review" and items[2]["gaps"] == ["TikTok is not connected"]
    assert items[2]["next_assessment_at"] is not None and "collector_config" not in items[2]
    assert items[3]["value"] == 186000 and len(items[3]["series"]) == 1

    # A section that names its numbers shows exactly those, in its order, whatever the viewer hid.
    async with db_module.async_session() as session:
        stored = await session.get(Workspace, workspace["id"])
        stored.operating_model = {**(stored.operating_model or {}), "app_view": {"sections": [
            {"block": "metrics", "items": ["goal:revenue", "stat:hidden", "stat:orders", "stat:missing"]},
        ]}}
        await session.commit()
    chosen = (await client.get(endpoint, headers=headers)).json()
    assert [(item["kind"], item["title"]) for item in chosen["items"]] == [
        ("goal", "Revenue"), ("stat", "hidden"), ("stat", "orders"),
    ]
    assert chosen["suggestions"] == []


@pytest.mark.asyncio
async def test_empty_tracking_offers_audience_metrics_only_where_they_fit(client):
    import packages.core.database as db_module
    from packages.core.models.workspace import Workspace
    from packages.core.stats.service import create_stat

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_audience_offer", "email": "app_audience_offer@test.com",
        "password": "pass123", "entity_name": "Audience Offer",
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    entity_id = (await client.get("/api/v1/auth/me", headers=headers)).json()["entity_id"]
    workspace_id = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Weekly notes"})).json()["id"]
    endpoint = f"/api/v1/workspaces/{workspace_id}/app-view/sections/metrics"

    # The default page has no tracking block at all.
    assert (await client.get(endpoint, headers=headers)).status_code == 404

    # Asking for a tracking block is enough; templates already tracked are not offered again.
    async with db_module.async_session() as session:
        workspace = await session.get(Workspace, workspace_id)
        workspace.operating_model = {**(workspace.operating_model or {}), "app_view": {"sections": [{"block": "metrics"}]}}
        await create_stat(session, entity_id=entity_id, workspace_id=workspace_id, key="followers", name="Followers",
                          collector_type="manual", install_schedule=False)
        await session.commit()
    offered = (await client.get(endpoint, headers=headers)).json()
    assert offered["items"] and offered["suggestions"] == []
    async with db_module.async_session() as session:
        from packages.core.stats.service import delete_stat
        from packages.core.models.workspace_stat import WorkspaceStat
        from sqlalchemy import select
        stat = (await session.execute(select(WorkspaceStat).where(WorkspaceStat.workspace_id == workspace_id))).scalar_one()
        await delete_stat(session, stat)
        await session.commit()
    keys = [item["key"] for item in (await client.get(endpoint, headers=headers)).json()["suggestions"]]
    assert keys == ["latest_video_views", "latest_video_comments", "latest_video_likes", "followers", "active_users"]


@pytest.mark.parametrize("payload", [
    pytest.param(payload, id=payload["manifest"]["slug"])
    for payload in __import__("packages.core.blueprints.catalogue", fromlist=["x"]).get_platform_blueprints()
    + __import__("packages.core.blueprints.catalogue", fromlist=["x"]).get_practical_blueprints()
])
def test_every_builtin_flow_that_waits_reaches_needs_you(payload):
    """Needs you lists chat cards: a started Flow without one waits where nobody looks."""
    for flow in payload["recipe"].get("workflows") or []:
        if flow.get("internal") or not any(step.get("type") == "wait" for step in flow.get("steps") or []):
            continue
        entrypoint = (flow.get("binding_config") or {}).get("chat_entrypoint") or {}
        assert entrypoint.get("enabled") and entrypoint.get("wait_bridge"), flow["slug"]


@pytest.mark.parametrize("payload", [
    pytest.param(payload, id=payload["manifest"]["slug"])
    for payload in __import__("packages.core.blueprints.catalogue", fromlist=["x"]).get_platform_blueprints()
    + __import__("packages.core.blueprints.catalogue", fromlist=["x"]).get_practical_blueprints()
])
def test_inputs_filled_from_an_earlier_flow_stay_out_of_app_forms(payload):
    """A structured result another Flow hands over is not something a person types."""
    for flow in payload["recipe"].get("workflows") or []:
        for item in flow.get("run_inputs") or []:
            if (item.get("prefill") or {}).get("source") == "workflow_result" and item.get("type") == "json":
                assert item.get("hidden"), f"{flow['slug']}.{item['key']}"


@pytest.mark.parametrize("payload", [
    pytest.param(payload, id=payload["manifest"]["slug"])
    for payload in __import__("packages.core.blueprints.catalogue", fromlist=["x"]).get_platform_blueprints()
    + __import__("packages.core.blueprints.catalogue", fromlist=["x"]).get_practical_blueprints()
])
def test_every_builtin_blueprint_opens_as_a_short_app(payload):
    """A Blueprint's App lists what a person starts, not every part it ships."""
    from packages.core.services.workspace_app_view import suggested_hidden_actions, workflow_service_keys
    from packages.core.stats.review_setup import REVIEW_WORKFLOW_SLUG

    recipe = payload["recipe"]
    model = recipe.get("operating_model") or {}
    config = normalize_app_view_config(model.get("app_view"))
    flows = [flow for flow in recipe.get("workflows") or [] if not flow.get("internal")]
    slugs = {flow["slug"] for flow in flows}
    actions = [{"key": f"workflow:{slug}"} for slug in slugs] + [
        {"key": f"service:{service.get('key') or service.get('service_key')}"} for service in model.get("services") or []
    ] + [
        {"key": f"automation:{job['job_id']}",
         "target_workflow_id": (job.get("execution_target") or {}).get("workflow_slug") in slugs or None}
        for job in recipe.get("scheduled_jobs") or []
    ] + [{"key": key} for key in config["entries"]]
    plumbing = {f"workflow:{REVIEW_WORKFLOW_SLUG}"} | {
        f"automation:{job['job_id']}" for job in recipe.get("scheduled_jobs") or []
        if (job.get("execution_target") or {}).get("workflow_slug") == REVIEW_WORKFLOW_SLUG
    }
    hidden = set(config["hidden"]) | set(suggested_hidden_actions(
        actions, flow_service_keys=workflow_service_keys([flow.get("steps") for flow in flows]),
        plumbing_keys=plumbing, named=set(config["order"]) | set(config["hidden"]), hidden=config["hidden"],
    ))
    visible = [action["key"] for action in actions if action["key"] not in hidden]
    assert 1 <= len(visible) <= 12, visible
    if config["hero"]:
        assert config["hero"] in visible


def test_web_app_blocks_match_the_server_block_catalog():
    """The App block contract has one source: the server catalog.

    The web union type (which keys its renderer and settings registries) and
    the view labels the settings offer must cover exactly what the server
    publishes, or a block or view would silently not render or not be editable.
    """
    import re
    from pathlib import Path

    from packages.core.services.workspace_app_view import APP_SECTION_BLOCKS, APP_ZONES, app_block_catalog

    web = Path(__file__).resolve().parents[1] / "apps" / "web" / "src"
    api_types = (web / "lib" / "api.ts").read_text()
    union = re.search(r"export type WorkspaceAppSectionBlock = ([^;]+);", api_types)
    assert union, "WorkspaceAppSectionBlock union not found"
    assert set(re.findall(r'"([a-z_]+)"', union.group(1))) == set(APP_SECTION_BLOCKS)
    zones = re.search(r"export type WorkspaceAppZone = ([^;]+);", api_types)
    assert zones, "WorkspaceAppZone union not found"
    assert re.findall(r'"([a-z_]+)"', zones.group(1)) == list(APP_ZONES)
    assert {spec["zone"] for spec in APP_SECTION_BLOCKS.values()} <= set(APP_ZONES)
    from packages.core.services.workspace_app_view import APP_ENTRY_ICONS
    icons = re.search(r"export type WorkspaceAppEntryIcon =([^;]+);", api_types)
    assert icons, "WorkspaceAppEntryIcon union not found"
    assert re.findall(r'"([a-z_]+)"', icons.group(1)) == list(APP_ENTRY_ICONS)
    # Every name maps to a glyph on the page.
    display = (web / "components" / "workspaces" / "workspaceAppRunDisplay.ts").read_text()
    glyphs = re.search(r"export const ENTRY_ICONS[^=]*= \{([^}]+)\};", display)
    assert glyphs and set(re.findall(r"([a-z_]+): Icon", glyphs.group(1))) == set(APP_ENTRY_ICONS)

    english = (web / "lib" / "i18n" / "en.ts").read_text()
    for spec in app_block_catalog():
        if len(spec["views"]) > 1:
            for view in spec["views"]:
                assert f'"component.workspace_app_view.sections_view_{view}"' in english, (spec["block"], view)
        assert spec["views"], spec["block"]
        if spec["limit"]:
            assert 1 <= spec["limit"]["default"] <= spec["limit"]["max"]


@pytest.mark.asyncio
async def test_connections_block_lists_what_is_missing_required_first(client, monkeypatch):
    from packages.core.services import workspace_connection_status

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_connections_block", "email": "app_connections_block@test.com",
        "password": "pass123", "entity_name": "Connections Block",
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    workspace_id = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Studio"})).json()["id"]

    def requirement(key, required, ready):
        return {"key": key, "kind": "integration", "provider": key, "label": key, "required": required,
                "ready": ready, "reason": "", "service_keys": [], "setup_kind": None}

    async def status(db, *, workspace, user_id):
        return {"workspace_id": workspace.id, "required_issue_count": 1, "requirements": [
            requirement("telegram", False, False), requirement("chrome", True, False), requirement("notion", True, True),
        ]}

    monkeypatch.setattr(workspace_connection_status.WorkspaceConnectionStatusFactory, "create", staticmethod(status))
    body = (await client.get(f"/api/v1/workspaces/{workspace_id}/app-view/sections/connections", headers=headers)).json()
    assert body["block"] == "connections"
    assert [item["key"] for item in body["items"]] == ["chrome", "telegram"]


def test_blocks_keep_zone_order_whatever_order_they_are_written_in():
    """Notices, functions, data, outputs: a block only moves within its zone."""
    view = project_workspace_app_view(_workspace({"sections": [
        {"id": "made", "block": "outputs"}, {"block": "metrics"}, {"block": "actions"},
        {"id": "reports", "block": "outputs", "source": "workflow:binding-1"}, {"block": "queue"}, {"block": "connections"},
    ]}), [_flow()])
    assert [section["id"] for section in view["sections"]] == ["queue", "connections", "actions", "metrics", "made", "reports"]
    assert {spec["block"]: spec["zone"] for spec in view["blocks"]} == {
        "connections": "notices", "queue": "notices", "actions": "functions", "metrics": "data", "outputs": "outputs",
    }


def test_builtin_blueprints_ship_the_curated_app_from_one_place():
    """Each built-in Blueprint's App page equals its entry in builtin_app_views.

    Generator scripts write it on export and hand-kept configs carry it, so the
    page a Workspace opens with is decided in one module.
    """
    from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS, builtin_app_view
    from packages.core.blueprints.catalogue import get_platform_blueprints

    shipped = {payload["manifest"]["slug"]: payload for payload in get_platform_blueprints()}
    assert set(BUILTIN_APP_VIEWS) <= set(shipped)
    for slug, payload in shipped.items():
        app_view = payload["recipe"]["operating_model"].get("app_view")
        if slug in BUILTIN_APP_VIEWS:
            assert app_view == builtin_app_view(slug), slug
        else:
            # Only a Blueprint with its own data views may keep its App in its config.
            assert app_view and (app_view.get("components") or app_view.get("data_sources")), slug


def test_builtin_app_copy_fits_one_line():
    """The App shows its description and its main function's line on one line
    each, without scrolling; a Flow's own description is for builders, so the
    main function always carries its own short line."""
    from packages.core.blueprints.builtin_app_views import BUILTIN_APP_VIEWS

    for slug, view in BUILTIN_APP_VIEWS.items():
        lines = [view["description"]]
        if view.get("hero"):
            lines.append(view.get("descriptions", {}).get(view["hero"], ""))
            assert lines[-1], slug
        for text in lines:
            assert len(text) <= 60, (slug, text)
        for overlay in (view.get("i18n") or {}).values():
            assert len(overlay.get("description", "")) <= 30, slug


def test_entry_presets_offer_one_flow_as_several_prefilled_entries():
    """A preset fills the target Flow's inputs, winning over the Flow's own default."""
    flow = SimpleNamespace(
        binding_id="binding-1", workflow_id="workflow-1", title="Make a deck", description="Any deck",
        placeholder="", run_inputs=(
            {"key": "brief", "label": "What should we make?", "required": True,
             "schema": {"type": "string", "default": "flow default"}},
            {"key": "source_paths", "label": "Sources", "default": ""},
        ),
    )
    view = project_workspace_app_view(_workspace({
        "entries": {"action:pitch": {"title": "Pitch deck"}},
        "targets": {"action:pitch": "workflow:binding-1"},
        "presets": {"action:pitch": {"brief": "Pitch deck for [company]: problem, solution, the ask."}},
    }), [flow])
    pitch = next(action for action in view["actions"] if action["key"] == "action:pitch")
    assert pitch["kind"] == "workflow" and pitch["available"] is not False
    brief = next(item for item in pitch["inputs"] if item["key"] == "brief")
    assert brief["default"] == brief["schema"]["default"] == "Pitch deck for [company]: problem, solution, the ask."
    # The Flow's own entry keeps its own default.
    plain = next(action for action in view["actions"] if action["key"] == "workflow:binding-1")
    assert next(item for item in plain["inputs"] if item["key"] == "brief")["schema"]["default"] == "flow default"

    state = {"operating_model": _workspace().operating_model}
    added = _apply_single_patch(state, {"op": "app_view.update", "payload": {
        "entries": {"action:pitch": {"title": "Pitch deck"}}, "targets": {"action:pitch": "workflow:binding-1"},
        "presets": {"action:pitch": {"brief": "Pitch"}},
    }})
    assert added["operating_model"]["app_view"]["presets"] == {"action:pitch": {"brief": "Pitch"}}
    cleared = _apply_single_patch(added, {"op": "app_view.update", "payload": {"presets": {"action:pitch": None}}})
    assert cleared["operating_model"]["app_view"]["presets"] == {}
    # Removing the entry removes everything that belonged to it.
    removed = _apply_single_patch(added, {"op": "app_view.update", "payload": {"entries": {"action:pitch": None}}})
    assert removed["operating_model"]["app_view"]["presets"] == {} and removed["operating_model"]["app_view"]["targets"] == {}


@pytest.mark.parametrize("presets", [
    {"bad key": {"brief": "x"}}, {"action:pitch": {"brief": "x" * 4001}}, {"action:pitch": {"1bad": "x"}},
    {"action:pitch": "brief"},
])
def test_entry_presets_are_validated(presets):
    with pytest.raises(ValueError):
        normalize_app_view_config({"presets": presets})


def test_workspace_choices_come_first_among_a_free_text_inputs_suggestions():
    """A Workspace's own templates lead the Flow's suggestions and carry their presets; fixed lists stay as defined."""
    flow = SimpleNamespace(
        binding_id="binding-1", workflow_id="workflow-1", title="Make a video", description="", placeholder="",
        run_inputs=(
            {"key": "style", "label": "Style", "schema": {
                "type": "string", "examples": ["Comic", "Chalkboard"], "x-example-labels": ["Comic", "Chalk"],
                "x-example-images": ["/a.webp", "/b.webp"]}},
            {"key": "mode", "label": "Mode", "schema": {"type": "string", "enum": ["video", "slideshow"]}},
        ),
    )
    house = {"value": "Navy ink on cream paper", "label": "House style", "image": "/api/v1/fs/house.png",
             "group": "Ours", "sample": "/api/v1/fs/house.mp3", "presets": {"mode": "slideshow"}}
    view = project_workspace_app_view(_workspace({"choices": {"workflow:binding-1": {
        "style": [house], "mode": [{"value": "gif"}]}}}), [flow])
    inputs = {item["key"]: item for item in next(a for a in view["actions"] if a["key"] == "workflow:binding-1")["inputs"]}
    assert inputs["style"]["schema"]["examples"] == ["Navy ink on cream paper", "Comic", "Chalkboard"]
    assert inputs["style"]["schema"]["x-example-labels"] == ["House style", "Comic", "Chalk"]
    assert inputs["style"]["schema"]["x-example-images"] == ["/api/v1/fs/house.png", "/a.webp", "/b.webp"]
    assert inputs["style"]["schema"]["x-example-presets"] == [{"mode": "slideshow"}, None, None]
    # A catalog is browsed by group, and a template can be heard before it is picked.
    assert inputs["style"]["schema"]["x-example-groups"] == ["Ours", "", ""]
    assert inputs["style"]["schema"]["x-example-samples"] == ["/api/v1/fs/house.mp3", "", ""]
    # A run checks a fixed list against the Flow itself, so a Workspace cannot add to one.
    assert inputs["mode"]["schema"]["enum"] == ["video", "slideshow"]

    state = {"operating_model": _workspace().operating_model}
    added = _apply_single_patch(state, {"op": "app_view.update", "payload": {"choices": {"workflow:binding-1": {"style": [house]}}}})
    assert added["operating_model"]["app_view"]["choices"] == {"workflow:binding-1": {"style": [house]}}
    cleared = _apply_single_patch(added, {"op": "app_view.update", "payload": {"choices": {"workflow:binding-1": None}}})
    assert cleared["operating_model"]["app_view"]["choices"] == {}


@pytest.mark.parametrize("choices", [
    {"bad key": {"style": [{"value": "x"}]}}, {"action:a": {"style": []}}, {"action:a": {"style": [{"value": ""}]}},
    {"action:a": {"style": [{"value": "x", "image": "//elsewhere/a.png"}]}},
    {"action:a": {"style": [{"value": "x", "image": "http://plain/a.png"}]}},
    {"action:a": {"style": [{"value": "x", "presets": {"1bad": "y"}}]}},
    {"action:a": {"style": [{"value": "x", "script": "y"}]}},
    {"action:a": {"style": [{"value": "x", "sample": "http://plain/a.mp3"}]}},
    {"action:a": {"style": [{"value": "x", "group": "g" * 41}]}},
    {"action:a": {"style": [{"value": str(n)} for n in range(61)]}},
])
def test_workspace_choices_are_validated(choices):
    with pytest.raises(ValueError):
        normalize_app_view_config({"choices": choices})


@pytest.mark.asyncio
async def test_requests_sent_from_app_entries_to_agents_show_in_outputs_and_needs_you(client):
    """An Agent entry's request lists like a Flow run; its file-review question waits in Needs you."""
    from datetime import datetime, timedelta, timezone

    import packages.core.database as db_module
    from packages.core.ai.runtime.control import (
        HUMAN_INPUT_SUSPENSION_KIND, HUMAN_INPUT_SUSPENSION_REASON, RuntimeAgentCheckpoint, RuntimeToolSuspension,
    )
    from packages.core.models.base import generate_ulid
    from packages.core.models.document import Document
    from packages.core.models.runtime_run import RuntimeRunStatus
    from packages.core.models.task import Conversation
    from packages.core.models.workspace import Workspace
    from packages.core.services.conversation_messages import add_message
    from packages.core.services.runtime_human_input import record_human_input_question_message
    from packages.core.services.runtime_run_service import create_runtime_run, persist_runtime_run_suspension
    from packages.core.services.workspace_app_sections import WORKSPACE_APP_ACTION_META_KEY

    owner = (await client.post("/api/v1/auth/register", json={
        "username": "app_agent_outputs", "email": "app_agent_outputs@test.com",
        "password": "pass123", "entity_name": "App Agent Outputs",
    })).json()
    headers = {"Authorization": f"Bearer {owner['access_token']}"}
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    entity_id = me["entity_id"]
    workspace = (await client.post("/api/v1/workspaces", headers=headers, json={"name": "Carousels"})).json()
    entry = "service:creative.carousel"
    captions, page = generate_ulid(), generate_ulid()

    async def ask(session, conversation_id: str, *, content: str):
        request = await add_message(session, conversation_id, role="user", content=content,
                                    meta={"author_user_id": me["id"], WORKSPACE_APP_ACTION_META_KEY: entry})
        reply = await add_message(session, conversation_id, role="assistant", content="",
                                  meta={"origin_user_message_id": request.id})
        run = await create_runtime_run(
            session, conversation_id=conversation_id, assistant_message_id=reply.id, entity_id=entity_id,
            user_id=me["id"], agent_id=None, workspace_id=workspace["id"], execution_payload={"message": content},
        )
        return request, reply, run

    async with db_module.async_session() as session:
        stored = await session.get(Workspace, workspace["id"])
        stored.operating_model = {**(stored.operating_model or {}),
                                  "services": [{"key": "creative.carousel", "name": "Carousel producer"}]}
        session.add_all([
            Document(id=captions, entity_id=entity_id, name="captions.md", fs_path="artifacts/v1/captions.md",
                     file_type="md", source="generated", visibility="entity", owner_id=me["id"]),
            Document(id=page, entity_id=entity_id, name="page-01.png", fs_path="artifacts/v2/page-01.png",
                     file_type="png", source="generated", visibility="entity", owner_id=me["id"]),
        ])
        conversation = Conversation(id=generate_ulid(), entity_id=entity_id, workspace_id=workspace["id"], title="Chat")
        session.add(conversation)
        await session.flush()
        # A finished request: its reply links what it delivered.
        _, done_reply, done_run = await ask(session, conversation.id, content="Make a tips carousel")
        done_reply.content = f"Done. [captions.md](/viewer/{captions}) and a note."
        done_run.status = RuntimeRunStatus.COMPLETED.value
        done_run.completed_at = datetime.now(timezone.utc)
        await session.flush()
        # A later request that now waits on the person to review its pages.
        _, waiting_reply, waiting_run = await ask(session, conversation.id, content="Make a myth vs fact carousel")
        suspension = RuntimeToolSuspension(
            kind=HUMAN_INPUT_SUSPENSION_KIND, reservation_id="", poll_after_seconds=5,
            deadline_at=datetime.now(timezone.utc) + timedelta(hours=24), reason=HUMAN_INPUT_SUSPENSION_REASON,
            payload={"question": "Review the finished carousel", "options": ["Accept", "Request changes"],
                     "justification": {"kind": "file_review", "file_refs": [page]}},
        )
        control = suspension.to_dict()
        control["checkpoint"] = RuntimeAgentCheckpoint(
            messages=[{"role": "user", "content": "x"}], usage={}, rounds=1, tool_calls_made=[],
            pending_tool_call={"id": "ask", "name": "request_input", "arguments": {}},
        ).to_dict()
        waiting_run = await persist_runtime_run_suspension(session, run_id=waiting_run.id, control=control)
        question = await record_human_input_question_message(session, run=waiting_run)
        await session.commit()

    endpoint = f"/api/v1/workspaces/{workspace['id']}/app-view/sections"
    outputs = (await client.get(f"{endpoint}/outputs", headers=headers)).json()["items"]
    assert [(item["kind"], item["title"], item["status"], item["app_action"]) for item in outputs] == [
        ("chat", "Carousel producer", "paused", entry),
        ("chat", "Carousel producer", "completed", entry),
    ]
    assert [item["message_id"] for item in outputs] == [waiting_reply.id, done_reply.id]
    # While it waits it shows what it asked about; once done, what its reply delivered.
    assert [f["document_id"] for f in outputs[0]["files"]] == [page]
    assert [f["document_id"] for f in outputs[1]["files"]] == [captions]

    [decision] = (await client.get(f"{endpoint}/queue", headers=headers)).json()["items"]
    assert decision["kind"] == "runtime_input" and decision["message_id"] == question.id
    assert decision["summary"] == "Review the finished carousel"
    assert [preview["document_id"] for preview in decision["previews"]] == [page]


def test_an_entry_asks_for_a_hidden_flow_input_the_other_entries_never_show():
    """'Next episode' asks which series first; the Flow's other entries never ask."""
    flow = SimpleNamespace(
        binding_id="binding-1", workflow_id="workflow-1", title="Make an episode", description="", placeholder="",
        run_inputs=(
            {"key": "series_id", "label": "Series", "hidden": True, "required": False, "default": "",
             "schema": {"type": "string"}},
            {"key": "brief", "label": "What is it about?", "required": True, "schema": {"type": "string"}},
        ),
    )
    view = project_workspace_app_view(_workspace({
        "entries": {"action:next": {"title": "Next episode"}},
        "targets": {"action:next": "workflow:binding-1"},
        "asks": {"action:next": ["series_id"]},
    }), [flow])
    actions = {action["key"]: action for action in view["actions"]}
    asked = next(item for item in actions["action:next"]["inputs"] if item["key"] == "series_id")
    assert asked["hidden"] is False and asked["required"] is True
    assert "series_id" not in {item["key"] for item in actions["workflow:binding-1"]["inputs"]}

    state = {"operating_model": _workspace().operating_model}
    added = _apply_single_patch(state, {"op": "app_view.update", "payload": {
        "entries": {"action:next": {"title": "Next episode"}}, "asks": {"action:next": ["series_id"]}}})
    assert added["operating_model"]["app_view"]["asks"] == {"action:next": ["series_id"]}
    removed = _apply_single_patch(added, {"op": "app_view.update", "payload": {"entries": {"action:next": None}}})
    assert removed["operating_model"]["app_view"]["asks"] == {}
