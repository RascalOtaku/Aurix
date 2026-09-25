"""The disk story: a 96%-full server was invisible to the 92% watchdog because 'percent' was used/total, not df's used/(used+avail)."""
import asyncio
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import commands, qol, sysview, watchdog  # noqa: E402

DU = namedtuple("usage", "total used free")
GB = 10 ** 9
# the real host root filesystem on 2026-09-19: 234G total, 213G used, only 8.8G available (ext4 reserves ~5% for root)
FULL = DU(234 * GB, 213 * GB, int(8.8 * GB))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MeasureTests(unittest.TestCase):
    def test_percent_matches_df_not_used_over_total(self):
        with mock.patch.object(sysview.shutil, "disk_usage", return_value=FULL):
            d = sysview.disk_usage("/")
        self.assertAlmostEqual(d["pct"], 96.0, delta=0.5)            # what `df` printed: 97% / 96%
        self.assertLess(FULL.used / FULL.total * 100, 92)             # the old measure: 91%, so no alert was ever sent
        self.assertEqual(d["free_gb"], 8.8)

    def test_the_watchdog_now_sees_a_disk_the_old_measure_hid(self):
        with mock.patch.object(sysview.shutil, "disk_usage", return_value=FULL):
            pct = sysview.probe_system()["disk_pct"]
        self.assertGreaterEqual(pct, watchdog.DISK_PCT_LIMIT)

    def test_empty_and_healthy_disks(self):
        with mock.patch.object(sysview.shutil, "disk_usage", return_value=DU(100 * GB, 0, 100 * GB)):
            self.assertEqual(sysview.disk_usage("/")["pct"], 0.0)
        with mock.patch.object(sysview.shutil, "disk_usage", return_value=DU(0, 0, 0)):
            self.assertEqual(sysview.disk_usage("/")["pct"], 0.0)     # no divide-by-zero on a weird filesystem


class DiskCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def text(self, usage):
        with mock.patch.object(sysview.shutil, "disk_usage", return_value=usage):
            return qol.disk_text()

    def test_critical_warning_and_ok_levels(self):
        crit = self.text(FULL)
        self.assertIn("96% used", crit)
        self.assertIn("8.8 GB free of 234 GB", crit)
        self.assertIn("Critically full", crit)
        self.assertIn("the host - not your PC", crit)
        warn = self.text(DU(100 * GB, 88 * GB, 12 * GB))
        self.assertIn("Getting full", warn)
        self.assertNotIn("Critically", warn)
        ok = self.text(DU(100 * GB, 40 * GB, 60 * GB))
        self.assertIn("Plenty of room", ok)
        self.assertIn("40% used", ok)

    def test_reports_aurixs_own_footprint(self):
        ws = os.path.join(self.tmp.name, "data", "workspace", "m-abc123")
        os.makedirs(ws)
        with open(os.path.join(ws, "big.bin"), "wb") as f:
            f.write(b"x" * 3_000_000)
        out = self.text(DU(100 * GB, 40 * GB, 60 * GB))
        self.assertIn("mission workspaces 3 MB", out)

    def test_survives_an_unreadable_disk(self):
        with mock.patch.object(sysview.shutil, "disk_usage", side_effect=OSError("boom")):
            self.assertIn("couldn't read the disk", qol.disk_text())

    def test_html_is_telegram_safe(self):
        out = self.text(FULL)
        self.assertNotIn("<script", out)
        self.assertEqual(out.count("<b>"), out.count("</b>"))
        self.assertEqual(out.count("<i>"), out.count("</i>"))


class WiringTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_accepts_the_natural_ways_to_ask(self):
        for text in ("disk", "Disk?", "/disk", "disk space", "Disk Space", "disk usage", "storage", "df", "  DISK  "):
            self.assertEqual(commands.parse(text), ("disk", ""), text)

    def test_ordinary_chat_is_not_swallowed(self):
        for text in ("disk drive review please", "what is the disk space on my pc", "check disk space using powershell",
                     "my storage unit is full", "df -h", "disk is full again"):
            self.assertIsNone(commands.parse(text), text)

    def test_help_mentions_disk(self):
        self.assertIn("<code>disk</code>", commands.HELP)

    async def test_handler_returns_the_report_without_a_model_or_the_gate(self):
        sent = []

        async def notify(t):
            sent.append(t)

        async def agent(t):
            raise AssertionError("the agent must not be involved in `disk`")
        f = commands.Foundation(agent, notify, llm=None)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": tmp}), \
                mock.patch.object(sysview.shutil, "disk_usage", return_value=FULL):
            out = await f.handle("disk", "")
        self.assertIn("96% used", out)

    def test_the_bash_tool_tells_the_agent_it_is_on_linux_not_windows(self):
        with open(os.path.join(ROOT, "src", "agent_loop.py"), encoding="utf-8") as f:
            src = f.read()
        block = src[src.index('"bash": """'):src.index('"python": """')]
        self.assertIn("WHERE THIS RUNS", block)
        for word in ("Linux", "powershell", "wsl", "df -h"):
            self.assertIn(word, block)


if __name__ == "__main__":
    unittest.main()
