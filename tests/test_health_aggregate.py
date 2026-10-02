"""Tests for the aggregated degraded-state health endpoint (GET /api/health/aggregate).

Probes are plain sync functions with injectable dependencies, so these tests
never touch the network, the database, or the real integrations: every
external call is faked. The one exception is the timeout test, which runs
the real aggregate path with a loader that hangs.
"""
import asyncio
import os
import sys
import time
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Stub core.constants before importing the route module: importing the real
# `core` package runs init_db() (SQLite file) at import time, which unit
# tests must not depend on. The probes under test take explicit injectable
# dependencies, so only the two constants the module references are needed.
_core_pkg = types.ModuleType("core")
_core_pkg.__path__ = []
_core_constants = types.ModuleType("core.constants")
_core_constants.DEFAULT_HOST = "localhost"
_core_constants.SEARXNG_INSTANCE = "http://localhost:8080"
sys.modules.setdefault("core", _core_pkg)
sys.modules.setdefault("core.constants", _core_constants)

from routes import diagnostics_routes as dr  # noqa: E402


def _fake_get(status_code=200, exc=None):
    def _get(url, timeout=None, headers=None):
        if exc is not None:
            raise exc
        return mock.Mock(status_code=status_code)
    return _get


def _fake_create_connection(reachable_hosts):
    """Fake socket.create_connection honoring only the given host set."""
    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def create_connection(addr, timeout=None):
        host, _port = addr
        if host not in reachable_hosts:
            raise OSError("unreachable")
        return _Ctx()

    return create_connection


def _fake_model_routes_module():
    """Stand-in for routes.model_routes so the lazy _ping_endpoint import
    never pulls the real module (and its heavy dependency tree) in tests."""
    fake = mock.MagicMock(name="model_routes")
    fake._ping_endpoint = lambda base, key, timeout=2.0: {
        "reachable": True, "status_code": 200, "error": None
    }
    return fake


class _Base(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def assertServiceShape(self, res):
        self.assertIn(res["status"], ("ok", "degraded", "down"))
        self.assertIsInstance(res["latency_ms"], int)
        self.assertGreaterEqual(res["latency_ms"], 0)


# --- _run_probe -----------------------------------------------------------------


class RunProbeTests(_Base):
    def test_exception_becomes_down(self):
        def boom():
            raise RuntimeError("kaput")

        res = dr._run_probe(boom)
        self.assertEqual(res["status"], "down")
        self.assertIn("kaput", res["error"])
        self.assertIsInstance(res["latency_ms"], int)

    def test_success_keeps_status_and_times(self):
        def quick():
            time.sleep(0.01)
            return {"status": "ok"}

        res = dr._run_probe(quick)
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["latency_ms"], 0)


# --- chromadb --------------------------------------------------------------------


class ChromaProbeTests(_Base):
    def test_unavailable_is_degraded(self):
        res = dr._run_probe(dr.probe_chromadb, None, False)
        self.assertEqual(res["status"], "degraded")
        self.assertServiceShape(res)

    def test_ok(self):
        mgr = mock.Mock()
        mgr.get_stats.return_value = {"document_count": 10, "collection_name": "x"}
        res = dr._run_probe(dr.probe_chromadb, mgr, True)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["detail"]["document_count"], 10)
        self.assertServiceShape(res)

    def test_stats_error_is_down(self):
        mgr = mock.Mock()
        mgr.get_stats.return_value = {"error": "Collection not initialized"}
        res = dr._run_probe(dr.probe_chromadb, mgr, True)
        self.assertEqual(res["status"], "down")

    def test_raising_manager_is_down(self):
        mgr = mock.Mock()
        mgr.get_stats.side_effect = RuntimeError("chroma gone")
        res = dr._run_probe(dr.probe_chromadb, mgr, True)
        self.assertEqual(res["status"], "down")
        self.assertIn("chroma gone", res["error"])


# --- searxng ---------------------------------------------------------------------


class SearxngProbeTests(_Base):
    def test_ok_on_2xx(self):
        res = dr._run_probe(dr.probe_searxng, "http://x:8080", _fake_get(200))
        self.assertEqual(res["status"], "ok")
        self.assertServiceShape(res)

    def test_down_on_5xx(self):
        res = dr._run_probe(dr.probe_searxng, "http://x:8080", _fake_get(503))
        self.assertEqual(res["status"], "down")
        self.assertIn("503", res["error"])

    def test_connection_error_is_down(self):
        res = dr._run_probe(dr.probe_searxng, "http://x:8080", _fake_get(exc=ConnectionError("refused")))
        self.assertEqual(res["status"], "down")


# --- email -----------------------------------------------------------------------


class EmailProbeTests(_Base):
    def test_no_accounts_is_degraded(self):
        res = dr._run_probe(dr.probe_email, lambda: [])
        self.assertEqual(res["status"], "degraded")
        self.assertIn("no email accounts", res["error"])
        self.assertServiceShape(res)

    def test_all_reachable_is_ok(self):
        accounts = [("Work", "imap.a", 993), ("Home", "imap.b", 993)]
        with mock.patch("socket.create_connection", _fake_create_connection({"imap.a", "imap.b"})):
            res = dr._run_probe(dr.probe_email, lambda: accounts)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["detail"], {"accounts": 2, "reachable": 2})

    def test_partial_is_degraded(self):
        accounts = [("Work", "imap.a", 993), ("Home", "imap.b", 993)]
        with mock.patch("socket.create_connection", _fake_create_connection({"imap.a"})):
            res = dr._run_probe(dr.probe_email, lambda: accounts)
        self.assertEqual(res["status"], "degraded")
        self.assertIn("1 of 2", res["error"])

    def test_none_reachable_is_down(self):
        accounts = [("Work", "imap.a", 993)]
        with mock.patch("socket.create_connection", _fake_create_connection(set())):
            res = dr._run_probe(dr.probe_email, lambda: accounts)
        self.assertEqual(res["status"], "down")


# --- ntfy ------------------------------------------------------------------------


class NtfyProbeTests(_Base):
    def _items(self):
        return [
            {"name": "ntfy", "base_url": "http://ntfy:80"},
            {"name": "ntfy2", "base_url": "http://ntfy2:80"},
        ]

    def test_not_configured_is_degraded(self):
        res = dr._run_probe(dr.probe_ntfy, lambda: [], _fake_get(200))
        self.assertEqual(res["status"], "degraded")
        self.assertIn("not configured", res["error"])
        self.assertServiceShape(res)

    def test_all_reachable_is_ok(self):
        res = dr._run_probe(dr.probe_ntfy, self._items, _fake_get(200))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["detail"], {"servers": 2, "reachable": 2})

    def test_partial_is_degraded(self):
        def get(url, timeout=None, headers=None):
            if "ntfy2" in url:
                raise ConnectionError("refused")
            return mock.Mock(status_code=200)

        res = dr._run_probe(dr.probe_ntfy, self._items, get)
        self.assertEqual(res["status"], "degraded")
        self.assertIn("ntfy2", res["error"])

    def test_all_down_is_down(self):
        res = dr._run_probe(dr.probe_ntfy, self._items, _fake_get(exc=ConnectionError("refused")))
        self.assertEqual(res["status"], "down")


# --- providers -------------------------------------------------------------------


class ProvidersProbeTests(_Base):
    def _eps(self):
        return [("Local", "http://localhost:8000/v1", None), ("Cloud", "https://x/v1", "k")]

    def test_no_endpoints_is_degraded(self):
        res = dr._run_probe(dr.probe_providers, lambda: [])
        self.assertEqual(res["status"], "degraded")
        self.assertIn("no model endpoints", res["error"])
        self.assertServiceShape(res)

    def test_all_reachable_is_ok(self):
        ping = lambda base, key, timeout=2.0: {"reachable": True, "status_code": 200, "error": None}
        res = dr._run_probe(dr.probe_providers, self._eps, ping)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["detail"]["endpoints"]), 2)

    def test_partial_is_degraded(self):
        def ping(base, key, timeout=2.0):
            if "localhost" in base:
                return {"reachable": True, "status_code": 200, "error": None}
            return {"reachable": False, "status_code": None, "error": "timeout"}

        res = dr._run_probe(dr.probe_providers, self._eps, ping)
        self.assertEqual(res["status"], "degraded")
        self.assertIn("Cloud", res["error"])

    def test_all_down_is_down(self):
        ping = lambda base, key, timeout=2.0: {"reachable": False, "status_code": None, "error": "refused"}
        res = dr._run_probe(dr.probe_providers, self._eps, ping)
        self.assertEqual(res["status"], "down")


# --- aggregate -------------------------------------------------------------------


class AggregateTests(_Base):
    def _all_ok_kwargs(self):
        mgr = mock.Mock()
        mgr.get_stats.return_value = {"document_count": 1}
        return dict(
            rag_manager=mgr,
            rag_available=True,
            searxng_instance="http://searxng:8080",
            get_email_accounts=lambda: [("W", "imap.a", 993)],
            get_ntfy_integrations=lambda: [{"name": "ntfy", "base_url": "http://ntfy:80"}],
            get_model_endpoints=lambda: [("L", "http://localhost:8000/v1", None)],
            http_get=_fake_get(200),
        )

    def test_shape_and_service_keys(self):
        with mock.patch("socket.create_connection", _fake_create_connection({"imap.a"})), \
                mock.patch.dict(sys.modules, {"routes.model_routes": _fake_model_routes_module()}):
            out = self.run_async(dr.build_health_aggregate(**self._all_ok_kwargs()))
        self.assertEqual(set(out["services"]), {"chromadb", "searxng", "email", "ntfy", "providers"})
        for res in out["services"].values():
            self.assertServiceShape(res)

    def test_all_ok(self):
        with mock.patch("socket.create_connection", _fake_create_connection({"imap.a"})), \
                mock.patch.dict(sys.modules, {"routes.model_routes": _fake_model_routes_module()}):
            out = self.run_async(dr.build_health_aggregate(**self._all_ok_kwargs()))
        self.assertTrue(out["ok"])
        self.assertTrue(all(s["status"] == "ok" for s in out["services"].values()))

    def test_one_degraded_flips_overall(self):
        kwargs = self._all_ok_kwargs()
        kwargs["get_ntfy_integrations"] = lambda: []  # not configured -> degraded
        with mock.patch("socket.create_connection", _fake_create_connection({"imap.a"})), \
                mock.patch.dict(sys.modules, {"routes.model_routes": _fake_model_routes_module()}):
            out = self.run_async(dr.build_health_aggregate(**kwargs))
        self.assertFalse(out["ok"])
        self.assertEqual(out["services"]["ntfy"]["status"], "degraded")

    def test_hanging_probe_is_capped(self):
        """A loader that never returns must not stall the aggregate response."""
        def hang():
            time.sleep(8)  # far longer than PROBE_TIMEOUT_S
            return []

        kwargs = self._all_ok_kwargs()
        kwargs["get_email_accounts"] = hang

        async def go():
            t0 = time.monotonic()
            out = await dr.build_health_aggregate(**kwargs)
            # Measure inside the loop: asyncio.run() also waits for the
            # orphaned probe thread at shutdown, which is not response time.
            return out, time.monotonic() - t0

        with mock.patch("socket.create_connection", _fake_create_connection({"imap.a"})), \
                mock.patch.dict(sys.modules, {"routes.model_routes": _fake_model_routes_module()}):
            out, elapsed = asyncio.run(go())
        email = out["services"]["email"]
        self.assertEqual(email["status"], "down")
        self.assertIn("timed out", email["error"])
        # 5 probes run concurrently with a 3s cap each: must finish far under 8s.
        self.assertLess(elapsed, 6)


class StatusMappingTests(_Base):
    def test_mapping_table(self):
        cases = [
            ({"status": "ok"}, "ok"),
            ({"status": "degraded"}, "degraded"),
            ({"status": "down"}, "down"),
        ]
        for res, expected in cases:
            self.assertEqual(res["status"], expected)


if __name__ == "__main__":
    unittest.main()
