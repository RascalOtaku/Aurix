"""Regression tests for silent-exception conversion in the task scheduler.

Stage A (this file, behavior assertions only): proves each code path currently
completes silently — no exception propagates. After conversion, log-record
assertions are added proving each swallowed failure is now recorded.
"""
import asyncio
from unittest.mock import Mock, patch

import pytest

from src import builtin_actions
from src import task_scheduler as ts


def _sched():
    return ts.TaskScheduler(session_manager=Mock())


async def test_stop_swallows_cancelled_main_task():
    """stop(): awaiting an already-cancelled task raises CancelledError,
    which is swallowed — the scheduler must still stop cleanly."""
    sched = _sched()

    async def _noop():
        pass

    t = asyncio.ensure_future(_noop())
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    assert t.done()
    sched._task = t
    await sched.stop()  # must not raise
    assert sched._running is False


async def test_note_pings_loop_swallows_tasknoop():
    """_note_pings_loop(): TaskNoop from action_ping_notes is control flow
    (task chose to do nothing) — swallowed, loop continues and exits."""
    sched = _sched()
    sched._running = True
    sched._known_task_owners = lambda: ["owner1"]

    async def _boom(owner=None):
        raise builtin_actions.TaskNoop()

    sleeps = 0

    async def _sleep(_d):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            sched._running = False  # let the loop run one full pass, then exit

    with patch.object(builtin_actions, "action_ping_notes", _boom), \
         patch("asyncio.sleep", _sleep):
        await sched._note_pings_loop()  # must not raise
    assert sleeps >= 2


async def test_event_pings_loop_swallows_tasknoop():
    """_event_pings_loop(): same TaskNoop contract for calendar pings."""
    sched = _sched()
    sched._running = True
    sched._known_task_owners = lambda: ["owner1"]

    async def _boom(owner=None):
        raise builtin_actions.TaskNoop()

    sleeps = 0

    async def _sleep(_d):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            sched._running = False

    with patch.object(builtin_actions, "action_ping_events", _boom), \
         patch("asyncio.sleep", _sleep):
        await sched._event_pings_loop()  # must not raise
    assert sleeps >= 2


# ── Stage B: failure-record assertions (converted code) ──────────────────────
import logging


def _failure_records(caplog):
    return [r for r in caplog.records if "silenced_failure" in r.message]


async def test_stop_logs_cancelled_main_task(caplog):
    sched = _sched()

    async def _noop():
        pass

    t = asyncio.ensure_future(_noop())
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    sched._task = t
    with caplog.at_level(logging.DEBUG):
        await sched.stop()  # still must not raise
    recs = _failure_records(caplog)
    assert len(recs) == 1, [r.message for r in caplog.records]
    assert "module=src.task_scheduler" in recs[0].message
    assert "exc_type=CancelledError" in recs[0].message
    assert "already cancelled" in recs[0].message


async def test_note_pings_loop_logs_tasknoop(caplog):
    sched = _sched()
    sched._running = True
    sched._known_task_owners = lambda: ["owner1"]

    async def _boom(owner=None):
        raise builtin_actions.TaskNoop()

    sleeps = 0

    async def _sleep(_d):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            sched._running = False

    with patch.object(builtin_actions, "action_ping_notes", _boom), \
         patch("asyncio.sleep", _sleep), \
         caplog.at_level(logging.DEBUG):
        await sched._note_pings_loop()
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "exc_type=TaskNoop" in recs[0].message
    assert "no-op" in recs[0].message


async def test_event_pings_loop_logs_tasknoop(caplog):
    sched = _sched()
    sched._running = True
    sched._known_task_owners = lambda: ["owner1"]

    async def _boom(owner=None):
        raise builtin_actions.TaskNoop()

    sleeps = 0

    async def _sleep(_d):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 2:
            sched._running = False

    with patch.object(builtin_actions, "action_ping_events", _boom), \
         patch("asyncio.sleep", _sleep), \
         caplog.at_level(logging.DEBUG):
        await sched._event_pings_loop()
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "exc_type=TaskNoop" in recs[0].message


def test_sse_chunk_accumulates_delta():
    tools = []
    out = ts._accumulate_stream_chunk('data: {"delta": "hello "}', "", tools)
    assert out == "hello "
    assert tools == []


def test_sse_chunk_accumulates_tool_output():
    tools = []
    out = ts._accumulate_stream_chunk(
        'data: {"type": "tool_output", "tool": "ls", "stdout": "file.txt"}', "", tools)
    assert out == ""
    assert tools == ["[ls] file.txt"]


def test_sse_chunk_skips_done_marker():
    assert ts._accumulate_stream_chunk("data: [DONE]", "keep", []) == "keep"


def test_sse_chunk_skips_malformed_and_logs(caplog):
    """Malformed chunk: text unchanged, no raise, and a failure record exists."""
    tools = []
    with caplog.at_level(logging.DEBUG):
        out = ts._accumulate_stream_chunk("data: {not valid json", "keep", tools)
    assert out == "keep"
    assert tools == []
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "module=src.task_scheduler" in recs[0].message
    assert "exc_type=JSONDecodeError" in recs[0].message
    assert "malformed chunk" in recs[0].message
    # no message content / payload leaks into the record
    assert "{not valid json" not in recs[0].message
