"""Watchdog: alerts only for real problems, once each, re-alert after 6 h, say when cleared, restart notice."""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, sysview, mission as ms, watchdog  # noqa: E402

T0 = 1_800_000_000.0
GRANTED = dict(env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False, authorizations={})


def snap(audit_ok=True, disk=40.0, sandbox="isolated, running", missions=(), standing=()):
    return {"audit": {"ok": audit_ok}, "missions": list(missions), "standing": list(standing),
            "organs": {"circulatory": {"metrics": {"disk %": disk}}, "muscular": {"metrics": {"sandbox": sandbox}}}}


class EvaluateTests(unittest.TestCase):
    def run_once(self, snapshot, state=None, now=T0, stuck=None):
        return watchdog.evaluate(snapshot, state or {}, now, stuck)

    def test_healthy_system_is_silent(self):
        msgs, state = self.run_once(snap())
        self.assertEqual(msgs, [])
        self.assertEqual(state["seen"], {})

    def test_audit_tamper_alerts_once_then_stays_quiet(self):
        msgs, state = self.run_once(snap(audit_ok=False))
        self.assertEqual(len(msgs), 1)
        self.assertIn("BROKEN", msgs[0])
        msgs2, state2 = self.run_once(snap(audit_ok=False), state, now=T0 + 600)
        self.assertEqual(msgs2, [])                                        # not repeated every 5 minutes
        self.assertEqual(state2["seen"]["audit_tamper"]["since"], T0)

    def test_realerts_after_six_hours_marked_still(self):
        _, state = self.run_once(snap(audit_ok=False))
        msgs, _ = self.run_once(snap(audit_ok=False), state, now=T0 + watchdog.REALERT_SECONDS + 1)
        self.assertEqual(len(msgs), 1)
        self.assertIn("still", msgs[0])

    def test_clears_with_one_line_then_silence(self):
        _, state = self.run_once(snap(audit_ok=False))
        msgs, state2 = self.run_once(snap(), state, now=T0 + 300)
        self.assertEqual(len(msgs), 1)
        self.assertIn("Cleared", msgs[0])
        self.assertIn("audit_tamper", msgs[0])
        msgs3, _ = self.run_once(snap(), state2, now=T0 + 600)
        self.assertEqual(msgs3, [])

    def test_disk_threshold(self):
        # three tiers: quiet below 85, a gentle heads-up from 85 (added after a disk sat at 96% unnoticed), the alarm from 92
        self.assertEqual(self.run_once(snap(disk=84.9))[0], [])
        early, _ = self.run_once(snap(disk=91.9))
        self.assertEqual(len(early), 1)
        self.assertIn("91% full", early[0])                                  # truncated, so it never reads "92% full ... before it hits 92%"
        self.assertNotIn("will start failing", early[0])
        msgs, _ = self.run_once(snap(disk=93.0))
        self.assertIn("93% full", msgs[0])
        self.assertIn("will start failing", msgs[0])

    def test_sandbox_down_needs_two_strikes_and_a_reason_to_care(self):
        active = [{"status": "active", "sandboxed": True}]
        m1, s1 = self.run_once(snap(sandbox="UNREACHABLE", missions=active))
        self.assertEqual(m1, [])                                            # one failed probe is noise
        m2, s2 = self.run_once(snap(sandbox="UNREACHABLE", missions=active), s1, now=T0 + 300)
        self.assertEqual(len(m2), 1)
        self.assertIn("sandbox is unreachable", m2[0])
        m3, _ = self.run_once(snap(sandbox="UNREACHABLE", missions=active), s2, now=T0 + 600)
        self.assertEqual(m3, [])

    def test_sandbox_down_with_nothing_depending_on_it_is_not_worth_a_ping(self):
        state = {}
        for i in range(4):
            msgs, state = self.run_once(snap(sandbox="UNREACHABLE"), state, now=T0 + 300 * i)
            self.assertEqual(msgs, [])

    def test_sandbox_blip_resets_the_strike_count(self):
        active = [{"status": "active", "sandboxed": True}]
        _, s = self.run_once(snap(sandbox="UNREACHABLE", missions=active))
        _, s = self.run_once(snap(missions=active), s, now=T0 + 300)              # recovered
        msgs, _ = self.run_once(snap(sandbox="UNREACHABLE", missions=active), s, now=T0 + 600)
        self.assertEqual(msgs, [])                                                # back to strike one

    def test_standing_failures(self):
        ok = [{"id": "sm-aaaaaa", "status": "active", "title": "hunt", "failing": 1}]
        bad = [{"id": "sm-aaaaaa", "status": "active", "title": "hunt <b>", "failing": 2}]
        self.assertEqual(self.run_once(snap(standing=ok))[0], [])
        msgs, _ = self.run_once(snap(standing=bad))
        self.assertIn("sm-aaaaaa", msgs[0])
        self.assertIn("&lt;b&gt;", msgs[0])                                       # escaped

    def test_stuck_mission(self):
        self.assertEqual(self.run_once(snap(), stuck=("m-abc123", 30))[0], [])
        msgs, _ = self.run_once(snap(), stuck=("m-abc123", 75))
        self.assertIn("m-abc123", msgs[0])
        self.assertIn("75 min", msgs[0])

    def test_independent_problems_alert_independently(self):
        msgs, state = self.run_once(snap(audit_ok=False, disk=95))
        self.assertEqual(len(msgs), 2)
        msgs2, _ = self.run_once(snap(audit_ok=False, disk=50), state, now=T0 + 300)
        self.assertEqual(len(msgs2), 1)
        self.assertIn("disk_low", msgs2[0])


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0",
                                                "AURIX_WATCHDOG_MINUTES": "5"})
        self.env.start()
        sysview._verify_cache.update(key=None, at=0.0, value=None)
        ag.reset_state()
        self.said = []

        async def notify(t):
            self.said.append(t)

        async def agent(p):
            return "STEP DONE: ok"

        self.f = commands.Foundation(agent, notify, llm=None, session_id="tg", **GRANTED)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()


class TickTests(_Base):
    async def test_disabled_when_zero(self):
        with mock.patch.dict(os.environ, {"AURIX_WATCHDOG_MINUTES": "0"}), \
                mock.patch.object(sysview, "snapshot") as snapshot:
            self.assertEqual(await self.f.tick_watchdog(T0), [])
        snapshot.assert_not_called()

    async def test_throttled_and_persisted_and_audited(self):
        bad = snap(audit_ok=False)
        with mock.patch.object(sysview, "snapshot", return_value=bad) as sn:
            first = await self.f.tick_watchdog(T0)
            second = await self.f.tick_watchdog(T0 + 60)               # inside the 5-minute interval
            third = await self.f.tick_watchdog(T0 + 360)               # due again, but already told
        self.assertEqual((len(first), second, third), (1, [], []))
        self.assertEqual(sn.call_count, 2)
        self.assertEqual(len(self.said), 1)
        self.assertTrue(any(r["event"] == "watchdog_alert" for r in audit.recent(10)))
        self.assertIn("audit_tamper", watchdog.load_state()["seen"])

    async def test_state_survives_a_restart(self):
        with mock.patch.object(sysview, "snapshot", return_value=snap(audit_ok=False)):
            await self.f.tick_watchdog(T0)
            fresh = commands.Foundation(self.f.run_agent, self.f.notify, llm=None, **GRANTED)      # "restarted"
            self.assertEqual(await fresh.tick_watchdog(T0 + 400), [])

    async def test_tick_standing_runs_the_watchdog_but_never_breaks_on_it(self):
        with mock.patch.object(sysview, "snapshot", side_effect=RuntimeError("probe blew up")):
            await self.f.tick_standing(T0)                             # must not raise
        with mock.patch.object(sysview, "snapshot", return_value=snap(disk=99)):
            await self.f.tick_standing(T0 + 1000)
        self.assertTrue(any("Disk" in s for s in self.said))


class StartupNoticeTests(_Base):
    def _active(self):
        m = self.f.store.propose(ms.MissionContract(id="m-abc123", objective="convert a skull CT",
                                                     steps=[ms.Step(id="s1", title="a"), ms.Step(id="s2", title="b")]))
        self.f.store.activate(m.id, decided_by="owner")

    async def test_no_mission_no_notice(self):
        self.assertIsNone(await self.f.startup_notice(T0))
        self.assertEqual(self.said, [])

    async def test_active_mission_without_a_runner_gets_a_notice_once(self):
        self._active()
        text = await self.f.startup_notice(T0)
        self.assertIn("restarted", text)
        self.assertIn("m-abc123", text)
        self.assertIn("resume", text)
        self.assertIsNone(await self.f.startup_notice(T0 + 60))            # crash-loop guard
        self.assertEqual(len(self.said), 1)
        self.assertIsNotNone(await self.f.startup_notice(T0 + 700))

    async def test_no_notice_when_a_runner_is_alive(self):
        self._active()
        self.f.runner_task = asyncio.create_task(asyncio.sleep(5))
        self.addCleanup(self.f.runner_task.cancel)
        self.assertIsNone(await self.f.startup_notice(T0))


class StuckInfoTests(_Base):
    async def test_minutes_since_last_progress(self):
        self.assertIsNone(watchdog.stuck_info(self.f.store))
        m = self.f.store.propose(ms.MissionContract(id="m-abc123", objective="x"))
        self.f.store.activate(m.id, decided_by="owner")
        mtime = (self.f.store.dir / "m-abc123.json").stat().st_mtime
        mid, age = watchdog.stuck_info(self.f.store, now=mtime + 90 * 60)
        self.assertEqual(mid, "m-abc123")
        self.assertAlmostEqual(age, 90, delta=0.1)


if __name__ == "__main__":
    unittest.main()
