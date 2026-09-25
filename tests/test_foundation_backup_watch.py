"""AURIX notices when its nightly NAS backup stops: a failed run, or no good run for 36 h (cron dead, NAS unmounted...)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import command_center as cc, commands, watchdog  # noqa: E402

NOW = 5_000_000.0
H = 3600.0


def snap(backup="__absent__"):
    s = {"organs": {"circulatory": {"title": "c", "status": "ok", "metrics": {"disk %": 40.0, "memory %": 40.0, "swap %": 5.0}}},
         "audit": {"ok": True}, "missions": [], "standing": []}
    if backup != "__absent__":
        s["backup"] = backup
    return s


def run(s, state=None, now=NOW):
    return watchdog.evaluate(s, state or {}, now)


class BackupAlertTests(unittest.TestCase):
    def test_no_status_file_means_backups_are_not_set_up_and_nothing_is_said(self):
        for absent in ("__absent__", None):
            self.assertEqual(run(snap(absent))[0], [])

    def test_a_fresh_good_backup_is_silent(self):
        self.assertEqual(run(snap({"ok": True, "last_ok": NOW - 5 * H}))[0], [])
        self.assertEqual(run(snap({"ok": True, "last_ok": NOW - 35.9 * H}))[0], [])

    def test_a_failed_run_alerts_with_the_reason_and_the_last_good_time(self):
        msgs, _ = run(snap({"ok": False, "message": "the NAS is not mounted at /mnt/hearthnode", "last_ok": NOW - 26 * H}))
        self.assertEqual(len(msgs), 1)
        self.assertIn("failed", msgs[0])
        self.assertIn("the NAS is not mounted", msgs[0])
        self.assertIn("Last good backup: 26 h ago", msgs[0])

    def test_a_failure_before_any_success_says_so(self):
        msgs, _ = run(snap({"ok": False, "message": "cannot create snapshot dir", "last_ok": None}))
        self.assertIn("no successful backup yet", msgs[0])

    def test_silence_is_caught_too_a_dead_cron_leaves_a_stale_status_file(self):
        msgs, _ = run(snap({"ok": True, "last_ok": NOW - 40 * H}))
        self.assertEqual(len(msgs), 1)
        self.assertIn("No successful AURIX backup for 40 hours", msgs[0])
        self.assertIn("Is the NAS mounted", msgs[0])
        self.assertEqual(len(run(snap({"ok": True, "last_ok": NOW - 36 * H}))[0]), 1)              # exactly at the limit counts

    def test_once_then_cleared_when_the_next_run_succeeds(self):
        bad = snap({"ok": False, "message": "boom", "last_ok": NOW - 30 * H})
        msgs, st = run(bad, now=NOW)
        self.assertEqual(len(msgs), 1)
        again, st = run(bad, st, now=NOW + 300)
        self.assertEqual(again, [])                                                                  # not repeated within 6 h
        cleared, st = run(snap({"ok": True, "last_ok": NOW + 400}), st, now=NOW + 600)
        self.assertEqual(len(cleared), 1)
        self.assertIn("Cleared", cleared[0])

    def test_the_message_text_is_escaped(self):
        msgs, _ = run(snap({"ok": False, "message": "<script>alert(1)</script> & co", "last_ok": NOW}))
        self.assertNotIn("<script>", msgs[0])
        self.assertIn("&lt;script&gt;", msgs[0])

    def test_junk_status_values_never_crash(self):
        for junk in ({}, {"ok": "yes"}, {"ok": True, "last_ok": "yesterday"}, {"ok": True, "last_ok": True}, {"last_ok": [1]}):
            run(snap(junk))
        self.assertIsNone(watchdog.backup_age_hours({"last_ok": True}, NOW))                        # a bool is not a timestamp


class ReadingTheStatusFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, text):
        p = watchdog.backup_status_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def test_missing_garbage_and_wrong_shapes_read_as_not_set_up(self):
        self.assertIsNone(watchdog.read_backup_status())
        for junk in ("{not json", "[]", "null", '"text"'):
            self.write(junk)
            self.assertIsNone(watchdog.read_backup_status(), junk)

    def test_the_exact_file_the_shell_script_writes_is_understood(self):
        self.write(json.dumps({"ok": True, "message": "ok: 5236 files, 2 database(s)", "snapshot": "2026-09-19_2006",
                               "finished": NOW, "finished_iso": "2026-09-19T20:08:08-0600", "last_ok": NOW}))
        st = watchdog.read_backup_status()
        self.assertTrue(st["ok"])
        self.assertEqual(watchdog.backup_age_hours(st, NOW + 2 * H), 2.0)

    def test_the_dashboard_server_block_reports_the_backup(self):
        self.assertNotIn("backup", cc.server_stats({}))                                              # not set up: no row
        self.write(json.dumps({"ok": True, "message": "ok", "last_ok": NOW - 3.26 * H}))
        b = cc.server_stats({}, now=NOW)["backup"]
        self.assertEqual((b["ok"], b["age_h"], b["stale_h"]), (True, 3.3, 36.0))
        self.write(json.dumps({"ok": False, "message": "NAS not mounted", "last_ok": NOW - 50 * H}))
        b = cc.server_stats({}, now=NOW)["backup"]
        self.assertFalse(b["ok"])
        self.assertEqual(b["message"], "NAS not mounted")

    def test_the_real_tick_sends_a_backup_alert(self):
        import asyncio
        sent = []

        async def notify(t):
            sent.append(t)

        async def agent(t):
            return ""
        self.write(json.dumps({"ok": False, "message": "the NAS is not mounted", "last_ok": None}))
        f = commands.Foundation(agent, notify, llm=None)
        with mock.patch.dict(os.environ, {"AURIX_WATCHDOG_MINUTES": "1"}), \
                mock.patch.object(commands.sysview, "snapshot", return_value=snap()), \
                mock.patch("src.foundation.command_center.probe_homelab", return_value=[]):
            asyncio.run(f.tick_watchdog(now=NOW))
        self.assertTrue(any("nightly AURIX backup" in m and "not mounted" in m for m in sent), sent)


class PageTests(unittest.TestCase):
    def test_server_card_shows_the_backup_row_and_colours_a_late_or_failed_one(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "command.html"), encoding="utf-8").read()
        body = src[src.index("function drawServer"):src.index("function drawHomelab")]
        self.assertIn('"Backup"', body)
        self.assertIn("FAILED: ", body)
        self.assertIn("b.stale_h", body)
        self.assertNotIn("innerHTML", body)


if __name__ == "__main__":
    unittest.main()
