"""Tests for src/foundation/addons.py (dashboard tiles, monitor strip, the one-push check) and the `dashboard` / `all` commands."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import addons, audit, commands, evals, fastlane, gaming, sandbox, teacher  # noqa: E402

NOW = time.time()


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_GAMING_DIR": str(t / "gaming"), "AURIX_BRAIN": str(t / "brain"),
                                                 "ANTHROPIC_API_KEY": ""})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    def by_key(self):
        return {a["key"]: a for a in addons.probe_addons()}

    def put_game_report(self, age_s=60, pending=False):
        d = Path(os.environ["AURIX_GAMING_DIR"])
        d.mkdir(parents=True, exist_ok=True)
        rep = {"generated_ts": NOW - age_s, "games": [{"appid": "1", "name": "G", "size_gb": 1}], "libraries": []}
        (d / "report.json").write_text(json.dumps(rep))
        if pending:
            gaming.save_proposal({"id": "g-abc123", "key": "k", "game": "G", "appid": "1", "kind": "missing_master", "severity": "high", "title": "t",
                                  "evidence": ["e"], "fix": {"actions": [], "summary": "s", "risk": "r"}, "status": "pending", "created": NOW})

    def put_trade_report(self, **over):
        d = Path(os.environ["AURIX_BRAIN"]) / "runtime"
        d.mkdir(parents=True, exist_ok=True)
        rep = {"lifeforce_pct": 101.2, "return_pct": 1.2, "excess_pct": 0.3, "open_positions": [{"symbol": "AAPL"}, {"symbol": "SPY", "core": True}],
               "last_scan": "2026-09-21T14:15:00", "killed": False, "dead": False, "equity": 1}
        rep.update(over)
        (d / "trade_report.json").write_text(json.dumps(rep))


class TileTests(_Base):
    def test_every_addon_has_a_complete_tile(self):
        tiles = addons.probe_addons()
        self.assertEqual([t["key"] for t in tiles], ["gaming", "trading", "evals", "teacher", "fastlane", "forge", "standing", "backup"])
        for t in tiles:
            self.assertEqual({"key", "title", "icon", "status", "summary", "detail", "hint", "how", "actions"}, set(t))
            self.assertIn(t["status"], ("ok", "warn", "bad", "off"))
            self.assertTrue(t["summary"])

    def test_a_broken_probe_is_a_red_tile_not_a_broken_dashboard(self):
        with mock.patch.object(addons, "_trading", side_effect=RuntimeError("boom")), \
                mock.patch.object(addons, "ADDONS", [("trading", "Paper trading", "📈", addons._trading)]):
            [t] = addons.probe_addons()
        self.assertEqual((t["status"], t["summary"], t["detail"]), ("bad", "could not be checked", "RuntimeError"))

    def test_game_doctor_states(self):
        self.assertEqual(self.by_key()["gaming"]["status"], "off")
        self.put_game_report(age_s=120)
        self.assertEqual(self.by_key()["gaming"]["status"], "ok")
        self.assertIn("all healthy", self.by_key()["gaming"]["summary"])
        self.put_game_report(age_s=120, pending=True)
        t = self.by_key()["gaming"]
        self.assertEqual((t["status"], t["hint"]), ("warn", "fixes"))
        self.assertIn("1 fix waiting", t["summary"])
        self.put_game_report(age_s=6 * 3600)
        self.assertIn("old", self.by_key()["gaming"]["summary"])
        self.assertEqual(self.by_key()["gaming"]["status"], "warn")

    def test_paper_trading_states(self):
        self.assertEqual(self.by_key()["trading"]["status"], "off")
        self.put_trade_report()
        t = self.by_key()["trading"]
        self.assertEqual(t["status"], "ok")
        self.assertIn("101.2%", t["summary"])
        self.assertIn("vs SPY +0.30%", t["summary"])
        self.put_trade_report(killed=True)
        self.assertEqual(self.by_key()["trading"]["status"], "warn")
        self.put_trade_report(dead=True)
        self.assertEqual(self.by_key()["trading"]["status"], "bad")
        self.put_trade_report(last_scan="")
        self.assertEqual(self.by_key()["trading"]["status"], "off")

    def test_teacher_states(self):
        self.assertEqual(self.by_key()["teacher"]["status"], "off")
        self.assertIn("no API key", self.by_key()["teacher"]["summary"])
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x"}):
            self.assertIn("switched off", self.by_key()["teacher"]["summary"])
            teacher.set_config(True, 5)
            self.assertEqual(self.by_key()["teacher"]["status"], "ok")
            teacher.new_lesson({"title": "t", "diagnosis": "", "guidance": ["g"], "applies_when": ["x"]}, "s", "m")
            t = self.by_key()["teacher"]
            self.assertEqual((t["status"], t["hint"]), ("warn", "lessons"))

    def test_fast_lane_and_evals_and_forge(self):
        self.assertEqual(self.by_key()["fastlane"]["status"], "ok")
        fastlane.set_enabled(False)
        self.assertEqual(self.by_key()["fastlane"]["status"], "off")
        self.assertEqual(self.by_key()["evals"]["status"], "off")
        evals.record([{"id": "a", "tier": "plan", "passed": True, "seconds": 0, "error": ""}])
        self.assertEqual(self.by_key()["evals"]["status"], "ok")
        evals.record([{"id": "a", "tier": "plan", "passed": False, "seconds": 0, "error": "x"}])
        self.assertEqual(self.by_key()["evals"]["status"], "warn")
        self.assertIn("plan 0/1", self.by_key()["evals"]["summary"])
        self.assertEqual(self.by_key()["forge"]["status"], "ok")


class MonitorTests(_Base):
    def test_strip_combines_organs_homelab_and_addons(self):
        organs = [{"key": "audit", "title": "Audit chain", "status": "ok"}, {"key": "disk", "title": "Disk", "status": "warn", "detail": "88% full"}]
        homelab = [{"name": "Jellyfin", "ok": True}, {"name": "NAS", "ok": False}]
        mons = addons.probe_monitors(organs, homelab, addons.probe_addons())
        titles = [m["title"] for m in mons]
        self.assertEqual(titles[:2], ["Audit chain", "Disk"])
        self.assertIn("Homelab", titles)
        self.assertIn("Game PC agent", titles)
        home = [m for m in mons if m["title"] == "Homelab"][0]
        self.assertEqual(home["status"], "bad")
        self.assertIn("NAS", home["detail"])

    def test_an_organ_with_only_a_plain_description_still_says_what_it_watches(self):
        mons = addons.probe_monitors([{"key": "nervous", "title": "Nervous system", "status": "ok", "sub": "Telegram & schedulers"}], None, None)
        self.assertEqual(mons[0]["detail"], "Telegram & schedulers")

    def test_empty_inputs(self):
        self.assertEqual(addons.probe_monitors(None, None, None), [])


class RunAllTests(_Base):
    def _run(self):
        with mock.patch.object(sandbox, "available", return_value=True), mock.patch.object(sandbox, "presence_kw",
                                                                                            return_value=dict(env={}, which=lambda b: "/usr/bin/" + b, find_spec=lambda m: object(), authorizations={}, probe=lambda h, p: True)), \
                mock.patch("src.foundation.command_center.probe_homelab", return_value=[{"name": "A", "ok": True}]):
            return addons.run_all()

    def test_every_check_runs_and_reports(self):
        r = self._run()
        names = [s["name"] for s in r["steps"]]
        self.assertEqual(names, ["Audit chain", "Homelab services", "Code sandbox", "Nightly backup", "Game PC agent", "Planner self-check", "Paper trading",
                                 "Waiting on you"])
        by = {s["name"]: s for s in r["steps"]}
        self.assertTrue(by["Audit chain"]["ok"])
        self.assertTrue(by["Homelab services"]["ok"])
        self.assertTrue(by["Planner self-check"]["ok"], by["Planner self-check"])
        self.assertIn("11/11", by["Planner self-check"]["detail"])
        self.assertIsNone(by["Nightly backup"]["ok"])                       # no backup status here: "not applicable", not a failure
        self.assertTrue(by["Waiting on you"]["ok"])

    def test_it_asks_the_game_pc_for_fresh_facts_and_changes_nothing_else(self):
        before = fastlane.enabled(), teacher.config()["enabled"]
        self._run()
        self.assertTrue((Path(os.environ["AURIX_GAMING_DIR"]) / "requests" / "collect-now").exists())
        self.assertEqual((fastlane.enabled(), teacher.config()["enabled"]), before)

    def test_waiting_on_you_counts_everything_that_needs_a_decision(self):
        self.put_game_report(pending=True)
        teacher.new_lesson({"title": "t", "diagnosis": "", "guidance": ["g"], "applies_when": ["x"]}, "s", "m")
        by = {s["name"]: s for s in self._run()["steps"]}
        self.assertFalse(by["Waiting on you"]["ok"])
        self.assertIn("1 game fix(es)", by["Waiting on you"]["detail"])
        self.assertIn("1 lesson(s)", by["Waiting on you"]["detail"])

    def test_a_failing_step_is_isolated_and_counted(self):
        with mock.patch.object(sandbox, "available", side_effect=RuntimeError("sandbox exploded")), \
                mock.patch("src.foundation.command_center.probe_homelab", return_value=[]):
            r = addons.run_all()
        by = {s["name"]: s for s in r["steps"]}
        self.assertFalse(by["Code sandbox"]["ok"])
        self.assertIn("sandbox exploded", by["Code sandbox"]["detail"])
        self.assertEqual(len(r["steps"]), 8)                                # the rest still ran
        self.assertFalse(r["ok"])
        self.assertGreaterEqual(r["attention"], 1)
        self.assertIn("need", r["summary"])

    def test_render(self):
        text = addons.render_run_all({"summary": "Everything checks out.", "ok": True, "attention": 0,
                                      "steps": [{"name": "A <b>", "ok": True, "detail": "<i>x</i>", "seconds": 0}, {"name": "B", "ok": False, "detail": "bad", "seconds": 0},
                                                {"name": "C", "ok": None, "detail": "n/a", "seconds": 0}]})
        self.assertIn("✅", text)
        self.assertIn("⚠️", text)
        self.assertIn("➖", text)
        self.assertNotIn("<b>A <b>", text)
        self.assertIn("Nothing was changed", text)


class DashboardTextTests(_Base):
    def test_text_lists_every_monitor_and_addon_with_how_to(self):
        mons = [{"key": "audit", "title": "Audit chain", "status": "ok", "detail": "74 records"}, {"key": "x", "title": "Disk", "status": "bad", "detail": "96% full"}]
        text = addons.dashboard_text(mons, addons.probe_addons())
        self.assertIn("🟢 Audit chain - 74 records", text)
        self.assertIn("🔴 Disk - 96% full", text)
        for title in ("Game doctor", "Paper trading", "Self-check", "Frontier teacher", "Fast lane", "Skill forge", "Standing missions", "Nightly backup"):
            self.assertIn(title, text)
        self.assertIn("Send <code>all</code>", text)

    def test_text_is_escaped(self):
        text = addons.dashboard_text([{"key": "k", "title": "<b>x</b>", "status": "ok", "detail": "<i>"}], [])
        self.assertNotIn("<b>x</b>", text)


class CommandTests(_Base):
    def test_parse(self):
        for t, kind in (("dashboard", "dashboard"), ("Monitors", "dashboard"), ("add-ons", "dashboard"), ("all", "run_all"), ("Check everything!", "run_all"),
                        ("do it all", "run_all"), ("/run all", "run_all")):
            self.assertEqual(commands.parse(t)[0], kind, t)
        self.assertNotEqual((commands.parse("deny all") or ("",))[0], "run_all")             # existing commands keep priority
        self.assertIsNone(commands.parse("all of my games are great"))

    async def test_handlers_return_the_report_and_the_dashboard(self):
        async def notify(t):
            pass

        async def agent(p):
            return "ok"
        f = commands.Foundation(agent, notify, llm=None, session_id="tg", env={}, which=lambda b: None, find_spec=lambda m: None, probe=lambda h, p: False,
                                authorizations={})
        with mock.patch.object(sandbox, "available", return_value=False), mock.patch("src.foundation.command_center.probe_homelab", return_value=[]):
            out = await f.handle("run_all", "")
        self.assertIn("Audit chain", out)
        self.assertIn("Nothing was changed", out)
        with mock.patch("src.foundation.command_center.snapshot", return_value={"monitors": [{"key": "a", "title": "Audit chain", "status": "ok", "detail": ""}],
                                                                                  "addons": addons.probe_addons()}):
            dash = await f.handle("dashboard", "")
        self.assertIn("AURIX dashboard", dash)
        self.assertIn("Game doctor", dash)


if __name__ == "__main__":
    unittest.main()
