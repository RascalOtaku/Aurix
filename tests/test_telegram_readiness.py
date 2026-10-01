"""sysview.telegram_readiness: READY only when the listener task runs AND Telegram answered a poll recently. No secrets in the body."""
import inspect
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import sysview  # noqa: E402

ENV = {"TELEGRAM_BOT_TOKEN": "123:secret-token", "TELEGRAM_CHAT_ID": "424242", "TELEGRAM_ENABLED": "true"}


class Readiness(unittest.TestCase):
    def test_ready_needs_a_live_task_and_a_recent_real_poll(self):
        self.assertTrue(sysview.telegram_readiness({"listener_alive": True, "last_poll_age_s": 12.0}, ENV)[0])

    def test_not_ready_cases_say_why(self):
        for probe, why in (({"listener_alive": False, "last_poll_age_s": 3.0}, "listener task not running"),
                           ({"listener_alive": True, "last_poll_age_s": None}, "no successful Telegram poll yet"),
                           ({"listener_alive": True, "last_poll_age_s": 900.0}, "last good poll 900s ago")):
            ready, body = sysview.telegram_readiness(probe, ENV)
            self.assertFalse(ready)
            self.assertEqual(body["telegram"], why)

    def test_unconfigured_telegram_is_not_a_failure_and_says_so(self):
        ready, body = sysview.telegram_readiness({"listener_alive": False, "last_poll_age_s": None}, {})
        self.assertTrue(ready)
        self.assertEqual((body["telegram"], body["checked"]), ("not configured", False))

    def test_the_body_never_contains_secrets(self):
        for probe in ({"listener_alive": True, "last_poll_age_s": 1.0}, {"listener_alive": False, "last_poll_age_s": None}):
            text = json.dumps(sysview.telegram_readiness(probe, ENV)[1])
            self.assertNotIn("secret-token", text)
            self.assertNotIn("424242", text)

    def test_the_endpoint_answers_loopback_only(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()
        body = src.split('@app.get("/api/health/telegram")', 1)[1].split("@app.", 1)[0]
        self.assertIn('client not in ("127.0.0.1", "::1")', body)
        self.assertIn("status_code=404", body)
        self.assertIn("status_code=200 if ready else 503", body)


if __name__ == "__main__":
    unittest.main()
