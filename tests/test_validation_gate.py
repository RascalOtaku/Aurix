"""The validation gate: a measured sample, a defined offer, one capped and approved experiment, and demand for THIS offer."""
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import commands  # noqa: E402
from src.foundation.economy import freedom, hub, ledger, pipeline, scorecard, seed, store, validation  # noqa: E402

OID = "property_dossiers"


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_TIMEZONE": "UTC"})
        p.start()
        self.addCleanup(p.stop)
        seed.seed_if_empty()
        for kind in ("research", "competitor"):
            pipeline.add_evidence(OID, kind, f"{kind} notes here", f"https://example.com/{kind}")
        pipeline.advance(OID)

    def ready_to_test(self):
        validation.record_sample(OID, "owner=95 machine=4 cost=0 findings=20 sourced=19 unresolved=4 value=medium access gap found")
        pipeline.add_evidence(OID, "differentiator", "advertised access vs recorded access, and who settles each gap")
        pipeline.add_evidence(OID, "offer", "$79 sourced buyer's dossier in 3 days; no title opinion or survey")
        pipeline.add_evidence(OID, "legal_ok", "public records; disclaimers; marketplace terms allow it", "https://example.com/terms")

    def start_experiment(self, cap=20):
        text = validation.propose(OID, cap, "list one $79 offer on one marketplace for 30 days")
        aid = re.search(r"yes econ (x-[0-9a-f]{6})", text).group(1)
        return aid, pipeline.resolve(aid, True)


class SampleTests(GateBase):
    def test_a_sample_is_measured_and_judged(self):
        out = validation.record_sample(OID, "owner=95 machine=4 cost=0 findings=20 sourced=12 unresolved=4 value=low repeats free data")
        self.assertIn("only 60% of findings are sourced", out)
        self.assertIn("decision value is low", out)
        self.assertEqual(ledger.hours_by_category(OID), {"production": 1.58})
        self.assertIn("build", [x["kind"] for x in scorecard.evidence(OID)])

    def test_a_sample_needs_the_owner_minutes_a_value_and_a_point(self):
        self.assertIn("owner= is required", validation.record_sample(OID, "machine=4 value=high it is good"))
        self.assertIn("none, low, medium or high", validation.record_sample(OID, "owner=10 value=great it is good stuff"))
        self.assertIn("free tools", validation.record_sample(OID, "owner=10 value=high"))
        self.assertIn("cannot exceed", validation.record_sample(OID, "owner=10 findings=2 sourced=3 value=high real point here"))


class ExperimentTests(GateBase):
    def test_no_experiment_before_the_offer_is_defined(self):
        out = validation.propose(OID, 20, "list one $79 offer on one marketplace for 30 days")
        for item in ("sample", "differentiator", "specific offer", "rules checked"):
            self.assertIn(item, out)
        self.assertEqual(store.rows("SELECT * FROM approvals"), [])

    def test_the_cap_must_fit_the_test_limit(self):
        self.ready_to_test()
        self.assertIn("over your test limit", validation.propose(OID, 500, "list one $79 offer on one marketplace for 30 days"))

    def test_approval_starts_it_and_spending_is_tracked_against_the_cap(self):
        self.ready_to_test()
        aid, out = self.start_experiment(20)
        self.assertIn("is running", out)
        self.assertEqual(pipeline.get(OID)["stage"], "researched")                 # an experiment never moves the stage
        self.assertIn("of the experiment's", hub.handle(*commands.parse(f"spent 15 {OID} listing fee")))
        self.assertIn("Over the experiment", hub.handle(*commands.parse(f"spent 10 {OID} boost")))

    def test_no_demand_means_close_or_change_once_and_compare_the_next(self):
        self.ready_to_test()
        self.start_experiment()
        out = validation.result(OID, "30 days listed, 40 views, no messages, no orders")
        self.assertIn("No demand evidence", out)
        self.assertIn("next-best candidate", out)
        self.assertIn("cannot move researched", pipeline.advance(OID)[0])
        self.assertIn("found no demand", hub.review_text())

    def test_the_whole_gate_opens_only_with_demand_for_this_offer(self):
        self.ready_to_test()
        self.assertIn("5/7", validation.checklist_text(pipeline.get(OID)))           # everything but the experiment and its result
        self.start_experiment()
        pipeline.add_evidence(OID, "customer_interest", "two buyers asked for a quote on the listing", "marketplace inbox")
        validation.result(OID, "2 quote requests in 12 days, 1 order pending")
        self.assertTrue(all(d for _, d in validation.checklist(pipeline.get(OID))))
        self.assertIn("validated", pipeline.advance(OID)[0])

    def test_denied_experiment_does_nothing(self):
        self.ready_to_test()
        text = validation.propose(OID, 20, "list one $79 offer on one marketplace for 30 days")
        aid = re.search(r"yes econ (x-[0-9a-f]{6})", text).group(1)
        self.assertIn("Nothing is contacted", pipeline.resolve(aid, False))
        self.assertIsNone(validation.running(OID))


class HoursAndVariantsTests(GateBase):
    def test_hours_by_category_and_per_hour_over_all_of_them(self):
        hub.handle(*commands.parse(f"hours 2 {OID} acquisition posting the listing"))
        hub.handle(*commands.parse(f"hours 1 {OID} revision"))
        hub.handle(*commands.parse(f"hours 1 {OID} wrote it"))
        self.assertEqual(ledger.hours_by_category(OID), {"acquisition": 2, "revision": 1, "production": 1})
        ledger.add("revenue", 200, OID, "collected")
        self.assertEqual(ledger.venture_totals(OID)["per_hour"], 50)
        self.assertIn("acquisition 2 h", hub.opp_text(OID))

    def test_delivery_and_all_in_economics_are_kept_apart(self):
        ledger.add("revenue", 200, OID, "collected")
        ledger.add("cost", 20, OID, category="direct")
        ledger.add("cost", 30, OID, category="acquisition")
        ledger.log_hours(2, OID, category="production")
        ledger.log_hours(4, OID, category="acquisition")                # incl. pitches that went nowhere
        ledger.log_hours(10, OID, category="engineering")               # building AURIX: visible, never commercial
        t = ledger.venture_totals(OID)
        self.assertEqual(t["delivery_per_hour"], 90)                    # (200 - 20) / 2
        self.assertEqual(t["per_hour"], 25)                             # (200 - 50) / 6
        self.assertEqual(t["engineering_hours"], 10)
        text = hub.opp_text(OID)
        self.assertIn("delivery $90.00/h", text)
        self.assertIn("all-in $25.00/h", text)
        self.assertIn("10 h building (not commercial)", hub.progress_line())

    def test_spending_during_an_experiment_is_acquisition(self):
        self.ready_to_test()
        self.start_experiment()
        hub.handle(*commands.parse(f"spent 5 {OID} listing fee"))
        hub.handle(*commands.parse(f"spent 3 {OID} direct county copy fee"))
        self.assertEqual(ledger.venture_totals(OID)["acquisition_costs"], 5)

    def test_variants_are_added_once_to_an_existing_database(self):
        self.assertIsNotNone(pipeline.get("parcel_screen"))
        self.assertIsNotNone(pipeline.get("parcel_monitor"))
        self.assertEqual(seed.add_variants(), [])

    def test_an_old_hours_table_gets_its_category_column(self):
        path = store.db_path()
        path.unlink()
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE hours (id INTEGER PRIMARY KEY AUTOINCREMENT, opp_id TEXT, hours REAL NOT NULL, "
                     "note TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")
        conn.execute("INSERT INTO hours(opp_id, hours, note, at) VALUES('x', 1.5, '', 1)")
        conn.commit()
        conn.close()
        self.assertEqual(ledger.hours_by_category("x"), {"production": 1.5})

    def test_parse(self):
        self.assertEqual(commands.parse("opp Property_Dossiers experiment $20: list one offer for 30 days on one site"),
                         ("econ_experiment", "property_dossiers\t20\tlist one offer for 30 days on one site"))
        self.assertEqual(commands.parse("opp x_y result: No Orders"), ("econ_result", "x_y\tNo Orders"))
        self.assertEqual(commands.parse("sample x_y: owner=5 value=low Point"), ("econ_sample", "x_y\towner=5 value=low Point"))
        self.assertEqual(freedom.profile()["test_cash"], 50)


if __name__ == "__main__":
    unittest.main()
