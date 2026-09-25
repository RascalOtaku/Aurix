"""The money hub: honest ranking, statuses, the crypto refusal, the lab leaderboard, and the wiring."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import actions, audit, buttons, commands, identity, money  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "brain" / "runtime").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_LAB_RUNTIME": str(t / "brain" / "runtime")})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    def lab(self):
        rep = {"generated": "2026-09-21", "simulation": True, "strategies_tried": 2, "span": ["2016-09-30", "2026-09-18"], "inception": "2026-09-21", "live_days": 40,
               "strategies": [
                   {"id": "spy", "name": "Buy & hold SPY", "idea": "x", "full": {"cagr": 15.3, "sharpe": 0.88, "max_dd": -33.7}, "out_of_sample": {"cagr": 12.0}, "live": {"total": 1.5}, "verdict": "benchmark", "weights_now": {"SPY": 1.0}},
                   {"id": "trend_spy", "name": "SPY 200-day trend", "idea": "y", "full": {"cagr": 11.7, "sharpe": 0.97, "max_dd": -22.6}, "out_of_sample": {"cagr": 9.0}, "live": None, "verdict": "did not beat SPY", "weights_now": {"SHY": 1.0}}]}
        (Path(os.environ["AURIX_LAB_RUNTIME"]) / "strategy_lab.json").write_text(json.dumps(rep))


class CatalogTests(_Base):
    def test_the_shipped_catalog_is_complete_and_honest(self):
        cat = money.load_catalog()
        self.assertIn("ESTIMATE", cat["about"])
        need = {"id", "name", "category", "summary", "aurix_does", "you_do", "gate", "capital", "est_low", "est_high", "hours_per_week", "months_to_first_dollar", "automation", "confidence", "legal_risk", "first_step"}
        ids = [i["id"] for i in cat["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 12)
        for it in cat["items"]:
            self.assertTrue(need <= set(it), it["id"])
            self.assertTrue(money.ID_RX.match(it["id"]))
            self.assertIn(it["legal_risk"], money.RISK_FACTOR)
            self.assertLessEqual(it["est_low"], it["est_high"])
            self.assertTrue(0 < it["confidence"] <= 1)
        self.assertEqual(money.get("crypto_yield")["score"], 0.0)

    def test_ranking_prefers_small_sure_things_over_long_shots_and_says_how(self):
        by = {r["id"]: r for r in money.ranked()}
        self.assertGreater(by["subscription_audit"]["score"], by["bug_bounty"]["score"])
        self.assertGreater(by["transcription"]["score"], by["bug_bounty"]["score"])
        self.assertEqual(money.score({"est_low": 100, "est_high": 300, "confidence": 0.5, "legal_risk": "low", "hours_per_week": 4}), 25.0)
        self.assertEqual(money.score({"est_low": 100, "est_high": 300, "confidence": 0.5, "legal_risk": "high", "hours_per_week": 4}), 7.5)
        self.assertEqual(money.score({"est_low": 0, "est_high": 0, "confidence": 0.9, "legal_risk": "low", "hours_per_week": 1}), 0.0)
        self.assertIn("score = ", money.__doc__)

    def test_status_changes_are_audited_and_rejected_ideas_sink(self):
        self.assertIn("now <b>rejected</b>", money.set_status("bug_bounty", "reject"))
        self.assertEqual(money.ranked()[-1]["id"], "bug_bounty")
        self.assertIn("now <b>active</b>", money.set_status("hysa_tbills", "start"))
        self.assertEqual(money.get("hysa_tbills")["status"], "active")
        self.assertIn("No money idea", money.set_status("nope_idea", "start"))
        self.assertIn("Status must be", money.set_status("hysa_tbills", "banana"))
        self.assertEqual([e["event"] for e in audit.recent(5) if e["event"] == "money_status"], ["money_status", "money_status"])

    def test_real_money_crypto_cannot_be_activated(self):
        self.assertIn("refused", money.set_status("crypto_yield", "start"))
        self.assertNotEqual(money.get("crypto_yield")["status"], "active")
        self.assertIn("now <b>rejected</b>", money.set_status("crypto_yield", "reject"))

    def test_text_views(self):
        t = money.list_text()
        self.assertIn("Estimates only", t)
        self.assertIn("never moves real money", t)
        d = money.detail_text("gpu_rental")
        for needle in ("AURIX does", "You do", "Gate", "First step"):
            self.assertIn(needle, d)
        self.assertNotIn("<script", money.detail_text("<script>"))

    def _report(self, gpus):
        (Path(os.environ["AURIX_BRAIN"]) / "gaming").mkdir(parents=True, exist_ok=True)
        (Path(os.environ["AURIX_BRAIN"]) / "gaming" / "report.json").write_text(json.dumps({"gpus": gpus}), encoding="utf-8")

    def test_gpu_rental_reads_the_real_card_instead_of_asking(self):
        """The owner should never have to type a fact AURIX can read from the game doctor's own PC report."""
        self.assertIsNone(money._gpu_estimate())                                   # no report yet: falls back to the static prompt
        self.assertIn("Tell me your GPU model", money.detail_text("gpu_rental"))
        self._report([{"name": "NVIDIA GeForce GTX 1660 SUPER", "adapter_ram_gb_reported": 4.3}])
        d = money.detail_text("gpu_rental")
        self.assertIn("GTX 1660 SUPER", d)
        self.assertNotIn("Tell me your GPU model", d)
        self.assertIn("near the bottom", d)                                        # an honest, unglamorous take on an older card

    def test_a_modern_card_gets_the_optimistic_note_an_old_one_does_not(self):
        self._report([{"name": "NVIDIA GeForce RTX 4090", "adapter_ram_gb_reported": 24.0}])
        self.assertIn("higher end", money._gpu_estimate())

    def test_other_money_ideas_are_never_touched_by_the_gpu_report(self):
        self._report([{"name": "NVIDIA GeForce RTX 4090"}])
        self.assertNotIn("RTX 4090", money.detail_text("transcription"))


class LabViewTests(_Base):
    def test_no_report_yet(self):
        self.assertIn("has not produced a report", money.lab_text())
        self.assertIsNone(money.panel()["lab"])

    def test_leaderboard_and_panel(self):
        self.lab()
        t = money.lab_text()
        self.assertIn("Simulation", t)
        self.assertIn("LIVE column is the honest one", t)
        self.assertLess(t.index("200-day trend"), t.index("Buy &amp; hold SPY"))       # ranked by Sharpe
        self.assertIn("live +1.5%", t)
        p = money.panel()
        self.assertEqual(p["lab"]["rows"][0]["id"], "trend_spy")
        self.assertEqual(p["lab"]["live_days"], 40)
        self.assertTrue(p["items"])
        json.dumps(p)


class WiringTests(_Base):
    def test_commands(self):
        self.assertEqual(commands.parse("money"), ("money", ""))
        self.assertEqual(commands.parse("ways to make money?"), ("money", ""))
        self.assertEqual(commands.parse("money gpu_rental"), ("money_show", "gpu_rental"))
        self.assertEqual(commands.parse("money gpu_rental start"), ("money_set", "gpu_rental start"))
        self.assertEqual(commands.parse("Money bug_bounty REJECT"), ("money_set", "bug_bounty reject"))
        self.assertEqual(commands.parse("lab"), ("lab", ""))
        self.assertEqual(commands.parse("strategy lab"), ("lab", ""))

    def test_buttons_reach_the_read_only_views_but_not_status_changes(self):
        for k in ("money", "money_show", "lab"):
            self.assertIn(k, buttons.ALLOWED_KINDS)
        self.assertIsNotNone(buttons.command_from_data("do:money bug_bounty explore"))    # a tap can explore, pause or drop an idea ...
        self.assertIsNotNone(buttons.command_from_data("do:money bug_bounty reject"))
        self.assertIsNone(buttons.command_from_data("do:money bug_bounty start"))         # ... but activating one stays a typed command

    def test_dashboard_action(self):
        self.assertIn("is now", actions.run_action("money_set", "hysa_tbills:start")["message"])
        self.assertIn("money_set", actions.ACTION_NAMES)
        self.assertIn("No money idea", actions.run_action("money_set", "zzz_nothing:start")["message"])

    def test_protected(self):
        self.assertEqual(identity.protected_component_for("src/foundation/money.py"), "approval_gate")
        self.assertEqual(identity.protected_component_for("money/opportunities.json"), "approval_gate")

    def test_snapshot_carries_the_panel(self):
        from src.foundation import command_center
        self.assertIn("money", command_center.snapshot())


if __name__ == "__main__":
    unittest.main()
