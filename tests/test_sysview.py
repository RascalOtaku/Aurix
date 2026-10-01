"""AURIX Systems: snapshot shape, resilience to broken probes, honest audit/approval reporting, STOP."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, sysview, mission as ms, standing as st  # noqa: E402

ORGANS = {"skeletal", "nervous", "brain", "circulatory", "immune", "muscular", "memory", "endocrine"}
GOOD = {
    "system": lambda: {"cpu_pct": 12.0, "mem_pct": 40.0, "disk_pct": 50.0, "disk_free_gb": 200.0, "load1": 0.4, "uptime_h": 30.0},
    "endpoints": lambda: [{"name": "Precision 3431", "ok": True, "url": "http://x:11434",
                           "loaded": [{"name": "qwen2.5:14b", "vram_gb": 5.1, "size_gb": 9.0}], "models": None},
                          {"name": "7070 local", "ok": False, "url": "http://y:11434", "loaded": [], "models": None}],
    "sandbox": lambda: {"available": True, "tools_present": 9, "tools_total": 11, "openhands": True, "missing": ["blender"]},
    "telegram": lambda: {"configured": True, "listener_alive": True, "scheduler_alive": True, "in_flight": 0},
    "governor": lambda: {"mode": "balanced"},
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0"})
        self.env.start()
        sysview._verify_cache.update(key=None, at=0.0, value=None)
        ag.reset_state()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()


class SnapshotTests(_Base):
    def test_empty_system_has_every_organ(self):
        snap = sysview.snapshot(GOOD, now=1_800_000_000)
        self.assertEqual(set(snap["organs"]), ORGANS)
        for o in snap["organs"].values():
            self.assertIn(o["status"], {"ok", "warn", "bad", "idle"})
            self.assertTrue(o["metrics"])
        self.assertEqual((snap["missions"], snap["standing"], snap["approvals"]), ([], [], []))
        self.assertTrue(snap["audit"]["ok"])
        self.assertFalse(snap["stop"])

    def test_brain_reports_endpoint_up_and_down(self):
        m = sysview.snapshot(GOOD)["organs"]["brain"]
        self.assertEqual(m["status"], "ok")
        self.assertIn("qwen2.5:14b", m["metrics"]["Precision 3431"])
        self.assertEqual(m["metrics"]["7070 local"], "DOWN")

    def test_all_endpoints_down_is_bad_and_no_endpoints_is_idle(self):
        down = dict(GOOD, endpoints=lambda: [{"name": "a", "ok": False, "loaded": []}])
        self.assertEqual(sysview.snapshot(down)["organs"]["brain"]["status"], "bad")
        self.assertEqual(sysview.snapshot(dict(GOOD, endpoints=lambda: []))["organs"]["brain"]["status"], "idle")

    def test_a_crashing_probe_greys_one_organ_not_the_page(self):
        def boom():
            raise RuntimeError("probe exploded")
        snap = sysview.snapshot(dict(GOOD, sandbox=boom, endpoints=boom, telegram=boom))
        self.assertEqual(set(snap["organs"]), ORGANS)
        self.assertEqual(snap["organs"]["muscular"]["status"], "warn")
        self.assertEqual(snap["organs"]["brain"]["status"], "idle")
        self.assertEqual(snap["organs"]["nervous"]["status"], "idle")

    def test_sandbox_unreachable_is_flagged(self):
        s = sysview.snapshot(dict(GOOD, sandbox=lambda: {"available": False}))["organs"]["muscular"]
        self.assertEqual(s["status"], "warn")
        self.assertEqual(s["metrics"]["sandbox"], "UNREACHABLE")

    def test_high_memory_warns(self):
        s = sysview.snapshot(dict(GOOD, system=lambda: {"mem_pct": 95.0}))["organs"]["circulatory"]
        self.assertEqual(s["status"], "warn")

    def test_no_probes_at_all_still_renders(self):
        snap = sysview.snapshot({})
        self.assertEqual(set(snap["organs"]), ORGANS)


class StateReportingTests(_Base):
    def test_missions_and_standing_are_listed(self):
        store = ms.MissionStore()
        m = store.propose(ms.MissionContract(id="m-abc123", objective="convert a CT scan",
                                             steps=[ms.Step(id="s1", title="load scan")]))
        store.activate(m.id, decided_by="owner")
        snap = sysview.snapshot(GOOD)
        self.assertEqual(snap["active"], "m-abc123")
        self.assertEqual(snap["missions"][0]["status"], "active")
        self.assertEqual(snap["missions"][0]["current"], "load scan")
        self.assertEqual(snap["organs"]["memory"]["metrics"]["missions"], 1)

    def test_pending_approval_is_shown(self):
        loop = asyncio.new_event_loop()
        try:
            p = ag.PendingApproval(id="a1b2", tool="bash", preview="ls   -la\n/tmp", session_id="s", owner="o",
                                   created=100.0, future=loop.create_future(), key="k", risk="MEDIUM")
            ag._pending["a1b2"] = p
            snap = sysview.snapshot(GOOD, now=130.0)
        finally:
            loop.close()
        self.assertEqual(snap["approvals"][0]["id"], "a1b2")
        self.assertEqual(snap["approvals"][0]["preview"], "ls -la /tmp")     # whitespace collapsed
        self.assertEqual(snap["approvals"][0]["age_s"], 30)
        self.assertEqual(snap["organs"]["immune"]["metrics"]["waiting on you"], 1)

    def test_stop_state_is_visible(self):
        ag.engage_stop(60)
        snap = sysview.snapshot(GOOD)
        self.assertTrue(snap["stop"])
        self.assertEqual(snap["organs"]["immune"]["status"], "warn")

    def test_events_newest_first_with_detail(self):
        for i in range(3):
            audit.append("probe_event", note=f"note {i}")
        ev = sysview.snapshot(GOOD)["events"]
        self.assertEqual(ev[0]["detail"], "note 2")
        self.assertEqual([e["seq"] for e in ev], sorted((e["seq"] for e in ev), reverse=True))

    def test_tampered_audit_chain_is_bad(self):
        audit.append("first")
        audit.append("second")
        path = audit.audit_path()
        path.write_text(path.read_text(encoding="utf-8").replace("first", "FORGED"), encoding="utf-8")
        snap = sysview.snapshot(GOOD)                      # cache key includes size+mtime, so this re-verifies
        self.assertFalse(snap["audit"]["ok"])
        self.assertEqual(snap["organs"]["immune"]["status"], "bad")
        self.assertEqual(snap["organs"]["immune"]["metrics"]["audit chain"], "TAMPERING DETECTED")

    def test_protected_component_refusals_are_counted(self):
        audit.append("denied", reason="protected_component", tool="write_file")
        audit.append("denied", reason="protected_component", tool="write_file")
        m = sysview.snapshot(GOOD)["organs"]["immune"]["metrics"]
        self.assertEqual(m["protected-file attempts refused"], 2)

    def test_standing_missions_shown_in_endocrine(self):
        store = st.StandingStore()
        sm = store.create("research", st.Schedule("daily", "03:00"), ms.MissionContract(id="m-000001", objective="research"))
        store.approve(sm.id)
        snap = sysview.snapshot(GOOD)
        self.assertEqual(snap["organs"]["endocrine"]["metrics"]["standing active"], 1)
        self.assertEqual(snap["standing"][0]["schedule"], "every day at 03:00")


class StopTests(_Base):
    def test_do_stop_engages_gate_stop(self):
        with mock.patch.dict(sys.modules, {"services.telegram.listener": None}):    # force the no-listener path
            msg = asyncio.run(sysview.do_stop())
        self.assertIn("STOPPED", msg)
        self.assertTrue(ag.stop_engaged())
        self.assertTrue(any(r["event"] == "owner_stop" for r in audit.recent(5)))


class ProbeTests(unittest.TestCase):
    def test_ollama_root_normalisation(self):
        f = sysview._ollama_root
        self.assertEqual(f("http://h:11434/v1"), "http://h:11434")
        self.assertEqual(f("http://h:11434/v1/"), "http://h:11434")
        self.assertEqual(f("http://h:11434"), "http://h:11434")
        self.assertEqual(f("http://h:11434/v1/chat/completions"), "http://h:11434")

    def test_unreachable_endpoint_is_reported_not_raised(self):
        r = sysview._probe_one_endpoint({"name": "dead", "base_url": "http://127.0.0.1:9/v1"}, timeout=0.3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["name"], "dead")

    def test_non_http_endpoint_is_never_opened(self):
        with mock.patch("urllib.request.urlopen") as op:
            r = sysview._probe_one_endpoint({"name": "x", "base_url": "file:///etc/passwd"})
        op.assert_not_called()
        self.assertFalse(r["ok"])

    def test_governor_probe_never_raises(self):
        self.assertIn("mode", sysview.probe_governor())


class PageTests(unittest.TestCase):
    """The page renders model-influenced text (mission objectives, tool previews): never via innerHTML."""

    PAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "sysview.html")

    def _src(self):
        with open(self.PAGE, encoding="utf-8") as f:
            return f.read()

    def test_page_never_uses_innerhtml(self):
        code = self._src().split("<script", 1)[1]
        for bad in (".innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, code)
        self.assertIn("X-AURIX-Confirm", code)

    def test_every_script_is_allowed_by_the_apps_csp(self):
        """Regression: the app sends script-src 'self' 'nonce-...'. An inline <script> without the nonce is silently
        blocked, which is exactly how the first deploy rendered a blank dashboard."""
        import re
        src = self._src()
        tags = re.findall(r"<script\b[^>]*>", src)
        self.assertTrue(tags)
        for tag in tags:
            self.assertTrue('nonce="{{CSP_NONCE}}"' in tag or " src=" in tag, f"script would be blocked by CSP: {tag}")
        page = sysview.render_page(src, "abc123nonce")
        self.assertNotIn("{{CSP_NONCE}}", page)
        self.assertEqual(len(re.findall(r'<script nonce="abc123nonce">', page)), len(tags))
        self.assertEqual(sysview.render_page(src, ""), src.replace("{{CSP_NONCE}}", ""))       # no nonce -> harmless

    def test_page_only_talks_to_its_own_origin(self):
        """CSP is default-src 'self'; connect-src 'self' (fonts/scripts only from self + jsdelivr): nothing external."""
        import re
        src = self._src()
        self.assertEqual(re.findall(r"""(?:src|href|action)=["']https?://""", src), [])
        for url in re.findall(r"""fetch\(\s*["']([^"']+)""", src):
            self.assertTrue(url.startswith("/"), url)


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:                                         # the dev PC has no fastapi; the 7070 image does
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class RouteTests(_Base):
    def _client(self):
        from routes.sysview_routes import setup_sysview_routes
        app = FastAPI()
        app.include_router(setup_sysview_routes())
        return TestClient(app)

    def test_stop_needs_confirm_header(self):
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "false"}):
            c = self._client()
            self.assertEqual(c.post("/api/systems/stop").status_code, 400)
            self.assertFalse(ag.stop_engaged())

    def test_snapshot_and_page_served(self):
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "false"}), \
                mock.patch.object(sysview, "live_probes", return_value=GOOD):
            c = self._client()
            r = c.get("/api/systems")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(set(r.json()["organs"]), ORGANS)
            self.assertIn("SYSTEMS", c.get("/systems").text)

    def test_unauthenticated_is_refused(self):
        with mock.patch.dict(os.environ, {"AUTH_ENABLED": "true"}):
            c = self._client()
            self.assertEqual(c.get("/api/systems").status_code, 403)
            self.assertEqual(c.post("/api/systems/stop", headers={"X-AURIX-Confirm": "STOP"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
