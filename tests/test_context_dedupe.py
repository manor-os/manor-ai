"""A run's request holds each piece of conversation once.

Staging 2026-09-27/28: a run's lossless checkpoint was carried into the next run
while that run reloaded the same history, and re-packing never de-duplicated.
One conversation's checkpoint grew to 333 records, 280 of them repeats, and
carried checkpoints were 21% of its prompt tokens. Compaction was also
triggered at 40 messages, which packed history verbatim without saving tokens.
"""
import json
from collections import Counter

import pytest

from packages.core.ai import llm_client
from packages.core.ai.agentic_loop import _compact_messages, _estimate_context_attribution
from packages.core.ai.runtime.context_compaction import (
    CHECKPOINT_PREFIX,
    HISTORY_DECORATION_MARKERS,
    _checkpoint_message,
    compact_context,
    context_message_key,
    dedupe_carried_checkpoint,
)
from packages.core.ai.runtime.context_window import context_digest
from packages.core.ai.runtime.output_policy import PREVIOUS_TOOL_ACTIVITY_MARKER
from packages.core.services.conversation_history import (
    COMPLETED_TOOL_ACTIVITY_FRAME,
    UNVERIFIED_TOOL_ACTIVITY_FRAME,
)
from tests.test_runtime_capability_refresh import mock_runtime, _envelope, _loop_kwargs  # noqa: F401


def turn(index):
    return [
        {"role": "user", "content": f"Question {index}: compare the Bay Area housing options again please."},
        {"role": "assistant", "content": f"Answer {index}: the RV site stays cheapest at about $900 a month. " + "x" * 400},
    ]


def history(turns):
    return [message for index in range(turns) for message in turn(index)]


TOOL_CALL = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_search", "type": "function", "function": {"name": "web_search", "arguments": "{\"query\": \"Bay Area RV park monthly rates\"}"}},
]}
TOOL_OUTPUT = {"role": "tool", "tool_call_id": "call_search", "content": json.dumps({"results": [{"title": "Long-term RV park", "price": "$850/month"}]})}


def lossless(records, archive_id="archive-1"):
    return _checkpoint_message({"records": records}, archive_id, context_digest(records))


def records_of(message):
    return json.loads(message["content"].split("\n", 2)[2])["records"]


def test_decoration_markers_match_history_rendering():
    assert COMPLETED_TOOL_ACTIVITY_FRAME.startswith(HISTORY_DECORATION_MARKERS[0].strip())
    assert UNVERIFIED_TOOL_ACTIVITY_FRAME.startswith(HISTORY_DECORATION_MARKERS[0].strip())
    assert PREVIOUS_TOOL_ACTIVITY_MARKER in HISTORY_DECORATION_MARKERS


def test_a_reply_is_the_same_message_with_or_without_its_tool_preview():
    reply = turn(0)[1]
    decorated = {**reply, "content": f"{reply['content']}\n\n{COMPLETED_TOOL_ACTIVITY_FRAME}\n{PREVIOUS_TOOL_ACTIVITY_MARKER}\n- web_search: {{...}}"}
    assert context_message_key(reply) == context_message_key(decorated)
    assert context_message_key({"role": "user", "content": "ok"}) is None
    assert context_message_key(TOOL_OUTPUT) is not None


def test_carried_checkpoint_keeps_only_what_history_lacks():
    loaded = history(6)
    carried = lossless([*history(3), TOOL_CALL, TOOL_OUTPUT])
    kept = dedupe_carried_checkpoint(carried, loaded)
    assert records_of(kept) == [TOOL_CALL, TOOL_OUTPUT]
    assert json.loads(kept["content"].split("\n", 2)[2])["archive_id"] == "archive-1"
    assert dedupe_carried_checkpoint(lossless(history(3)), loaded) is None
    summary = {"role": "user", "content": CHECKPOINT_PREFIX + "\nnote\n" + json.dumps({"summary": {"completed": ["x"]}})}
    assert dedupe_carried_checkpoint(summary, loaded) is summary


@pytest.mark.asyncio
@pytest.mark.parametrize("dedupe_on_carry", [True, False], ids=["carried-deduped", "pre-fix-checkpoint"])
async def test_repeated_carry_and_repack_never_accumulates_repeats(dedupe_on_carry):
    async def forbidden(*args, **kwargs):
        pytest.fail("this scenario stays on the lossless path")

    carried = None
    sizes = []
    for run in range(6):
        loaded = history(8 + run)
        # Without the carry-time de-duplication (checkpoints written before this
        # fix), re-packing alone must still keep each record once.
        prefix = [dedupe_carried_checkpoint(carried, loaded) if dedupe_on_carry else carried] if carried else []
        current = {"role": "user", "content": f"Run {run}: what is the best option under $1000 with two cats?"}
        messages = [{"role": "system", "content": "Rules."}, *[m for m in prefix if m], *loaded, TOOL_CALL, TOOL_OUTPUT, current]
        compacted = await compact_context(
            messages, model=None, metadata=None, tools=[], window=400_000, reserve=1_000, ratio=.75,
            active_user_message=current["content"], fresh_tail_start=len(messages), complete=forbidden,
            keep_recent=4, count_threshold=10,
        )
        carried = next(m for m in compacted if str(m.get("content", "")).startswith(CHECKPOINT_PREFIX))
        records = records_of(carried)
        keys = [context_message_key(r) for r in records if context_message_key(r)]
        assert max(Counter(keys).values()) == 1, f"run {run} re-packed a repeated record"
        sizes.append(len(records))
    # Each run adds at most its own new turn, never another copy of the history.
    assert all(later - earlier <= 4 for earlier, later in zip(sizes, sizes[1:]))


@pytest.mark.asyncio
async def test_message_count_alone_no_longer_compacts(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("62 messages far below the token trigger must not be summarized")

    monkeypatch.setattr("packages.core.ai.agentic_loop.runtime_execute_agentic_compaction_completion", forbidden)
    messages = [{"role": "system", "content": "Rules."}, *history(30), {"role": "user", "content": "And now?"}]
    assert len(messages) > 40
    assert await _compact_messages(messages, None, 0.2, active_user_message="And now?") == messages


def test_carried_checkpoint_is_attributed_as_checkpoint_not_user_input():
    checkpoint = lossless([TOOL_CALL, TOOL_OUTPUT])
    request = {"role": "user", "content": "What is the best option under $1000?"}
    attribution = _estimate_context_attribution([{"role": "system", "content": "s"}, checkpoint, *history(2), request], None)
    assert attribution["checkpoint_chars"] == len(checkpoint["content"])
    assert attribution["user_input_chars"] == len(request["content"])


@pytest.mark.asyncio
async def test_automatic_billing_records_the_rounds_prompt_breakdown(monkeypatch):
    recorded = []
    scheduled = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            return None

    async def record(db, **kwargs):
        recorded.append(kwargs["usage"])

    monkeypatch.setattr("packages.core.database.async_session", lambda: Session())
    monkeypatch.setattr("packages.core.services.usage_service.record_llm_usage", record)
    monkeypatch.setattr(llm_client, "fire_and_forget", lambda factory, **kwargs: scheduled.append(factory) or True)
    billing = llm_client.LLMBillingContext(entity_id="entity", source="worker")

    with llm_client.llm_context_attribution_scope({"system_tokens": 10, "history_tokens": 5}):
        llm_client._schedule_billing_record(billing, {"prompt": 15}, 1)
    llm_client._schedule_billing_record(billing, {"prompt": 15}, 1)
    with llm_client.llm_context_attribution_scope({"system_tokens": 1}):
        llm_client._schedule_billing_record(billing, {"prompt": 15, "context_attribution": {"own": 1}}, 1)
    for factory in scheduled:
        await factory()

    assert recorded[0]["context_attribution"] == {"system_tokens": 10, "history_tokens": 5}
    assert "context_attribution" not in recorded[1]
    assert recorded[2]["context_attribution"] == {"own": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("carried_records,expect_carried", [
    ("history-and-tools", True),
    ("history-only", False),
])
async def test_harness_carries_only_what_reloaded_history_lacks(mock_runtime, monkeypatch, carried_records, expect_carried):  # noqa: F811
    from packages.core.ai.runtime.harness import runtime_execute_agentic_loop

    loaded = history(4)
    records = [*history(2), TOOL_CALL, TOOL_OUTPUT] if carried_records == "history-and-tools" else history(2)
    seen = []

    async def callbacks(**_kwargs):
        async def save(_messages):
            return None
        return None, None, save, lossless(records)

    async def complete(messages, tools, **kwargs):
        seen.append(messages)
        return "Done.", [], {"total": 1}

    monkeypatch.setattr("packages.core.services.runtime_context_archive.runtime_context_archive_callbacks", callbacks)
    monkeypatch.setattr(mock_runtime, "runtime_execute_agentic_round_tool_completion", complete)
    kwargs = _loop_kwargs(_envelope("read_file"), None)
    kwargs.update(runtime_run_id="run-2", initial_messages=loaded, dynamic_tool_handlers={},
                  capability_refresh_tool_names=set(), max_rounds=2)
    await runtime_execute_agentic_loop(**kwargs)

    sent = seen[0]
    checkpoints = [m for m in sent if str(m.get("content", "")).startswith(CHECKPOINT_PREFIX)]
    assert bool(checkpoints) is expect_carried
    if expect_carried:
        assert records_of(checkpoints[0]) == [TOOL_CALL, TOOL_OUTPUT]
    keys = [context_message_key(m) for m in sent if context_message_key(m)]
    keys += [context_message_key(r) for c in checkpoints for r in records_of(c) if context_message_key(r)]
    assert max(Counter(keys).values()) == 1
