"""AURIX version history + big-shift review tickets (src/foundation/versions.py). Offline, temp code tree and data root."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, commands, identity, versions  # noqa: E402


class Versions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()
        self.write("app.py", "app")
        self.write("src/foundation/alpha.py", "a")
        self.write("data/secret.json", "never fingerprinted")
        self.write(".env", "KEY=never")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def test_baseline_then_small_change_bumps_patch(self):
        v1 = versions.record("deploy")
        self.assertEqual((v1["version"], v1["review"]), ("1.0.0", "none"))
        self.assertNotIn("data/secret.json", versions.fingerprint())
        self.assertNotIn(".env", versions.fingerprint())
        self.assertTrue(versions.record("deploy")["unchanged"])                         # nothing changed: no new version
        self.write("src/foundation/alpha.py", "a2")
        v2 = versions.record("deploy")
        self.assertEqual((v2["version"], v2["big_shift"], v2["modified"]), ("1.0.1", False, ["src/foundation/alpha.py"]))

    def test_big_shifts_bump_minor_and_open_a_review_ticket(self):
        versions.record("deploy")
        self.write("src/approval_gate.py", "changed")                                    # protected component
        v = versions.record("deploy")
        self.assertEqual(v["version"], "1.1.0")
        self.assertTrue(any("protected component" in r for r in v["reasons"]))
        ticket = (self.root / "data/versions/reviews/v1.1.0.md").read_text()
        self.assertIn("big-shift review", ticket)
        self.assertIn("src/approval_gate.py", ticket)
        self.write("src/foundation/brand_new.py", "x")                                    # new module
        self.assertIn("new Foundation module", " ".join(versions.record("deploy")["reasons"]))
        for n in range(versions.BIG_FILES):                                              # many files
            self.write(f"routes/r{n}.py", "x")
        self.assertIn("files changed", " ".join(versions.record("deploy")["reasons"]))

    def test_review_can_be_closed_and_status_is_honest(self):
        self.assertIn("No AURIX version recorded", versions.status_text())
        versions.record("deploy")
        self.write("src/approval_gate.py", "changed")
        versions.record("deploy")
        self.assertIn("Open review tickets", versions.status_text())
        self.assertIn("closed", versions.mark_reviewed("v1.1.0"))
        self.assertIn("no open review", versions.mark_reviewed("1.1.0"))
        self.assertEqual(audit.recent(1)[0]["event"], "version_reviewed")

    def test_wiring(self):
        self.assertEqual(commands.parse("version"), ("version", ""))
        self.assertEqual(commands.parse("reviewed v1.2.0"), ("reviewed", "v1.2.0"))
        self.assertEqual(identity.protected_component_for("data/versions/history.json"), "approval_gate")


if __name__ == "__main__":
    unittest.main()
