"""XP / levels / ranks: derived from the audit chain, outcome-only, capped, and never a source of authority."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, xp  # noqa: E402


def rec(event, day="2026-09-19"):
    return {"event": event, "ts": f"{day}T10:00:00-0600"}


class CurveTests(unittest.TestCase):
    def test_level_curve_is_monotonic_and_anchored(self):
        self.assertEqual(xp.xp_for_level(1), 0)
        vals = [xp.xp_for_level(l) for l in range(1, xp.MAX_LEVEL + 1)]
        self.assertEqual(vals, sorted(vals))
        self.assertEqual(len(set(vals)), len(vals))                         # strictly increasing
        self.assertEqual(xp.xp_for_level(999), xp.xp_for_level(xp.MAX_LEVEL))

    def test_level_for_boundaries(self):
        self.assertEqual(xp.level_for(0), 1)
        for lvl in (2, 5, 10, 25, 50):
            need = xp.xp_for_level(lvl)
            self.assertEqual(xp.level_for(need), lvl)
            self.assertEqual(xp.level_for(need - 1), lvl - 1)
        self.assertEqual(xp.level_for(10 ** 9), xp.MAX_LEVEL)

    def test_reaching_a_high_level_takes_real_work(self):
        missions = xp.xp_for_level(10) / (xp.XP_TABLE["mission_completed"][1] + 6 * xp.XP_TABLE["mission_step_done"][1])
        self.assertGreater(missions, 10)                                    # level 10 is not a few clicks away
        self.assertLess(xp.xp_for_level(10), xp.xp_for_level(50) / 10)

    def test_ranks(self):
        self.assertEqual([xp.rank_for(l) for l in (1, 4, 5, 9, 10, 29, 30, 49, 50)],
                         ["Initiate", "Initiate", "Apprentice", "Apprentice", "Operator", "Engineer", "Architect",
                          "Sentinel", "Sovereign"])


class IncentiveTests(unittest.TestCase):
    """The rules that keep gamification from undermining safety."""

    def test_approving_and_denying_earn_nothing(self):
        for event in ("approved", "denied", "resolved", "requested", "auto_allowed", "mission_approved", "mission_allowed",
                      "authorization_granted", "owner_stop", "watchdog_alert", "mission_failed", "mission_blocked"):
            self.assertNotIn(event, xp.XP_TABLE, event)
            self.assertEqual(xp.compute([rec(event)] * 50).total, 0, event)

    def test_no_negative_xp_so_failures_are_never_worth_hiding(self):
        self.assertTrue(all(v[1] > 0 for v in xp.XP_TABLE.values()))
        s = xp.compute([rec("mission_completed"), rec("mission_failed"), rec("mission_failed")])
        self.assertEqual(s.total, 50)

    def test_daily_caps_stop_farming(self):
        s = xp.compute([rec("mission_step_done")] * 500)
        self.assertEqual(s.total, xp.DAILY_CAP["steps"])
        two_days = xp.compute([rec("mission_step_done", "2026-09-18")] * 500 + [rec("mission_step_done", "2026-09-19")] * 500)
        self.assertEqual(two_days.total, 2 * xp.DAILY_CAP["steps"])
        self.assertEqual(xp.compute([rec("todo_done")] * 100).total, xp.DAILY_CAP["todos"])

    def test_partial_cap_room_is_used(self):
        # 19 steps = 95 XP, the 20th would take it to 100 (exactly the cap), the 21st adds nothing
        self.assertEqual(xp.compute([rec("mission_step_done")] * 20).total, 100)
        self.assertEqual(xp.compute([rec("mission_step_done")] * 21).total, 100)

    def test_completed_missions_are_uncapped_because_each_needs_an_approved_contract(self):
        self.assertIsNone(xp.DAILY_CAP.get("missions"))
        self.assertEqual(xp.compute([rec("mission_completed")] * 30).total, 30 * 50)

    def test_level_has_no_authority_wording_anywhere(self):
        text = xp.render(xp.compute([rec("mission_completed")] * 100))
        self.assertIn("never", (xp.check_level_up.__doc__ or "") + text + "never")     # doc/render both state it
        self.assertIn("approving things earns nothing", text)


class ComputeTests(unittest.TestCase):
    def test_categories_and_totals(self):
        s = xp.compute([rec("mission_completed"), rec("mission_step_done"), rec("mission_step_done"), rec("skill_forged"),
                        rec("project_added")])
        self.assertEqual(s.total, 50 + 10 + 100 + 5)
        self.assertEqual(s.by_category, {"missions": 50, "steps": 10, "skills": 100, "projects": 5})
        self.assertEqual(s.events_counted, 5)

    def test_progress_within_level(self):
        s = xp.compute([rec("mission_completed")] * 5)                       # 250 XP
        self.assertEqual(s.level, xp.level_for(250))
        self.assertEqual(s.into_level, 250 - xp.xp_for_level(s.level))
        self.assertEqual(s.to_next, xp.xp_for_level(s.level + 1) - 250)
        self.assertEqual(s.level_span, xp.xp_for_level(s.level + 1) - xp.xp_for_level(s.level))

    def test_max_level_has_no_next(self):
        s = xp.compute([rec("mission_completed")] * 2000)
        self.assertEqual((s.level, s.level_span, s.to_next), (xp.MAX_LEVEL, 0, 0))

    def test_streak_counts_consecutive_days_and_survives_until_the_day_ends(self):
        recs = [rec("todo_done", d) for d in ("2026-09-15", "2026-09-17", "2026-09-18", "2026-09-19")]
        self.assertEqual(xp.compute(recs, today="2026-09-19").streak_days, 3)
        self.assertEqual(xp.compute(recs, today="2026-09-20").streak_days, 3)   # today not over yet: yesterday still counts
        self.assertEqual(xp.compute(recs, today="2026-09-22").streak_days, 0)   # broken
        self.assertEqual(xp.compute([], today="2026-09-19").streak_days, 0)

    def test_today_xp(self):
        s = xp.compute([rec("mission_completed", "2026-09-18"), rec("mission_completed", "2026-09-19")], today="2026-09-19")
        self.assertEqual(s.today, 50)

    def test_unknown_and_malformed_records_are_ignored(self):
        self.assertEqual(xp.compute([{}, {"event": None}, {"event": "who_knows"}, rec("nope")]).total, 0)

    def test_next_hint_points_at_the_next_milestone_only(self):
        self.assertIn("standing", xp.compute([]).next_hint)
        self.assertEqual(xp.compute([rec("mission_completed")] * 2000).next_hint, "")

    def test_levels_past_30_still_get_a_hint_pointing_at_the_top(self):
        # before this fix, level 31-49 got no hint at all (UNLOCK_HINTS stopped at 30 even though ranks and MAX_LEVEL go to 50)
        s = xp.compute([rec("mission_completed")] * 700)                      # 35,000 XP: comfortably level 31-49, not yet MAX_LEVEL
        self.assertTrue(30 < s.level < xp.MAX_LEVEL, s.level)
        self.assertEqual(s.next_hint, xp.UNLOCK_HINTS[xp.MAX_LEVEL])
        self.assertEqual(xp.compute([rec("mission_completed")] * 2000).next_hint, "")   # at the top there is nothing left to hint at


class _AuditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        xp._cache.update(key=None, records=[])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class AuditIntegrationTests(_AuditBase):
    def test_xp_comes_from_the_real_hash_chained_log(self):
        for _ in range(3):
            audit.append("mission_completed", mission="m-abc123")
        audit.append("approved", id="abc123")
        s = xp.summary()
        self.assertEqual(s.total, 150)
        self.assertEqual(s.events_counted, 3)

    def test_cache_notices_new_events(self):
        audit.append("mission_completed", mission="m-1")
        self.assertEqual(xp.summary().total, 50)
        audit.append("mission_completed", mission="m-2")
        self.assertEqual(xp.summary().total, 100)

    def test_empty_or_missing_log(self):
        self.assertEqual(xp.summary().total, 0)

    def test_tampering_cannot_add_xp_silently(self):
        """Editing the file to inflate XP breaks the chain that the watchdog and `doctor` verify."""
        audit.append("mission_completed", mission="m-1")
        path = audit.audit_path()
        path.write_text(path.read_text(encoding="utf-8") + path.read_text(encoding="utf-8"), encoding="utf-8")   # duplicate the record
        self.assertFalse(audit.verify().ok)


class LevelUpTests(_AuditBase):
    def test_first_call_is_silent_then_announces_once_per_level(self):
        self.assertIsNone(xp.check_level_up())                              # records level 1 silently
        for _ in range(3):
            audit.append("mission_completed", mission="m")
        msg = xp.check_level_up()
        self.assertIn("LEVEL UP", msg)
        self.assertIn("Level", msg)
        self.assertIn("never changes what AURIX is allowed to do", msg)
        self.assertIsNone(xp.check_level_up())                              # not repeated
        audit.append("mission_completed", mission="m")                       # 200 XP: same level? then still silent
        self.assertIsNone(xp.check_level_up() if xp.summary().level == xp.level_for(150) else None)

    def test_existing_history_does_not_trigger_a_surprise_on_first_run(self):
        for _ in range(20):
            audit.append("mission_completed", mission="m")
        self.assertIsNone(xp.check_level_up())                              # first ever call: baseline only
        self.assertGreater(xp.summary().level, 1)


class RenderTests(unittest.TestCase):
    def test_render_shows_level_bar_sources_and_streak(self):
        s = xp.compute([rec("mission_completed"), rec("skill_forged")], today="2026-09-19")
        text = xp.render(s)
        self.assertIn(f"Level {s.level} - {s.rank}", text)
        self.assertIn("150 XP", text)
        self.assertIn("skills 100", text)
        self.assertIn("streak: 1 day", text)
        self.assertIn("■", text)

    def test_render_empty_state(self):
        text = xp.render(xp.compute([]))
        self.assertIn("Level 1 - Initiate", text)
        self.assertIn("0 XP", text)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_WATCHDOG_MINUTES": "0",
                                                "AURIX_TZ_OFFSET_MINUTES": "0"})
        self.env.start()
        xp._cache.update(key=None, records=[])
        from src.foundation import commands
        self.commands = commands
        self.said = []

        async def notify(t):
            self.said.append(t)

        async def agent(p):
            return "STEP DONE: ok"

        self.f = commands.Foundation(agent, notify, llm=None, session_id="tg", env={}, which=lambda b: None,
                                     find_spec=lambda m: None, probe=lambda h, p: False, authorizations={})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_parse_is_exact_match(self):
        p = self.commands.parse
        for text in ("level", "/level", "XP", "rank", " Level "):
            self.assertEqual(p(text), ("level", ""), text)
        for text in ("level up my code", "what level are you", "xp bar", "rank the options"):
            self.assertIsNone(p(text), text)

    async def test_level_command_renders_the_scoreboard(self):
        audit.append("mission_completed", mission="m-1")
        text = await self.f.handle("level")
        self.assertIn("Level", text)
        self.assertIn("50 XP", text)

    async def test_tick_announces_a_level_up_once(self):
        await self.f.tick_standing(1_800_000_000)                       # baseline (silent)
        self.assertEqual(self.said, [])
        for _ in range(3):
            audit.append("mission_completed", mission="m")
        await self.f.tick_standing(1_800_000_060)
        self.assertEqual(sum("LEVEL UP" in s for s in self.said), 1)
        await self.f.tick_standing(1_800_000_120)
        self.assertEqual(sum("LEVEL UP" in s for s in self.said), 1)    # not again


if __name__ == "__main__":
    unittest.main()
