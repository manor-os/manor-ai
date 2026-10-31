"""Checkpoint recovery against the real packed window; no database or model IO."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from packages.core.ai.runtime import context_compaction as c


def summary(text="Verified evidence; publishing still needs approval."):
    return {**{key: [] for key in c.CHECKPOINT_FIELDS}, "evidence": [text]}


def history(tokens=7700, *, window=8000):
    messages = [
        {"role": "system", "content": "Ask before publishing; preserve source files."},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "Continue with the original constraints."},
    ]
    low, high = 0, window * 8
    while low < high:
        mid = (low + high) // 2
        messages[1]["content"] = "x" * mid
        if c.request_tokens(messages) < tokens:
            low = mid + 1
        else:
            high = mid
    messages[1]["content"] = "x" * low
    assert c.request_tokens(messages) == tokens
    return messages


def payload(prompt):
    return json.loads(prompt[prompt.index('{"budget_tokens"'):])


async def compact(messages, complete, *, usages=None, archive=None, window=8000,
                  reserve=200, ratio=.75, target_ratio=.5, ledger="", tools=None):
    return await c.compact_context(
        messages, model="configured-byok-model",
        metadata={"route_receipt": "same-actor-and-model"},
        tools=tools or [], window=window, reserve=reserve, ratio=ratio,
        target_ratio=target_ratio, active_user_message=messages[-1]["content"],
        fresh_tail_start=len(messages), keep_recent=0, complete=complete,
        archive=archive, ledger=ledger,
        record_usage=usages.append if usages is not None else None,
    )


def unpack_summary(result):
    checkpoints = [m for m in result if str(m.get("content", "")).startswith(c.CHECKPOINT_PREFIX)]
    assert len(checkpoints) == 1
    return json.loads(checkpoints[0]["content"].split("\n", 2)[2])["summary"]


@pytest.mark.asyncio
async def test_complete_summary_above_target_is_accepted_without_rewrite():
    messages = history(16000, window=32000)
    original = deepcopy(messages)
    response = summary("verified " * 2000)
    assert c.estimate(c.encode(response)) > 4096
    calls, usages, archives = [], [], []
    usage = {"prompt": 120, "completion": 4500, "total": 4620,
             "billing_mode": "byok", "api_key_source": "byok", "finish_reason": "stop"}

    async def archive(value):
        archives.append(deepcopy(value))
        return "archive-before-summary"

    async def complete(prompt, **kwargs):
        assert archives == [original]
        calls.append((payload(prompt), kwargs))
        return c.encode(response), usage

    result = await compact(messages, complete, window=32000, ratio=.5, target_ratio=.1,
                           usages=usages, archive=archive)
    assert len(calls) == 1
    assert c.estimate(c.encode(response)) > calls[0][0]["budget_tokens"]
    assert unpack_summary(result) == response
    assert result[0] == original[0] and result[-1] == original[-1]
    assert messages == original and usages == [usage]
    assert calls[0][1]["model"] == "configured-byok-model"
    assert calls[0][1]["metadata"] == {"route_receipt": "same-actor-and-model"}
    c.validate_request(result, [], window=32000, reserve=200)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,finish", [
    ('{"evidence":["cut off', "length"),
    (c.encode(summary()), "max_tokens"),
    (c.encode(summary()), "max_output_tokens"),
    ("malformed {", "stop"),
    (None, "stop"),
    (c.encode(summary("oversized " * 5000)), "stop"),
], ids=["truncated", "max-tokens", "max-output-tokens", "malformed", "null", "oversized"])
async def test_unusable_summary_retries_once_from_same_source(response, finish):
    messages = history()
    original = deepcopy(messages)
    calls, usages = [], []
    first_usage = {"completion": 20, "finish_reason": finish}
    second_usage = {"completion": 10, "finish_reason": "stop"}

    async def complete(prompt, **kwargs):
        calls.append((payload(prompt), kwargs))
        if len(calls) == 1:
            return response, first_usage
        return c.encode(summary()), second_usage

    result = await compact(messages, complete, usages=usages)
    assert len(calls) >= 2
    assert calls[0][0]["records"] == calls[1][0]["records"]
    assert calls[0][0]["previous_checkpoint"] == calls[1][0]["previous_checkpoint"]
    assert calls[1][0]["budget_tokens"] < calls[0][0]["budget_tokens"]
    assert calls[0][1]["model"] == calls[1][1]["model"] == "configured-byok-model"
    assert calls[0][1]["metadata"] == calls[1][1]["metadata"]
    assert all(call[0]["records"] != calls[0][0]["records"] for call in calls[2:])
    assert usages == [first_usage, *[second_usage for _ in calls[1:]]]
    assert messages == original and unpack_summary(result) == summary()
    c.validate_request(result, [], window=8000, reserve=200)


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,fits", [(7700, True), (7900, False)])
async def test_failed_retry_only_falls_back_when_original_and_output_reserve_fit(tokens, fits):
    messages = history(tokens)
    original = deepcopy(messages)
    usage = {"completion": 20, "finish_reason": "length"}
    complete = AsyncMock(return_value=('truncated {', usage))
    usages = []
    if fits:
        assert await compact(messages, complete, usages=usages) is messages
    else:
        with pytest.raises(c.ContextBudgetExceeded):
            await compact(messages, complete, usages=usages)
    assert complete.await_count == 2
    assert messages == original and usages == [usage, usage]


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,fits", [(7700, True), (7900, False)])
async def test_provider_failure_does_not_add_another_transport_retry(tokens, fits):
    messages = history(tokens)
    original = deepcopy(messages)
    usage = {"error": "provider unavailable", "completion": 3}
    complete = AsyncMock(return_value=("", usage))
    usages = []
    if fits:
        assert await compact(messages, complete, usages=usages) is messages
    else:
        with pytest.raises(c.ContextBudgetExceeded):
            await compact(messages, complete, usages=usages)
    complete.assert_awaited_once()
    assert messages == original and usages == [usage]


@pytest.mark.asyncio
async def test_small_summary_window_keeps_fitting_original_without_provider_call():
    messages = [
        {"role": "system", "content": "Keep source"},
        {"role": "assistant", "content": "x" * 3900},
        {"role": "user", "content": "Continue"},
    ]
    original = deepcopy(messages)
    complete = AsyncMock()
    c.validate_request(messages, [], window=1300, reserve=100)
    assert await compact(messages, complete, window=1300, reserve=100,
                         ratio=.5, target_ratio=.2) is messages
    complete.assert_not_awaited()
    assert messages == original


@pytest.mark.asyncio
async def test_small_summary_still_checks_escaped_packed_request_before_accepting():
    messages = [
        {"role": "system", "content": "r " * 13000},
        {"role": "assistant", "content": "a " * 1000},
        {"role": "user", "content": "Continue"},
    ]
    original = deepcopy(messages)
    response = summary("\\" * 1300)
    assert c.request_tokens(messages) + 200 < 8000
    calls, usages = [], []

    async def complete(prompt, **kwargs):
        calls.append(payload(prompt))
        assert c.estimate(c.encode(response)) < calls[0]["budget_tokens"]
        return c.encode(response), {"total": 123, "finish_reason": "stop"}

    assert await compact(messages, complete, usages=usages) is messages
    assert len(calls) == 2 and sum(item["total"] for item in usages) == 246
    assert messages == original


@pytest.mark.asyncio
async def test_cancel_during_retry_propagates_and_preserves_completed_usage():
    messages = history()
    original = deepcopy(messages)
    usage = {"completion": 20, "finish_reason": "length"}
    complete = AsyncMock(side_effect=[('truncated {', usage), asyncio.CancelledError()])
    usages = []
    with pytest.raises(asyncio.CancelledError):
        await compact(messages, complete, usages=usages)
    assert complete.await_count == 2
    assert messages == original and usages == [usage]


@pytest.mark.asyncio
async def test_archive_failure_does_not_start_summary_or_replace_source():
    messages = history()
    original = deepcopy(messages)
    archive = AsyncMock(side_effect=RuntimeError("archive ownership lost"))
    complete = AsyncMock()
    with pytest.raises(RuntimeError, match="archive ownership lost"):
        await compact(messages, complete, archive=archive)
    complete.assert_not_awaited()
    assert messages == original


@pytest.mark.asyncio
async def test_multi_batch_checkpoint_keeps_next_request_output_allowance():
    messages = history(45000, window=32000)
    original = deepcopy(messages)
    large = summary("\\" * 6000)
    calls, usages = [], []

    async def complete(prompt, **kwargs):
        value = payload(prompt)
        calls.append((value, kwargs))
        assert c.request_tokens([{"role": "user", "content": prompt}]) + kwargs["max_tokens"] + 512 <= 32000
        if len(calls) == 1:
            return c.encode(large), {"total": 123, "finish_reason": "stop"}
        return c.encode(summary()), {"total": 10, "finish_reason": "stop"}

    result = await compact(messages, complete, usages=usages, window=32000)
    assert len(calls) > 2
    assert calls[0][0]["records"] == calls[1][0]["records"]
    assert calls[0][0]["previous_checkpoint"] == calls[1][0]["previous_checkpoint"]
    assert messages == original
    assert all(call[1]["max_tokens"] >= call[0]["budget_tokens"] for call in calls)
    c.validate_request(result, [], window=32000, reserve=200)
    assert sum(usage["total"] for usage in usages) == 123 + 10 * (len(calls) - 1)
