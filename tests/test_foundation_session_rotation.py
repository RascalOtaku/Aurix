"""Daily session rotation: close out yesterday's Telegram chat with a log entry, start a clean session. Pure
stdlib + sqlite3, so this runs everywhere (unlike routes/chat_helpers.py, which needs fastapi/httpx)."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import session_rotation as sr  # noqa: E402

SCHEMA = """
CREATE TABLE sessions (
    id VARCHAR NOT NULL, name VARCHAR NOT NULL, endpoint_url VARCHAR NOT NULL, model VARCHAR NOT NULL,
    owner VARCHAR, rag BOOLEAN, archived BOOLEAN, folder VARCHAR, headers JSON,
    last_accessed DATETIME, last_message_at DATETIME, is_important BOOLEAN, message_count INTEGER,
    total_input_tokens INTEGER, total_output_tokens INTEGER, mode VARCHAR, crew_member_id VARCHAR,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, PRIMARY KEY (id)
);
CREATE TABLE chat_messages (
    id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, role VARCHAR NOT NULL, content TEXT NOT NULL,
    metadata TEXT, timestamp DATETIME, PRIMARY KEY (id)
);
"""
OLD_SID = "11111111-1111-1111-1111-111111111111"


def fake_local(reply):
    def local(system, prompt, max_tokens):
        return reply, ""
    return local


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        db_path = Path(self.tmp.name) / "data" / "app.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(db_path))
        con.executescript(SCHEMA)
        now = "2026-09-18 02:50:17.626774"
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (OLD_SID, "Telegram Agent", "http://100.64.0.10:11434/v1/chat/completions", "",
                     "rascal", False, False, None, "{}", now, now, False, 2, 500, 100, "agent", None, now, now))
        con.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?)",
                    ("m1", OLD_SID, "user", "What's my GPU?", None, now))
        con.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?)",
                    ("m2", OLD_SID, "assistant", "You told me: a 1660 Super.", None, now))
        con.commit()
        con.close()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class CurrentSessionIdTests(_Base):
    def test_falls_back_to_the_env_var_before_any_rotation(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            self.assertEqual(sr.current_session_id(), OLD_SID)

    def test_none_when_nothing_is_configured_at_all(self):
        os.environ.pop("TELEGRAM_AGENT_SESSION_ID", None)
        self.assertIsNone(sr.current_session_id())

    def test_returns_the_pointer_once_a_rotation_has_happened(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            entry = sr.daily_rotate(now=time.time(), local=fake_local("a summary"))
        self.assertEqual(sr.current_session_id(), entry["new_session_id"])
        self.assertNotEqual(sr.current_session_id(), OLD_SID)


class DailyRotateTests(_Base):
    def test_nothing_configured_is_a_safe_no_op(self):
        os.environ.pop("TELEGRAM_AGENT_SESSION_ID", None)
        self.assertIsNone(sr.daily_rotate(now=time.time(), local=fake_local("x")))

    def test_missing_app_db_is_a_safe_no_op(self):
        (Path(self.tmp.name) / "data" / "app.db").unlink()
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            self.assertIsNone(sr.daily_rotate(now=time.time(), local=fake_local("x")))

    def test_a_real_rotation_creates_a_fresh_session_seeded_with_the_summary(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            entry = sr.daily_rotate(now=time.time(), local=fake_local("Owner asked about GPU; told them 1660 Super."))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["old_session_id"], OLD_SID)
        new_id = entry["new_session_id"]
        self.assertNotEqual(new_id, OLD_SID)

        con = sqlite3.connect(str(Path(self.tmp.name) / "data" / "app.db"))
        con.row_factory = sqlite3.Row
        new_row = con.execute("SELECT * FROM sessions WHERE id = ?", (new_id,)).fetchone()
        self.assertIsNotNone(new_row)
        self.assertEqual(new_row["name"], "Telegram Agent")
        self.assertEqual(new_row["message_count"], 0)
        msgs = con.execute("SELECT role, content FROM chat_messages WHERE session_id = ?", (new_id,)).fetchall()
        con.close()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("1660 Super", msgs[0]["content"])

        # the old, full session is untouched - nothing here ever deletes it
        con = sqlite3.connect(str(Path(self.tmp.name) / "data" / "app.db"))
        old_msgs = con.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (OLD_SID,)).fetchone()[0]
        con.close()
        self.assertEqual(old_msgs, 2)

    def test_the_log_file_gets_a_durable_entry(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            sr.daily_rotate(now=time.time(), local=fake_local("a real day's summary"))
        entries = sr.log_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["summary"], "a real day's summary")
        self.assertEqual(entries[0]["message_count"], 2)

    def test_running_twice_the_same_day_does_nothing_the_second_time(self):
        now = time.time()
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            first = sr.daily_rotate(now=now, local=fake_local("day one"))
            second = sr.daily_rotate(now=now + 60, local=fake_local("should not run"))
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(sr.log_entries()), 1)

    def test_force_rotates_again_even_on_the_same_day(self):
        now = time.time()
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            sr.daily_rotate(now=now, local=fake_local("day one"))
            second = sr.daily_rotate(now=now, local=fake_local("day one again"), force=True)
        self.assertIsNotNone(second)
        self.assertEqual(len(sr.log_entries()), 2)

    def test_an_empty_new_session_still_rotates_without_a_summary(self):
        con = sqlite3.connect(str(Path(self.tmp.name) / "data" / "app.db"))
        con.execute("DELETE FROM chat_messages")
        con.commit()
        con.close()
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": OLD_SID}):
            entry = sr.daily_rotate(now=time.time(), local=fake_local("should never be called"))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["summary"], "")


if __name__ == "__main__":
    unittest.main()
