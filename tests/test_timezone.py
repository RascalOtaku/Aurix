"""The "7am report fired at 1am" bug: scheduled times were read as UTC. Tests for the fix."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import task_scheduler as ts  # noqa: E402
from src.foundation import standing as st  # noqa: E402


def _has_tzdata():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/Denver")
        return True
    except Exception:
        return False


HAS_TZ = _has_tzdata()


class DefaultTimezoneTests(unittest.TestCase):
    def test_default_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"AURIX_TIMEZONE": " America/Denver "}):
            self.assertEqual(ts._default_timezone(), "America/Denver")
        with mock.patch.dict(os.environ, {"AURIX_TIMEZONE": ""}):
            self.assertIsNone(ts._default_timezone())
        with mock.patch.dict(os.environ):
            os.environ.pop("AURIX_TIMEZONE", None)
            self.assertIsNone(ts._default_timezone())

    def test_task_without_a_crew_member_now_gets_the_owner_zone(self):
        task = SimpleNamespace(crew_member_id=None)
        with mock.patch.dict(os.environ, {"AURIX_TIMEZONE": "America/Denver"}):
            self.assertEqual(ts._resolve_task_timezone(None, task), "America/Denver")
        with mock.patch.dict(os.environ, {"AURIX_TIMEZONE": ""}):
            self.assertIsNone(ts._resolve_task_timezone(None, task))          # legacy behaviour when unset

    def test_a_broken_crew_lookup_falls_back_to_the_owner_zone(self):
        task = SimpleNamespace(crew_member_id="c1")
        with mock.patch.dict(os.environ, {"AURIX_TIMEZONE": "America/Denver"}):
            self.assertEqual(ts._resolve_task_timezone(object(), task), "America/Denver")    # db.query fails


@unittest.skipUnless(HAS_TZ, "no tz database on this machine (the container gets one from the tzdata package)")
class ComputeNextRunTests(unittest.TestCase):
    def test_seven_am_local_is_not_seven_am_utc(self):
        after = datetime(2026, 9, 19, 2, 0)                       # naive UTC: 20:00 Sep 18 in Denver
        legacy = ts.compute_next_run("daily", "07:00", after=after)
        fixed = ts.compute_next_run("daily", "07:00", after=after, tz_name="America/Denver")
        self.assertEqual(legacy, datetime(2026, 9, 19, 7, 0))       # 01:00 Denver: the bug
        self.assertEqual(fixed, datetime(2026, 9, 19, 13, 0))       # 07:00 MDT = 13:00 UTC
        self.assertEqual(fixed - legacy, timedelta(hours=6))

    @unittest.skipUnless(__import__("importlib").util.find_spec("croniter"), "croniter not installed here")
    def test_cron_follows_the_zone_too(self):
        after = datetime(2026, 9, 19, 2, 0)
        self.assertEqual(ts.compute_next_run("cron", None, after=after, cron_expression="0 7 * * *",
                                             tz_name="America/Denver"), datetime(2026, 9, 19, 13, 0))

    def test_winter_offset_is_seven_hours(self):
        after = datetime(2026, 12, 15, 2, 0)
        self.assertEqual(ts.compute_next_run("daily", "07:00", after=after, tz_name="America/Denver"),
                         datetime(2026, 12, 15, 14, 0))                # MST = UTC-7

    def test_standing_missions_use_the_same_setting_and_follow_dst(self):
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        winter = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.dict(os.environ, {"AURIX_TIMEZONE": "America/Denver"}):
            os.environ.pop("AURIX_TZ_OFFSET_MINUTES", None)
            self.assertEqual(st.tz_offset(summer), timedelta(hours=-6))
            self.assertEqual(st.tz_offset(winter), timedelta(hours=-7))


class StandingOffsetTests(unittest.TestCase):
    def test_fixed_override_beats_everything(self):
        with mock.patch.dict(os.environ, {"AURIX_TZ_OFFSET_MINUTES": "0", "AURIX_TIMEZONE": "America/Denver"}):
            self.assertEqual(st.tz_offset(), timedelta(0))

    def test_default_when_nothing_is_configured_is_mountain(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AURIX_TZ_OFFSET_MINUTES", None)
            os.environ.pop("AURIX_TIMEZONE", None)
            self.assertEqual(st.tz_offset(), timedelta(hours=-6))

    def test_bad_values_fall_back_safely(self):
        with mock.patch.dict(os.environ, {"AURIX_TZ_OFFSET_MINUTES": "abc", "AURIX_TIMEZONE": "Not/AZone"}):
            self.assertEqual(st.tz_offset(), timedelta(hours=-6))


if __name__ == "__main__":
    unittest.main()
