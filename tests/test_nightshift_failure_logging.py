"""Batch 6 (Stage A): foundation/nightshift silent blocks — behavior only.

Proves each best-effort path currently fails silently. Stage B adds
failure-record assertions after conversion.
"""
import logging
import time
from unittest.mock import Mock, patch

import pytest

from src.foundation import heartbeat as hb
from src.foundation import nightshift as ns


def _healthy_gift_stubs():
    return (
        patch("src.foundation.evals.load_history", return_value=[]),
        patch("src.foundation.shards.stale", return_value=[]),
    )


def test_gift_health_swallows_disk_usage_failure():
    """_gift_health(): disk_usage failing is skipped — the gift still renders."""
    p1, p2 = _healthy_gift_stubs()
    with p1, p2, patch("shutil.disk_usage", side_effect=OSError("no disk")):
        g = ns._gift_health(123.0, {})
    assert g["id"] == "health"
    assert "disk" not in g["body"]


def test_gift_health_swallows_evals_failure():
    p_disk = patch("shutil.disk_usage")
    with p_disk, \
         patch("src.foundation.evals.load_history", side_effect=RuntimeError("evals down")), \
         patch("src.foundation.shards.stale", return_value=[]):
        g = ns._gift_health(123.0, {})
    assert g["id"] == "health"


def test_gift_health_swallows_shards_failure():
    p_disk = patch("shutil.disk_usage")
    with p_disk, \
         patch("src.foundation.evals.load_history", return_value=[]), \
         patch("src.foundation.shards.stale", side_effect=RuntimeError("shards down")):
        g = ns._gift_health(123.0, {})
    assert g["id"] == "health"


def test_run_shift_swallows_pause_check_failure():
    """run_shift(): a failing pause check is swallowed — the shift proceeds
    (here it then hits the already-ran-today short-circuit, no raise)."""
    with patch("src.foundation.shards.is_paused", side_effect=RuntimeError("shards down")), \
         patch.object(ns, "state",
                      return_value={"last_shift_day": time.strftime("%Y-%m-%d")}):
        out = ns.run_shift()
    assert out == {"skipped": "already ran today"}


def test_unwrap_swallows_waiting_failure():
    """unwrap(): a failing waiting-for-you section is skipped silently."""
    fake = [{"ts": 1, "day": "2026-01-01", "gifts": [], "opened": True}]
    with patch.object(ns, "shifts", return_value=fake), \
         patch.object(hb, "waiting_for_you", side_effect=RuntimeError("wfy down")):
        out = ns.unwrap(mark_opened=False, waiting=True)
    assert isinstance(out, str)


# ── Stage B: failure-record assertions (converted code) ──────────────────────

def _failure_records(caplog):
    return [r for r in caplog.records if "silenced_failure" in r.message]


def test_gift_health_logs_disk_usage_failure(caplog):
    p1, p2 = _healthy_gift_stubs()
    with p1, p2, patch("shutil.disk_usage", side_effect=OSError("no disk")), \
         caplog.at_level(logging.DEBUG):
        g = ns._gift_health(123.0, {})
    assert g["id"] == "health"
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "module=src.foundation.nightshift" in recs[0].message
    assert "exc_type=OSError" in recs[0].message
    assert "disk usage check failed" in recs[0].message
    assert recs[0].levelname == "WARNING"


def test_gift_health_logs_evals_failure(caplog):
    with patch("shutil.disk_usage"), \
         patch("src.foundation.evals.load_history", side_effect=RuntimeError("evals down")), \
         patch("src.foundation.shards.stale", return_value=[]), \
         caplog.at_level(logging.DEBUG):
        g = ns._gift_health(123.0, {})
    assert g["id"] == "health"
    recs = _failure_records(caplog)
    assert any("eval history failed" in r.message for r in recs)


def test_gift_health_logs_shards_failure(caplog):
    with patch("shutil.disk_usage"), \
         patch("src.foundation.evals.load_history", return_value=[]), \
         patch("src.foundation.shards.stale", side_effect=RuntimeError("shards down")), \
         caplog.at_level(logging.DEBUG):
        g = ns._gift_health(123.0, {})
    assert g["id"] == "health"
    recs = _failure_records(caplog)
    assert any("helper health check failed" in r.message for r in recs)


def test_run_shift_logs_pause_check_failure(caplog):
    with patch("src.foundation.shards.is_paused", side_effect=RuntimeError("shards down")), \
         patch.object(ns, "state",
                      return_value={"last_shift_day": time.strftime("%Y-%m-%d")}), \
         caplog.at_level(logging.DEBUG):
        out = ns.run_shift()
    assert out == {"skipped": "already ran today"}
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "pause check failed" in recs[0].message
    assert recs[0].levelname == "WARNING"


def test_unwrap_logs_waiting_failure(caplog):
    fake = [{"ts": 1, "day": "2026-01-01", "gifts": [], "opened": True}]
    with patch.object(ns, "shifts", return_value=fake), \
         patch.object(hb, "waiting_for_you", side_effect=RuntimeError("wfy down")), \
         caplog.at_level(logging.DEBUG):
        out = ns.unwrap(mark_opened=False, waiting=True)
    assert isinstance(out, str)
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "waiting-for-you section failed" in recs[0].message
    assert recs[0].levelname == "WARNING"
