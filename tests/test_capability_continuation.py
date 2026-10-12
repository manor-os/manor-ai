import pytest


def _scope(**overrides):
    base = {
        "conversation_id": "conversation-1",
        "entity_id": "entity-1",
        "user_id": "user-1",
        "agent_id": "agent-1",
        "workspace_id": "workspace-1",
    }
    return {**base, **overrides}


def test_evidence_persists_bounded_root_used_and_loaded_tool_hints():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    evidence = RuntimeToolEvidence()
    evidence.configure_capability_scope(_scope())
    evidence.observe_loaded_tools(["mcp__chrome__read_page", "mcp__chrome__read_page"])
    evidence.observe_tool_result("mcp__chrome__read_page", "loaded-then-used", {"ok": True})
    evidence.observe_tool_result("mcp__chrome__open_tabs", "root-call", {"ok": True})
    evidence.observe_tool_result("mcp__chrome__click", "child-call", {"ok": True})

    state = evidence.checkpoint_state()["capability_hints"]
    hints = {item["name"]: item for item in state["hints"]}
    assert hints["mcp__chrome__read_page"]["source"] == "used"
    assert hints["mcp__chrome__open_tabs"]["source"] == "used"
    assert hints["mcp__chrome__click"]["source"] == "used"
    assert state["scope"] == _scope()


def test_loaded_candidates_do_not_evict_a_tool_that_was_actually_used():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    evidence = RuntimeToolEvidence()
    evidence.configure_capability_scope(_scope())
    evidence.observe_tool_result("mcp__example__read_object", "read", {"content": "observed"})
    evidence.observe_loaded_tools([f"mcp__example__candidate_{index}" for index in range(12)])
    hints = evidence.checkpoint_state()["capability_hints"]["hints"]
    assert len(hints) == 12
    assert hints[0]["name"] == "mcp__example__read_object"
    assert hints[0]["source"] == "used"


def test_using_a_known_tool_again_refreshes_its_retention_priority():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    evidence = RuntimeToolEvidence()
    evidence.configure_capability_scope(_scope())
    for index in range(12):
        evidence.observe_tool_result(f"tool_{index}", f"call_{index}", {"content": "observed"})
    evidence.observe_tool_result("tool_0", "revisit", {"content": "refreshed"})
    evidence.observe_tool_result("tool_12", "new", {"content": "new"})
    names = [hint["name"] for hint in evidence.checkpoint_state()["capability_hints"]["hints"]]
    assert names[:2] == ["tool_12", "tool_0"]
    assert "tool_1" not in names


def test_hint_recency_uses_timestamp_order_across_offsets():
    from packages.core.ai.runtime.capability_continuation import runtime_capability_hint_state

    state = runtime_capability_hint_state(scope=_scope(), hints=[
        {"name": "older", "source": "used", "observed_at": "2026-09-29T01:00:00+08:00"},
        {"name": "newer", "source": "used", "observed_at": "2026-09-29T00:30:00+00:00"},
        {"name": "unknown", "source": "used", "observed_at": "invalid"},
    ])
    assert [hint["name"] for hint in state["hints"]] == ["newer", "older", "unknown"]


@pytest.mark.asyncio
async def test_later_discovery_only_reply_does_not_hide_prior_used_tool():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from packages.core.services.runtime_chat_context import _recent_chat_capability_hints

    def metadata(hints):
        return {"capability_hints": {"version": 1, "scope": _scope(), "hints": hints}}

    rows = [
        metadata([{"name": f"candidate_{index}", "source": "loaded", "observed_at": "2026-09-29T01:00:00Z"} for index in range(12)]),
        metadata([{"name": "read_object", "source": "used", "observed_at": "2026-09-29T00:00:00Z"}]),
    ]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: rows)))
    hints = await _recent_chat_capability_hints(db, conversation_id="conversation-1", scope=_scope())
    assert len(hints) == 12
    assert hints[0]["name"] == "read_object"


def test_hints_are_not_accepted_across_scope_or_without_exact_identity():
    from packages.core.ai.runtime.capability_continuation import runtime_known_capability_hints

    state = {
        "version": 1,
        "scope": _scope(),
        "hints": [
            {"name": "mcp__chrome__read_page", "source": "used", "observed_at": "2026-09-29T00:00:00+00:00"},
            {"name": "", "source": "used", "observed_at": "2026-09-29T00:00:00+00:00"},
        ],
    }
    assert [item["name"] for item in runtime_known_capability_hints(state, scope=_scope())] == ["mcp__chrome__read_page"]
    assert runtime_known_capability_hints(state, scope=_scope(user_id="other-user")) == []


def test_checkpoint_does_not_import_hints_into_a_different_runtime_scope():
    from packages.core.ai.runtime.tool_evidence import RuntimeToolEvidence

    source = RuntimeToolEvidence()
    source.configure_capability_scope(_scope())
    source.observe_loaded_tools(["mcp__chrome__read_page"])
    resumed = RuntimeToolEvidence()
    resumed.configure_capability_scope(_scope(user_id="other-user"))
    resumed.restore_checkpoint_state(source.checkpoint_state())
    assert resumed.checkpoint_state()["capability_hints"]["hints"] == []


@pytest.mark.asyncio
async def test_restore_excludes_blocked_revoked_and_schema_changed_hints():
    from packages.core.ai.runtime.capability_continuation import runtime_restore_known_capability_schemas

    hints = [
        {"name": "mcp__chrome__read_page", "source": "used"},
        {"name": "mcp__chrome__click", "source": "used"},
        {"name": "mcp__chrome__old_method", "source": "loaded"},
    ]
    requested = []

    async def prepare_exact(names):
        requested.append(tuple(names))
        return {"mcp__chrome__read_page"}

    async def schema_for_actor(name):
        return {"function": {"name": name}} if name == "mcp__chrome__read_page" else None

    schemas, restored = await runtime_restore_known_capability_schemas(
        hints,
        blocked_tool_names={"mcp__chrome__click"},
        prepare_exact=prepare_exact,
        schema_for_actor=schema_for_actor,
    )
    assert requested == [("mcp__chrome__read_page", "mcp__chrome__old_method")]
    assert [schema["function"]["name"] for schema in schemas] == ["mcp__chrome__read_page"]
    assert restored == ("mcp__chrome__read_page",)


@pytest.mark.asyncio
async def test_restore_never_uses_semantic_matching_or_renews_hint_time():
    from packages.core.ai.runtime.capability_continuation import runtime_restore_known_capability_schemas

    hint = {"name": "mcp__chrome__read_page", "source": "used", "observed_at": "2026-09-29T00:00:00+00:00"}
    calls = []

    async def prepare_exact(names):
        calls.append(tuple(names))
        return set(names)

    async def schema_for_actor(name):
        return {"function": {"name": name}}

    await runtime_restore_known_capability_schemas(
        [hint], prepare_exact=prepare_exact, schema_for_actor=schema_for_actor,
    )
    assert calls == [("mcp__chrome__read_page",)]
    assert hint["observed_at"] == "2026-09-29T00:00:00+00:00"


@pytest.mark.asyncio
async def test_stale_exact_provider_does_not_disable_another_known_tool():
    from packages.core.ai.runtime.capability_continuation import runtime_restore_known_capability_schemas

    async def prepare_exact(names):
        if len(names) > 1 or names[0] == "mcp__chrome__stale":
            raise RuntimeError("stale provider")
        return names

    async def schema_for_actor(name):
        return {"function": {"name": name}}

    schemas, restored = await runtime_restore_known_capability_schemas(
        [
            {"name": "mcp__chrome__stale", "source": "used"},
            {"name": "mcp__chrome__read_page", "source": "used"},
        ],
        prepare_exact=prepare_exact,
        schema_for_actor=schema_for_actor,
    )
    assert restored == ("mcp__chrome__read_page",)
    assert schemas[0]["function"]["name"] == "mcp__chrome__read_page"


@pytest.mark.asyncio
async def test_chat_context_prepares_known_tools_exactly_under_current_scope(monkeypatch):
    from packages.core.services import runtime_chat_context

    calls = []

    async def discover_tools(*, arguments, **kwargs):
        calls.append(arguments)
        return '{"loaded_tools":["mcp__chrome__read_page"]}'

    async def schema_for_actor(name, **kwargs):
        return {"function": {"name": name}}

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_discover_tools", discover_tools,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", schema_for_actor,
    )

    class Envelope:
        blocked_tool_names = ()

        def effective_allowed_tool_names(self):
            return {"search_tools"}

    schemas, names = await runtime_chat_context._restore_chat_capability_schemas(
        hints=[{"name": "mcp__chrome__read_page", "source": "used"}],
        envelope=Envelope(), entity_id="entity-1", user_id="user-1", agent_id="agent-1",
        workspace_id=None, conversation_id="conversation-1", active_user_message="read this page",
    )
    assert names == ("mcp__chrome__read_page",)
    assert schemas[0]["function"]["name"] == "mcp__chrome__read_page"
    assert calls[0]["mode"] == "exact"
    assert calls[0]["exact_names"] == ["mcp__chrome__read_page"]
    assert "query" not in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [8, 12])
async def test_chat_restore_respects_real_exact_discovery_request_limit(monkeypatch, count):
    import json
    from types import SimpleNamespace

    from packages.core.ai.runtime.tool_discovery import runtime_prepare_search_tools_request
    from packages.core.services import runtime_chat_context

    names = [f"mcp__example__read_{index}" for index in range(count)]
    requests = []

    async def discover_tools(*, arguments, **kwargs):
        request = runtime_prepare_search_tools_request(**{
            key: arguments[key] for key in ("mode", "exact_names", "max_results")
        })
        requests.append(request)
        if not request.ok:
            return json.dumps({"error": request.error})
        return json.dumps({"loaded_tools": request.exact_names})

    async def schema_for_actor(name, **kwargs):
        return {"type": "function", "function": {"name": name}}

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_discover_tools", discover_tools)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", schema_for_actor)
    envelope = SimpleNamespace(
        blocked_tool_names=(), effective_allowed_tool_names=lambda: {"search_tools"},
    )
    schemas, restored = await runtime_chat_context._restore_chat_capability_schemas(
        hints=[{"name": name, "source": "used"} for name in names],
        envelope=envelope, entity_id="entity-1", user_id="user-1", agent_id=None,
        workspace_id=None, conversation_id="conversation-1", active_user_message="continue reading",
    )

    assert list(restored) == names
    assert [schema["function"]["name"] for schema in schemas] == names
    assert all(request.ok for request in requests)


@pytest.mark.asyncio
async def test_chat_context_restored_schema_keeps_discovered_account_options(monkeypatch):
    from packages.core.services import runtime_chat_context

    async def discover_tools(*, arguments, **kwargs):
        return (
            '{"loaded_tools":["mcp__chrome__read_page"],"matches":['
            '{"name":"mcp__chrome__read_page","account_options":['
            '{"id":"account-1","display_name":"Primary","is_default":true}],'
            '"requires_explicit_account":true}]}'
        )

    async def schema_for_actor(name, **kwargs):
        return {"function": {"name": name, "parameters": {"type": "object", "properties": {}}}}

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_discover_tools", discover_tools)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", schema_for_actor)

    class Envelope:
        blocked_tool_names = ()

        def effective_allowed_tool_names(self):
            return {"search_tools"}

    schemas, _ = await runtime_chat_context._restore_chat_capability_schemas(
        hints=[{"name": "mcp__chrome__read_page", "source": "used"}],
        envelope=Envelope(), entity_id="entity-1", user_id="user-1", agent_id="agent-1",
        workspace_id=None, conversation_id="conversation-1", active_user_message="read this page",
    )
    properties = schemas[0]["function"]["parameters"]["properties"]
    assert "integration_account_id" in properties


@pytest.mark.asyncio
async def test_chat_context_ignores_prior_metadata_when_current_prepare_revokes_tool(monkeypatch):
    from packages.core.services import runtime_chat_context

    async def discover_tools(*, arguments, **kwargs):
        return '{"loaded_tools":[]}'

    async def schema_for_actor(name, **kwargs):
        raise AssertionError("a revoked tool must not load a schema")

    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_discover_tools", discover_tools,
    )
    monkeypatch.setattr(
        "packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", schema_for_actor,
    )

    class Envelope:
        blocked_tool_names = ()

        def effective_allowed_tool_names(self):
            return {"search_tools"}

    schemas, names = await runtime_chat_context._restore_chat_capability_schemas(
        hints=[{"name": "mcp__chrome__read_page", "source": "used"}],
        envelope=Envelope(), entity_id="entity-1", user_id="user-1", agent_id="agent-1",
        workspace_id=None, conversation_id="conversation-1", active_user_message="read this page",
    )
    assert schemas == []
    assert names == ()


@pytest.mark.asyncio
async def test_chat_context_reassembles_the_new_turn_with_reauthorized_prior_schema(monkeypatch):
    from types import SimpleNamespace

    from packages.core.ai.runtime.capability_continuation import runtime_capability_hint_state
    from packages.core.services import runtime_chat_context

    hint_state = runtime_capability_hint_state(
        scope=_scope(),
        hints=[{"name": "mcp__chrome__read_page", "source": "used"}],
    )

    class Result:
        def scalars(self):
            return [{"capability_hints": hint_state}]

    class Db:
        async def execute(self, _statement):
            return Result()

    async def runtime_for_chat(*_args, **_kwargs):
        return SimpleNamespace(
            workspace_id="workspace-1", tool_profile=None, is_master=False,
            task_id=None, thread_ref_kind=None, thread_ref_id=None,
            bound_tool_names=[], mcp_allowed_names=set(), extra_context=None,
            mcp_provider_scopes=(), mcp_scope_unrestricted=False,
        )

    assemble_calls = []

    async def assemble(_db, *, request, extra_tool_schemas, **_kwargs):
        assemble_calls.append([schema["function"]["name"] for schema in extra_tool_schemas])
        tools = list(extra_tool_schemas)
        envelope = SimpleNamespace(
            principal=SimpleNamespace(actor_user_id="user-1"),
            blocked_tool_names=(),
            metadata={},
            effective_allowed_tool_names=lambda: {"search_tools"},
        )
        return SimpleNamespace(
            context=SimpleNamespace(agent=None, entity=None, user=None),
            envelope=envelope, tool_schemas=tools, prompt="prompt",
        )

    async def discover_tools(*, arguments, **_kwargs):
        assert arguments["mode"] == "exact"
        return '{"loaded_tools":["mcp__chrome__read_page"]}'

    async def schema_for_actor(name, **_kwargs):
        return {"function": {"name": name}}

    async def no_history(*_args, **_kwargs):
        return []

    async def no_forced_tools(*_args, **_kwargs):
        return []

    async def no_task_host(*_args, **_kwargs):
        return None

    monkeypatch.setattr("packages.core.services.task_session.task_session_host_for_conversation", no_task_host)
    monkeypatch.setattr("packages.core.services.workspace_runtime.resolve_workspace_runtime", runtime_for_chat)
    monkeypatch.setattr(runtime_chat_context, "runtime_assemble_prompt_for_turn", assemble)
    monkeypatch.setattr(runtime_chat_context, "load_conversation_history", no_history)
    monkeypatch.setattr(runtime_chat_context, "runtime_auto_skill_forced_tool_calls", no_forced_tools)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_discover_tools", discover_tools)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", schema_for_actor)

    _prompt, tools, _history, _ctx = await runtime_chat_context.resolve_runtime_chat_context(
        Db(), "read the page", entity_id="entity-1", user_id="user-1",
        agent_id="agent-1", workspace_id="workspace-1", conversation_id="conversation-1",
    )
    assert assemble_calls == [[], ["mcp__chrome__read_page"]]
    assert [schema["function"]["name"] for schema in tools] == ["mcp__chrome__read_page"]


@pytest.mark.asyncio
async def test_shared_gateway_restore_reauthorizes_plain_names_under_the_given_envelope(monkeypatch):
    """Chat and the harness share one restore: names in, exact discovery under the current envelope."""
    from types import SimpleNamespace

    from packages.core.ai.runtime.capability_continuation import runtime_restore_gateway_tools

    seen = []

    async def discover_tools(*, arguments, **kwargs):
        seen.append(arguments)
        return '{"loaded_tools":["generate_file"]}'

    async def schema_for_actor(name, **kwargs):
        return {"type": "function", "function": {"name": name}}

    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_discover_tools", discover_tools)
    monkeypatch.setattr("packages.core.ai.runtime.tool_registry.runtime_tool_schema_for_actor", schema_for_actor)
    envelope = SimpleNamespace(
        blocked_tool_names=("mcp__blocked__x",), effective_allowed_tool_names=lambda: {"search_tools"},
    )

    schemas, names = await runtime_restore_gateway_tools(
        ["generate_file", "mcp__blocked__x"], envelope=envelope, entity_id="entity-1", user_id="user-1",
        agent_id=None, workspace_id="workspace-1", conversation_id="conversation-1", active_user_message="go",
    )

    assert names == ("generate_file",)
    assert [schema["function"]["name"] for schema in schemas] == ["generate_file"]
    assert seen[0]["mode"] == "exact" and seen[0]["exact_names"] == ["generate_file"]
    assert seen[0]["_runtime_envelope_from_context"] is envelope
    assert await runtime_restore_gateway_tools(
        ["generate_file"], envelope=envelope, entity_id="entity-1", user_id=None,
        agent_id=None, workspace_id=None, conversation_id=None, active_user_message=None,
    ) == ([], ())
