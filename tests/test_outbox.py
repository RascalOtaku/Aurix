"""The outbox: quiet hours 22:00-06:00, the hourly cap, emergencies with ONE follow-up - never 120 messages."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import outbox  # noqa: E402


def at(h, m=0, day=8):
    return datetime(2026, 10, day, h, m, tzinfo=timezone.utc).timestamp()


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TIMEZONE": "UTC", "AURIX_QUIET_HOURS": "",
                                         "AURIX_PULSE_QUIET_HOURS": "", "AURIX_NOTIFY_MAX_PER_HOUR": "8"})
        p.start()
        self.addCleanup(p.stop)

    def test_quiet_hours_hold_and_one_summary_at_six(self):
        self.assertEqual(outbox.decide("📝 Draft ready: Ten ways to save on groceries", at(23)), "hold")
        self.assertEqual(outbox.decide("🧰 Found a real listing: Python script for CSV cleanup", at(2)), "hold")
        self.assertEqual(outbox.due(at(5, 59)), [])                                    # still quiet
        (summary,) = outbox.due(at(6, 1))
        self.assertIn("While you slept", summary)
        self.assertIn("Ten ways to save on groceries", summary)
        self.assertIn("CSV cleanup", summary)
        self.assertEqual(outbox.due(at(6, 2)), [])                                     # delivered once

    def test_emergencies_get_through_once_and_follow_up_once(self):
        msg = "🚨 <b>Audit chain is BROKEN</b> - the tamper-evident log was edited."
        self.assertEqual(outbox.decide(msg, at(3)), "send")
        self.assertEqual(outbox.decide(msg, at(3, 5)), "drop")                         # not repeated
        self.assertEqual(outbox.due(at(3, 10)), [])                                    # too early to follow up
        (follow,) = outbox.due(at(3, 21))
        self.assertIn("Still waiting for you", follow)
        self.assertIn("will not repeat it again", follow)
        self.assertEqual(outbox.due(at(3, 45)), [])                                    # and that is all
        self.assertEqual(outbox.due(at(5, 0)), [])

    def test_no_follow_up_once_the_owner_has_seen_it(self):
        outbox.decide("🚨 Disk is 97% full on the AURIX host.", at(14))
        outbox.note_owner_active(at(14, 5))
        self.assertEqual(outbox.due(at(14, 30)), [])

    def test_an_awake_owner_overrides_quiet_hours(self):
        outbox.note_owner_active(at(23, 0))
        self.assertEqual(outbox.decide("🏁 Mission m-abc123 COMPLETE", at(23, 10)), "send")
        self.assertEqual(outbox.decide("📝 later note", at(23, 45)), "hold")           # 35 min of silence: back to quiet

    def test_hourly_cap_folds_the_rest_into_one_message(self):
        sent = [outbox.decide(f"update number {i}", at(12, i)) for i in range(11)]
        self.assertEqual(sent.count("send"), 8)
        self.assertEqual(sent.count("hold"), 3)
        self.assertEqual(outbox.due(at(12, 30)), [])                                   # the hour is still full
        (catch_up,) = outbox.due(at(13, 15))
        self.assertIn("3 more updates", catch_up)

    def test_a_held_report_arrives_in_full(self):
        report = "<b>Daily Report - Thu</b>\n" + "line\n" * 50
        self.assertEqual(outbox.decide(report, at(5), whole=True), "hold")
        out = outbox.due(at(6, 1))
        self.assertEqual(len(out), 2)
        self.assertIn("in full below", out[0])
        self.assertEqual(out[1], report)

    def test_alert_api_counts_as_an_emergency_without_the_mark(self):
        self.assertEqual(outbox.decide("Kuma: Vaultwarden is down", at(1), emergency=True), "send")
        self.assertEqual(outbox.decide("Kuma: Vaultwarden is down", at(1, 2), emergency=True), "drop")

    def test_window_setting(self):
        with mock.patch.dict(os.environ, {"AURIX_QUIET_HOURS": "23:30-07:00"}):
            self.assertEqual(outbox.decide("note", at(23)), "send")
            self.assertEqual(outbox.decide("note 2", at(23, 45)), "hold")
        with mock.patch.dict(os.environ, {"AURIX_QUIET_HOURS": "nonsense"}):
            self.assertEqual(outbox.decide("note 3", at(3)), "send")                   # a typo never silences Aurix

    def test_a_long_held_report_is_kept_whole(self):
        """Patch 11: the sender splits long messages, so a held report must not be cut."""
        report = "<b>Daily Report</b>\n" + "x" * 9000
        outbox.decide(report, at(5), whole=True)
        self.assertEqual(outbox.due(at(6, 1))[1], report)

    def test_no_follow_up_when_the_owner_was_active_just_before(self):
        """Patch 12: an owner active right before the emergency was awake when it arrived."""
        outbox.note_owner_active(at(14, 0))
        outbox.decide("🚨 Disk is 97% full on the AURIX host.", at(14, 5))
        self.assertEqual(outbox.due(at(14, 40)), [])


if __name__ == "__main__":
    unittest.main()
