"""The Economic Engine: the freedom number, evidence-gated stages, owner-only approvals, an honest ledger, and learning from forecasts."""
import asyncio
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import buttons, commands  # noqa: E402
from src.foundation.economy import freedom, hub, learning, ledger, pipeline, scorecard, seed, store  # noqa: E402


def ts(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc).timestamp()


NOW = ts(2026, 10, 23)


class EconBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TIMEZONE": "UTC"})
        p.start()
        self.addCleanup(p.stop)

    def ready_to_approve(self, oid="property_dossiers"):
        seed.seed_if_empty()
        for kind, text in (("research", "three sites sell parcel reports"), ("competitor", "one sells at $79"),
                           ("customer_interest", "two forum posts asking"), ("legal_ok", "records are public")):
            pipeline.add_evidence(oid, kind, text, "https://example.com/" + kind)
        pipeline.advance(oid)                                           # researched
        self.validate(oid)
        pipeline.advance(oid)                                           # validated
        for f, v in (("success", "2 paid reports"), ("stop", "no sale after 3 listings"), ("due", "2026-12-15"), ("budget", "30")):
            pipeline.set_field(oid, f, v)
        return pipeline.get(oid)

    def validate(self, oid="property_dossiers"):
        from src.foundation.economy import validation
        validation.record_sample(oid, "owner=90 machine=3 cost=0 findings=20 sourced=19 unresolved=3 value=medium shows access gap")
        pipeline.add_evidence(oid, "differentiator", "records access vs advertised access, with who resolves each gap")
        pipeline.add_evidence(oid, "offer", "$79 sourced buyer's dossier, 3 days, no title opinion or survey")
        text = validation.propose(oid, 20, "list one $79 offer on one marketplace for 30 days")
        validation.resolve(store.one("SELECT * FROM approvals WHERE action = 'experiment' ORDER BY created DESC"), True)
        validation.result(oid, "two buyers asked for a quote, one paid")
        return text

    def approve(self, oid="property_dossiers"):
        text, kb = pipeline.advance(oid)
        aid = re.search(r"yes econ (x-[0-9a-f]{6})", text).group(1)
        return aid, text, kb


class FreedomTests(EconBase):
    def test_the_worked_example(self):
        c = freedom.compute(NOW)
        self.assertEqual(c["monthly_costs"], 2250)
        self.assertEqual(c["target"], 2812.5)
        self.assertEqual(c["emergency_fund"], 13500)
        self.assertEqual(c["gap"], 2812.5)
        self.assertIn("essentials", freedom.placeholders())

    def test_settings_are_bounded_and_aliased(self):
        self.assertIn("Set", freedom.set_value("costs", 1800))
        self.assertEqual(freedom.profile()["essentials"], 1800)
        self.assertIn("between", freedom.set_value("margin", 95))
        self.assertIn("Unknown", freedom.set_value("yacht", 1))
        self.assertNotIn("essentials", freedom.placeholders())

    def test_milestones(self):
        freedom.set_value("saved", 13500)
        self.assertTrue(freedom.milestone(freedom.compute(NOW)).startswith("breathing room"))


class LedgerTests(EconBase):
    def test_only_collected_cash_minus_costs_and_tax_is_available(self):
        pipeline.create("thing", "Thing", "research")
        ledger.add("revenue", 1000, "thing", "projected", at=ts(2026, 9, 5))
        ledger.add("revenue", 300, "thing", "booked", at=ts(2026, 9, 6))
        ledger.add("revenue", 500, "thing", "collected", at=ts(2026, 9, 7))
        ledger.add("cost", 100, "thing", at=ts(2026, 9, 8))
        s = ledger.month_summary("2026-09")
        self.assertEqual((s["collected"], s["booked"], s["projected"]), (500, 300, 1000))
        self.assertEqual(s["operating_profit"], 400)
        self.assertEqual(s["tax_reserve"], 100)                         # 25% default
        self.assertEqual(s["available"], 300)

    def test_dependable_is_the_worst_of_three_full_months(self):
        for month, amount in ((7, 900), (8, 400), (9, 1200)):
            ledger.add("revenue", amount, None, "collected", at=ts(2026, month, 10))
        ledger.add("revenue", 5000, None, "collected", at=ts(2026, 10, 2))   # the current month never counts yet
        dep, months = ledger.dependable(NOW)
        self.assertEqual(dep, 300)                                      # 400 - 25% tax reserve
        self.assertEqual([m for m, _ in months], ["2026-09", "2026-08", "2026-07"])

    def test_one_good_month_is_not_dependable(self):
        ledger.add("revenue", 2000, None, "collected", at=ts(2026, 9, 10))
        self.assertEqual(ledger.dependable(NOW)[0], 0)

    def test_bad_rows_are_refused(self):
        for args in (("revenue", -5), ("cost", 5, None, "booked"), ("gift", 5)):
            with self.assertRaises(ValueError):
                ledger.add(*args)


class FinancialLogicTests(EconBase):
    def test_refunds_come_off_the_month_they_are_paid(self):
        pipeline.create("thing", "Thing", "research")
        ledger.add("revenue", 200, "thing", "collected", at=ts(2026, 8, 20))
        ledger.add("refund", 200, "thing", at=ts(2026, 9, 3))
        self.assertEqual(ledger.month_summary("2026-08")["collected"], 200)
        self.assertEqual(ledger.month_summary("2026-09")["available"], -200)        # a loss month is shown
        tot = ledger.venture_totals("thing")
        self.assertEqual((tot["collected"], tot["sales"]), (0, 1))

    def test_per_hour_is_measured_not_guessed(self):
        pipeline.create("thing", "Thing", "research")
        ledger.add("revenue", 200, "thing", "collected", at=NOW)
        ledger.log_hours(15, "thing", at=NOW)
        self.assertEqual(ledger.venture_totals("thing")["per_hour"], 13.33)
        self.assertIn("all-in $13.33/h", hub.opp_text("thing"))
        self.assertIn("1 sale(s) collected", hub.progress_line(NOW))

    def test_history_months(self):
        self.assertEqual(ledger.history_months(NOW), 0)
        ledger.add("revenue", 10, None, "collected", at=ts(2026, 7, 1))
        self.assertEqual(ledger.history_months(NOW), 3)
        self.assertIn("not a forecast", hub.dashboard(NOW))

    def test_the_deploy_check_passes_and_never_touches_the_live_ledger(self):
        from src.foundation.economy import check
        out = check.run(NOW)
        self.assertNotIn("❌", out)
        self.assertIn("8/8", out)
        self.assertEqual(store.rows("SELECT * FROM transactions"), [])               # the scenario ran on a scratch file
        self.assertRegex(out, r"code <code>[0-9a-f]{12}</code>")


class PipelineTests(EconBase):
    def test_five_candidates_and_land_is_blocked_by_the_hard_limits(self):
        self.assertEqual(len(seed.seed_if_empty()), 7)                  # 5 candidates + 2 variants of the property offer
        self.assertEqual(seed.seed_if_empty(), [])                      # once
        rows = scorecard.ranked()
        self.assertEqual(rows[-1]["id"], "land_parcel")
        self.assertTrue(any("test limit" in b for b in rows[-1]["blocked"]))
        self.assertTrue(all(r["demand"] == "none" for r in rows))

    def test_every_move_needs_evidence(self):
        seed.seed_if_empty()
        text, _ = pipeline.advance("market_monitor")
        self.assertIn("cannot move discovered", text)
        pipeline.add_evidence("market_monitor", "research", "five tools exist", "https://example.com/a")
        pipeline.add_evidence("market_monitor", "competitor", "one charges $29/mo", "https://example.com/b")
        pipeline.advance("market_monitor")
        text, _ = pipeline.advance("market_monitor")
        self.assertIn("demand evidence", text)                          # a projection is not demand
        self.assertIn("legal_ok", text)
        self.assertEqual(pipeline.get("market_monitor")["stage"], "researched")

    def test_unsourced_research_and_hypotheses_never_open_a_gate(self):
        seed.seed_if_empty()
        self.assertIn("does not count", pipeline.add_evidence("market_monitor", "research", "lots of tools"))
        pipeline.add_evidence("market_monitor", "competitor", "a rival", "https://example.com/rival")
        self.assertIn("never counted", pipeline.add_evidence("market_monitor", "hypothesis", "people would pay $29"))
        self.assertIn("cannot move", pipeline.advance("market_monitor")[0])
        pipeline.add_evidence("market_monitor", "research", "five tools exist", "https://example.com/list")
        self.assertIn("researched", pipeline.advance("market_monitor")[0])
        self.assertEqual(scorecard.demand("market_monitor"), "none")

    def test_approval_needs_the_owner_and_freezes_the_forecast(self):
        self.ready_to_approve()
        aid, text, kb = self.approve()
        for part in ("Action:", "Cost:", "Expected:", "Downside:", "Permissions:", "Stops when:"):
            self.assertIn(part, text)
        self.assertEqual(pipeline.get("property_dossiers")["stage"], "validated")   # the card alone moves nothing
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], f"do:yes econ {aid}")
        self.assertEqual(self.approve()[0], aid)                        # asking again re-shows the same card
        self.assertIn("now <b>approved</b>", pipeline.resolve(aid, True))
        self.assertEqual(pipeline.get("property_dossiers")["stage"], "approved")
        self.assertEqual(store.one("SELECT net_mid FROM forecasts")["net_mid"], 375)
        self.assertIn("already approved", pipeline.resolve(aid, True))

    def test_denied_and_expired_cards_move_nothing(self):
        self.ready_to_approve()
        aid, _, _ = self.approve()
        self.assertIn("Denied", pipeline.resolve(aid, False))
        self.assertEqual(pipeline.get("property_dossiers")["stage"], "validated")
        aid2, _, _ = self.approve()
        store.execute("UPDATE approvals SET created = created - ? WHERE id = ?", (15 * 86400, aid2))
        self.assertIn("expired", pipeline.resolve(aid2, True))
        self.assertEqual(pipeline.get("property_dossiers")["stage"], "validated")

    def test_a_card_cannot_be_used_for_a_bigger_budget(self):
        self.ready_to_approve()
        aid, _, _ = self.approve()
        pipeline.set_field("property_dossiers", "budget", "45")
        self.assertIn("budget was raised", pipeline.resolve(aid, True))
        self.assertEqual(pipeline.get("property_dossiers")["stage"], "validated")

    def test_hard_limits_are_rechecked_at_approval(self):
        self.ready_to_approve()
        aid, _, _ = self.approve()
        freedom.set_value("test", 10)                                   # the owner tightened the limit after the card
        self.assertIn("Not approved", pipeline.resolve(aid, True))

    def test_a_running_venture_cannot_quietly_take_more_risk(self):
        self.ready_to_approve()
        pipeline.resolve(self.approve()[0], True)
        self.assertIn("needs your approval", pipeline.set_field("property_dossiers", "budget", "500"))
        self.assertIn("budget = 20", pipeline.set_field("property_dossiers", "budget", "20"))   # lowering is fine

    def test_running_ventures_use_up_the_hours_budget(self):
        self.ready_to_approve()
        pipeline.resolve(self.approve()[0], True)                       # 4 of 10 h
        freedom.set_value("hours", 6)
        blocked = scorecard.constraints(pipeline.get("market_monitor"))   # needs 3, only 2 free
        self.assertTrue(any("h/week" in b for b in blocked))

    def test_close_keeps_the_reason_as_a_lesson(self):
        seed.seed_if_empty()
        self.assertIn("Closed", pipeline.modify("research_templates", "no demand anywhere", close=True))
        self.assertEqual(scorecard.evidence("research_templates")[-1]["kind"], "lesson")
        self.assertNotIn("research_templates", [r["id"] for r in scorecard.ranked()])


class LearningTests(EconBase):
    def test_reality_discounts_new_projections(self):
        self.assertEqual(learning.calibration(NOW), 1.0)               # no evidence yet
        for oid in ("aaa", "bbb"):
            pipeline.create(oid, oid, "research", net_low=200, net_high=200)
            o = pipeline.get(oid)
            pipeline._move(o, "approved", "owner")
            store.execute("INSERT INTO stage_log(opp_id, from_stage, to_stage, by, reason, at) VALUES(?,?,?,?,?,?)",
                          (oid, "built", "launched", "owner", "", NOW - 61 * 86400))
            ledger.add("revenue", 200, oid, "collected", at=NOW - 30 * 86400)   # ~$100/mo against $200 forecast
        self.assertAlmostEqual(learning.calibration(NOW), 0.5, places=1)
        self.assertEqual(len(learning.comparisons(NOW)), 2)


class CommandTests(EconBase):
    def test_parse(self):
        self.assertEqual(commands.parse("freedom"), ("econ_freedom", ""))
        self.assertEqual(commands.parse("Freedom set Essentials $1,800"), ("econ_set", "essentials\t$1,800".replace("$", "")))
        self.assertEqual(commands.parse("opp Market_Monitor set next Call Bob's Diner"),
                         ("econ_opp_set", "market_monitor\tnext\tCall Bob's Diner"))
        self.assertEqual(commands.parse("evidence x_y research: Found It https://a.b/c"),
                         ("econ_evidence", "x_y\tresearch\tFound It\thttps://a.b/c"))
        self.assertEqual(commands.parse("spent 12.50 abc"), ("econ_spent", "12.50\tabc"))
        self.assertEqual(commands.parse("yes econ x-ABC123"), ("econ_yes", "x-abc123"))
        self.assertEqual(commands.parse("opp abc close: Nobody Bought"), ("econ_modify", "abc\tclose\tNobody Bought"))
        self.assertEqual(commands.parse("review")[0], "econ_review")
        self.assertEqual(commands.parse("refund 20 Abc sorry"), ("econ_refund", "20\tabc\tsorry"))
        self.assertEqual(commands.parse("econ check"), ("econ_check", ""))
        self.assertIsNone(commands.parse("opp"))

    def test_taps_reach_the_card_but_not_new_ventures(self):
        self.ready_to_approve()
        aid, text, _ = self.approve()
        kb = buttons.for_reply("econ_advance", text)
        data = kb["inline_keyboard"][0][0]["callback_data"]
        kind, arg = commands.parse(buttons.command_from_data(data))
        self.assertIn(kind, buttons.ALLOWED_KINDS)
        self.assertNotIn("econ_new", buttons.ALLOWED_KINDS)
        self.assertNotIn("econ_spent", buttons.ALLOWED_KINDS)
        self.assertIn("approved", hub.handle(kind, arg))

    def test_dashboard_and_review_through_the_foundation(self):
        f = commands.Foundation(run_agent=None, notify=None)
        text = asyncio.run(f.handle(*commands.parse("freedom")))
        self.assertIn("Freedom Dashboard", text)
        self.assertIn("7 candidate opportunities", text)
        self.assertIn("Weekly portfolio review", asyncio.run(f.handle(*commands.parse("review"))))

    def test_earned_mirrors_into_the_engine_for_an_opportunity(self):
        seed.seed_if_empty()
        f = commands.Foundation(run_agent=None, notify=None)
        reply = asyncio.run(f.handle(*commands.parse("earned 79 property_dossiers first report")))
        self.assertIn("Economic Engine", reply)
        self.assertEqual(ledger.venture_totals("property_dossiers")["collected"], 79)
        self.assertEqual(scorecard.demand("property_dossiers"), "paid")
        reply = asyncio.run(f.handle(*commands.parse("earned 20 transcription")))
        self.assertNotIn("Economic Engine", reply)

    def test_weekly_review_is_sent_once_on_sunday_evening_only_after_opening(self):
        said = []

        async def notify(text, kb=None):
            said.append((text, kb))
        f = commands.Foundation(run_agent=None, notify=notify)
        sunday = ts(2026, 10, 25, 18)
        asyncio.run(f._maybe_economy(sunday))
        self.assertEqual(said, [])                                     # never opened: nothing
        hub.dashboard()
        asyncio.run(f._maybe_economy(ts(2026, 10, 25, 17)))
        self.assertEqual(said, [])
        asyncio.run(f._maybe_economy(sunday))
        asyncio.run(f._maybe_economy(sunday + 600))
        self.assertEqual(len(said), 1)
        self.assertIn("Weekly portfolio review", said[0][0])


if __name__ == "__main__":
    unittest.main()
