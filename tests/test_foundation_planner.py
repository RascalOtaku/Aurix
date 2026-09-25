"""Tests for src/foundation/planner.py."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, mission as ms, planner  # noqa: E402
from src.foundation.risk import RiskTier as T  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)
CT = "Convert CT scan of skull into clean 3d print file and find cheapest option to produce"


class ExtractJsonTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(planner.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(planner.extract_json('Sure! ```json\n{"a": {"b": [1, 2]}}\n``` hope that helps'),
                         {"a": {"b": [1, 2]}})
        self.assertEqual(planner.extract_json('junk {not json} then {"ok": true}'), {"ok": True})
        self.assertEqual(planner.extract_json('{"s": "brace } inside"}'), {"s": "brace } inside"})
        self.assertIsNone(planner.extract_json("no json here"))
        self.assertIsNone(planner.extract_json(""))
        self.assertIsNone(planner.extract_json('{"unterminated": '))

    def test_parse_llm_plan_clamps_and_validates(self):
        raw = json.dumps({"steps": [{"title": "T" * 500, "description": "d" * 900, "capabilities": ["pydicom", 7, ""]},
                                    {"description": "no title"}, "junk", {"title": "ok", "capabilities": "notalist"}] +
                                   [{"title": f"s{i}"} for i in range(40)],
                          "success_criteria": ["a", 3, "b"], "unknowns": "not a list"})
        plan = planner.parse_llm_plan(raw)
        self.assertLessEqual(len(plan["steps"]), planner.MAX_STEPS)
        self.assertEqual(len(plan["steps"][0]["title"]), 120)
        self.assertEqual(len(plan["steps"][0]["description"]), 400)
        self.assertEqual(plan["steps"][0]["capabilities"], ["pydicom"])
        self.assertEqual(plan["steps"][1]["capabilities"], [])
        self.assertEqual(plan["success_criteria"], ["a", "b"])
        self.assertEqual(plan["unknowns"], [])

    def test_garbage_gives_empty_plan(self):
        self.assertEqual(planner.parse_llm_plan("lol"), {"steps": [], "success_criteria": [], "unknowns": []})


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()

    def propose(self, goal, llm=None, **kw):
        return asyncio.run(planner.propose_mission(goal, llm=llm, session_id="tg", **{**NOTHING, **kw}))


class ProposeTests(_Base):
    def test_ct_goal_without_any_llm(self):
        m = self.propose(CT)
        self.assertEqual(m.status, ms.MissionStatus.PROPOSED)
        titles = [s.title for s in m.steps]
        self.assertEqual(titles[0], "Install missing Python packages")
        self.assertIn("Extract the surface", titles)
        self.assertIn("Estimate print cost and gather quotes", titles)
        self.assertIn("pip install --user pydicom", m.steps[0].description)
        self.assertTrue(any("patient-data-consent" in x for x in m.requirements["manual"]))
        self.assertTrue(any("NOT clinical" in x for x in m.requirements["legal"]))
        self.assertIn("watertight STL exported", m.success_criteria)
        self.assertEqual(m.session_id, "tg")
        self.assertEqual(m.objective, CT)

    def test_authority_comes_from_code_not_the_model(self):
        evil = json.dumps({"steps": [{"title": "Take over", "capabilities": ["bash", "root-shell"]}],
                           "risk_ceiling": "HIGH", "allowed_tools": ["python", "send_email"],
                           "prohibited": [], "resources": {"max_cost_usd": 9999}})

        async def llm(system, prompt):
            return evil

        m = self.propose(CT, llm=llm)
        self.assertEqual(m.risk_ceiling, T.MEDIUM)
        self.assertEqual(sorted(m.allowed_tools), sorted(ms.DEFAULT_ALLOWED_TOOLS))
        self.assertEqual(m.resources.max_cost_usd, 0.0)
        for rule in planner.BASE_PROHIBITED:
            self.assertIn(rule, m.prohibited)
        self.assertIn("root-shell", m.requirements["unknown"])           # surfaced, not trusted

    def test_llm_adds_detail_but_cannot_remove_pack_steps_or_legal_gates(self):
        async def llm(system, prompt):
            self.assertIn("pydicom", prompt)                             # it is shown the capability list
            return json.dumps({"steps": [{"title": "Compare shipping to Colorado", "capabilities": ["web_search"]}],
                               "success_criteria": ["owner picks a vendor"]})

        m = self.propose(CT, llm=llm)
        titles = [s.title for s in m.steps]
        self.assertIn("Compare shipping to Colorado", titles)
        self.assertIn("Extract the surface", titles)
        self.assertIn("owner picks a vendor", m.success_criteria)
        self.assertTrue(any("NOT clinical" in x for x in m.requirements["legal"]))

    def test_broken_llm_falls_back_to_packs(self):
        async def boom(system, prompt):
            raise RuntimeError("model down")

        m = self.propose(CT, llm=boom)
        self.assertIn("Extract the surface", [s.title for s in m.steps])

    def test_unknown_goal_asks_for_clarification_instead_of_guessing(self):
        m = self.propose("bake a cake")
        self.assertEqual([s.title for s in m.steps], ["Clarify the goal with the owner"])
        self.assertEqual(m.success_criteria, ["the owner confirms the result"])

    def test_unknown_goal_uses_a_valid_llm_plan(self):
        async def llm(system, prompt):
            return json.dumps({"steps": [{"title": "Find a recipe", "capabilities": ["web_search"]},
                                         {"title": "Preheat the oven", "capabilities": ["oven-control"]}]})

        m = self.propose("bake a cake", llm=llm)
        self.assertEqual([s.title for s in m.steps], ["Find a recipe", "Preheat the oven"])
        self.assertEqual(m.requirements["unknown"], ["oven-control"])

    def test_trading_contract_forbids_the_live_endpoint(self):
        m = self.propose("Daily stock trading")
        import re
        self.assertTrue(any(re.search(p, "curl https://api.alpaca.markets/v2/orders") for p in m.prohibited_patterns))
        self.assertFalse(any(re.search(p, "curl https://paper-api.alpaca.markets/v2/orders")
                             for p in m.prohibited_patterns))
        self.assertTrue(any("live-trading" in x for x in m.requirements["manual"]))

    def test_real_estate_carries_the_license_warning_and_no_contact_rule(self):
        m = self.propose("Finding homes for sale and taking cut off")
        self.assertTrue(any("Colorado" in x for x in m.requirements["legal"]))
        self.assertIn("Do not collect any fee.", m.prohibited)

    def test_multi_goal_merges_packs(self):
        m = self.propose("transcribe interviews and post shorts on youtube")
        self.assertEqual(set(m.requirements["packs"]), {"transcription", "social_content"})

    def test_proposal_is_persisted_audited_and_renderable(self):
        m = self.propose(CT)
        back = ms.MissionStore().load(m.id)
        self.assertEqual(back.objective, CT)
        self.assertEqual(audit.recent(1)[0]["event"], "mission_proposed")
        text = ms.render_proposal(m)
        self.assertIn(f"approve mission {m.id}", text)
        self.assertLessEqual(len(text), 3600)

    def test_empty_goal_rejected(self):
        with self.assertRaises(ValueError):
            self.propose("   ")

    def test_no_installable_no_install_step(self):
        ok = dict(NOTHING, find_spec=lambda m: object())
        m = self.propose("Reynolds Gang ongoing treasure hunt", **ok)
        self.assertNotIn("Install missing Python packages", [s.title for s in m.steps])


class ModelCannotDemandCredentialsTests(_Base):
    """2026-09-20: the local model listed `alpaca-credentials` for "print the first 10 primes"; it became a hard requirement and the
    approved mission was blocked at preflight. A model may add tools/packages/binaries, never credentials, authorizations or services."""
    PRIMES = "write a python script that prints the first 10 primes run it and report the output"

    def llm_saying(self, steps, unknowns=()):
        async def llm(system, prompt):
            return json.dumps({"steps": steps, "success_criteria": ["prints 10 primes"], "unknowns": list(unknowns)})
        return llm

    def test_hallucinated_credentials_do_not_become_requirements(self):
        llm = self.llm_saying([{"title": "Write the script", "capabilities": ["alpaca-credentials", "alpaca-py", "bash"]}],
                              unknowns=["alpaca-credentials", "which python version?"])
        m = self.propose(self.PRIMES, llm=llm)
        self.assertFalse(any("alpaca" in x for x in m.requirements["manual"]), m.requirements["manual"])
        self.assertNotIn("alpaca-credentials", m.requirements["capabilities"])
        self.assertNotIn("alpaca-credentials", [c for s in m.steps for c in s.capabilities])
        self.assertIn("which python version?", m.requirements["notes"])            # open questions are shown to the owner as notes...
        self.assertFalse(m.requirements["unknown"])                                # ...and are never requirements

    def test_a_models_none_is_not_an_unknown_capability(self):
        """Found by the eval runner with the live model: `"unknowns": ["None"]` became an unknown capability (max risk) and blocked the
        fast lane on every mission."""
        for empties in (["None"], ["n/a", "none", ""], ["?"]):
            llm = self.llm_saying([{"title": "Write the script", "capabilities": ["None", "bash"]}], unknowns=empties)
            m = self.propose(self.PRIMES, llm=llm)
            self.assertEqual(m.requirements["unknown"], [], empties)
            self.assertEqual(m.requirements["notes"], [], empties)
            self.assertNotIn("None", [c for s in m.steps for c in s.capabilities])

    def test_model_may_add_packages_that_are_already_present(self):
        llm = self.llm_saying([{"title": "Read the data", "capabilities": ["pandas", "bash"]}])
        m = self.propose(self.PRIMES, llm=llm, find_spec=lambda mod: object())     # everything importable
        self.assertIn("pandas", m.requirements["capabilities"])

    def test_model_cannot_add_a_package_that_is_not_present(self):
        """A hallucinated `alpaca-py` for a primes script must not become an install requirement (found by the eval runner)."""
        llm = self.llm_saying([{"title": "Write the script", "capabilities": ["alpaca-py", "pandas", "bash"]}])
        m = self.propose(self.PRIMES, llm=llm)                                     # NOTHING is installed
        self.assertNotIn("alpaca-py", m.requirements["capabilities"])
        self.assertNotIn("pandas", m.requirements["capabilities"])
        self.assertFalse(m.requirements["installable"])
        self.assertFalse(m.requirements["owner_install"] and any("alpaca" in x for x in m.requirements["owner_install"]))

    def test_a_real_trading_goal_still_needs_its_credentials_from_the_pack(self):
        m = self.propose("Run daily stock trading on my portfolio")
        self.assertTrue(any("alpaca" in x for x in m.requirements["manual"]), m.requirements["manual"])

    def test_screen_keeps_unknown_names_and_drops_gated_kinds(self):
        out = planner.screen_model_capabilities(["alpaca-credentials", "bash", "totally-made-up", "patient-data-consent", "alpaca-py"],
                                                **NOTHING)
        self.assertEqual(out, ["bash", "totally-made-up"])
        present = planner.screen_model_capabilities(["alpaca-py", "alpaca-credentials"], **{**NOTHING, "find_spec": lambda m: object()})
        self.assertEqual(present, ["alpaca-py"])                                    # present packages are fine; credentials never

    def test_steps_that_only_restate_a_pack_step_are_dropped(self):
        llm = self.llm_saying([{"title": "Understand the task"}, {"title": "Verify the output"}, {"title": "Implement with the coding agent"},
                               {"title": "Write a haiku about primes"}])
        m = self.propose(self.PRIMES, llm=llm)
        titles = [s.title for s in m.steps]
        self.assertEqual(len(titles), len({t.lower() for t in titles}))
        self.assertNotIn("Understand the task", titles)                             # restates "Understand the task and the code it touches"
        self.assertNotIn("Verify the output", titles)                               # restates "Verify"
        self.assertIn("Write a haiku about primes", titles)                         # genuinely new detail survives

    def test_restates_helper(self):
        self.assertTrue(planner.restates("Understand the task", ["Understand the task and the code it touches"]))
        self.assertTrue(planner.restates("", ["anything"]))
        self.assertFalse(planner.restates("Capture output", ["Understand the task", "Verify"]))


if __name__ == "__main__":
    unittest.main()
