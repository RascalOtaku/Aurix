"""Tests for the in-memory failure-record store and the user-visible
GET /api/scheduler/failures surface.

The store backs the requirement "Failure records must be inspectable on a
user-visible surface": every `record()` call appends a bounded, secret-free
dict, and the app exposes it as JSON (most recent first) with limit/level
filtering. app.py cannot be imported in this environment (its third-party
deps are container-only), so the endpoint is verified structurally via AST:
route registration, handler signature, JSON shape, filter delegation, and
that the path stays auth-protected.
"""
import ast
import logging
import os
import threading
from datetime import datetime

import pytest

from src import failure_log


@pytest.fixture(autouse=True)
def _isolated_store():
    with failure_log._store_lock:
        failure_log._store.clear()
    yield
    with failure_log._store_lock:
        failure_log._store.clear()


def _log(name="src.task_scheduler"):
    return logging.getLogger(name)


# ── store behavior ──────────────────────────────────────────────────────────

def test_store_captures_all_fields():
    failure_log.record(_log(), ValueError("boom"), context="test path",
                       level=logging.WARNING)
    recs = failure_log.get_records()
    assert len(recs) == 1
    rec = recs[0]
    assert rec["module"] == "src.task_scheduler"
    assert rec["exc_type"] == "ValueError"
    assert rec["exc_msg"] == "boom"
    assert rec["context"] == "test path"
    assert rec["level"] == "WARNING"
    ts = datetime.fromisoformat(rec["timestamp"])
    assert ts.tzinfo is not None  # UTC-aware ISO timestamp


def test_store_captures_debug_level():
    failure_log.record(_log(), ValueError("quiet"), level=logging.DEBUG)
    recs = failure_log.get_records()
    assert len(recs) == 1
    assert recs[0]["level"] == "DEBUG"


def test_store_most_recent_first():
    failure_log.record(_log(), ValueError("a"), context="first")
    failure_log.record(_log(), ValueError("b"), context="second")
    recs = failure_log.get_records()
    assert [r["context"] for r in recs] == ["second", "first"]


def test_store_bounded_at_200():
    for i in range(250):
        failure_log.record(_log(), ValueError(f"e{i}"), context=f"ctx-{i}")
    recs = failure_log.get_records(limit=500)
    assert len(recs) == 200
    assert recs[0]["context"] == "ctx-249"   # most recent first
    assert recs[-1]["context"] == "ctx-50"   # oldest 50 evicted
    assert len(failure_log.get_records()) == 50  # default limit


def test_get_records_limit_and_level_filter():
    failure_log.record(_log(), ValueError("w1"), level=logging.WARNING)
    failure_log.record(_log(), ValueError("d1"), level=logging.DEBUG)
    failure_log.record(_log(), ValueError("w2"), level=logging.WARNING)
    failure_log.record(_log(), ValueError("d2"), level=logging.DEBUG)
    assert [r["exc_msg"] for r in failure_log.get_records(level="debug")] == ["d2", "d1"]
    assert [r["exc_msg"] for r in failure_log.get_records(level="WARNING")] == ["w2", "w1"]
    assert failure_log.get_records(level="bogus") == []
    assert len(failure_log.get_records(limit=2)) == 2
    assert failure_log.get_records(limit=0) == []


def test_returned_records_are_copies():
    failure_log.record(_log(), ValueError("x"), context="c")
    recs = failure_log.get_records()
    recs[0]["context"] = "MUTATED"
    assert failure_log.get_records()[0]["context"] == "c"


def test_record_never_raises_with_hostile_logger_and_exc():
    class BadLogger:
        @property
        def name(self):
            raise RuntimeError("no name")

        def log(self, *args, **kwargs):
            raise RuntimeError("no log")

    class BadExc(Exception):
        def __str__(self):
            raise RuntimeError("bad str")

    failure_log.record(BadLogger(), BadExc("x"), context="hostile")  # must not raise
    failure_log.record(_log("t"), BadExc("y"))  # must not raise
    # the logging side still swallowed everything; nothing recorded is required


def test_store_thread_safe_under_concurrent_writes():
    def hammer(tag, n):
        log = _log("m")
        for i in range(n):
            failure_log.record(log, ValueError(f"t{i}"), context=f"{tag}-{i}")

    threads = [threading.Thread(target=hammer, args=(f"th{k}", 50)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recs = failure_log.get_records(limit=500)
    assert len(recs) == 200  # bounded, no corruption, nothing raised


# ── endpoint surface (structural: app.py is not importable here) ────────────

def _app_tree():
    app_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
    with open(app_path, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename="app.py")


def _failures_handler(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "scheduler_failures":
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "get"
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
                and dec.args[0].value == "/api/scheduler/failures"
            ):
                return node
    return None


def test_endpoint_registered_with_limit_and_level_params():
    handler = _failures_handler(_app_tree())
    assert handler is not None, "GET /api/scheduler/failures route not found"
    arg_names = [a.arg for a in handler.args.args]
    assert "limit" in arg_names and "level" in arg_names
    defaults = dict(
        zip(
            [a.arg for a in handler.args.args[-len(handler.args.defaults):]],
            [ast.literal_eval(d) for d in handler.args.defaults],
        )
    )
    assert defaults.get("limit") == 50
    assert defaults.get("level") == ""


def test_endpoint_returns_records_json_from_store():
    handler = _failures_handler(_app_tree())
    assert handler is not None
    returns_records = False
    calls_get_records = False
    forwards_filters = False
    for node in ast.walk(handler):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            keys = [k.value for k in node.value.keys
                    if isinstance(k, ast.Constant)]
            if "records" in keys:
                returns_records = True
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_records"):
            calls_get_records = True
            kw_names = {kw.arg for kw in node.keywords}
            if {"limit", "level"} <= kw_names:
                forwards_filters = True
    assert returns_records, "handler must return a dict with a 'records' key"
    assert calls_get_records, "handler must delegate to failure_log.get_records"
    assert forwards_filters, "handler must forward limit/level to the store"


def _exempt_strings(tree):
    exact, prefixes = set(), []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            name = node.targets[0].id
            vals = []
            if isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
                vals = [e.value for e in node.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if name == "AUTH_EXEMPT_EXACT":
                exact.update(vals)
            elif name == "AUTH_EXEMPT_PREFIXES":
                prefixes.extend(vals)
    return exact, prefixes


def test_endpoint_stays_auth_protected():
    exact, prefixes = _exempt_strings(_app_tree())
    path = "/api/scheduler/failures"
    assert path not in exact, "endpoint must not be auth-exempt"
    assert not any(path.startswith(p) for p in prefixes), \
        "endpoint must not match an auth-exempt prefix"


def test_endpoint_is_admin_only():
    """Exception messages can carry URLs, paths or another user's details: only admins may read them."""
    handler = _failures_handler(_app_tree())
    calls = [n.func.id for n in ast.walk(handler) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "require_admin" in calls
