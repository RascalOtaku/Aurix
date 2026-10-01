import unittest
from src.foundation.xp import compute


class T(unittest.TestCase):
    def test_malformed_timestamp_no_crash(self):
        # Records with garbage timestamps should not crash the XP computation.
        recs = [{"event": "mission_completed", "ts": "garbage"}]
        s = compute(recs)
        self.assertEqual(s.total, 50)
        self.assertEqual(s.streak_days, 0)

    def test_invalid_today_argument_no_crash(self):
        # An invalid 'today' string should not crash the XP computation.
        recs = [{"event": "mission_completed", "ts": "2024-01-01T10:00:00"}]
        s = compute(recs, today="not-a-date")
        self.assertEqual(s.total, 50)
        self.assertEqual(s.streak_days, 0)
