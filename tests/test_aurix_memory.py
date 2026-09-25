"""The AURIX LIVE MEMORY injection: useful notes only, no health-check spam, no duplicates, bounded."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import aurix_memory as am  # noqa: E402


def note(brain, tier, name, body, tag="update", age=0):
    d = Path(brain) / "memory" / tier
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"---\ntag: {tag}\ntier: {tier}\n---\n{body}\n", encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


class ParseTests(unittest.TestCase):
    def test_front_matter(self):
        meta, body = am.parse_note("---\ntag: Health\ntier: hot\n---\nHEALTH CHECK OK\nline2\n")
        self.assertEqual(meta, {"tag": "Health", "tier": "hot"})
        self.assertEqual(body, "HEALTH CHECK OK\nline2")

    def test_no_or_broken_front_matter_is_all_body(self):
        self.assertEqual(am.parse_note("just text"), ({}, "just text"))
        meta, body = am.parse_note("---\ntag: x\nnever closed")
        self.assertEqual(meta, {})
        self.assertIn("never closed", body)
        self.assertEqual(am.parse_note(""), ({}, ""))
        self.assertEqual(am.parse_note(None), ({}, ""))


class RecentMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.brain = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_spam_is_skipped_and_real_memory_survives(self):
        for i in range(60):
            note(self.brain, "hot", f"memory-{i:03d}-health.md", "HEALTH CHECK OK: all 7 scripts pass syntax check.",
                 tag="health", age=i * 1800)
        note(self.brain, "hot", "real.md", "Rascal prefers concise answers and paper-trades only.", age=99999)
        chunks = am.recent_memory(self.brain)
        self.assertEqual(chunks, ["Rascal prefers concise answers and paper-trades only."])

    def test_the_old_behaviour_would_have_injected_five_health_lines(self):
        # documents the bug this module fixes: newest-5 by mtime is 5 identical health notes
        for i in range(10):
            note(self.brain, "hot", f"h{i}.md", "HEALTH CHECK OK", tag="health", age=i)
        newest5 = sorted((Path(self.brain) / "memory" / "hot").glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        self.assertEqual(len(newest5), 5)
        self.assertEqual(am.recent_memory(self.brain), [])

    def test_untagged_identical_notes_are_deduplicated_across_tiers(self):
        note(self.brain, "hot", "a.md", "Bike rebuild is waiting on parts.", age=5)
        note(self.brain, "hot", "b.md", "  bike   rebuild is waiting on PARTS. ", age=10)
        note(self.brain, "longterm", "c.md", "Bike rebuild is waiting on parts.", age=20)
        self.assertEqual(am.recent_memory(self.brain), ["Bike rebuild is waiting on parts."])

    def test_newest_first_per_tier_and_caps(self):
        for i in range(9):
            note(self.brain, "hot", f"n{i}.md", f"fact number {i}", age=i * 10)
        chunks = am.recent_memory(self.brain, per_tier=3)
        self.assertEqual(chunks, ["fact number 0", "fact number 1", "fact number 2"])

    def test_long_notes_are_clipped_and_total_is_bounded(self):
        for i in range(5):
            note(self.brain, "hot", f"l{i}.md", f"{i}" + "x" * 5000, age=i)
        chunks = am.recent_memory(self.brain, max_chars=100, max_total_chars=250)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        self.assertLessEqual(sum(len(c) for c in chunks), 250 + 100)

    def test_missing_or_empty_directories_are_fine(self):
        self.assertEqual(am.recent_memory(self.brain), [])
        self.assertEqual(am.recent_memory("/definitely/not/here"), [])
        (Path(self.brain) / "memory" / "hot").mkdir(parents=True)
        note(self.brain, "hot", "empty.md", "")
        self.assertEqual(am.recent_memory(self.brain), [])

    def test_only_scans_the_newest_files(self):
        for i in range(30):
            note(self.brain, "hot", f"s{i:02d}.md", "HEALTH", tag="health", age=i)          # 30 newest are noise
        note(self.brain, "hot", "ancient.md", "old but real", age=10_000)
        self.assertEqual(am.recent_memory(self.brain, scan=20), [])                           # bounded scan never reaches it
        self.assertEqual(am.recent_memory(self.brain, scan=80), ["old but real"])

    def test_hot_notes_older_than_the_cutoff_are_history_but_longterm_is_durable(self):
        day = 86400
        note(self.brain, "hot", "recent.md", "this week's decision", age=2 * day)
        note(self.brain, "hot", "stale.md", "a May daily report", age=120 * day)
        note(self.brain, "longterm", "core.md", "Rascal's standing preference", age=200 * day)
        self.assertEqual(am.recent_memory(self.brain), ["this week's decision", "Rascal's standing preference"])
        # explicit override: no age limits at all
        self.assertEqual(len(am.recent_memory(self.brain, max_age_days={})), 3)

    def test_message_format(self):
        self.assertEqual(am.live_memory_message(self.brain), "")
        note(self.brain, "hot", "a.md", "one")
        note(self.brain, "longterm", "b.md", "two")
        msg = am.live_memory_message(self.brain)
        self.assertTrue(msg.startswith("AURIX LIVE MEMORY:\n"))
        self.assertIn("one", msg)
        self.assertIn("two", msg)

    def test_unreadable_binary_note_does_not_crash(self):
        d = Path(self.brain) / "memory" / "hot"
        d.mkdir(parents=True)
        (d / "bin.md").write_bytes(b"\xff\xfe\x00garbage\x80")
        note(self.brain, "hot", "ok.md", "fine")
        self.assertIn("fine", am.recent_memory(self.brain))


if __name__ == "__main__":
    unittest.main()
