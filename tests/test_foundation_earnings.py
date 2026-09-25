"""The earnings ledger: AURIX never touches money, but "approve, then see it come in" needs somewhere real to look. Estimated entries
(from finished work) and confirmed entries (the owner reporting a real payment) are tracked and shown separately, never conflated."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, buttons, commands, earnings  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()


class LedgerTests(_Base):
    def test_confirmed_and_estimated_are_tracked_and_totalled_separately(self):
        earnings.auto_estimate(12.5, "transcription", "job t-abc")
        earnings.record(20, "transcription", "paid by client")
        self.assertEqual(earnings.total(), 32.5)
        self.assertEqual(earnings.total(confirmed_only=True), 20)

    def test_record_rejects_non_positive_amounts(self):
        self.assertIn("positive", earnings.record(0, "x"))
        self.assertIn("positive", earnings.record(-5, "x"))
        self.assertEqual(earnings.entries(), [])

    def test_by_source_breaks_down_correctly(self):
        earnings.record(10, "transcription")
        earnings.record(5, "transcription")
        earnings.record(7, "freelance_scripts")
        self.assertEqual(earnings.by_source(confirmed_only=True), {"transcription": 15, "freelance_scripts": 7})

    def test_render_labels_estimated_entries_distinctly_and_never_crashes_empty(self):
        self.assertIn("nothing logged", earnings.render().lower())
        earnings.auto_estimate(3, "transcription")
        earnings.record(50, "freelance_scripts", "invoice #1")
        text = earnings.render()
        self.assertIn("~ $3.00", text)
        self.assertIn("$50.00", text)
        self.assertIn("confirmed", text.lower())
        self.assertIn("estimated", text.lower())

    def test_every_write_is_audited(self):
        earnings.auto_estimate(1, "x")
        earnings.record(1, "x")
        events = [r["event"] for r in audit.recent(10)]
        self.assertIn("earnings_estimated", events)
        self.assertIn("earnings_confirmed", events)

    def test_panel_for_the_dashboard(self):
        earnings.record(10, "transcription")
        p = earnings.panel()
        self.assertEqual(p["confirmed_total"], 10)
        self.assertEqual(len(p["recent"]), 1)


class WiringTests(_Base):
    def test_commands_parse(self):
        self.assertEqual(commands.parse("earnings"), ("earnings", ""))
        self.assertEqual(commands.parse("ledger"), ("earnings", ""))
        self.assertEqual(commands.parse("earned 45 transcription"), ("earned", "45 transcription"))
        self.assertEqual(commands.parse("earned 12.50 freelance_scripts invoice paid"), ("earned", "12.50 freelance_scripts invoice paid"))

    def test_read_only_view_is_a_button_but_recording_income_stays_typed(self):
        self.assertIn("earnings", buttons.ALLOWED_KINDS)
        self.assertNotIn("earned", buttons.ALLOWED_KINDS)                       # typed only, like money_set: it carries an amount
        cmds = [b["callback_data"] for row in buttons.for_reply("menu", "hi")["inline_keyboard"] for b in row]
        self.assertTrue(any("earnings" in c for c in cmds))


if __name__ == "__main__":
    unittest.main()
