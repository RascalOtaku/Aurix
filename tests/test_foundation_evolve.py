"""Tests for src/foundation/evolve.py (the generic GA/PBT engine) and evolve_domains.py (the first domain plugged into it:
forge.FORGE_SYSTEM's evolvable guidance text). Everything is offline: the "LLM" is a plain async stub, the "sandbox" runner
is never actually invoked by these tests because evaluate() is exercised directly with a fake evals.run_code_tier."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, evolve  # noqa: E402


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()
        # _DOMAINS is a process-wide module global, not per-test state (same class of bug as strategy_evolve's
        # module globals, see test_strategy_evolve.py's _patch_module_globals): clearing it without restoring
        # permanently wipes the real "forge_guidance" registration for every OTHER test file that runs later in
        # this same process (import caching means re-importing evolve_domains afterward does not re-register it).
        self._real_domains = dict(evolve._DOMAINS)
        evolve._DOMAINS.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        evolve._DOMAINS.clear()
        evolve._DOMAINS.update(self._real_domains)


def _numeric_domain(name="toy", target=0.77):
    """A trivial numeric-genome domain (payload is a single float): fitness is how close it gets to `target`. Cheap and
    fully deterministic, so the GA/PBT loop itself can be tested without any real model or sandbox call."""
    import random

    async def evaluate(payload, llm):
        return (1.0 - abs(payload - target), {"payload": payload})

    async def mutate(payload, llm):
        return max(0.0, min(1.0, payload + random.uniform(-0.1, 0.1)))

    async def crossover(a, b, llm):
        return (a + b) / 2

    return evolve.Domain(name=name, random_genome=lambda: random.uniform(0, 1), mutate=mutate, crossover=crossover,
                         evaluate=evaluate, describe=lambda p: f"payload={p:.3f}", worth_promoting=lambda f: f > 0.9,
                         apply=lambda p: None, pop_size=5, keep_top=2)


class EngineTests(Base):
    async def test_advance_never_lets_the_best_fitness_get_worse(self):
        evolve.register(_numeric_domain())
        pop1 = await evolve.advance("toy", None, generations=1)
        best1 = pop1["genomes"][0]["fitness"]
        pop2 = await evolve.advance("toy", None, generations=5)
        best2 = pop2["genomes"][0]["fitness"]
        self.assertGreaterEqual(best2, best1 - 1e-9)

    async def test_population_persists_between_calls(self):
        evolve.register(_numeric_domain())
        await evolve.advance("toy", None, generations=1)
        reloaded = evolve._load_pop("toy")
        self.assertEqual(len(reloaded["genomes"]), 5)
        self.assertEqual(reloaded["generation"], 1)

    async def test_unregistered_domain_is_refused_not_silently_ignored(self):
        with self.assertRaises(KeyError):
            await evolve.advance("nope", None)

    async def test_propose_promotion_refuses_a_genome_that_is_not_worth_it(self):
        d = _numeric_domain(target=0.0)
        evolve.register(d)
        evolve._save_pop("toy", {"generation": 1, "genomes": [{"payload": 0.5, "fitness": 0.3, "meta": {}, "generation": 1}]})
        msg = evolve.propose_promotion("toy")
        self.assertIn("isn't clearly better yet", msg)
        self.assertEqual(evolve.pending("toy"), [])

    async def test_propose_promotion_makes_a_card_once_the_bar_is_cleared(self):
        evolve.register(_numeric_domain())
        evolve._save_pop("toy", {"generation": 3, "genomes": [{"payload": 0.77, "fitness": 0.98, "meta": {}, "generation": 3}]})
        msg = evolve.propose_promotion("toy")
        self.assertIn("evolve e-", msg)
        self.assertEqual(len(evolve.pending("toy")), 1)

    async def test_approving_a_card_calls_the_domains_apply_hook(self):
        applied = []
        d = _numeric_domain()
        d = evolve.Domain(**{**d.__dict__, "apply": lambda p: applied.append(p)})
        evolve.register(d)
        evolve._save_pop("toy", {"generation": 1, "genomes": [{"payload": 0.77, "fitness": 0.95, "meta": {}, "generation": 1}]})
        evolve.propose_promotion("toy")
        pid = evolve.pending("toy")[0]["id"]
        msg = evolve.approve("toy", pid)
        self.assertIn("OWNER APPROVED", msg)
        self.assertEqual(applied, [0.77])
        self.assertEqual(evolve.pending("toy"), [])

    async def test_declining_a_card_never_calls_apply(self):
        applied = []
        d = _numeric_domain()
        d = evolve.Domain(**{**d.__dict__, "apply": lambda p: applied.append(p)})
        evolve.register(d)
        evolve._save_pop("toy", {"generation": 1, "genomes": [{"payload": 0.77, "fitness": 0.95, "meta": {}, "generation": 1}]})
        evolve.propose_promotion("toy")
        pid = evolve.pending("toy")[0]["id"]
        msg = evolve.decline("toy", pid)
        self.assertIn("Declined", msg)
        self.assertEqual(applied, [])

    async def test_deciding_an_already_decided_card_is_refused(self):
        evolve.register(_numeric_domain())
        evolve._save_pop("toy", {"generation": 1, "genomes": [{"payload": 0.77, "fitness": 0.95, "meta": {}, "generation": 1}]})
        evolve.propose_promotion("toy")
        pid = evolve.pending("toy")[0]["id"]
        evolve.approve("toy", pid)
        msg = evolve.approve("toy", pid)
        self.assertIn("already approved", msg)

    async def test_approve_any_finds_the_right_domain_without_being_told(self):
        evolve.register(_numeric_domain("alpha"))
        evolve.register(_numeric_domain("beta"))
        evolve._save_pop("beta", {"generation": 1, "genomes": [{"payload": 0.77, "fitness": 0.95, "meta": {}, "generation": 1}]})
        evolve.propose_promotion("beta")
        pid = evolve.pending("beta")[0]["id"]
        msg = evolve.approve_any(pid)
        self.assertIn("OWNER APPROVED", msg)

    async def test_approve_any_with_an_unknown_id_is_refused_not_a_crash(self):
        evolve.register(_numeric_domain())
        msg = evolve.approve_any("e-000000")
        self.assertIn("No pending evolve card", msg)

    def test_bad_id_shape_is_refused(self):
        evolve.register(_numeric_domain())
        self.assertIn("not an evolve card id", evolve.approve("toy", "not-an-id"))

    def test_panel_lists_every_domain_and_every_pending_card(self):
        evolve.register(_numeric_domain("alpha"))
        evolve.register(_numeric_domain("beta"))
        evolve._save_pop("beta", {"generation": 2, "genomes": [{"payload": 0.77, "fitness": 0.95, "meta": {}, "generation": 2}]})
        evolve.propose_promotion("beta")
        p = evolve.panel()
        self.assertEqual({d["domain"] for d in p["domains"]}, {"alpha", "beta"})
        beta = next(d for d in p["domains"] if d["domain"] == "beta")
        self.assertEqual(beta["generation"], 2)
        self.assertAlmostEqual(beta["best_fitness"], 0.95)
        self.assertEqual(len(p["pending"]), 1)
        self.assertEqual(p["pending"][0]["domain"], "beta")

    def test_panel_with_no_domains_registered_is_empty_not_an_error(self):
        self.assertEqual(evolve.panel(), {"domains": [], "pending": []})

    def test_command_center_snapshot_carries_the_evolve_panel(self):
        from src.foundation import command_center
        evolve.register(_numeric_domain())
        snap = command_center.snapshot()
        self.assertIn("evolve", snap)
        # command_center's own import of evolve_domains registers "forge_guidance" too - just check ours made it in.
        self.assertIn("toy", {d["domain"] for d in snap["evolve"]["domains"]})


class SelfImprovePromptsDomainTests(Base):
    """evolve_domains.py wires forge.FORGE_SYSTEM's guidance through evals.run_code_tier for real fitness. These tests stub
    evals.run_code_tier itself (never touching the sandbox) so only the WIRING - genome -> fitness -> apply -> forge read
    back - is under test."""

    def test_live_guidance_is_empty_until_something_is_applied(self):
        from src.foundation import evolve_domains
        self.assertEqual(evolve_domains.live_guidance(), "")

    def test_apply_writes_the_file_forge_reads_back(self):
        from src.foundation import evolve_domains
        evolve_domains._apply("always check for off-by-one errors")
        self.assertEqual(evolve_domains.live_guidance(), "always check for off-by-one errors")

    async def test_evaluate_turns_a_real_pass_rate_into_fitness(self):
        from src.foundation import evolve_domains

        async def fake_run_code_tier(tasks, llm, guidance=""):
            return [{"passed": True, "failure_class": ""}, {"passed": False, "failure_class": "hidden_case_failed"}]
        with mock.patch("src.foundation.evals.load_tasks", return_value=[{"id": "t1"}, {"id": "t2"}]), \
             mock.patch("src.foundation.evals.run_code_tier", side_effect=fake_run_code_tier):
            fitness, meta = await evolve_domains._evaluate("some guidance", None)
        self.assertAlmostEqual(fitness, 0.5)
        self.assertEqual(meta["passed"], 1)
        self.assertEqual(meta["total"], 2)

    async def test_evaluate_with_no_tasks_is_zero_not_a_crash(self):
        from src.foundation import evolve_domains
        with mock.patch("src.foundation.evals.load_tasks", return_value=[]):
            fitness, meta = await evolve_domains._evaluate("x", None)
        self.assertEqual(fitness, 0.0)
        self.assertIn("error", meta)

    async def test_mutate_with_no_llm_returns_the_genome_unchanged(self):
        from src.foundation import evolve_domains
        self.assertEqual(await evolve_domains._mutate("keep me", None), "keep me")

    async def test_mutate_calls_the_llm_and_truncates_to_400_chars(self):
        from src.foundation import evolve_domains

        async def llm(system, prompt):
            return "x" * 500
        out = await evolve_domains._mutate("old guidance", llm)
        self.assertEqual(len(out), 400)

    async def test_crossover_with_no_llm_falls_back_to_parent_a(self):
        from src.foundation import evolve_domains
        self.assertEqual(await evolve_domains._crossover("a-text", "b-text", None), "a-text")

    def test_worth_promoting_bar(self):
        from src.foundation import evolve_domains
        self.assertFalse(evolve_domains._worth_promoting(0.49))
        self.assertTrue(evolve_domains._worth_promoting(0.5))

    def test_describe_handles_empty_guidance(self):
        from src.foundation import evolve_domains
        self.assertIn("no extra guidance", evolve_domains._describe(""))
        self.assertIn("no extra guidance", evolve_domains._describe("   "))

    async def test_forge_propose_appends_the_live_guidance_to_the_system_prompt(self):
        from src.foundation import evolve_domains, forge, sandbox
        evolve_domains._apply("never forget the trailing newline")
        seen_systems = []

        async def llm(system, prompt):
            seen_systems.append(system)
            return ("widget\ndoes a thing\n=== skill.py ===\ndef run(args):\n    return {}\n"
                    "=== test_skill.py ===\nimport unittest\nclass T(unittest.TestCase):\n    def test_x(self): pass\n")
        with mock.patch.object(sandbox, "available", return_value=True), \
             mock.patch.object(forge, "run_tests", return_value={"ok": True, "failures": 0, "errors": 0, "log": ""}):
            await forge.propose("a widget that does a thing", llm)
        self.assertTrue(any("never forget the trailing newline" in s for s in seen_systems))


class CrossCuttingWiringTests(Base):
    """The exact checklist this project already learned to run on every new pending-decision type (see
    aurix-return-from-trip-2026-09-30's LandPilot finding): a pending evolve card must show up as a dashboard decision
    card, in the morning digest, and in the weekly trust tally - not just in its own module. Run proactively here instead
    of being discovered as a gap later."""

    def _pending_card(self):
        evolve.register(_numeric_domain())
        evolve._save_pop("toy", {"generation": 1, "genomes": [{"payload": 0.77, "fitness": 0.95, "meta": {}, "generation": 1}]})
        evolve.propose_promotion("toy")
        return evolve.pending("toy")[0]

    def test_shows_up_as_a_dashboard_decision_card(self):
        from src.foundation import actions
        c = self._pending_card()
        cards = [x for x in actions.decisions() if x["kind"] == "evolve"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], c["id"])
        self.assertEqual([b["action"] for b in cards[0]["buttons"]], ["evolve_yes", "evolve_no"])
        self.assertIn("evolve_yes", actions.ACTION_NAMES)
        self.assertIn("OWNER APPROVED", actions.run_action("evolve_yes", c["id"])["message"])

    def test_shows_up_in_the_morning_digest(self):
        from src.foundation import heartbeat
        self._pending_card()
        text = heartbeat.waiting_for_you()
        self.assertIn("Evolved toy", text)
        self.assertIn("yes evolve e-", text)

    def test_a_declined_card_counts_toward_the_weekly_track_record(self):
        from src.foundation import growth
        c = self._pending_card()
        before = growth.trust()["week"]["you_denied"]
        evolve.decline("toy", c["id"])
        self.assertEqual(growth.trust()["week"]["you_denied"], before + 1)

    def test_an_approved_card_counts_toward_the_weekly_track_record(self):
        from src.foundation import growth
        c = self._pending_card()
        before = growth.trust()["week"]["asked_and_you_approved"]
        evolve.approve("toy", c["id"])
        self.assertEqual(growth.trust()["week"]["asked_and_you_approved"], before + 1)


if __name__ == "__main__":
    unittest.main()
