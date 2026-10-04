"""Standing missions through the owner command surface, the scheduler pass, STOP, and the digest."""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, forge, freelance, heartbeat, mission as ms, standing as st  # noqa: E402
from src.foundation.land import hub as land  # noqa: E402

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
        # AURIX_GAMING_DIR included: without it, gaming.tick()/gaming.pending() read whatever the REAL machine's
        # gaming data mount has (a container with real data gets a real, genuinely-new-looking crash proposal
        # mid-test) instead of this test's own empty tempdir - a pre-existing gap in this file specifically
        # (test_foundation_evolution.py's _Base already isolates this correctly) that intermittently failed
        # DigestTests/PulseTests depending on what the surrounding machine's real gaming data happened to hold.
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TZ_OFFSET_MINUTES": "0", "AURIX_WATCHDOG_MINUTES": "0",
                                                 "AURIX_GAMING_DIR": str(Path(self.tmp.name) / "gaming")})
        self.env.start()
        os.environ.pop("AURIX_DIGEST_AT", None)
        # A real deployment sets AURIX_PULSE_EVERY_MINUTES/AURIX_PULSE_QUIET_HOURS in its own .env - mock.patch.dict
        # without clear=True leaves that ambient value in place, so running these tests INSIDE that container (not
        # a clean dev-box shell) leaked a real pulse into tests that never touch pulse settings. Same off-by-default
        # treatment as AURIX_DIGEST_AT above, for the same reason.
        os.environ.pop("AURIX_PULSE_EVERY_MINUTES", None)
        os.environ.pop("AURIX_PULSE_QUIET_HOURS", None)
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


class PulseTests(_Base):
    async def test_off_by_default(self):
        await self.f.tick_standing(now=ts(2026, 9, 20, 12, 0))
        self.assertEqual(self.said, [])

    async def test_fires_once_per_interval_during_waking_hours(self):
        with mock.patch.dict(os.environ, {"AURIX_PULSE_EVERY_MINUTES": "120"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 12, 0))
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 1)
            await self.f.tick_standing(now=ts(2026, 9, 20, 13, 0))                 # only 60 min later: no second one
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 1)
            await self.f.tick_standing(now=ts(2026, 9, 20, 14, 1))                 # 121 min after the first: fires again
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 2)

    async def test_never_fires_during_the_default_quiet_hours(self):
        with mock.patch.dict(os.environ, {"AURIX_PULSE_EVERY_MINUTES": "1"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 23, 30))                # 23:30, inside 23:00-07:00
            self.assertEqual(self.said, [])
            await self.f.tick_standing(now=ts(2026, 9, 21, 3, 0))                  # 03:00, still inside (wraps midnight)
            self.assertEqual(self.said, [])
            await self.f.tick_standing(now=ts(2026, 9, 21, 7, 1))                  # just past the window: fires
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 1)

    async def test_a_custom_quiet_window_is_respected(self):
        with mock.patch.dict(os.environ, {"AURIX_PULSE_EVERY_MINUTES": "1", "AURIX_PULSE_QUIET_HOURS": "01:00-05:00"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 23, 30))                # outside the custom window now
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 1)
            await self.f.tick_standing(now=ts(2026, 9, 21, 2, 0))                  # inside the custom window
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 1)

    async def test_bad_interval_setting_is_ignored(self):
        with mock.patch.dict(os.environ, {"AURIX_PULSE_EVERY_MINUTES": "soon"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 12, 0))
        self.assertEqual(self.said, [])

    async def test_unparseable_quiet_window_never_goes_permanently_quiet(self):
        with mock.patch.dict(os.environ, {"AURIX_PULSE_EVERY_MINUTES": "1", "AURIX_PULSE_QUIET_HOURS": "garbage"}):
            await self.f.tick_standing(now=ts(2026, 9, 20, 2, 0))                  # would be "quiet" under the default window
            self.assertEqual(sum("AURIX pulse" in s for s in self.said), 1)


class QuietHoursTests(unittest.TestCase):
    def test_default_window_wraps_past_midnight(self):
        from datetime import datetime
        quiet = lambda h, m=0: commands._in_quiet_hours(datetime(2026, 9, 20, h, m))  # noqa: E731
        self.assertTrue(quiet(23, 30))
        self.assertTrue(quiet(0, 0))
        self.assertTrue(quiet(6, 59))
        self.assertFalse(quiet(7, 0))
        self.assertFalse(quiet(12, 0))
        self.assertFalse(quiet(22, 59))

    def test_a_same_day_window_does_not_wrap(self):
        from datetime import datetime
        with mock.patch.dict(os.environ, {"AURIX_PULSE_QUIET_HOURS": "13:00-14:00"}):
            self.assertFalse(commands._in_quiet_hours(datetime(2026, 9, 20, 12, 59)))
            self.assertTrue(commands._in_quiet_hours(datetime(2026, 9, 20, 13, 30)))
            self.assertFalse(commands._in_quiet_hours(datetime(2026, 9, 20, 14, 0)))


class PulseDigestContentTests(unittest.TestCase):
    def test_includes_self_improve_skills_landpilot_sections(self):
        text = heartbeat.pulse_digest()
        self.assertIn("AURIX pulse", text)
        self.assertIn("forge_guidance", text)          # the registered self-improve domain's own status line
        self.assertIn("Skill requests", text)
        self.assertIn("LandPilot", text)

    def test_a_broken_section_reports_unavailable_rather_than_crashing_the_whole_pulse(self):
        from src.foundation import forge
        with mock.patch.object(forge, "pending", side_effect=RuntimeError("boom")):
            text = heartbeat.pulse_digest()
        self.assertIn("Skill requests: unavailable right now", text)
        self.assertIn("AURIX pulse", text)                             # the rest of the pulse still renders


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


class AutonomyNotificationTests(_Base):
    """2026-10-01: a real autonomous proposal (forge noticing a gap, LandPilot proposing research) reached
    Telegram as plain text with no tap-to-approve buttons - owner's own words, "no clear approval screen".
    The bug: both call sites used self.notify(result) instead of self.notify_ui(result, kind), and the
    fake notify() in this file's own setUp only accepts one positional arg, so a naive test asserting the
    message TEXT arrived would pass either way (notify_ui's TypeError fallback masks the difference) -
    these assert notify_ui specifically gets called, not just that the owner-visible words looked right."""

    async def test_forge_autonomy_uses_notify_ui_not_plain_notify(self):
        with mock.patch.object(forge, "check_autonomous_trigger", new=mock.AsyncMock(return_value="approve skill foo")), \
             mock.patch.object(self.f, "notify_ui", new=mock.AsyncMock()) as nui, \
             mock.patch.object(self.f, "notify", new=mock.AsyncMock()) as plain:
            await self.f._maybe_forge_autonomy(ts(2026, 9, 20, 8, 0))
        nui.assert_awaited_once_with("approve skill foo", "skills")
        plain.assert_not_awaited()

    async def test_land_research_uses_notify_ui_not_plain_notify(self):
        with mock.patch.object(land, "check_research_trigger", new=mock.AsyncMock(return_value="approve mission m-abcdef")), \
             mock.patch.object(self.f, "notify_ui", new=mock.AsyncMock()) as nui, \
             mock.patch.object(self.f, "notify", new=mock.AsyncMock()) as plain:
            await self.f._maybe_land_research(ts(2026, 9, 20, 8, 0))
        nui.assert_awaited_once_with("approve mission m-abcdef", "land")
        plain.assert_not_awaited()

    async def test_freelance_search_uses_notify_ui_not_plain_notify(self):
        with mock.patch.object(freelance, "find_lead", return_value="🧰 found one"), \
             mock.patch.object(self.f, "notify_ui", new=mock.AsyncMock()) as nui, \
             mock.patch.object(self.f, "notify", new=mock.AsyncMock()) as plain:
            await self.f._maybe_freelance_search(ts(2026, 9, 20, 8, 0))
        nui.assert_awaited_once_with("🧰 found one", "freelance")
        plain.assert_not_awaited()

    async def test_freelance_search_stays_quiet_when_nothing_new(self):
        with mock.patch.object(freelance, "find_lead", return_value="Checked a real remote-jobs feed - nothing new and small enough to draft right now."), \
             mock.patch.object(self.f, "notify_ui", new=mock.AsyncMock()) as nui:
            await self.f._maybe_freelance_search(ts(2026, 9, 20, 8, 0))
        nui.assert_not_awaited()

    async def test_freelance_search_stays_quiet_when_the_feed_is_down(self):
        with mock.patch.object(freelance, "find_lead", return_value="Could not check for real leads right now: could not reach it: URLError."), \
             mock.patch.object(self.f, "notify_ui", new=mock.AsyncMock()) as nui:
            await self.f._maybe_freelance_search(ts(2026, 9, 20, 8, 0))
        nui.assert_not_awaited()

    async def test_freelance_search_runs_at_most_once_a_day(self):
        with mock.patch.object(freelance, "find_lead", return_value="🧰 found one") as fl, \
             mock.patch.object(self.f, "notify_ui", new=mock.AsyncMock()):
            await self.f._maybe_freelance_search(ts(2026, 9, 20, 8, 0))
            await self.f._maybe_freelance_search(ts(2026, 9, 20, 14, 0))
        fl.assert_called_once()

    async def test_a_real_forge_autonomy_message_actually_gets_a_tappable_button(self):
        """End to end, through the real buttons.for_reply - not mocked - so a regression here fails loudly."""
        with mock.patch.object(forge, "check_autonomous_trigger", new=mock.AsyncMock(return_value="...approve skill foo_bar...")):
            with mock.patch.object(self.f, "notify", new=mock.AsyncMock()) as plain:
                await self.f._maybe_forge_autonomy(ts(2026, 9, 20, 8, 0))
        self.assertEqual(plain.await_args.args[0], "...approve skill foo_bar...")
        kb = plain.await_args.args[1]
        self.assertIn({"text": "✅ Approve skill", "callback_data": "do:approve skill foo_bar"}, kb["inline_keyboard"][0])

    async def test_a_real_land_research_message_actually_gets_a_tappable_button(self):
        with mock.patch.object(land, "check_research_trigger", new=mock.AsyncMock(return_value="...approve mission m-abcdef...")):
            with mock.patch.object(self.f, "notify", new=mock.AsyncMock()) as plain:
                await self.f._maybe_land_research(ts(2026, 9, 20, 8, 0))
        kb = plain.await_args.args[1]
        self.assertIn({"text": "✅ Approve", "callback_data": "do:approve mission m-abcdef"}, kb["inline_keyboard"][0])


if __name__ == "__main__":
    unittest.main()
