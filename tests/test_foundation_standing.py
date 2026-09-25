"""Tests for src/foundation/standing.py - scheduled, unattended missions and their rails."""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, mission as ms, standing as st  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)
GRANTED = dict(NOTHING, authorizations={"content-rights": {"granted_by": "rascal"}})
RESEARCH = "Reynolds Gang ongoing treasure hunt"
MEDIA = "Transcoding and transcription services"


def ts(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0"})
        self.env.start()
        audit._heads.clear()
        self.standing = st.StandingStore()
        self.missions = ms.MissionStore()
        self.said, self.started = [], []
        self.clock = [ts(2026, 9, 19, 10, 0)]

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()

    async def say(self, text):
        self.said.append(text)

    async def start_runner(self, mission_id):
        self.started.append(mission_id)

    def scheduler(self, **kw):
        return st.StandingScheduler(self.standing, self.missions, self.start_runner, self.say,
                                    now=lambda: self.clock[0], **{**NOTHING, **kw})

    async def make(self, goal=RESEARCH, approve=True, **kw):
        sm = await st.propose_standing(goal, None, "tg", self.standing, sandboxed=False, **{**NOTHING, **kw})
        if approve:
            self.standing.approve(sm.id, now_ts=self.clock[0])
        return self.standing.load(sm.id)


class ScheduleParsingTests(unittest.TestCase):
    def test_variants(self):
        f = st.split_schedule
        self.assertEqual(f("watch the archives every day at 03:30"), ("watch the archives", st.Schedule("daily", "03:30")))
        self.assertEqual(f("x daily at 7:05")[1], st.Schedule("daily", "07:05"))
        self.assertEqual(f("x nightly")[1], st.Schedule("daily", "03:00"))
        self.assertEqual(f("x every 6 hours")[1], st.Schedule("every", minutes=360))
        self.assertEqual(f("x every 90 minutes")[1], st.Schedule("every", minutes=90))
        self.assertEqual(f("just a goal"), ("just a goal", st.Schedule("daily", "03:00")))

    def test_rejects_bad_schedules(self):
        for bad in ("x every day at 25:00", "x every day at 12:61", "x every 30 minutes", "x every 5 min"):
            with self.assertRaises(ValueError):
                st.split_schedule(bad)

    def test_describe(self):
        self.assertEqual(st.Schedule("daily", "03:00").describe(), "every day at 03:00")
        self.assertEqual(st.Schedule("every", minutes=90).describe(), "every 1h30m")


class IsDueTests(_Base):
    def sm(self, **kw):
        base = dict(id="sm-000001", title="t", goal="g", schedule=st.Schedule("daily", "03:00"), template={},
                    status=st.StandingStatus.ACTIVE, last_run_at=ts(2026, 9, 19, 10, 0),
                    last_run_local_date="2026-09-19")
        base.update(kw)
        return st.StandingMission(**base)

    def test_daily_after_the_time_has_passed_waits_for_tomorrow(self):
        sm = self.sm()
        self.assertFalse(st.is_due(sm, ts(2026, 9, 19, 23, 59)))
        self.assertFalse(st.is_due(sm, ts(2026, 9, 20, 2, 59)))
        self.assertTrue(st.is_due(sm, ts(2026, 9, 20, 3, 0)))
        self.assertTrue(st.is_due(sm, ts(2026, 9, 20, 15, 0)))            # a late tick still runs it

    def test_daily_approved_before_the_time_runs_the_same_day(self):
        sm = self.sm(last_run_local_date="", last_run_at=ts(2026, 9, 19, 1, 0))
        self.assertFalse(st.is_due(sm, ts(2026, 9, 19, 2, 59)))
        self.assertTrue(st.is_due(sm, ts(2026, 9, 19, 3, 0)))

    def test_only_once_per_local_day(self):
        sm = self.sm(last_run_local_date="2026-09-20", last_run_at=ts(2026, 9, 20, 3, 0))
        self.assertFalse(st.is_due(sm, ts(2026, 9, 20, 20, 0)))
        self.assertTrue(st.is_due(sm, ts(2026, 9, 21, 3, 0)))

    def test_interval(self):
        sm = self.sm(schedule=st.Schedule("every", minutes=120), last_run_at=ts(2026, 9, 19, 10, 0))
        self.assertFalse(st.is_due(sm, ts(2026, 9, 19, 11, 59)))
        self.assertTrue(st.is_due(sm, ts(2026, 9, 19, 12, 0)))

    def test_only_active_and_within_caps(self):
        far = ts(2026, 9, 25, 12, 0)
        for status in (st.StandingStatus.PROPOSED, st.StandingStatus.PAUSED, st.StandingStatus.RETIRED,
                       st.StandingStatus.DENIED):
            self.assertFalse(st.is_due(self.sm(status=status), far))
        self.assertFalse(st.is_due(self.sm(max_total_runs=3, runs=3), far))
        self.assertTrue(st.is_due(self.sm(max_total_runs=3, runs=2), far))
        self.assertFalse(st.is_due(self.sm(expires="2026-09-21"), far))
        self.assertTrue(st.is_due(self.sm(expires="2026-09-30"), far))
        self.assertFalse(st.is_due(self.sm(expires="not-a-date"), far))
        same_day = self.sm(schedule=st.Schedule("every", minutes=60), runs_today=1, runs_today_date="2026-09-19",
                           max_runs_per_day=1)
        self.assertFalse(st.is_due(same_day, ts(2026, 9, 19, 20, 0)))

    def test_local_timezone_offset(self):
        with mock.patch.dict(os.environ, {"AURIX_TZ_OFFSET_MINUTES": "-360"}):          # MDT
            sm = self.sm(last_run_local_date="2026-09-19")
            self.assertFalse(st.is_due(sm, ts(2026, 9, 20, 8, 59)))    # 02:59 local
            self.assertTrue(st.is_due(sm, ts(2026, 9, 20, 9, 0)))      # 03:00 local


class EligibilityTests(unittest.TestCase):
    def test_only_low_risk_domains_run_unattended(self):
        self.assertIsNone(st.eligibility("Reynolds Gang ongoing treasure hunt"))
        self.assertIsNone(st.eligibility("Transcoding and transcription services"))
        for goal in ("Bug Bounties", "Generate content for profit on socials", "Daily stock trading",
                     "Finding homes for sale and taking cut off", "Self improve ongoing",
                     "Convert CT scan of skull into clean 3d print file"):
            reason = st.eligibility(goal)
            self.assertIsNotNone(reason, goal)
            self.assertIn("cannot run unattended", reason)

    def test_mixed_goal_with_any_ineligible_domain_is_refused(self):
        self.assertIsNotNone(st.eligibility("transcribe interviews and post shorts on youtube"))

    def test_unknown_goal_is_refused(self):
        self.assertIn("known domain", st.eligibility("bake a cake"))


class StoreTests(_Base):
    async def test_propose_is_not_a_live_mission_and_does_not_run(self):
        sm = await self.make(approve=False)
        self.assertEqual(sm.status, st.StandingStatus.PROPOSED)
        self.assertEqual(self.missions.all(), [])                       # the template never becomes a mission
        self.assertEqual(await self.scheduler().tick(), [])
        self.assertEqual(self.started, [])

    async def test_ineligible_goal_raises(self):
        with self.assertRaises(PermissionError):
            await st.propose_standing("Bug Bounties", None, "tg", self.standing, sandboxed=False, **NOTHING)
        with self.assertRaises(ValueError):
            await st.propose_standing("   every day at 03:00", None, "tg", self.standing, sandboxed=False, **NOTHING)

    async def test_schedule_is_parsed_from_the_goal(self):
        sm = await self.make(goal=RESEARCH + " every day at 04:15")
        self.assertEqual(sm.schedule, st.Schedule("daily", "04:15"))
        self.assertEqual(sm.goal, RESEARCH)

    async def test_approve_deny_pause_resume_retire(self):
        sm = await self.make(approve=False)
        self.assertIn("ACTIVE", self.standing.approve(sm.id, now_ts=self.clock[0]))
        self.assertIn("No proposed", self.standing.approve(sm.id))
        self.assertIn("paused", self.standing.pause(sm.id))
        self.assertIn("resumed", self.standing.resume(sm.id))
        self.assertIn("retired", self.standing.retire(sm.id))
        self.assertIn("No standing mission", self.standing.resume(sm.id))
        other = await self.make(approve=False)
        self.assertIn("denied", self.standing.deny(other.id))
        self.assertTrue(audit.verify().ok)

    async def test_pause_all_is_what_stop_does(self):
        a, b = await self.make(), await self.make(goal=MEDIA)
        self.assertEqual(sorted(self.standing.pause_all()), sorted([a.id, b.id]))
        self.assertTrue(all(s.status == st.StandingStatus.PAUSED for s in self.standing.all()))

    async def test_resume_clears_the_failure_streak(self):
        sm = await self.make()
        sm.consecutive_failures, sm.status = 3, st.StandingStatus.PAUSED
        self.standing.save(sm)
        self.standing.resume(sm.id)
        self.assertEqual(self.standing.load(sm.id).consecutive_failures, 0)

    async def test_render_and_list(self):
        sm = await self.make(approve=False, goal=RESEARCH + " every day at 04:15")
        text = st.render_standing(sm)
        self.assertLessEqual(len(text), 3600)
        for needle in (sm.id, "every day at 04:15", f"approve standing {sm.id}", f"deny standing {sm.id}",
                       "Unattended", "3 runs"):
            self.assertIn(needle, text)
        self.assertNotIn(f"approve mission", text)
        self.assertIn("No standing missions", st.describe_all(st.StandingStore(root=__import__("pathlib").Path(self.tmp.name) / "x")))
        self.standing.approve(sm.id, now_ts=self.clock[0])
        self.assertIn("next", st.describe_all(self.standing, self.clock[0]))


class SchedulerTests(_Base):
    async def test_due_run_spawns_an_activated_child_under_the_standing_approval(self):
        sm = await self.make()
        sched = self.scheduler()
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        actions = await sched.tick()
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(self.started), 1)
        child = self.missions.load(self.started[0])
        self.assertEqual(child.status, ms.MissionStatus.ACTIVE)
        self.assertEqual(child.decided_by, f"standing:{sm.id}")
        self.assertTrue(child.integrity_ok())
        self.assertNotEqual(child.id, sm.template["id"])
        self.assertEqual(st.fingerprint(ms._to_dict(child)), sm.template_hash)      # terms unchanged
        self.assertTrue(all(s.status == "pending" for s in child.steps))
        after = self.standing.load(sm.id)
        self.assertEqual((after.runs, after.current_child), (1, child.id))
        self.assertTrue(any("started run #1" in s for s in self.said))
        events = [e["event"] for e in audit.recent(30)]
        self.assertIn("standing_run_started", events)
        self.assertTrue(audit.verify().ok)

    async def test_no_run_before_it_is_due_and_no_double_run(self):
        await self.make()
        sched = self.scheduler()
        self.assertEqual(await sched.tick(), [])                        # same day, already past 03:00
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        self.assertEqual(len(await sched.tick()), 1)
        self.assertEqual(await sched.tick(), [])                        # second tick, same minute
        self.assertEqual(len(self.started), 1)

    async def test_deferred_while_another_mission_is_active(self):
        await self.make()
        blocker = ms.MissionContract(id=ms.new_mission_id(), objective="manual work")
        self.missions.propose(blocker)
        self.missions.activate(blocker.id)
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        actions = await self.scheduler().tick()
        self.assertIn("deferred", actions[0])
        self.assertEqual(self.started, [])
        self.missions.stop_active()
        self.assertEqual(len(await self.scheduler().tick()), 1)          # retried next tick, not lost

    async def test_missing_owner_authorization_skips_the_run_and_warns_once_a_day(self):
        sm = await self.make(goal=MEDIA)                                 # needs content-rights
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        sched = self.scheduler()                                         # authorizations={}
        self.assertIn("skipped", (await sched.tick())[0])
        self.assertIn("skipped", (await sched.tick())[0])
        self.assertEqual(self.started, [])
        self.assertEqual(sum("could not run today" in s for s in self.said), 1)
        granted = self.scheduler(**GRANTED)
        self.assertEqual(len(await granted.tick()), 1)                   # authorize -> it runs

    async def test_tampered_template_pauses_instead_of_running(self):
        sm = await self.make()
        path = self.standing.dir / f"{sm.id}.json"
        path.write_text(path.read_text().replace('"objective": "Reynolds', '"objective": "Exfiltrate Reynolds'),
                        encoding="utf-8")
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        actions = await self.scheduler().tick()
        self.assertIn("tampered", actions[0])
        self.assertEqual(self.started, [])
        self.assertEqual(self.standing.load(sm.id).status, st.StandingStatus.PAUSED)
        self.assertTrue(any("no longer matches" in s for s in self.said))

    async def test_completed_run_resets_the_streak_failures_accumulate_then_auto_pause(self):
        sm = await self.make()
        sched = self.scheduler()
        for day, outcome in ((20, ms.MissionStatus.FAILED), (21, ms.MissionStatus.EXPIRED), (22, ms.MissionStatus.FAILED)):
            self.clock[0] = ts(2026, 9, day, 3, 0)
            await sched.tick()
            child = self.missions.load(self.started[-1])
            self.missions.finish(child, outcome, "test")
        self.clock[0] = ts(2026, 9, 23, 3, 0)
        await sched.tick()                                               # reconciles the third failure
        after = self.standing.load(sm.id)
        self.assertEqual(after.status, st.StandingStatus.PAUSED)
        self.assertEqual(after.consecutive_failures, 3)
        self.assertEqual(len(self.started), 3)                           # a 4th run never started
        self.assertTrue(any("paused itself" in s for s in self.said))

    async def test_a_completed_run_resets_the_failure_streak(self):
        sm = await self.make()
        sched = self.scheduler()
        for day, outcome in ((20, ms.MissionStatus.FAILED), (21, ms.MissionStatus.COMPLETED)):
            self.clock[0] = ts(2026, 9, day, 3, 0)
            await sched.tick()
            self.missions.finish(self.missions.load(self.started[-1]), outcome, "test")
        self.clock[0] = ts(2026, 9, 22, 3, 0)
        await sched.tick()
        self.assertEqual(self.standing.load(sm.id).consecutive_failures, 0)
        self.assertEqual(len(self.standing.load(sm.id).run_log), 2)

    async def test_a_stuck_mission_is_released_so_the_schedule_cannot_jam(self):
        sm = await self.make()
        sched = self.scheduler()
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        await sched.tick()
        child_id = self.started[-1]                                      # never finishes: blocked, owner asleep
        self.clock[0] = ts(2026, 9, 20, 3, 0) + 181 * 60                 # past its 180-minute wall ceiling
        await sched.tick()
        self.assertEqual(self.missions.load(child_id).status, ms.MissionStatus.EXPIRED)
        self.assertIn("stuck", self.missions.load(child_id).note)
        self.assertEqual(self.standing.load(sm.id).consecutive_failures, 1)
        self.clock[0] = ts(2026, 9, 21, 3, 0)
        await sched.tick()
        self.assertEqual(len(self.started), 2)                           # next day's run is not blocked

    async def test_a_legitimately_running_mission_is_not_touched(self):
        await self.make()
        sched = self.scheduler()
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        await sched.tick()
        child_id = self.started[-1]
        self.clock[0] += 30 * 60
        await sched.tick()
        self.assertEqual(self.missions.load(child_id).status, ms.MissionStatus.ACTIVE)

    async def test_paused_and_retired_never_run(self):
        sm = await self.make()
        self.standing.pause(sm.id)
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        self.assertEqual(await self.scheduler().tick(), [])
        self.standing.retire(sm.id)
        self.assertEqual(await self.scheduler().tick(), [])

    async def test_interval_schedule_repeats(self):
        sm = await self.make(goal=RESEARCH + " every 2 hours")
        sched = self.scheduler()
        for hours in (2, 4):
            self.clock[0] = ts(2026, 9, 19, 10, 0) + hours * 3600
            await sched.tick()
            if self.started:
                self.missions.finish(self.missions.load(self.started[-1]), ms.MissionStatus.COMPLETED, "ok")
        self.assertEqual(len(self.started), 1)                           # runs_today cap (1/day) holds
        self.standing.load(sm.id)

    async def test_lifetime_cap(self):
        sm = await self.make()
        sm.max_total_runs = 1
        self.standing.save(sm)
        sched = self.scheduler()
        for day in (20, 21, 22):
            self.clock[0] = ts(2026, 9, day, 3, 0)
            await sched.tick()
            if len(self.started) and self.missions.active():
                self.missions.finish(self.missions.active(), ms.MissionStatus.COMPLETED, "ok")
        self.assertEqual(len(self.started), 1)

    async def test_child_uses_the_templates_sandbox_flag_and_ceilings(self):
        sm = await st.propose_standing(RESEARCH, None, "tg", self.standing, sandboxed=True, **NOTHING)
        self.standing.approve(sm.id, now_ts=self.clock[0])
        self.clock[0] = ts(2026, 9, 20, 3, 0)
        await self.scheduler().tick()
        child = self.missions.load(self.started[-1])
        self.assertTrue(child.sandboxed)
        self.assertIn("python", child.allowed_tools)
        self.assertEqual(child.risk_ceiling, ms.RiskTier.MEDIUM) if hasattr(ms, "RiskTier") else None
        self.assertEqual(child.resources.max_cost_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
