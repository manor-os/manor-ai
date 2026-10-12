"""``_exec_llm`` robustness (Section W5/W6)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from packages.core.workers import internal


class _FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def llm_env(monkeypatch):
    monkeypatch.setattr("packages.core.database.async_session", lambda: _FakeSession())

    async def fake_persist(*args, **kwargs):
        return None

    monkeypatch.setattr(internal, "runtime_persist_internal_worker_runtime_events", fake_persist)

    async def fake_llm(**_kwargs):
        return SimpleNamespace(content="a real answer", usage={"prompt_tokens": 20, "completion_tokens": 8})

    monkeypatch.setattr(internal, "runtime_execute_internal_worker_llm_step", fake_llm)
    return monkeypatch


def _step(**extra):
    step = {"params": {"prompt": "draft the report"}, "entity_id": "ent", "user_id": "u"}
    step.update(extra)
    return step


async def test_context_failure_for_a_bound_agent_fails_the_attempt(llm_env):
    async def broken_context(*args, **kwargs):
        raise RuntimeError("agent row vanished")

    llm_env.setattr("packages.core.ai.context.build_agent_context", broken_context)

    with pytest.raises(RuntimeError, match="agent context unavailable for step: agent row vanished"):
        await internal._exec_llm(_step(resolved_agent_id="agent-1"))


async def test_context_failure_without_an_agent_degrades_and_flags_the_metadata(llm_env, caplog):
    async def broken_context(*args, **kwargs):
        raise RuntimeError("context service down")

    llm_env.setattr("packages.core.ai.context.build_agent_context", broken_context)

    with caplog.at_level(logging.WARNING, logger=internal.logger.name):
        out = await internal._exec_llm(_step(resolved_agent_id=None))

    assert out["result"] == {"text": "a real answer"}
    assert out["metadata"]["context_degraded"] is True
    assert any("context" in r.getMessage() for r in caplog.records)


async def test_context_success_does_not_flag_degradation(llm_env):
    async def ok_context(*args, **kwargs):
        return SimpleNamespace(model="model-y", system_prompt="sys", byok=False, llm_metadata=None,
                               runtime_envelope=None)

    llm_env.setattr("packages.core.ai.context.build_agent_context", ok_context)
    llm_env.setattr(internal, "runtime_metadata_from_context", lambda ctx: {"runtime": {"x": 1}})

    out = await internal._exec_llm(_step(resolved_agent_id="agent-1"))

    assert out["metadata"] == {"runtime": {"x": 1}}


async def test_llm_cost_is_priced_with_the_resolved_model(llm_env):
    priced: list[tuple] = []

    async def ok_context(*args, **kwargs):
        return SimpleNamespace(model="model-y", system_prompt="sys", byok=False, llm_metadata=None,
                               runtime_envelope=None)

    def fake_estimate(input_tokens, output_tokens, model=None, **_kw):
        priced.append((input_tokens, output_tokens, model))
        return 0.07

    llm_env.setattr("packages.core.ai.context.build_agent_context", ok_context)
    llm_env.setattr(internal, "estimate_token_cost_usd", fake_estimate)

    out = await internal._exec_llm(_step(resolved_agent_id="agent-1"))

    assert out["cost"]["usd"] == 0.07
    assert out["cost"]["llm_usd"] == 0.07
    assert out["cost"]["budget_usd"] == 0.0
    assert out["cost"]["llm_tokens_input"] == 20
    assert priced == [(20, 8, "model-y")]


async def test_llm_cost_accepts_provider_native_usage_keys(llm_env):
    priced: list[tuple[int, int]] = []

    async def fake_llm(**_kwargs):
        return SimpleNamespace(
            content="answer",
            usage={"prompt": 21, "completion": 9},
            model="model-native",
        )

    llm_env.setattr(internal, "runtime_execute_internal_worker_llm_step", fake_llm)
    llm_env.setattr(
        internal,
        "estimate_token_cost_usd",
        lambda input_tokens, output_tokens, **_kwargs: (
            priced.append((input_tokens, output_tokens)) or 0.08
        ),
    )

    out = await internal._exec_llm({"params": {"prompt": "draft the report"}})

    assert priced == [(21, 9)]
    assert out["cost"]["llm_tokens_input"] == 21
    assert out["cost"]["llm_tokens_output"] == 9
    assert out["cost"]["llm_usd"] == 0.08


async def test_llm_cost_falls_back_to_zero_when_pricing_fails(llm_env):
    def exploding(*_a, **_kw):
        raise RuntimeError("no table")

    llm_env.setattr(internal, "estimate_token_cost_usd", exploding)

    out = await internal._exec_llm({"params": {"prompt": "draft the report"}})
    assert out["cost"]["usd"] == 0.0
    assert out["cost"]["llm_usd"] == 0.0
    assert out["cost"]["budget_usd"] == 0.0


async def test_llm_cost_without_usage_prices_zero_tokens(llm_env):
    priced: list[tuple] = []

    async def fake_llm(**_kwargs):
        return SimpleNamespace(content="answer", usage={})

    llm_env.setattr(internal, "runtime_execute_internal_worker_llm_step", fake_llm)
    llm_env.setattr(internal, "estimate_token_cost_usd", lambda i, o, model=None, **_k: priced.append((i, o)) or 0.0)

    out = await internal._exec_llm({"params": {"prompt": "draft the report"}})
    assert out["cost"]["usd"] == 0.0
    assert priced == [(0, 0)]
