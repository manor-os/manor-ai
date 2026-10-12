"""The agent loop budgets against the window of the model it is calling.

Before this, every model was treated as 128K: 1M models compacted at ~80K and
an 8K route was sent requests it could not hold.
"""
import json
import re
from pathlib import Path

import pytest

from packages.core.ai.runtime.context_compaction import (
    CHECKPOINT_FIELDS, compact_context, encode, model_window, request_tokens,
)
from packages.core.constants.models import CATALOG, CONTEXT_WINDOW_ROLES, MODEL_CONTEXT_WINDOWS
from packages.core.services import model_pricing
from packages.core.services.model_context_window import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    effective_context_window_tokens,
    model_context_window_tokens,
)
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def gateway_caches(tmp_path, monkeypatch):
    """Point both synced gateway caches at writable test files."""
    paths = {"vercel": tmp_path / "vercel-pricing.json", "openrouter": tmp_path / "openrouter-pricing.json"}
    monkeypatch.setenv("VERCEL_PRICING_CACHE_PATH", str(paths["vercel"]))
    monkeypatch.setenv("OPENROUTER_PRICING_CACHE_PATH", str(paths["openrouter"]))
    for name, value in (("_VERCEL_CACHE_MTIME", 0.0), ("_VERCEL_CACHE_MODELS", {}),
                        ("_PRICING_CACHE_MTIME", 0.0), ("_PRICING_CACHE_MODELS", {})):
        monkeypatch.setattr(model_pricing, name, value)
    monkeypatch.delenv("MANOR_CONTEXT_WINDOW_CAP_TOKENS", raising=False)

    def write(gateway, models):
        paths[gateway].write_text(json.dumps({"models": models}))
    return write


def test_every_agent_loop_catalog_model_records_its_context_window():
    catalog_ids = {item["id"] for role in CONTEXT_WINDOW_ROLES for item in CATALOG[role]}
    assert catalog_ids - set(MODEL_CONTEXT_WINDOWS) == set()
    assert set(MODEL_CONTEXT_WINDOWS) - catalog_ids == set()
    assert all(isinstance(value, int) and value > 0 for value in MODEL_CONTEXT_WINDOWS.values())


def test_live_gateway_windows_override_the_snapshot_only_downward(gateway_caches):
    gateway_caches("vercel", {"openai/gpt-5.6-terra": {"context_window": 400_000}})
    gateway_caches("openrouter", {"openai/gpt-5.6-terra": {"context_window": 2_000_000}})
    assert model_context_window_tokens("openai/gpt-5.6-terra") == 400_000
    # Vercel lists qwen under its alibaba/ wire id.
    gateway_caches("vercel", {"alibaba/qwen3.8-max": {"context_window": 131_072}})
    assert model_context_window_tokens("qwen/qwen3.8-max") == 131_072
    # A model no catalog knows is learnt from the synced caches alone.
    gateway_caches("openrouter", {"vendor/custom-byok": {"context_window": 64_000}})
    assert model_context_window_tokens("vendor/custom-byok") == 64_000
    assert model_context_window_tokens("vendor/never-seen") is None


@pytest.mark.parametrize("model,cap,expected", [
    # 1M window, no price tier: the platform cap bounds per-round resend volume.
    ("anthropic/claude-sonnet-4.6", None, 200_000),
    ("anthropic/claude-sonnet-4.6", "500000", 500_000),
    # 1.05M window with a 272K long-context price tier.
    ("openai/gpt-5.6-terra", None, 200_000),
    ("openai/gpt-5.6-terra", "500000", 272_000),
    # gemini-2.5-pro bills above 200K.
    ("google/gemini-2.5-pro", "500000", 200_000),
    # A 32K price tier cannot hold the loop's fixed prompt: pay the tier.
    ("qwen/qwen3.7-flash", "500000", 500_000),
    ("openai/gpt-4o", None, 128_000),
    ("vendor/never-seen", None, DEFAULT_CONTEXT_WINDOW_TOKENS),
    (None, None, DEFAULT_CONTEXT_WINDOW_TOKENS),
])
def test_effective_window_is_the_model_window_within_cost_caps(gateway_caches, monkeypatch, model, cap, expected):
    if cap:
        monkeypatch.setenv("MANOR_CONTEXT_WINDOW_CAP_TOKENS", cap)
    assert effective_context_window_tokens(model) == expected


def test_models_outside_the_catalog_are_sized_from_the_synced_caches(gateway_caches):
    # A stored legacy preference keeps working against its real 8K window.
    gateway_caches("openrouter", {"openai/gpt-4": {"context_window": 8_191}})
    assert effective_context_window_tokens("openai/gpt-4") == 8_191


def test_explicit_window_still_overrides_the_model(gateway_caches):
    assert model_window({"context_window_tokens": 8000}, 128_000, model="anthropic/claude-sonnet-4.6") == 8000
    assert model_window(None, 128_000, model="anthropic/claude-sonnet-4.6") == 200_000
    assert model_window(None, 128_000) == 128_000


def _turns(count, chars=6000):
    messages = [{"role": "system", "content": "Rules."}]
    for index in range(count):
        messages.append({"role": "user", "content": f"Question {index}"})
        messages.append({"role": "assistant", "content": f"Answer {index} " + "x" * chars})
    messages.append({"role": "user", "content": "Current request"})
    return messages


async def _checkpoint(prompt, **kwargs):
    return encode({**{field: [] for field in CHECKPOINT_FIELDS}, "completed": ["Earlier answers were given."]}), {}


async def _compact(messages, *, target_ratio, complete=_checkpoint):
    return await compact_context(
        messages, model="test-model", metadata=None, tools=[], window=20_000, reserve=1_000,
        ratio=.75, count_threshold=1_000, active_user_message="Current request",
        fresh_tail_start=len(messages), complete=complete, keep_recent=12, target_ratio=target_ratio,
    )


@pytest.mark.asyncio
async def test_compaction_triggers_at_the_ratio_and_lands_at_the_target():
    messages = _turns(30)
    trigger, target = int(20_000 * .75) - 1_000, int(20_000 * .5) - 1_000
    assert request_tokens(messages) > trigger

    without_target = await _compact(messages, target_ratio=None)
    with_target = await _compact(messages, target_ratio=.5)

    assert target < request_tokens(without_target) <= trigger
    assert request_tokens(with_target) <= target


@pytest.mark.asyncio
async def test_compacted_context_has_headroom_before_the_next_trigger():
    compacted = await _compact(_turns(30), target_ratio=.5)
    grown = [*compacted, {"role": "assistant", "content": "y" * 8_000}, {"role": "user", "content": "Current request"}]

    async def forbidden(prompt, **kwargs):
        pytest.fail("a context at the target must not re-trigger summarization one round later")

    assert await _compact(grown, target_ratio=.5, complete=forbidden) is grown


@pytest.mark.asyncio
@pytest.mark.parametrize("model,window", [("anthropic/claude-sonnet-4.6", 200_000), ("test-model", 128_000)])
async def test_loop_budgets_against_the_called_models_window(mock_runtime, monkeypatch, gateway_caches, model, window):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    async def complete(messages, tools, **kwargs):
        return "Done.", [], {"total": 1}

    async def route(*, model=None, metadata=None, **kwargs):
        return model, {}, True

    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    monkeypatch.setattr("packages.core.ai.runtime.completions.runtime_resolve_text_completion_route", route)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(model=model, dynamic_tool_handlers={}, capability_refresh_tool_names=set(), max_rounds=2)
    result = await runtime_execute_agentic_loop(**kwargs)
    assert result.usage["context_window_last"]["context_window_tokens"] == window


def test_no_hard_coded_window_outside_the_model_window_fallback():
    """128K is only the fallback for a model nobody can size."""
    allowed = {
        "MAX_CONTEXT_TOKENS = 128_000",
        # _context_compaction_token_threshold: test-only legacy helper.
        "base_threshold = int(MAX_CONTEXT_TOKENS * LOOP_COMPACT_RATIO)",
    }
    offenders = []
    for root in ("packages", "apps"):
        for path in (REPO / root).rglob("*.py"):
            if "tests" in path.parts or "node_modules" in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                text = line.strip()
                if "MAX_CONTEXT_TOKENS" not in text or text in allowed or text.startswith("#"):
                    continue
                if re.search(r"model_window\(.*MAX_CONTEXT_TOKENS, model=", text):
                    continue
                offenders.append(f"{path.relative_to(REPO)}:{number}: {text}")
    assert offenders == []


@pytest.mark.asyncio
async def test_loop_compaction_uses_the_route_models_window_and_target_ratio(monkeypatch, gateway_caches):
    from packages.core.ai import agentic_loop
    from packages.core.ai.runtime import context_compaction

    seen = {}

    async def capture(messages, **kwargs):
        seen.update(kwargs)
        return messages

    monkeypatch.setattr(context_compaction, "compact_context", capture)
    await agentic_loop._compact_messages(_turns(2), "openai/gpt-5.6-terra", 0.2)
    assert seen["window"] == 200_000
    assert seen["ratio"] == agentic_loop.LOOP_COMPACT_RATIO == 0.75
    assert seen["target_ratio"] == agentic_loop.LOOP_COMPACT_TARGET_RATIO == 0.5
