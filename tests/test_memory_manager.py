"""Memory policy/cache contract tests; all model calls are replaced locally."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from packages.core.memory import manager
from packages.core.memory.policy import canonical_filename, content_fingerprint


class DecisionCache:
    def __init__(self):
        self.data = {}
        self.deadlines = {}
        self.now = time.monotonic
        self.leases = {}
        self.available = True

    async def get(self, key):
        if self.deadlines.get(key, float("inf")) <= self.now():
            await self.delete(key)
        return self.data.get(key) if self.available else None

    async def set(self, key, value, ttl):
        if self.available:
            self.data[key] = deepcopy(value)
            self.deadlines[key] = self.now() + ttl
        return self.available

    async def delete(self, key):
        self.data.pop(key, None)
        self.deadlines.pop(key, None)

    async def acquire_lease(self, key, token, ttl):
        if not self.available:
            return None
        if key in self.leases:
            return False
        self.leases[key] = token
        return True

    async def release_lease(self, key, token):
        assert self.leases.pop(key) == token


@pytest.fixture
def worker(monkeypatch):
    from packages.core.ai.runtime import completions

    manager._LOCAL_DECISIONS.clear()
    manager._INFLIGHT.clear()
    cache = DecisionCache()
    monkeypatch.setattr(manager, "cache", cache)
    state = SimpleNamespace(reply={"ids": ["one"]}, calls=[], route="worker-test", cache=cache)

    async def route(**kwargs):
        assert kwargs["source"] == "internal_worker"
        return state.route, {}, None

    async def complete(messages, **kwargs):
        state.calls.append((messages, kwargs))
        return SimpleNamespace(content=json.dumps(state.reply), usage={})

    monkeypatch.setattr(completions, "runtime_resolve_text_completion_route", route)
    monkeypatch.setattr(completions, "runtime_execute_text_completion", complete)
    yield state
    manager._LOCAL_DECISIONS.clear()
    manager._INFLIGHT.clear()


def snapshot(revision="v1"):
    return {"query": "report language", "records": [{"id": "one", "content": "Chinese", "revision": revision}]}


@pytest.mark.asyncio
async def test_cached_selection_no_repeat_llm_and_no_mutable_alias(worker):
    first = await manager.run_memory_skill("select", snapshot(), entity_id="e", user_id="u")
    first["ids"].clear()
    assert await manager.run_memory_skill("select", snapshot(), entity_id="e", user_id="u") == {"ids": ["one"]}
    assert len(worker.calls) == 1
    assert not worker.cache.leases
    messages, kwargs = worker.calls[0]
    assert "# Memory Management" in messages[0]["content"]
    assert kwargs["source"] == "internal_worker"
    assert kwargs["temperature"] == 0
    assert kwargs["model"] == "worker-test"
    assert kwargs["reasoning_effort"] == "none"


@pytest.mark.asyncio
async def test_truncated_extraction_is_not_cached_and_can_retry(worker, monkeypatch):
    from packages.core.ai.runtime import completions

    original = completions.runtime_execute_text_completion

    async def truncated(messages, **kwargs):
        assert kwargs["reasoning_effort"] == "none"
        return SimpleNamespace(content='{"entries": [{"body": "unfinished', usage={})

    payload = {"messages": [{"id": "m", "content": "Remember my preference"}]}
    monkeypatch.setattr(completions, "runtime_execute_text_completion", truncated)
    with pytest.raises(json.JSONDecodeError):
        await manager.run_memory_skill("extract", payload, entity_id="e", user_id="u")
    assert not worker.cache.data
    assert not worker.cache.leases
    assert not manager._INFLIGHT
    worker.reply = {"entries": []}
    monkeypatch.setattr(completions, "runtime_execute_text_completion", original)
    assert await manager.run_memory_skill("extract", payload, entity_id="e", user_id="u") == {"entries": []}
    assert len(worker.calls) == 1


@pytest.mark.asyncio
async def test_worker_billing_is_independent_and_restores_suppressed_parent(worker, monkeypatch):
    from packages.core.ai import llm_client
    from packages.core.ai.runtime import completions

    seen = []
    parent = llm_client.LLMBillingContext(entity_id="e", source="chat", suppress=True, durable_per_call=True)
    token = llm_client._billing_ctx_var.set(parent)

    async def complete(*args, **kwargs):
        billing = llm_client._billing_ctx_var.get()
        seen.append(billing)
        assert billing.entity_id == "e" and billing.source == "internal_worker"
        assert not billing.suppress
        return SimpleNamespace(content='{"ids":["one"]}', usage={"prompt": 5})

    monkeypatch.setattr(completions, "runtime_execute_text_completion", complete)
    try:
        for _ in range(2):
            await manager.run_memory_skill("select", snapshot(), entity_id="e")
            assert llm_client._billing_ctx_var.get() is parent
        assert len(seen) == 1  # cache hit has no worker call/billing scope
        worker.reply = {}
        monkeypatch.setattr(completions, "runtime_execute_text_completion", AsyncMock(side_effect=RuntimeError("provider down")))
        with pytest.raises(RuntimeError):
            await manager.run_memory_skill("select", snapshot("new"), entity_id="e")
        assert llm_client._billing_ctx_var.get() is parent
    finally:
        llm_client._billing_ctx_var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["entity", "user", "workspace", "agent", "revision", "query", "route", "skill"])
async def test_cache_isolated_and_invalidated(worker, monkeypatch, change):
    scope = dict(entity_id="e", user_id="u", workspace_id="w", agent_id="a")
    payload = snapshot()
    await manager.run_memory_skill("select", payload, **scope)
    if change in {"entity", "user", "workspace", "agent"}:
        scope[change + "_id"] += "2"
    elif change == "revision":
        payload = snapshot("v2")
    elif change == "query":
        payload["query"] += " please"
    elif change == "route":
        worker.route = "another-worker"
    else:
        original = manager.read_memory_text(str(manager.SKILL_PATH))
        monkeypatch.setattr(manager, "read_memory_text", lambda _: original + "\nUpdated policy")
    await manager.run_memory_skill("select", payload, **scope)
    assert len(worker.calls) == 2


@pytest.mark.asyncio
async def test_valid_empty_extraction_is_cached_and_existing_snapshot_invalidates(worker):
    worker.reply = {"entries": []}
    payload = {"messages": "m1: same preference", "source_message_ids": ["m1"], "existing": []}
    for _ in range(2):
        assert await manager.run_memory_skill("extract", payload, entity_id="e") == {"entries": []}
    payload["existing"] = [{"id": "old", "content": "changed"}]
    await manager.run_memory_skill("extract", payload, entity_id="e")
    assert len(worker.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [{"ids": ["forged"]}, {"ids": "one"}, [], {}])
async def test_invalid_outputs_never_cached(worker, reply):
    worker.reply = reply
    for _ in range(2):
        with pytest.raises(ValueError):
            await manager.run_memory_skill("select", snapshot(), entity_id="e")
    assert len(worker.calls) == 2
    assert not worker.cache.data and not worker.cache.leases and not manager._INFLIGHT


@pytest.mark.asyncio
async def test_missing_evidence_is_rejected(worker):
    worker.reply = {
        "entries": [
            {"scope": "fact", "title": "Fact", "body": "A fact", "confidence": 0.7, "source_message_id": "forged"}
        ]
    }
    with pytest.raises(ValueError, match="evidence"):
        await manager.run_memory_skill("extract", {"source_message_ids": ["real"]}, entity_id="e")
    assert not worker.cache.data


@pytest.mark.asyncio
async def test_redis_outage_keeps_local_cache(worker):
    worker.cache.available = False
    for _ in range(2):
        await manager.run_memory_skill("select", snapshot(), entity_id="e")
    assert len(worker.calls) == 1


@pytest.mark.asyncio
async def test_local_concurrent_call_and_cancellation_release(worker, monkeypatch):
    from packages.core.ai.runtime import completions

    entered, hold = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await hold.wait()

    monkeypatch.setattr(completions, "runtime_execute_text_completion", blocked)
    task = asyncio.create_task(manager.run_memory_skill("select", snapshot(), entity_id="e"))
    await entered.wait()
    with pytest.raises(RuntimeError, match="already running"):
        await manager.run_memory_skill("select", snapshot(), entity_id="e")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not worker.cache.leases and not manager._INFLIGHT and not worker.cache.data


@pytest.mark.asyncio
async def test_other_process_lease_prevents_duplicate_cost(worker, monkeypatch):
    monkeypatch.setattr(worker.cache, "acquire_lease", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError):
        await manager.run_memory_skill("select", snapshot(), entity_id="e")
    assert not worker.calls


@pytest.mark.asyncio
async def test_expired_local_and_shared_cache_recomputes(worker):
    await manager.run_memory_skill("select", snapshot(), entity_id="e")
    for key, (_, data) in list(manager._LOCAL_DECISIONS.items()):
        manager._LOCAL_DECISIONS[key] = (0, data)
    worker.cache.data.clear()
    await manager.run_memory_skill("select", snapshot(), entity_id="e")
    assert len(worker.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("hit_layer", ["local", "redis"])
@pytest.mark.parametrize("operation", ["extract", "extract_legacy"])
async def test_seven_day_sliding_expiry_across_cache_layers(worker, monkeypatch, hit_layer, operation):
    day = 24 * 60 * 60
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    worker.cache.now = lambda: now[0]
    payload = snapshot()
    if operation == "extract":
        payload = {"messages": "m1: use Chinese reports", "source_message_ids": ["m1"], "existing": []}
        worker.reply = {
            "entries": [
                {
                    "scope": "preference",
                    "title": "Reports",
                    "body": "Use Chinese reports",
                    "source_message_id": "m1",
                    "confidence": 0.7,
                }
            ]
        }
    elif operation == "extract_legacy":
        payload = {"messages": "Use Chinese reports", "existing": []}
        worker.reply = {"memories": [{"type": "preference", "content": "Use Chinese reports", "importance": 5}]}

    assert manager.EXTRACTION_CACHE_TTL == 7 * day
    await manager.run_memory_skill(operation, payload, entity_id="e")
    key = next(iter(worker.cache.data))
    assert worker.cache.deadlines[key] == 7 * day

    now[0] = 6 * day
    if hit_layer == "redis":
        manager._LOCAL_DECISIONS.clear()
    await manager.run_memory_skill(operation, payload, entity_id="e")
    assert worker.cache.deadlines[key] == 13 * day
    assert manager._LOCAL_DECISIONS[key][0] == 13 * day

    # A fresh process can reuse the refreshed shared value beyond creation+7d.
    now[0] = 8 * day
    manager._LOCAL_DECISIONS.clear()
    await manager.run_memory_skill(operation, payload, entity_id="e")
    assert len(worker.calls) == 1
    assert worker.cache.deadlines[key] == 15 * day

    # After seven full idle days, both layers expire and the worker runs again.
    now[0] = 15 * day
    await manager.run_memory_skill(operation, payload, entity_id="e")
    assert len(worker.calls) == 2


@pytest.mark.asyncio
async def test_cache_hit_after_lease_recheck_also_renews(worker, monkeypatch):
    worker.reply = {"entries": []}
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    worker.cache.now = lambda: now[0]
    await manager.run_memory_skill("extract", {"source_message_ids": []}, entity_id="e")
    key = next(iter(worker.cache.data))
    manager._LOCAL_DECISIONS.clear()
    now[0] = 6 * 24 * 60 * 60
    real_get = worker.cache.get
    first_read = True

    async def miss_then_hit(cache_key):
        nonlocal first_read
        if first_read:
            first_read = False
            return None
        return await real_get(cache_key)

    monkeypatch.setattr(worker.cache, "get", miss_then_hit)
    await manager.run_memory_skill("extract", {"source_message_ids": []}, entity_id="e")
    assert len(worker.calls) == 1
    assert worker.cache.deadlines[key] == now[0] + manager.EXTRACTION_CACHE_TTL
    assert manager._LOCAL_DECISIONS[key][0] == now[0] + manager.EXTRACTION_CACHE_TTL
    assert not worker.cache.leases


@pytest.mark.asyncio
async def test_redis_outage_local_hit_renews_and_recovery_repopulates(worker, monkeypatch):
    day = 24 * 60 * 60
    worker.reply = {"entries": []}
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    worker.cache.now = lambda: now[0]
    worker.cache.available = False
    for timestamp in (0, 6 * day, 8 * day):
        now[0] = timestamp
        await manager.run_memory_skill("extract", {"source_message_ids": []}, entity_id="e")
    assert len(worker.calls) == 1
    assert not worker.cache.data
    worker.cache.available = True
    await manager.run_memory_skill("extract", {"source_message_ids": []}, entity_id="e")
    assert len(worker.calls) == 1
    assert next(iter(worker.cache.deadlines.values())) == 15 * day


@pytest.mark.asyncio
async def test_changed_content_never_renews_old_decision(worker, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    worker.cache.now = lambda: now[0]
    await manager.run_memory_skill("select", snapshot(), entity_id="e")
    old_key = next(iter(worker.cache.data))
    now[0] = 100
    updated = snapshot("v2")
    updated["records"][0]["content"] = "English"
    await manager.run_memory_skill("select", updated, entity_id="e")
    assert len(worker.calls) == 2
    assert worker.cache.deadlines[old_key] == manager.JUDGMENT_CACHE_TTL
    assert len(worker.cache.data) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_id", [None, "w"])
async def test_chat_extractor_boundary_uses_sliding_cache_for_both_chat_scopes(worker, monkeypatch, workspace_id):
    from packages.core.ai.runtime import runtime_execute_chat_insight_extraction_completion

    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    worker.cache.now = lambda: now[0]
    worker.reply = {"entries": []}
    for timestamp in (0, 6 * 24 * 60 * 60):
        now[0] = timestamp
        result = await runtime_execute_chat_insight_extraction_completion(
                payload="m1: reports in Chinese",
                entity_id="e",
                workspace_id=workspace_id,
                user_id="u",
                source_message_ids=["m1"],
                existing=[],
        )
        assert json.loads(result.content) == {"entries": []}
    assert len(worker.calls) == 1
    assert worker.calls[0][1]["workspace_id"] == workspace_id
    assert worker.calls[0][1]["user_id"] == "u"
    assert next(iter(worker.cache.deadlines.values())) == now[0] + manager.EXTRACTION_CACHE_TTL


@pytest.mark.asyncio
async def test_oversized_input_no_provider_call(worker):
    with pytest.raises(ValueError, match="budget"):
        await manager.run_memory_skill("select", {"records": [], "query": "a" * 100_001}, entity_id="e")
    assert not worker.calls


def test_file_cache_reads_once_external_replace_and_delete(tmp_path):
    from packages.core.memory.cache import read_memory_text, _read_version

    _read_version.cache_clear()
    path = tmp_path / "MEMORY.md"
    path.write_text("old")
    assert read_memory_text(str(path)) == "old"
    assert read_memory_text(str(path)) == "old"
    assert _read_version.cache_info().misses == 1
    replacement = tmp_path / "new.md"
    replacement.write_text("new")  # same length; inode/mtime invalidates
    replacement.replace(path)
    assert read_memory_text(str(path)) == "new"
    path.unlink()
    assert read_memory_text(str(path)) is None


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("learning", "LEARNINGS.md"),
        ("guidance", "RULES.md"),
        ("preference", "MEMORY.md"),
        ("fact", "MEMORY.md"),
        ("decision", "MEMORY.md"),
        ("procedure", "RUNBOOKS.md"),
        ("tool_experience", "TOOLS.md"),
    ],
)
def test_category_precedes_keywords(kind, expected):
    assert canonical_filename(kind, "必须校验 / must check") == expected


def test_dedup_does_not_collapse_number_negation_or_case():
    assert content_fingerprint("a\n b") == content_fingerprint(" a b ")
    for a, b in [("预算 100", "预算 1000"), ("允许发布", "不允许发布"), ("Key", "key")]:
        assert content_fingerprint(a) != content_fingerprint(b)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["workspace:w:", "agent_files:e:a:entity::"])
async def test_prompt_selection_protects_mandatory_sources_and_falls_back(worker, monkeypatch, prefix):
    from packages.core.ai.runtime.context_window import context_section

    ctx = SimpleNamespace(entity_id="e", user_id="u", workspace_id="w", agent_id=None, active_user_message="report")

    def section(name, category="memory_files", content="x" * 8500):
        return context_section(name, content, category=category, source=prefix + name)

    sections = [
        section("RULES.md", "agent_instructions"),
        section("WORKSPACE.md"),
        section("MEMORY.md"),
        section("TOOLS.md"),
    ]
    worker.reply = {"ids": [sections[2]["source"]]}
    result = await manager.select_prompt_memories(ctx, sections)
    assert all(result[i]["content"] for i in (0, 1, 2))
    assert result[3]["content"] == ""
    assert result[3]["source_digest"] == sections[3]["source_digest"]
    assert len(json.loads(worker.calls[0][0][1]["content"])["records"][0]["content"]) == 8500
    monkeypatch.setattr(manager, "run_memory_skill", AsyncMock(side_effect=TimeoutError))
    assert await manager.select_prompt_memories(ctx, sections) == sections


@pytest.mark.asyncio
async def test_small_memory_skips_worker(worker):
    from packages.core.ai.runtime.context_window import context_section

    ctx = SimpleNamespace(entity_id="e", active_user_message="hi")
    sections = [context_section("MEMORY.md", "small", category="memory_files", source="workspace:w:MEMORY.md")]
    assert await manager.select_prompt_memories(ctx, sections) == sections
    assert not worker.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["local", "redis", "outage"])
async def test_selection_judgment_expires_after_fifteen_minutes_even_when_used(worker, monkeypatch, layer):
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    worker.cache.now = lambda: now[0]
    worker.cache.available = layer != "outage"
    for second in (0, 600, 899):
        now[0] = second
        if layer == "redis":
            manager._LOCAL_DECISIONS.clear()
        await manager.run_memory_skill("select", snapshot(), entity_id="e")
    assert len(worker.calls) == 1
    assert next(iter(manager._LOCAL_DECISIONS.values()))[0] == 900
    if layer != "outage":
        assert next(iter(worker.cache.deadlines.values())) == 900
    now[0] = 900
    worker.cache.available = True
    await manager.run_memory_skill("select", snapshot(), entity_id="e")
    assert len(worker.calls) == 2
    assert next(iter(manager._LOCAL_DECISIONS.values()))[0] == 1800


@pytest.mark.asyncio
async def test_cached_selection_cannot_resurrect_removed_record(worker):
    records = [{"id": "one", "content": "a" * 9000}, {"id": "two", "content": "b" * 9000}]
    selected = await manager.select_memory_records(records, query="which?", entity_id="e", user_id="u")
    assert [r["id"] for r in selected] == ["one"]
    worker.reply = {"ids": ["two"]}
    selected = await manager.select_memory_records(records[1:], query="which?", entity_id="e", user_id="u")
    assert [r["id"] for r in selected] == ["two"]
    assert len(worker.calls) == 2
