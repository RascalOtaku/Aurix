"""Standing missions through the owner command surface, the scheduler pass, STOP, and the digest."""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, heartbeat, mission as ms, standing as st  # noqa: E402

GRANTED = dict(env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False,
               authorizations={"content-rights": {"granted_by": "rascal"}})
RESEARCH = "Reynolds Gang ongoing treasure hunt"


def ts(y, mo, d, h=0, mi=0):
    # The mission runner checks expiry against the REAL clock, so the simulated dates must stay in the real future.
    # (A fixed 2026 date was a time bomb: this test failed once the wall clock passed it.) +28 years keeps every weekday
    # and leap-year alignment identical (the Gregorian calendar repeats every 28 years in this range).
    return datetime(y + 28, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class ParseTests(unittest.TestCase):
    def test_standing_commands(self):
        p = commands.parse
        self.assertEqual(p("standing: Reynolds Gang research every day at 03:00"),
                         ("standing_new", "Reynolds Gang research every day at 03:00"))
        self.assertEqual(p("/standing - transcribe my inbox nightly"), ("standing_new", "transcribe my inbox nightly"))
        self.assertEqual(p("standing"), ("standing_list", ""))
        self.assertEqual(p("standing list"), ("standing_list", ""))
        self.assertEqual(p("approve standing SM-ABC123"), ("approve_standing", "sm-abc123"))
        self.assertEqual(p("deny standing sm-abc123"), ("deny_standing", "sm-abc123"))
        self.assertEqual(p("pause standing sm-abc123"), ("pause_standing", "sm-abc123"))
        self.assertEqual(p("resume standing sm-abc123"), ("resume_standing", "sm-abc123"))
        self.assertEqual(p("retire standing sm-abc123"), ("retire_standing", "sm-abc123"))

    def test_not_standing_commands_and_no_collisions(self):
        p = commands.parse
        for text in ("standing:", "standing: ", "approve standing", "approve standing m-abc123",
                     "approve standing sm-zzzzzz", "pause standing", "the standing order"):
            self.assertIsNone(p(text), text)
        self.assertEqual(p("resume"), ("resume", ""))                       # unchanged
        self.assertEqual(p("approve mission m-abc123"), ("approve_mission", "m-abc123"))
        self.assertEqual(p("mission: do a thing"), ("new", "do a thing"))


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0", "AURIX_WATCHDOG_MINUTES": "0"})
        self.env.start()
        os.environ.pop("AURIX_DIGEST_AT", None)
        ag.reset_state()
        self.said, self.prompts = [], []

        async def notify(text):
            self.said.append(text)

        async def agent(prompt):
            self.prompts.append(prompt)
            return "MISSION COMPLETE: done" if "All steps are reported done" in prompt else "STEP DONE: ok"

        self.f = commands.Foundation(agent, notify, llm=None, session_id="tg", **GRANTED)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()


class CommandFlowTests(_Base):
    async def test_propose_approve_list_pause_resume_retire(self):
        proposal = await self.f.handle("standing_new", RESEARCH + " every day at 04:00")
        sid = self.f.standing.all()[0].id
        for needle in (sid, "every day at 04:00", f"approve standing {sid}", "Unattended"):
            self.assertIn(needle, proposal)
        self.assertEqual(self.f.store.all(), [])                        # a proposal is not a mission
        self.assertIn("ACTIVE", await self.f.handle("approve_standing", sid))
        self.assertIn(sid, await self.f.handle("standing_list", ""))
        self.assertIn("paused", await self.f.handle("pause_standing", sid))
        self.assertIn("resumed", await self.f.handle("resume_standing", sid))
        self.assertIn("retired", await self.f.handle("retire_standing", sid))
        self.assertNotIn(sid, await self.f.handle("standing_list", ""))

    async def test_ineligible_and_malformed_requests_are_explained_not_created(self):
        for goal in ("Bug Bounties every day at 03:00", "Daily stock trading", "Generate content for profit on socials"):
            reply = await self.f.handle("standing_new", goal)
            self.assertIn("Not as a standing mission", reply)
            self.assertIn("cannot run unattended", reply)
        self.assertIn("Could not set that up", await self.f.handle("standing_new", RESEARCH + " every 10 minutes"))
        self.assertIn("Could not set that up", await self.f.handle("standing_new", "every day at 03:00"))
        self.assertEqual(self.f.standing.all(), [])

    async def test_stop_pauses_every_standing_mission(self):
        await self.f.handle("standing_new", RESEARCH)
        sid = self.f.standing.all()[0].id
        await self.f.handle("approve_standing", sid)
        reply = await self.f.handle("stop")
        self.assertIn("Standing missions paused", reply)
        self.assertEqual(self.f.standing.load(sid).status, st.StandingStatus.PAUSED)
        self.assertNotIn("Standing missions paused", await self.f.handle("stop"))     # nothing left to pause

    async def test_unknown_subcommands_get_help(self):
        self.assertIn("standing:", await self.f.handle("nonsense"))


class SchedulerPassTests(_Base):
    async def approve_at(self, now_ts, goal=RESEARCH):
        await self.f.handle("standing_new", goal)
        sid = self.f.standing.all()[-1].id
        self.f.standing.approve(sid, now_ts=now_ts)
        return sid

    async def test_tick_runs_a_due_mission_end_to_end_with_the_real_runner(self):
        sid = await self.approve_at(ts(2026, 9, 19, 10, 0))
        self.assertEqual(await self.f.tick_standing(now=ts(2026, 9, 19, 20, 0)), [])       # not due yet
        actions = await self.f.tick_standing(now=ts(2026, 9, 20, 3, 0))
        self.assertEqual(len(actions), 1)
        await self.f.runner_task
        child = self.f.store.all()[0]
        self.assertEqual(child.status, ms.MissionStatus.COMPLETED)
        self.assertEqual(child.decided_by, f"standing:{sid}")
        self.assertTrue(any("Standing mission" in s and "started run #1" in s for s in self.said))
        self.assertTrue(audit.verify().ok)
        # the next day reconciles the completed run and starts run #2
        await self.f.tick_standing(now=ts(2026, 9, 21, 3, 0))
        await self.f.runner_task
        after = self.f.standing.load(sid)
        self.assertEqual((after.runs, after.consecutive_failures), (2, 0))

    async def test_tick_with_nothing_scheduled_does_no_work(self):
        self.assertEqual(await self.f.tick_standing(now=ts(2026, 9, 20, 3, 0)), [])
        self.assertEqual(self.f.store.all(), [])

    async def test_owner_stop_then_no_runs_until_resumed(self):
        sid = await self.approve_at(ts(2026, 9, 19, 10, 0))
        await self.f.handle("stop")
        self.assertEqual(await self.f.tick_standing(now=ts(2026, 9, 20, 3, 0)), [])
        await self.f.handle("resume_standing", sid)
        self.assertEqual(len(await self.f.tick_standing(now=ts(2026, 9, 20, 3, 5))), 1)
        self.f.runner_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.f.runner_task


class DigestTests(_Base):
    async def test_off_by_default(self):
        await self.f.tick_standing(now=ts(2026, 9, 20, 8, 0))
        self.assertEqual(self.said, [])

    async def test_sent_once_per_local_day_after_the_configured_time(self):
        with mock.patch.dict(os.environ, {"AURIX_DIGEST_AT": "07:00"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 6, 59))
            self.assertEqual(self.said, [])
            await self.f.tick_standing(now=ts(2026, 9, 20, 7, 0))
            await self.f.tick_standing(now=ts(2026, 9, 20, 9, 0))                   # same day: no second one
            self.assertEqual(sum("morning digest" in s for s in self.said), 1)
            await self.f.tick_standing(now=ts(2026, 9, 21, 7, 1))
            self.assertEqual(sum("morning digest" in s for s in self.said), 2)
        text = next(s for s in self.said if "morning digest" in s)
        self.assertIn("AURIX status", text)
        self.assertIn("chain OK", text)

    async def test_bad_setting_is_ignored(self):
        with mock.patch.dict(os.environ, {"AURIX_DIGEST_AT": "seven"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 8, 0))
        self.assertEqual(self.said, [])


class HeartbeatTests(_Base):
    async def test_status_mentions_standing_missions(self):
        self.assertNotIn("Standing missions", heartbeat.self_report(self.f.store))
        await self.f.handle("standing_new", RESEARCH)
        sid = self.f.standing.all()[0].id
        self.f.standing.approve(sid)
        self.assertIn("1 active", heartbeat.self_report(self.f.store))
        self.f.standing.pause(sid)
        self.assertIn(f"paused: {sid}", heartbeat.self_report(self.f.store))

    async def test_activity_summary_counts_from_the_audit_chain(self):
        for ev in ("mission_completed", "mission_completed", "mission_blocked", "standing_run_started",
                   "requested", "approved", "denied", "mission_allowed", "mission_allowed", "auto_allowed"):
            audit.append(ev)
        audit.append("denied", reason="protected_component")
        text = heartbeat.activity_summary(24)
        for needle in ("2 completed", "1 blocked", "standing runs started: 1", "1 asked", "1 approved",
                       "2 denied/refused", "refused: 1", "without a ping: 2", "auto-allowed: 1"):
            self.assertIn(needle, text)

    async def test_activity_summary_ignores_old_events_and_handles_empty(self):
        self.assertIn("nothing recorded", heartbeat.activity_summary(24))
        audit.append("mission_completed")
        self.assertIn("nothing recorded", heartbeat.activity_summary(24, now_ts=datetime.now(timezone.utc).timestamp()
                                                                     + 3 * 24 * 3600))


if __name__ == "__main__":
    unittest.main()
