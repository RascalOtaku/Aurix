"""The buyer's dossier never says more than the evidence: confirmed vs indicated vs open, who resolves each, and the scope note."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.foundation.land import buyer, store  # noqa: E402
from test_foundation_land import _build, _clean_raw, _replace  # noqa: E402


def raw_with_gaps():
    raw = _clean_raw()
    raw["sources"] += [{"id": "GIS", "type": "official_secondary", "title": "County GIS", "as_of": "2026-09-20",
                        "locator": "https://gis.example.gov/p/1"},
                       {"id": "LST", "type": "third_party", "title": "Listing", "as_of": "2026-09-20",
                        "locator": "https://listing.example.com/1"},
                       {"id": "AI", "type": "llm_report", "title": "Model research"}]
    _replace(raw, "legal_access", value="frontage on Route 1", sources=["GIS"])          # official, but not the deciding record
    raw["claims"] = [c for c in raw["claims"] if c["field"] not in ("title_chain", "zoning_district")]
    raw["claims"].append({"field": "zoning_district", "value": "A-1", "sources": ["AI"]})  # only a model said so
    raw["claims"].append({"field": "physical_access", "value": "dirt track", "sources": ["LST"], "as_of": "2026-09-20"})
    raw["claims"].append({"field": "acreage", "value": 5.2, "sources": ["LST"], "as_of": "2026-09-20"})   # disagrees with DOC
    return raw


class BuyerDossierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.d = _build(raw_with_gaps())
        self.text = buyer.render(self.d)

    def section(self, title):
        return self.text.split(f"## {title}")[1].split("\n## ")[0]

    def test_scope_is_stated_at_top_and_bottom(self):
        self.assertEqual(self.text.count("It is NOT a title opinion"), 2)
        for n in range(1, 8):
            self.assertIn(f"## {n}. ", self.text)

    def test_an_official_map_is_not_the_recorded_access(self):
        access = self.section("3. Legal access")
        self.assertIn("Apparent access is not recorded access", access)
        self.assertIn("not yet confirmed by the deciding record", access)
        self.assertNotIn("Confirmed in official records", access)

    def test_a_model_report_is_a_hypothesis_with_who_resolves_it(self):
        use = self.section("4. Land use")
        self.assertIn("a hypothesis to check, not a fact", use)
        self.assertIn("planning", use)

    def test_discrepancies_and_unknowns_are_open_questions(self):
        self.assertIn("records DISAGREE", self.section("1. Parcel identity"))
        self.assertIn("Title chain completeness", self.section("2. Ownership and title"))
        self.assertIn("Not enough evidence to decide yet", self.text)

    def test_metrics_measure_the_sample(self):
        m = buyer.metrics(self.d)
        self.assertEqual(m["discrepancies"], 1)
        self.assertEqual(m["unresolved"], 4)               # acreage conflict, title chain, legal access below primary, zoning
        self.assertLess(m["official"], m["findings"])

    def test_command_saves_the_report_and_prints_a_sample_line(self):
        store.save(self.d)
        text, path = buyer.command("1_2_3")
        self.assertTrue(path.exists())
        self.assertIn("sample property_dossiers: owner=??", text)
        self.assertIn("unresolved=4", text)


if __name__ == "__main__":
    unittest.main()
