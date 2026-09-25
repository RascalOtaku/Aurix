"""The overnight shift: gifts are built free and locally, opened once at the morning digest, pausable, and never touch anything but their own notes."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.foundation import audit, buttons, commands, money, nightshift, shards  # noqa: E402


class NightshiftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_SHARDS_DIR": str(t / "shards"),
                                                 "AURIX_NIGHTSHIFT_AT": "02:00", "AURIX_UPGRADES_DIR": str(t / "up"), "AURIX_AUDIT_DIR": str(t / "audit")})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()

    def test_a_shift_produces_gifts_and_health_is_always_one(self):
        res = nightshift.run_shift(1_800_000_000, force=True)
        self.assertTrue(res["ran"])
        ids = [g["id"] for g in nightshift.shifts()[-1]["gifts"]]
        self.assertIn("health", ids)
        self.assertIn("spotlight", ids)                                    # the catalog always has an unexplored idea to research

    def test_it_runs_once_per_day_unless_forced(self):
        now = 1_800_000_000
        nightshift.run_shift(now)
        self.assertEqual(nightshift.run_shift(now + 60), {"skipped": "already ran today"})
        self.assertTrue(nightshift.run_shift(now + 60, force=True)["ran"])

    def test_paused_shift_does_nothing(self):
        shards.set_paused("nightshift", True)
        self.assertEqual(nightshift.run_shift(force=True), {"skipped": "paused"})
        self.assertEqual(nightshift.shifts(), [])

    def test_spotlight_rotates_and_skips_rejected_ideas(self):
        now = 1_800_000_000
        nightshift.run_shift(now, force=True)
        first = nightshift.shifts()[-1]["gifts"][1]["money_id"]
        nightshift.run_shift(now + 86400, force=True)
        second = [g for g in nightshift.shifts()[-1]["gifts"] if g["id"] == "spotlight"][0]["money_id"]
        self.assertNotEqual(first, second)
        money.set_status(second, "reject")
        nightshift.run_shift(now + 2 * 86400, force=True)
        third = [g for g in nightshift.shifts()[-1]["gifts"] if g["id"] == "spotlight"][0]["money_id"]
        self.assertNotIn(third, (first, second))

    def test_crypto_is_never_spotlighted(self):
        for i in range(30):
            nightshift.run_shift(1_800_000_000 + i * 86400, force=True)
        ids = [g.get("money_id") for s in nightshift.shifts() for g in s["gifts"]]
        self.assertNotIn("crypto_yield", ids)

    def test_a_failing_gift_does_not_spoil_the_rest(self):
        def boom(now, st):
            raise RuntimeError("x")
        with mock.patch.object(nightshift, "GIFTS", [boom, nightshift._gift_health]):
            res = nightshift.run_shift(force=True)
        self.assertEqual(res["gifts"], 1)

    def test_unwrap_shows_gifts_once_then_marks_them_opened(self):
        nightshift.run_shift(force=True)
        self.assertEqual(len(nightshift.unopened()), 1)
        text = nightshift.unwrap()
        self.assertIn("overnight shift is ready", text)
        self.assertIn("Overnight health check", text)
        self.assertEqual(nightshift.unopened(), [])
        self.assertIn("already opened", nightshift.unwrap())

    def test_unwrap_before_any_shift_says_so(self):
        self.assertIn("No overnight shift", nightshift.unwrap())

    def test_spotlight_message_carries_explore_and_skip_buttons(self):
        nightshift.run_shift(force=True)
        text = nightshift.unwrap(mark_opened=False, waiting=False)
        kb = buttons.for_reply("unwrap", text)
        cmds = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertTrue(any(c.startswith("do:money ") and c.endswith(" explore") for c in cmds), cmds)
        self.assertTrue(any(c.endswith(" reject") for c in cmds))
        self.assertIn("money_set", buttons.ALLOWED_KINDS)
        for c in cmds:
            self.assertIsNotNone(buttons.command_from_data(c), c)

    def test_commands_parse(self):
        self.assertEqual(commands.parse("unwrap"), ("unwrap", ""))
        self.assertEqual(commands.parse("while I was away"), ("unwrap", ""))
        self.assertEqual(commands.parse("night now"), ("night_now", ""))
        self.assertEqual(commands.parse("overnight"), ("unwrap", ""))

    def test_panel_for_the_dashboard(self):
        p = nightshift.panel()
        self.assertEqual(p["gifts"], [])
        nightshift.run_shift(force=True)
        p = nightshift.panel()
        self.assertTrue(p["gifts"])
        self.assertNotIn("<", p["gifts"][0]["body"])                       # plain text for the page
        self.assertEqual(p["unopened"], 1)

    def test_it_is_a_registered_pausable_helper(self):
        self.assertIn("nightshift", shards.IDS)
        self.assertNotIn("nightshift", shards.PAUSABLE_NOT)
        self.assertEqual({r["id"]: r for r in shards.rows()}["nightshift"]["state"], "idle")

    def test_switched_off_when_the_time_is_empty(self):
        with mock.patch.dict(os.environ, {"AURIX_NIGHTSHIFT_AT": ""}):
            self.assertFalse(nightshift.enabled())
            self.assertEqual({r["id"]: r for r in shards.rows()}["nightshift"]["state"], "off")


class RotationGiftTests(unittest.TestCase):
    """_gift_rotation wires session_rotation.daily_rotate() into the nightly shift so the Telegram chat gets a
    real daily boundary automatically - see session_rotation.py for why (a 6-day, 727k-token session found live)."""

    def setUp(self):
        import sqlite3
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_SHARDS_DIR": str(t / "shards"),
                                                 "AURIX_NIGHTSHIFT_AT": "02:00", "AURIX_UPGRADES_DIR": str(t / "up"), "AURIX_AUDIT_DIR": str(t / "audit"),
                                                 "TELEGRAM_AGENT_SESSION_ID": "old-session-1"})
        self.env.start()
        audit._heads.clear()
        (t / "data").mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(t / "data" / "app.db"))
        con.executescript("""
            CREATE TABLE sessions (id VARCHAR NOT NULL, name VARCHAR NOT NULL, endpoint_url VARCHAR NOT NULL,
                model VARCHAR NOT NULL, owner VARCHAR, rag BOOLEAN, archived BOOLEAN, folder VARCHAR, headers JSON,
                last_accessed DATETIME, last_message_at DATETIME, is_important BOOLEAN, message_count INTEGER,
                total_input_tokens INTEGER, total_output_tokens INTEGER, mode VARCHAR, crew_member_id VARCHAR,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id));
            CREATE TABLE chat_messages (id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, role VARCHAR NOT NULL,
                content TEXT NOT NULL, metadata TEXT, timestamp DATETIME, PRIMARY KEY (id));
        """)
        now = "2026-09-18 00:00:00"
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("old-session-1", "Telegram Agent", "http://x:11434/v1/chat/completions", "", "rascal",
                     False, False, None, "{}", now, now, False, 1, 0, 0, "agent", None, now, now))
        con.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?)", ("m1", "old-session-1", "user", "hi", None, now))
        con.commit()
        con.close()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()

    def test_a_shift_rotates_the_chat_session_and_reports_it(self):
        from src.foundation import session_rotation
        with mock.patch.object(session_rotation, "_default_local", return_value=("worked on the gaming pc", "")):
            res = nightshift.run_shift(1_800_000_000, force=True)
        self.assertTrue(res["ran"])
        gifts = {g["id"]: g for g in nightshift.shifts()[-1]["gifts"]}
        self.assertIn("rotation", gifts)
        self.assertIn("worked on the gaming pc", gifts["rotation"]["body"])
        self.assertNotEqual(session_rotation.current_session_id(), "old-session-1")

    def test_a_second_shift_the_same_day_does_not_rotate_again(self):
        from src.foundation import session_rotation
        with mock.patch.object(session_rotation, "_default_local", return_value=("day one", "")):
            nightshift.run_shift(1_800_000_000, force=True)
        rotated_to = session_rotation.current_session_id()
        with mock.patch.object(session_rotation, "_default_local", return_value=("should not fire", "")):
            nightshift.run_shift(1_800_000_000 + 60, force=True)
        gifts = {g["id"]: g for g in nightshift.shifts()[-1]["gifts"]}
        self.assertNotIn("rotation", gifts)
        self.assertEqual(session_rotation.current_session_id(), rotated_to)


if __name__ == "__main__":
    unittest.main()
