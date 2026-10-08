"""Self-healing a filling disk: only Aurix's own old caches are deleted, never anything else."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import selfheal  # noqa: E402


class SelfHealTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.old = time.time() - 24 * 3600

    def make(self, rel, size=1000, old=True):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
        if old:
            os.utime(f, (self.old, self.old))
        return f

    def test_only_old_regenerable_caches_go(self):
        gone = [self.make("data/tts_cache/a.wav"), self.make("services/cache/k.cache"), self.make("data/emoji_cache/e.png"),
                self.make("logs/app.log.1"), self.make("logs/deploy.old")]
        kept = [self.make("data/tts_cache/fresh.wav", old=False),          # just written: may be in use
                self.make("data/memory/notes.json"), self.make("data/audit/chain.jsonl"), self.make("logs/app.log"),
                self.make("data/workspace/m-abc123/part.stl")]
        freed, removed = selfheal.free_disk()
        self.assertEqual((freed, removed), (5000, 5))
        self.assertTrue(all(not f.exists() for f in gone))
        self.assertTrue(all(f.exists() for f in kept))

    def test_symlinks_are_not_followed(self):
        target = self.make("data/memory/precious.json")
        link = self.root / "data" / "emoji_cache" / "link.png"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        selfheal.free_disk()
        self.assertTrue(target.exists())

    def test_the_alert_note(self):
        self.make("data/tts_cache/a.wav", size=3_000_000)
        with mock.patch("src.foundation.audit.append"):
            note = selfheal.heal_disk_note()
        self.assertIn("I already cleared 3 MB", note)
        self.assertIn("the disk is at", note)
        self.assertIn("Nothing of mine was safe to clear", selfheal.heal_disk_note())


if __name__ == "__main__":
    unittest.main()
