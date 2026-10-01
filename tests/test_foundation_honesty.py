"""The owner's rule (2026-09-28): never lie, never half work. Replays today's real Telegram replies through honesty.check."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import honesty  # noqa: E402


class TodaysRealReplies(unittest.TestCase):
    def test_a_printed_bash_call_is_reported_as_not_done(self):
        out = honesty.check('bash {"command": "rsync -avz /mnt/music/research/m-fd28e3/ /mnt/music/aurix_social/"}', [])
        self.assertTrue(out.startswith("❌ Not done"))
        self.assertIn("`bash`", out)
        self.assertNotIn("rsync", out)

    def test_a_fenced_or_other_printed_tool_call_is_caught(self):
        for t in ('```json\npipeline {"steps": [{"model": "qwen3-32b"}]}\n```', 'manage_settings {"action": "get", "key": "landpilot_status"}',
                  'web_search {"query": "5-10 acres", "time_filter": "day"}'):
            self.assertTrue(honesty.check(t, []).startswith("❌ Not done"), t)

    def test_claimed_actions_without_any_tool_are_withheld(self):
        for t in ("The changes have been reverted, but you requested to wait until you are home.",
                  "The command `ls /mnt/music/` was executed to list the contents. Here is the output: aurix_social odysseus_data",
                  "STEP DONE: Mission redefined to include phases for acquiring the property",
                  "I deleted the old files and restarted the service."):
            out = honesty.check(t, [])
            self.assertTrue(out.startswith("❌ Not done"), t)
            self.assertIn("withheld", out)

    def test_no_answer_is_said_plainly(self):
        for t in ("", "   ", "(agent ran but returned no clear final answer)"):
            self.assertTrue(honesty.check(t, []).startswith("❌ No answer"))


class RealWorkGetsAReceipt(unittest.TestCase):
    def test_tools_that_ran_are_listed_with_their_result(self):
        tools = [{"tool": "bash", "exit_code": 0, "output": "a b c"}, {"tool": "web_search", "exit_code": None, "output": "..."}]
        out = honesty.check("Here is the output: a b c", tools)
        self.assertTrue(out.startswith("Here is the output: a b c"))
        self.assertIn("🧾 Ran: bash (ok), web_search", out)

    def test_failed_and_denied_tools_are_flagged(self):
        tools = [{"tool": "bash", "exit_code": 2, "output": "No such file"}, {"tool": "write_file", "exit_code": None, "output": "Denied: protected"}]
        out = honesty.check("Tried both.", tools)
        self.assertIn("bash (FAILED, exit 2)", out)
        self.assertIn("write_file (denied)", out)

    def test_a_printed_call_for_a_tool_that_did_run_is_not_called_a_lie(self):
        out = honesty.check('bash {"command": "ls"}', [{"tool": "bash", "exit_code": 0, "output": "x"}])
        self.assertFalse(out.startswith("❌"))

    def test_plain_conversation_passes_through_untouched(self):
        t = "Colorado is your home state, so a license question applies only if you are paid to find land for others."
        self.assertEqual(honesty.check(t, []), t)


if __name__ == "__main__":
    unittest.main()
