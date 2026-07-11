import asyncio
from pathlib import Path
from pydantic import ValidationError
import pytest
from types import SimpleNamespace

from packages.core.plans.schema import PlanStep


def test_subagent_instruction_is_normalized_to_prompt():
    step = PlanStep(
        key="retrieve_schedule_records",
        kind="subagent",
        service_key="content_scheduling",
        params={"instruction": "Find schedule records."},
    )

    assert step.params["prompt"] == "Find schedule records."
    assert step.params["instruction"] == "Find schedule records."


def test_subagent_requires_prompt_like_param():
    with pytest.raises(ValidationError, match="requires params.prompt"):
        PlanStep(
            key="retrieve_schedule_records",
            kind="subagent",
            service_key="content_scheduling",
            params={"document_ids": ["doc_1"]},
        )


def test_action_step_infers_runtime_capability_id():
    step = PlanStep(
        key="create_file",
        kind="action",
        service_key="content_scheduling",
        provider="platform",
        action_key="generate_file",
        params={"path": "draft.md"},
    )

    assert step.capability_id == "file.write"


def test_action_step_rejects_wrong_runtime_capability_id():
    with pytest.raises(ValidationError, match="does not match provider/action capability"):
        PlanStep(
            key="create_file",
            kind="action",
            service_key="content_scheduling",
            provider="platform",
            action_key="generate_file",
            capability_id="external.social",
            params={"path": "draft.md"},
        )


def test_runtime_planner_capability_catalog_groups_actions_tools_and_skills():
    from packages.core.ai.runtime import runtime_planner_capability_catalog_dicts

    catalog = runtime_planner_capability_catalog_dicts(
        provider_actions={"twitter_x": ["publish_tweet", "search_tweets"]},
        platform_tools=["generate_file"],
        skills=[{"slug": "draft-posts", "name": "Draft Posts"}],
    )
    by_id = {entry["capability_id"]: entry for entry in catalog}

    assert by_id["external.social"]["provider_actions"][0]["action_key"] == "publish_tweet"
    assert "file.write" in by_id
    assert by_id["file.write"]["platform_tools"] == ["generate_file"]
    assert by_id["skill.invoke"]["skills"][0]["name"] == "Draft Posts"
    assert "search_tweets" not in str(catalog)


def test_runtime_planner_action_binding_carries_cached_tool_schema():
    from packages.core.ai.runtime import (
        runtime_planner_action_bindings,
        runtime_planner_action_specs_from_tools_cached,
    )

    specs = runtime_planner_action_specs_from_tools_cached(
        [
            {
                "name": "publish_tweet",
                "description": "Publish a tweet.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {"tweet_id": {"type": "string"}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_message",
                    "description": "Send a message.",
                    "parameters": {
                        "type": "object",
                        "properties": {"to": {"type": "string"}},
                    },
                },
            },
        ]
    )

    bindings = runtime_planner_action_bindings(
        {"twitter_x": ["publish_tweet"], "gmail": ["send_message"]},
        provider_action_specs={"twitter_x": specs, "gmail": specs},
    )
    by_action = {binding.action_key: binding for binding in bindings}

    publish = by_action["publish_tweet"].to_dict(include_schema=True)
    send = by_action["send_message"].to_dict()

    assert publish["description"] == "Publish a tweet."
    assert publish["parameters"] == ["text"]
    assert publish["input_schema"]["required"] == ["text"]
    assert publish["output_schema"]["properties"]["tweet_id"]["type"] == "string"
    assert send["description"] == "Send a message."
    assert send["parameters"] == ["to"]


def test_runtime_planner_chat_turn_wraps_tool_completion(monkeypatch):
    from packages.core.ai.runtime import runtime_execute_planner_chat_turn

    seen: dict[str, object] = {}

    async def fake_chat_completion_with_tools(messages, tools, **kwargs):
        seen["completion"] = {"messages": messages, "tools": tools, **kwargs}
        return "", [{"id": "call_1", "name": "submit_plan", "arguments": "{}"}], {"prompt_tokens": 7}

    monkeypatch.setattr(
        "packages.core.ai.llm_client.chat_completion_with_tools",
        fake_chat_completion_with_tools,
    )

    result = asyncio.run(
        runtime_execute_planner_chat_turn(
            messages=[{"role": "user", "content": "Plan this"}],
            tools=[{"type": "function", "function": {"name": "submit_plan"}}],
            system_prompt="Planner system",
            metadata={"route": "unit"},
        )
    )

    assert result.content == ""
    assert result.tool_calls[0]["name"] == "submit_plan"
    assert result.usage == {"prompt_tokens": 7}
    assert seen["completion"] == {
        "messages": [
            {"role": "system", "content": "Planner system"},
            {"role": "user", "content": "Plan this"},
        ],
        "tools": [{"type": "function", "function": {"name": "submit_plan"}}],
        "temperature": 0.7,
        "model": None,
        "metadata": {"route": "unit"},
    }


def test_planner_generate_plan_uses_runtime_chat_turn_boundary(monkeypatch):
    from packages.core.ai.runtime import RuntimePlannerChatTurnResult
    from packages.core.plans import planner

    calls: list[dict[str, object]] = []

    async def fake_runtime_execute_planner_chat_turn(**kwargs):
        calls.append(kwargs)
        return RuntimePlannerChatTurnResult(
            content="""
            {
              "steps": [
                {
                  "key": "draft_update",
                  "kind": "llm",
                  "service_key": "content",
                  "params": {"prompt": "Draft the update."}
                }
              ],
              "metadata": {"rationale": "unit"}
            }
            """,
            tool_calls=[],
            usage={"prompt_tokens": 9},
        )

    monkeypatch.setattr(
        planner,
        "runtime_execute_planner_chat_turn",
        fake_runtime_execute_planner_chat_turn,
    )

    ctx = planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="content", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Content Agent", system_prompt="")},
        allowed_service_keys={"content"},
        provider_actions={},
    )
    task = SimpleNamespace(
        title="Prepare update",
        description="Draft a customer update.",
        details={},
        input_contract=None,
        expected_output=None,
        owner_service_key="content",
        delegate_service_keys=[],
    )

    plan = asyncio.run(planner._generate_plan(task, ctx))

    assert plan.steps[0].key == "draft_update"
    assert calls[0]["messages"][0]["role"] == "user"
    assert calls[0]["tools"][0]["function"]["name"] == "list_tools"
    assert "Planner for Manor" in calls[0]["system_prompt"]

    source = Path("packages/core/plans/planner.py").read_text()
    assert "runtime_execute_planner_chat_turn" in source
    assert "runtime_execute_planner_tool_call" in source
    assert "runtime_planner_tool_schemas" in source
    assert "def _build_planner_tools" not in source
    assert "def _execute_planner_tool" not in source
    assert "AIEngine(" not in source
    assert "LLMConfig.from_env" not in source
    assert "engine.chat(" not in source


def test_planner_drops_unrequested_text_report_file_write_step(monkeypatch):
    from packages.core.ai.runtime import RuntimePlannerChatTurnResult
    from packages.core.plans import planner

    async def fake_runtime_execute_planner_chat_turn(**kwargs):
        return RuntimePlannerChatTurnResult(
            content="""
            {
              "steps": [
                {
                  "key": "compile_baseline_report",
                  "kind": "subagent",
                  "service_key": "analytics_reporting",
                  "capability_id": "knowledge.public_search",
                  "params": {
                    "prompt": "Produce a structured text report with baseline metrics."
                  },
                  "expected_output_schema": {
                    "type": "object",
                    "properties": {
                      "baseline_report": {"type": "string"},
                      "measurement_gaps": {"type": "array"}
                    }
                  }
                },
                {
                  "key": "write_report_file",
                  "kind": "subagent",
                  "service_key": "analytics_reporting",
                  "capability_id": "file.write",
                  "params": {
                    "prompt": "Using the generate_file tool, create a structured text report file. Save the file as a .txt or .md document and return the file_url or fs_path."
                  },
                  "depends_on": ["compile_baseline_report"],
                  "risk_level": "medium",
                  "requires_approval": true
                }
              ],
              "metadata": {"rationale": "unit"}
            }
            """,
            tool_calls=[],
            usage={"prompt_tokens": 9},
        )

    monkeypatch.setattr(
        planner,
        "runtime_execute_planner_chat_turn",
        fake_runtime_execute_planner_chat_turn,
    )

    ctx = planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="analytics_reporting", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Analytics Agent", system_prompt="")},
        allowed_service_keys={"analytics_reporting"},
        provider_actions={},
    )
    task = SimpleNamespace(
        title="Audit goal metrics and produce a measurement baseline report",
        description="Produce a simple measurement plan. Output as a structured text report.",
        details={},
        input_contract=None,
        expected_output=None,
        owner_service_key="analytics_reporting",
        delegate_service_keys=[],
    )

    plan = asyncio.run(planner._generate_plan(task, ctx))

    assert [step.key for step in plan.steps] == ["compile_baseline_report"]
    assert all(step.capability_id != "file.write" for step in plan.steps)


def test_planner_keeps_explicit_saved_report_file_write_step(monkeypatch):
    from packages.core.ai.runtime import RuntimePlannerChatTurnResult
    from packages.core.plans import planner

    async def fake_runtime_execute_planner_chat_turn(**kwargs):
        return RuntimePlannerChatTurnResult(
            content="""
            {
              "steps": [
                {
                  "key": "compile_report",
                  "kind": "subagent",
                  "service_key": "analytics_reporting",
                  "params": {"prompt": "Read the workspace files and draft the report."}
                },
                {
                  "key": "write_report_file",
                  "kind": "subagent",
                  "service_key": "analytics_reporting",
                  "capability_id": "file.write",
                  "params": {"prompt": "Use generate_file to save the report as a .md file and return fs_path."},
                  "depends_on": ["compile_report"],
                  "requires_approval": true
                }
              ]
            }
            """,
            tool_calls=[],
            usage={"prompt_tokens": 9},
        )

    monkeypatch.setattr(
        planner,
        "runtime_execute_planner_chat_turn",
        fake_runtime_execute_planner_chat_turn,
    )

    ctx = planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="analytics_reporting", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Analytics Agent", system_prompt="")},
        allowed_service_keys={"analytics_reporting"},
        provider_actions={},
    )
    task = SimpleNamespace(
        title="Audit goal metrics and produce a measurement baseline report",
        description="Read the workspace files and save the structured text report as a .md file.",
        details={},
        input_contract=None,
        expected_output=None,
        owner_service_key="analytics_reporting",
        delegate_service_keys=[],
    )

    plan = asyncio.run(planner._generate_plan(task, ctx))

    assert [step.key for step in plan.steps] == ["compile_report", "write_report_file"]
    assert plan.steps[-1].capability_id == "file.write"
    assert plan.steps[-1].requires_approval is False


def test_planner_clears_hard_approval_for_generated_pdf(monkeypatch):
    from packages.core.ai.runtime import RuntimePlannerChatTurnResult
    from packages.core.plans import planner

    async def fake_runtime_execute_planner_chat_turn(**kwargs):
        return RuntimePlannerChatTurnResult(
            content="""
            {
              "steps": [
                {
                  "key": "research_block_context",
                  "kind": "subagent",
                  "service_key": "launch_assets",
                  "capability_id": "workspace.search",
                  "params": {"prompt": "Research the product context."}
                },
                {
                  "key": "generate_brochure_pdf",
                  "kind": "subagent",
                  "service_key": "launch_assets",
                  "capability_id": "file.write",
                  "params": {"prompt": "Use generate_file to create the brochure as a PDF."},
                  "depends_on": ["research_block_context"],
                  "risk_level": "medium",
                  "requires_approval": true
                }
              ]
            }
            """,
            tool_calls=[],
            usage={"prompt_tokens": 9},
        )

    monkeypatch.setattr(
        planner,
        "runtime_execute_planner_chat_turn",
        fake_runtime_execute_planner_chat_turn,
    )

    ctx = planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="launch_assets", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Launch Agent", system_prompt="")},
        allowed_service_keys={"launch_assets"},
        provider_actions={},
    )
    task = SimpleNamespace(
        title="Regenerate Manor AI customer brochure as PDF with screenshots",
        description="Generate a customer brochure PDF.",
        details={},
        input_contract=None,
        expected_output=None,
        owner_service_key="launch_assets",
        delegate_service_keys=[],
    )

    plan = asyncio.run(planner._generate_plan(task, ctx))

    assert [step.key for step in plan.steps] == ["research_block_context", "generate_brochure_pdf"]
    assert plan.steps[-1].capability_id == "file.write"
    assert plan.steps[-1].requires_approval is False


def test_planner_clears_hard_approval_for_action_steps(monkeypatch):
    from packages.core.ai.runtime import RuntimePlannerChatTurnResult
    from packages.core.plans import planner

    async def fake_runtime_execute_planner_chat_turn(**kwargs):
        return RuntimePlannerChatTurnResult(
            content="""
            {
              "steps": [
                {
                  "key": "draft_reply",
                  "kind": "llm",
                  "service_key": "content",
                  "params": {"prompt": "Draft a reply."}
                },
                {
                  "key": "send_reply",
                  "kind": "action",
                  "service_key": "content",
                  "provider": "email",
                  "action_key": "external_message.send",
                  "capability_id": "external.message",
                  "params": {"body": "Hello"},
                  "depends_on": ["draft_reply"],
                  "requires_approval": true
                }
              ]
            }
            """,
            tool_calls=[],
            usage={"prompt_tokens": 9},
        )

    monkeypatch.setattr(
        planner,
        "runtime_execute_planner_chat_turn",
        fake_runtime_execute_planner_chat_turn,
    )

    ctx = planner._Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="content", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Content Agent", system_prompt="")},
        allowed_service_keys={"content"},
        provider_actions={"email": ["external_message.send"]},
    )
    task = SimpleNamespace(
        title="Send a customer follow-up",
        description="Draft and send a short follow-up.",
        details={},
        input_contract=None,
        expected_output=None,
        owner_service_key="content",
        delegate_service_keys=[],
    )

    plan = asyncio.run(planner._generate_plan(task, ctx))

    assert [step.key for step in plan.steps] == ["draft_reply", "send_reply"]
    assert plan.steps[-1].action_key == "external_message.send"
    assert plan.steps[-1].capability_id == "external.message"
    assert plan.steps[-1].requires_approval is False


def test_planner_list_tools_returns_capability_first_catalog():
    from packages.core.ai.runtime import (
        runtime_execute_planner_tool_call,
        runtime_planner_system_prompt,
    )
    from packages.core.plans.planner import _Context

    agent = SimpleNamespace(id="agent_1", name="Content Agent", system_prompt="Draft content")
    ctx = _Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="content", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": agent},
        allowed_service_keys={"content"},
        provider_actions={"twitter_x": ["publish_tweet"]},
        agent_tool_names={"agent_1": ["generate_file"]},
        agent_skill_names={"agent_1": [{"slug": "draft-posts", "name": "Draft Posts"}]},
    )

    result = runtime_execute_planner_tool_call(
        "list_tools",
        {"service_key": "content"},
        context=ctx,
    )
    by_id = {entry["capability_id"]: entry for entry in result["capability_catalog"]}
    prompt = runtime_planner_system_prompt(
        subscriptions=ctx.subscriptions,
        agents_by_id=ctx.agents_by_id,
        allowed_service_keys=ctx.allowed_service_keys,
        provider_actions=ctx.provider_actions,
        provider_action_specs=ctx.provider_action_specs,
        document_groups=ctx.document_groups,
        staff=ctx.staff,
        agent_tool_names=ctx.agent_tool_names,
        agent_skill_names=ctx.agent_skill_names,
    )

    assert by_id["external.social"]["provider_actions"][0]["provider"] == "twitter_x"
    assert by_id["file.write"]["platform_tools"] == ["generate_file"]
    publish_binding = next(binding for binding in result["action_bindings"] if binding["action_key"] == "publish_tweet")
    assert publish_binding["capability_id"] == "external.social"
    assert "Allowed runtime capabilities and bindings" in prompt
    assert "Do not use requires_approval to encode approval policy" in prompt
    assert "external.social" in prompt


def test_planner_action_catalog_is_scoped_to_selected_service():
    from packages.core.ai.runtime import runtime_execute_planner_tool_call
    from packages.core.plans.planner import CapabilityError, _Context, _enforce_allowlists, _parse_plan

    ctx = _Context(
        workspace=None,
        subscriptions=[
            SimpleNamespace(service_key="content", id="sub_content", agent_id="agent_content"),
            SimpleNamespace(service_key="research", id="sub_research", agent_id="agent_research"),
        ],
        agents_by_id={
            "agent_content": SimpleNamespace(id="agent_content", name="Content Agent", system_prompt=""),
            "agent_research": SimpleNamespace(id="agent_research", name="Research Agent", system_prompt=""),
        },
        allowed_service_keys={"content", "research"},
        provider_actions={"twitter_x": ["publish_tweet", "search_tweets"]},
        service_provider_actions={
            "content": {"twitter_x": ["publish_tweet"]},
            "research": {"twitter_x": ["search_tweets"]},
        },
    )

    content_tools = runtime_execute_planner_tool_call(
        "list_tools",
        {"service_key": "content"},
        context=ctx,
    )
    research_tools = runtime_execute_planner_tool_call(
        "list_tools",
        {"service_key": "research"},
        context=ctx,
    )

    assert [binding["action_key"] for binding in content_tools["action_bindings"]] == ["publish_tweet"]
    assert [binding["action_key"] for binding in research_tools["action_bindings"]] == ["search_tweets"]

    plan = _parse_plan("""
    {
      "steps": [
        {
          "key": "wrong_service_action",
          "kind": "action",
          "service_key": "content",
          "provider": "twitter_x",
          "action_key": "search_tweets",
          "params": {"query": "launch"}
        }
      ]
    }
    """)
    assert plan is not None
    with pytest.raises(CapabilityError, match="service_key='content'"):
        _enforce_allowlists(plan, ctx)


def test_planner_get_tool_schema_returns_runtime_action_binding_schema():
    from packages.core.ai.runtime import runtime_execute_planner_tool_call
    from packages.core.plans.planner import _Context

    ctx = _Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="content", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Content Agent", system_prompt="")},
        allowed_service_keys={"content"},
        provider_actions={"twitter_x": ["publish_tweet"]},
        provider_action_specs={
            "twitter_x": {
                "publish_tweet": {
                    "description": "Publish a tweet.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"tweet_id": {"type": "string"}},
                    },
                }
            }
        },
    )

    result = runtime_execute_planner_tool_call(
        "get_tool_schema",
        {"provider": "twitter_x", "action_key": "publish_tweet"},
        context=ctx,
    )

    assert result["available"] is True
    assert result["capability_id"] == "external.social"
    assert result["description"] == "Publish a tweet."
    assert result["parameters"] == ["text"]
    assert result["input_schema"]["required"] == ["text"]
    assert result["output_schema"]["properties"]["tweet_id"]["type"] == "string"
    assert "note" not in result


def test_planner_submit_plan_attaches_runtime_action_binding_schemas():
    from packages.core.ai.runtime import runtime_execute_planner_tool_call
    from packages.core.plans.planner import _Context, _enforce_allowlists, _parse_plan

    ctx = _Context(
        workspace=None,
        subscriptions=[SimpleNamespace(service_key="content", id="sub_1", agent_id="agent_1")],
        agents_by_id={"agent_1": SimpleNamespace(id="agent_1", name="Content Agent", system_prompt="")},
        allowed_service_keys={"content"},
        provider_actions={"twitter_x": ["publish_tweet"]},
        provider_action_specs={
            "twitter_x": {
                "publish_tweet": {
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"tweet_id": {"type": "string"}},
                    },
                }
            }
        },
    )
    plan_json = """
    {
      "steps": [
        {
          "key": "publish_post",
          "kind": "action",
          "service_key": "content",
          "provider": "twitter_x",
          "action_key": "publish_tweet",
          "params": {"text": "Hello"}
        }
      ],
      "metadata": {"rationale": "test"}
    }
    """

    result = runtime_execute_planner_tool_call(
        "submit_plan",
        {"plan_json": plan_json},
        context=ctx,
        parse_plan=_parse_plan,
        enforce_plan=lambda plan: _enforce_allowlists(plan, ctx),
    )
    step = result["_plan"].steps[0]

    assert step.expected_input_schema["required"] == ["text"]
    assert step.expected_output_schema["properties"]["tweet_id"]["type"] == "string"
