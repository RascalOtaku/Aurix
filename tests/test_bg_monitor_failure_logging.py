"""Batch 4 (Stage A): bg_monitor helper behavior — proves the extraction
preserved the silent-skip semantics. Stage B adds failure-record assertions.
"""
import sys
import types
from unittest.mock import patch

import pytest

try:                                    # the real module when it imports (CI, the server); a stub only on a
    import core.atomic_io  # noqa: F401   # bare workbench, so other tests' JSON saves are never no-ops
except Exception:
    _core_pkg = types.ModuleType("core")
    _core_pkg.__path__ = []
    sys.modules.setdefault("core", _core_pkg)
    _atomic_io = types.ModuleType("core.atomic_io")
    _atomic_io.atomic_write_json = lambda *a, **k: None
    sys.modules.setdefault("core.atomic_io", _atomic_io)

from src import bg_monitor as bm


def test_decode_sse_body_valid_dict():
    assert bm._decode_sse_body('{"delta": "hi"}') == {"delta": "hi"}


def test_decode_sse_body_malformed_returns_none():
    assert bm._decode_sse_body("{not json") is None
    assert bm._decode_sse_body("") is None


def test_decode_sse_body_non_dict_returns_none():
    assert bm._decode_sse_body("[1, 2]") is None
    assert bm._decode_sse_body("42") is None


def test_should_defer_true_when_busy():
    with patch("src.agent_runs.is_active", return_value=True):
        assert bm._should_defer("s1", "j1") is True


def test_should_defer_false_when_idle():
    with patch("src.agent_runs.is_active", return_value=False):
        assert bm._should_defer("s1", "j1") is False


def test_should_defer_swallows_check_failure():
    """A failing liveness check is swallowed and treated as 'not busy' —
    the follow-up proceeds instead of stalling."""
    with patch("src.agent_runs.is_active", side_effect=RuntimeError("db down")):
        assert bm._should_defer("s1", "j1") is False  # must not raise


# ── Stage B: failure-record assertions (converted code) ──────────────────────
import logging as _logging


def _failure_records(caplog):
    return [r for r in caplog.records if "silenced_failure" in r.message]


def test_decode_sse_body_logs_malformed_chunk(caplog):
    with caplog.at_level(_logging.DEBUG):
        assert bm._decode_sse_body("{not json") is None
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "module=src.bg_monitor" in recs[0].message
    assert "exc_type=JSONDecodeError" in recs[0].message
    assert "malformed chunk" in recs[0].message
    assert recs[0].levelname == "DEBUG"
    # no chunk content leaks into the record
    assert "{not json" not in recs[0].message


def test_decode_sse_body_logs_nothing_on_success(caplog):
    with caplog.at_level(_logging.DEBUG):
        assert bm._decode_sse_body('{"a": 1}') == {"a": 1}
    assert _failure_records(caplog) == []


def test_should_defer_logs_check_failure(caplog):
    with patch("src.agent_runs.is_active", side_effect=RuntimeError("db down")), \
         caplog.at_level(_logging.DEBUG):
        assert bm._should_defer("s1", "j1") is False
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "exc_type=RuntimeError" in recs[0].message
    assert "defer check failed" in recs[0].message
    assert recs[0].levelname == "WARNING"
