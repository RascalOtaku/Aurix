"""Batch 3 (Stage A): bg_jobs silent blocks — behavior only.

NOTE: `src.bg_jobs` does `from core.atomic_io import atomic_write_json`, and
importing the `core` package on this workbench pulls in `core.database`, which
needs SQLAlchemy (not installed here — pre-existing env gap, unrelated to this
change). The code paths under test never touch `atomic_write_json`, so we stub
`core.atomic_io` in sys.modules to keep the tests hermetic.
"""
import sys
import time
import types

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

from src import bg_jobs as bj


def test_load_swallows_corrupt_store(tmp_path, monkeypatch):
    """_load(): a corrupt store file yields an empty dict — never raises."""
    bad = tmp_path / "bg_jobs.json"
    bad.write_text("{not valid json")
    monkeypatch.setattr(bj, "_STORE", bad)
    assert bj._load() == {}


def test_prune_swallows_unlink_failure(tmp_path, monkeypatch):
    """_prune(): an undeletable job file can't break pruning — the record
    is still dropped and no exception propagates."""
    monkeypatch.setattr(bj, "_JOBS_DIR", tmp_path)
    jid = "job1"
    (tmp_path / f"{jid}.log").mkdir()  # unlink() on a directory raises
    jobs = {jid: {"followed_up": True, "ended_at": time.time() - 99999}}
    assert bj._prune(jobs, time.time()) is True  # must not raise
    assert jid not in jobs


def test_kill_swallows_undead_pid():
    """_kill(): killing a nonexistent pid fails both killpg and kill —
    swallowed, never raises."""
    bj._kill(99999999)  # must not raise
    bj._kill(None)  # early return, no-op


# ── Stage B: failure-record assertions (converted code) ──────────────────────
import logging as _logging


def _failure_records(caplog):
    return [r for r in caplog.records if "silenced_failure" in r.message]


def test_load_logs_corrupt_store(caplog, tmp_path, monkeypatch):
    bad = tmp_path / "bg_jobs.json"
    bad.write_text("{not valid json")
    monkeypatch.setattr(bj, "_STORE", bad)
    with caplog.at_level(_logging.DEBUG):
        assert bj._load() == {}
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "module=src.bg_jobs" in recs[0].message
    assert "exc_type=JSONDecodeError" in recs[0].message
    assert "store load failed" in recs[0].message
    assert recs[0].levelname == "WARNING"


def test_prune_logs_unlink_failure(caplog, tmp_path, monkeypatch):
    monkeypatch.setattr(bj, "_JOBS_DIR", tmp_path)
    jid = "job9"
    (tmp_path / f"{jid}.log").mkdir()
    jobs = {jid: {"followed_up": True, "ended_at": time.time() - 99999}}
    with caplog.at_level(_logging.DEBUG):
        assert bj._prune(jobs, time.time()) is True
    assert jid not in jobs
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "could not delete" in recs[0].message
    assert "job9.log" in recs[0].message
    assert recs[0].levelname == "WARNING"


def test_kill_logs_undead_pid(caplog):
    with caplog.at_level(_logging.DEBUG):
        bj._kill(99999999)  # still must not raise
    recs = _failure_records(caplog)
    assert len(recs) == 1
    assert "kill failed for pid 99999999" in recs[0].message
    assert recs[0].levelname == "WARNING"
