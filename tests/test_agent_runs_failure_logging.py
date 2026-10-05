"""Batch 2 (Stage A): agent_runs._drain silent blocks — behavior only.

Proves each code path currently completes silently. Stage B adds failure-record
assertions after conversion.
"""
import asyncio
from unittest.mock import Mock, patch

import pytest

from src import agent_runs as ar


@pytest.fixture(autouse=True)
def _clean_runs():
    yield
    for sid in list(ar._RUNS):
        r = ar._RUNS.pop(sid)
        for t in (r.task, r.evict_task):
            if t and not t.done():
                t.cancel()


async def _agen(events):
    for e in events:
        yield e


def _run_in(session_id):
    run = ar._Run()
    ar._RUNS[session_id] = run
    return run


async def test_drain_swallows_prev_task_wait_failure():
    """_drain: failure while awaiting the previous run's task is swallowed —
    the new run still drains to completion."""
    run = _run_in("s-prev")

    async def _never():
        await asyncio.sleep(60)

    prev = asyncio.ensure_future(_never())
    try:
        with patch("asyncio.wait", side_effect=RuntimeError("wait blew up")):
            await ar._drain("s-prev", _agen(["ev1"]), prev_task=prev)  # must not raise
    finally:
        prev.cancel()
    assert run.status == "done"
    assert run.buffer == ["ev1"]


async def test_drain_swallows_subscriber_fanout_failure():
    """_drain: a dead subscriber queue can't break the drain — the event is
    still buffered and the run completes (sentinel delivery also swallowed)."""
    run = _run_in("s-fan")
    bad = Mock()
    bad.put_nowait.side_effect = RuntimeError("queue gone")
    run.subscribers.add(bad)
    await ar._drain("s-fan", _agen(["ev1"]))  # must not raise
    assert run.buffer == ["ev1"]
    assert run.status == "done"


class _CancellingAgen:
    """Async iterator whose cancellation triggers the aclose() path."""

    def __init__(self, aclose_exc):
        self._aclose_exc = aclose_exc
        self.aclosed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise asyncio.CancelledError()

    async def aclose(self):
        self.aclosed = True
        raise self._aclose_exc


async def test_drain_swallows_aclose_failure():
    """_drain: a failing agen.aclose() after cancellation is swallowed —
    the run is still marked stopped."""
    run = _run_in("s-aclose")
    agen = _CancellingAgen(RuntimeError("aclose blew up"))
    await ar._drain("s-aclose", agen)  # must not raise
    assert agen.aclosed
    assert run.status == "stopped"


# ── Stage B: failure-record assertions (converted code) ──────────────────────
import logging as _logging


def _failure_records(caplog):
    return [r for r in caplog.records if "silenced_failure" in r.message]


async def test_drain_logs_prev_task_wait_failure(caplog):
    run = _run_in("s-prev2")

    async def _never():
        await asyncio.sleep(60)

    prev = asyncio.ensure_future(_never())
    try:
        with patch("asyncio.wait", side_effect=RuntimeError("wait blew up")), \
             caplog.at_level(_logging.DEBUG):
            await ar._drain("s-prev2", _agen(["ev1"]), prev_task=prev)
    finally:
        prev.cancel()
    assert run.status == "done"
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "module=src.agent_runs" in recs[0].message
    assert "exc_type=RuntimeError" in recs[0].message
    assert "previous run drain failed" in recs[0].message
    assert recs[0].levelname == "WARNING"


async def test_drain_logs_subscriber_fanout_and_sentinel_failures(caplog):
    run = _run_in("s-fan2")
    bad = Mock()
    bad.put_nowait.side_effect = RuntimeError("queue gone")
    run.subscribers.add(bad)
    with caplog.at_level(_logging.DEBUG):
        await ar._drain("s-fan2", _agen(["ev1"]))
    assert run.status == "done"
    recs = _failure_records(caplog)
    assert len(recs) == 2  # fanout + end-sentinel
    assert any("subscriber fanout failed" in r.message for r in recs)
    assert any("end-sentinel delivery failed" in r.message for r in recs)
    assert all(r.levelname == "DEBUG" for r in recs)


async def test_drain_logs_aclose_failure(caplog):
    run = _run_in("s-aclose2")
    agen = _CancellingAgen(RuntimeError("aclose blew up"))
    with caplog.at_level(_logging.DEBUG):
        await ar._drain("s-aclose2", agen)
    assert run.status == "stopped"
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "generator aclose failed" in recs[0].message
    assert recs[0].levelname == "WARNING"
