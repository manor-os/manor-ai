"""ChatTrace turn-preparation timing and event-loop stall probe."""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from packages.core.ai.chat_logger import ChatTrace


def test_marks_accumulate_wall_time_per_phase():
    trace = ChatTrace()
    time.sleep(0.02)
    trace.mark("context")
    trace.mark("context")
    time.sleep(0.01)
    trace.mark("history")
    assert trace.phase_ms["context"] >= 20
    assert trace.phase_ms["history"] >= 10
    assert list(trace.phase_ms) == ["context", "history"]


@pytest.mark.asyncio
async def test_probe_reports_synchronous_stalls(caplog):
    trace = ChatTrace()
    caplog.set_level(logging.WARNING, logger="manor.chat")
    trace.start_loop_probe(interval=0.01, report_ms=100)
    await asyncio.sleep(0.03)
    trace.mark("assemble_prompt")
    time.sleep(0.2)  # synchronous work blocks every coroutine
    await asyncio.sleep(0.05)
    trace.stop_loop_probe()
    assert trace.loop_max_lag_ms >= 150
    assert trace.loop_blocked_ms >= trace.loop_max_lag_ms
    assert any(
        "chat.loop_blocked" in record.getMessage()
        and "after_phase=assemble_prompt" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_phases_log_once_with_totals(caplog):
    trace = ChatTrace()
    caplog.set_level(logging.INFO, logger="manor.chat")
    trace.mark("context")
    trace.log_phases("prepare")
    trace.log_phases("prepare")
    lines = [r.getMessage() for r in caplog.records if "chat.phases" in r.getMessage()]
    assert len(lines) == 1
    assert "'phases_ms': {'context':" in lines[0]
    assert "'loop_blocked_ms': 0" in lines[0]


def test_probe_is_a_no_op_without_a_running_loop():
    trace = ChatTrace()
    trace.start_loop_probe()
    trace.stop_loop_probe()
    assert trace.loop_blocked_ms == 0
