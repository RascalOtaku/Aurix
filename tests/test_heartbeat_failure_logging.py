"""Batch 5 (Stage A): foundation/heartbeat silent blocks — behavior only.

Proves each best-effort section currently fails silently. Stage B adds
failure-record assertions after conversion.
"""
import logging
from unittest.mock import Mock, patch

import pytest

from src.foundation import heartbeat as hb


def test_activity_summary_skips_bad_timestamps():
    """activity_summary(): an unparseable audit timestamp is skipped —
    the summary still renders."""
    with patch.object(hb.audit, "recent",
                      return_value=[{"ts": "not-a-time", "event": "mission_completed"}]):
        out = hb.activity_summary(hours=24)
    assert out == "Last 24h: nothing recorded."


def test_waiting_for_you_swallows_section_failure():
    """waiting_for_you(): one failing approval section can't break the digest."""
    with patch("src.foundation.gaming.pending", side_effect=RuntimeError("gaming store down")):
        out = hb.waiting_for_you(limit=2)
    assert isinstance(out, str)


def test_waiting_for_you_swallows_shards_failure():
    with patch("src.foundation.shards.stale", side_effect=RuntimeError("shards down")), \
         patch("src.foundation.shards.paused_ids", return_value=[]):
        out = hb.waiting_for_you(limit=2)
    assert isinstance(out, str)


def _digest_stubs():
    return (
        patch.object(hb, "activity_summary", return_value="ACT"),
        patch.object(hb, "self_report", return_value="REP"),
    )


def test_pulse_digest_swallows_projects_failure():
    """pulse_digest(): a failing projects section is skipped silently."""
    with patch("src.foundation.projects.Registry", side_effect=RuntimeError("projects down")):
        out = hb.pulse_digest()
    assert isinstance(out, str) and "AURIX pulse" in out


def test_pulse_digest_swallows_waiting_failure():
    with patch.object(hb, "waiting_for_you", side_effect=RuntimeError("wfy down")):
        out = hb.pulse_digest()
    assert isinstance(out, str) and "AURIX pulse" in out


def _self_report_harness(**patches):
    store = Mock()
    store.active.return_value = None
    store.all.return_value = []
    ctx = [
        patch("src.foundation.standing.StandingStore"),
        patch.object(hb.audit, "verify"),
        patch.object(hb.audit, "head", return_value=(0, "abc")),
        patch.object(hb.audit, "recent", return_value=[]),
    ]
    return store, ctx


def test_self_report_swallows_approval_gate_failure():
    """self_report(): a failing approval-gate lookup is skipped silently."""
    store, ctx = _self_report_harness()
    with ctx[0] as SS, ctx[1] as av, ctx[2], ctx[3], \
         patch("src.approval_gate.pending_ids", side_effect=RuntimeError("gate down")):
        SS.return_value.all.return_value = []
        av.return_value = Mock(ok=True, acknowledged=[], records=5, bad_seq=0)
        out = hb.self_report(store=store)
    assert isinstance(out, str) and "AURIX status" in out


# ── Stage B: failure-record assertions (converted code) ──────────────────────

def _failure_records(caplog):
    return [r for r in caplog.records if "silenced_failure" in r.message]


def test_activity_summary_logs_bad_timestamp(caplog):
    with patch.object(hb.audit, "recent",
                      return_value=[{"ts": "not-a-time", "event": "mission_completed"}]), \
         caplog.at_level(logging.DEBUG):
        assert hb.activity_summary(hours=24) == "Last 24h: nothing recorded."
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "module=src.foundation.heartbeat" in recs[0].message
    assert "exc_type=ValueError" in recs[0].message
    assert "bad timestamp" in recs[0].message
    assert recs[0].levelname == "DEBUG"


def test_waiting_for_you_logs_section_failure(caplog):
    with patch("src.foundation.gaming.pending", side_effect=RuntimeError("gaming store down")), \
         caplog.at_level(logging.DEBUG):
        out = hb.waiting_for_you(limit=2)
    assert isinstance(out, str)
    recs = _failure_records(caplog)
    assert any("section gaming failed" in r.message for r in recs)
    assert all(r.levelname == "WARNING" for r in recs)


def test_waiting_for_you_logs_shards_failure(caplog):
    with patch("src.foundation.shards.stale", side_effect=RuntimeError("shards down")), \
         patch("src.foundation.shards.paused_ids", return_value=[]), \
         caplog.at_level(logging.DEBUG):
        out = hb.waiting_for_you(limit=2)
    assert isinstance(out, str)
    recs = _failure_records(caplog)
    assert any("shards section failed" in r.message for r in recs)


def test_pulse_digest_logs_projects_failure(caplog):
    with patch("src.foundation.projects.Registry", side_effect=RuntimeError("projects down")), \
         caplog.at_level(logging.DEBUG):
        out = hb.pulse_digest()
    assert "AURIX pulse" in out
    recs = _failure_records(caplog)
    assert any("projects section failed" in r.message for r in recs)


def test_pulse_digest_logs_waiting_failure(caplog):
    with patch.object(hb, "waiting_for_you", side_effect=RuntimeError("wfy down")), \
         caplog.at_level(logging.DEBUG):
        out = hb.pulse_digest()
    assert "AURIX pulse" in out
    recs = _failure_records(caplog)
    assert any("waiting-for-you section failed" in r.message for r in recs)


def test_self_report_logs_approval_gate_failure(caplog):
    store, ctx = _self_report_harness()
    with ctx[0] as SS, ctx[1] as av, ctx[2], ctx[3], \
         patch("src.approval_gate.pending_ids", side_effect=RuntimeError("gate down")), \
         caplog.at_level(logging.DEBUG):
        SS.return_value.all.return_value = []
        av.return_value = Mock(ok=True, acknowledged=[], records=5, bad_seq=0)
        out = hb.self_report(store=store)
    assert "AURIX status" in out
    recs = _failure_records(caplog)
    assert any("approval-gate section failed" in r.message for r in recs)
