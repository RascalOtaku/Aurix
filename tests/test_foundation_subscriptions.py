"""The subscription/bills audit: code-only (no model - real dollar amounts are never guessed by an LLM), builds a checklist with
a monthly-equivalent total and to-do reminders near renewal dates. Cancelling and reviewing statements are always the owner's."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, buttons, commands, projects, subscriptions as subs  # noqa: E402


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


class ParseTests(_Base):
    def test_a_typical_line_is_parsed_correctly(self):
        subs.add("Netflix $15.49 monthly renews 15th")
        row = subs.entries()[0]
        self.assertEqual((row["name"], row["amount"], row["unit"], row["day"]), ("Netflix", 15.49, "month", 15))

    def test_slash_mo_and_unstated_frequency_both_work(self):
        subs.add("Spotify $11.99/mo\nAnnual domain renewal $12")
        rows = subs.entries()
        self.assertEqual(rows[0]["unit"], "month")
        self.assertEqual(rows[1]["unit"], "year")                              # "annual" recognized even though it's not the frequency keyword order

    def test_yearly_and_weekly_convert_to_a_monthly_equivalent(self):
        subs.add("Amazon Prime $139 yearly\nCoffee subscription $8 weekly")
        rows = subs.entries()
        self.assertAlmostEqual(rows[0]["monthly_equiv"], 139 / 12, places=2)
        self.assertAlmostEqual(rows[1]["monthly_equiv"], 8 * 4.345, places=2)

    def test_a_line_with_no_dollar_amount_is_reported_not_guessed(self):
        msg = subs.add("Netflix $15.49 monthly\nsomething with no price at all")
        self.assertEqual(len(subs.entries()), 1)
        self.assertIn("Could not read", msg)
        self.assertIn("no price at all", msg)

    def test_empty_input_asks_for_the_format_instead_of_crashing(self):
        self.assertIn("one subscription per line", subs.add(""))
        self.assertIn("one subscription per line", subs.add("   \n  "))


class SummaryTests(_Base):
    def test_total_and_summary(self):
        subs.add("Netflix $15.49 monthly\nSpotify $9.99 monthly")
        self.assertEqual(subs.total_monthly(), 25.48)
        text = subs.render_summary()
        self.assertIn("2 tracked", text)
        self.assertIn("$25.48", text)

    def test_duplicates_are_flagged(self):
        subs.add("Netflix $15.49 monthly\nnetflix $17.99 monthly")               # a price bump under the same name
        self.assertEqual(len(subs.duplicates()), 1)
        self.assertIn("Possible duplicates", subs.render_summary())

    def test_remove_by_id(self):
        subs.add("Netflix $15.49 monthly")
        sid = subs.entries()[0]["id"]
        self.assertIn("Removed", subs.remove(sid))
        self.assertEqual(subs.entries(), [])
        self.assertIn("No subscription", subs.remove(sid))

    def test_a_renewal_day_creates_a_todo_a_day_without_one_does_not(self):
        subs.add("Netflix $15.49 monthly renews 15th\nSpotify $9.99 monthly")
        todos = projects.Registry().load()["todos"]
        self.assertEqual(len(todos), 1)
        self.assertIn("Netflix", todos[0].text)

    def test_panel_for_the_dashboard(self):
        subs.add("Netflix $15.49 monthly")
        p = subs.panel()
        self.assertEqual(p["count"], 1)
        self.assertEqual(p["items"][0]["name"], "Netflix")


class WiringTests(_Base):
    def test_commands_parse(self):
        self.assertEqual(commands.parse("subscriptions: Netflix $15 monthly")[0], "subscriptions_add")
        self.assertEqual(commands.parse("subs: Netflix $15 monthly")[0], "subscriptions_add")
        self.assertEqual(commands.parse("subscriptions"), ("subscriptions_status", ""))
        self.assertEqual(commands.parse("subs"), ("subscriptions_status", ""))
        self.assertEqual(commands.parse("subscriptions remove s-abc123"), ("subscriptions_remove", "s-abc123"))

    def test_buttons_and_dashboard(self):
        self.assertIn("subscriptions_status", buttons.ALLOWED_KINDS)
        self.assertNotIn("subscriptions_add", buttons.ALLOWED_KINDS)             # typed only: a real dollar amount, not a tap

    def test_shard_registered_and_pausable(self):
        from src.foundation import shards
        self.assertIn("subscriptions", shards.IDS)
        self.assertNotIn("subscriptions", shards.PAUSABLE_NOT)


if __name__ == "__main__":
    unittest.main()
