"""Tests for src/foundation/fastlane.py and its wiring in commands.Foundation (owner decision 2026-09-20: read-only / sandbox-only
missions may start without the approval tap; everything else still asks)."""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, fastlane, mission as ms, planner, sandbox  # noqa: E402

PRESENT = dict(env={}, which=lambda b: "/usr/bin/" + b, find_spec=lambda m: object(), authorizations={}, probe=lambda h, p: True)
NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)
PRIMES = "write a python script that prints the first 10 primes run it and report the output"


def propose(goal, sandboxed=True, **kw):
    return asyncio.run(planner.propose_mission(goal, llm=None, session_id="tg", sandboxed=sandboxed, **{**PRESENT, **kw}))


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        os.environ.pop("AURIX_FASTLANE", None)
        ag.reset_state()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()


class CheckTests(_Base):
    def test_sandboxed_code_goal_qualifies(self):
        ok, why = fastlane.check(propose(PRIMES))
        self.assertTrue(ok, why)
        self.assertEqual(why[0], "sandbox-only")

    def test_unsandboxed_code_goal_does_not(self):
        ok, why = fastlane.check(propose(PRIMES, sandboxed=False))
        self.assertFalse(ok)
        self.assertIn("outside the isolated sandbox", " ".join(why))

    def test_unrecognised_read_only_goals_now_ask(self):
        """UPDATED DELIBERATELY (owner rule, 2026-09-29): unknown domain fails closed. These used to qualify as "read-only"; no domain
        pack recognises them, so they now wait for a normal approval tap. Security over fast-lane convenience."""
        for goal in ("find the current weather in Denver", "list the biggest files in my workspace"):
            ok, why = fastlane.check(propose(goal))
            self.assertFalse(ok, goal)
            self.assertIn("no recognised domain pack", " ".join(why))

    def test_outward_or_destructive_goals_never_qualify(self):
        for goal in ("build a script that emails me the primes", "write a python script and deploy it to my server",
                     "write a script that deletes old files", "write a script that uses my api key to post on twitter"):
            ok, why = fastlane.check(propose(goal))
            self.assertFalse(ok, goal)
            self.assertIn("outward or destructive", " ".join(why))

    def test_gated_domains_never_qualify(self):
        for goal in ("Run daily stock trading on my portfolio", "convert my CT scan of a skull into an STL",
                     "find bug bounty programs on hackerone", "find homes for sale and take a finder's fee"):
            ok, why = fastlane.check(propose(goal))
            self.assertFalse(ok, goal)

    def test_anything_the_owner_must_provide_or_install_blocks_it(self):
        ok, why = fastlane.check(propose(PRIMES, **NOTHING))               # nothing installed: openhands etc. need the owner
        self.assertFalse(ok)

    def test_a_model_cannot_smuggle_a_gated_capability_in(self):
        async def llm(system, prompt):
            return '{"steps": [{"title": "Look things up", "capabilities": ["alpaca-credentials", "browser"]}], "unknowns": []}'
        m = asyncio.run(planner.propose_mission(PRIMES, llm=llm, session_id="tg", sandboxed=True, **PRESENT))
        ok, why = fastlane.check(m)
        self.assertFalse(any("alpaca" in w for w in why))                   # credential was screened out by the planner, so not a blocker

    def test_a_model_written_step_that_reaches_outside_blocks_it(self):
        async def llm(system, prompt):
            return '{"steps": [{"title": "Report", "description": "Email the answer to the owner", "capabilities": ["bash"]}], "unknowns": []}'
        m = asyncio.run(planner.propose_mission(PRIMES, llm=llm, session_id="tg", sandboxed=True, **PRESENT))
        ok, why = fastlane.check(m)
        self.assertFalse(ok)
        self.assertIn("outward or destructive", " ".join(why))

    def test_the_homestead_mission_of_2026_09_28_is_refused(self):
        """Replays m-fd28e3 exactly as the local planner wrote it. It was auto-approved: no pack matched, the authorizations it
        named had empty capability lists, and "consent from property owners" sat in the success criteria, which were not scanned."""
        m = propose("Aurix homestead finder - pulls records and loan delinquencies to find a realistic affordable homestead option "
                    "within 5-10 acres of land, derelict building and water feature is preferred. We only have 2-5K to use so must "
                    "be within affordable options")
        m.success_criteria = ["Found at least one affordable property within the budget",
                              "Checked delinquency records of all found properties",
                              "Gathered consent from property owners to check their delinquency records",
                              "Compiled a detailed report with all viable homestead options and legal status"]
        m.steps = [ms.Step(id=f"s{i}", title=t, description=d, capabilities=c) for i, (t, d, c) in enumerate([
            ("Search for affordable homestead options", "Use web_search to find real estate listings within 5-10 acres.", ["web_search"]),
            ("Check delinquency records", "Use alpaca-py or live-trading to check loan delinquencies of each property's owner.", []),
            ("Review the compiled list", "Use real-estate-license to review each property's legal status, zoning laws.", []),
            ("Finalize the list", "Use patient-data-consent to ensure that each property owner consents.", []),
            ("Present the findings", "Use write_file to present a detailed report.", ["write_file"])], 1)]
        ok, why = fastlane.check(m)
        self.assertFalse(ok)
        reasons = " | ".join(why)
        for expected in ("legally gated domain", "consent", "names live-trading", "names real-estate-license", "names patient-data-consent"):
            self.assertIn(expected, reasons)

    def test_each_new_rule_blocks_on_its_own(self):
        """UPDATED DELIBERATELY: the control case used to be a weather lookup, which no longer qualifies (unknown domain). The
        control is now the recognised software_dev goal PRIMES, and each rule is shown to block it on its own."""
        self.assertTrue(fastlane.check(propose(PRIMES))[0])                                  # control
        for goal in ("find properties with delinquent taxes near Denver",                  # unknown domain (and gated words)
                     PRIMES + " and contact the forecaster"):                               # contact is outward
            ok, why = fastlane.check(propose(goal))
            self.assertFalse(ok, (goal, why))
        m = propose(PRIMES)
        m.success_criteria = ["reached out to the station for comment"]                    # criteria are scanned
        self.assertFalse(fastlane.check(m)[0])

    def test_high_risk_ceiling_blocks(self):
        m = propose(PRIMES)
        m.risk_ceiling = type(m.risk_ceiling)(3)
        ok, why = fastlane.check(m)
        self.assertFalse(ok)
        self.assertIn("risk ceiling", " ".join(why))

    def test_empty_plan_blocks(self):
        m = propose(PRIMES)
        m.steps = []
        self.assertFalse(fastlane.check(m)[0])


class SwitchTests(_Base):
    def test_default_on_and_toggle_is_audited(self):
        self.assertTrue(fastlane.enabled())
        self.assertIn("OFF", fastlane.set_enabled(False))
        self.assertFalse(fastlane.enabled())
        self.assertIn("ON", fastlane.set_enabled(True))
        self.assertTrue(fastlane.enabled())
        self.assertEqual([r["event"] for r in audit.recent(5)].count("fast_lane_set"), 2)

    def test_env_forces_off(self):
        with mock.patch.dict(os.environ, {"AURIX_FASTLANE": "0"}):
            self.assertFalse(fastlane.enabled())

    def test_corrupt_state_file_falls_back_to_on(self):
        p = fastlane.state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertTrue(fastlane.enabled())


class ParseTests(unittest.TestCase):
    def test_parse(self):
        for text, want in (("fast lane", ""), ("Fast Lane on", "on"), ("fastlane off", "off"), ("/fast-lane status", "status")):
            kind, arg = commands.parse(text)
            self.assertEqual((kind, arg or ""), ("fastlane", want), text)
        self.assertIsNone(commands.parse("fast lane is great"))


class SecurityBoundary(_Base):
    """The fast-lane security boundary, 2026-09-29. Recognised domain or it asks; nothing named-but-undeclared; no outward action;
    and checking never changes anything."""

    def reasons(self, m):
        ok, why = fastlane.check(m)
        return ok, " | ".join(why)

    def test_recognised_domain_and_valid_goal_keeps_existing_behaviour(self):
        ok, why = self.reasons(propose(PRIMES))
        self.assertTrue(ok, why)
        self.assertIn("sandbox-only", why)

    def test_unknown_domain_is_rejected(self):
        ok, why = self.reasons(propose("research people who live alone in my neighborhood and their daily routines"))
        self.assertFalse(ok)
        self.assertIn("no recognised domain pack", why)

    def test_no_domain_pack_even_if_requirements_claim_one(self):
        m = propose("summarise the history of the printing press")
        m.requirements = dict(m.requirements or {}, packs=["software_dev"])               # a recorded pack alone is not recognition
        ok, why = self.reasons(m)
        self.assertFalse(ok)
        self.assertIn("no recognised domain pack", why)

    def test_ambiguous_domain_is_rejected(self):
        ok, why = self.reasons(propose("write a python script that lists homes for sale near Denver"))
        self.assertFalse(ok)
        self.assertIn("ambiguous domain", why)

    def test_hidden_authorization_in_text_is_rejected(self):
        m = propose(PRIMES)
        m.steps[0].description += " then double-check the result with live-trading"
        ok, why = self.reasons(m)
        self.assertFalse(ok)
        self.assertIn("names live-trading", why)

    def test_undeclared_capability_named_in_text_is_rejected(self):
        m = propose(PRIMES)
        m.steps[0].description += " and run nmap against the result"
        ok, why = self.reasons(m)
        self.assertFalse(ok)
        self.assertIn("names nmap in its plan without declaring it", why)

    def test_valid_declared_authorization_keeps_existing_behaviour(self):
        m = propose(PRIMES)
        m.steps[0].capabilities = list(m.steps[0].capabilities) + ["live-trading"]        # declared: still the owner's call
        ok, why = self.reasons(m)
        self.assertFalse(ok)
        self.assertIn("live-trading is a authorization", why)

    def test_outward_action_in_success_criteria_is_rejected(self):
        m = propose(PRIMES)
        m.success_criteria = ["results emailed to the team"]
        self.assertFalse(fastlane.check(m)[0])

    def test_contact_reach_out_and_consent_are_rejected_without_authorization(self):
        for extra in ("contact the owner of each result", "reach out to the maintainers", "obtain consent from each user",
                      "reached out to the station", "call the owners"):
            ok, why = self.reasons(propose(f"{PRIMES} and {extra}"))
            self.assertFalse(ok, extra)
            self.assertIn("outward or destructive action", why)

    def test_outward_action_in_a_step_is_rejected_without_its_capability(self):
        m = propose(PRIMES)
        m.steps[-1].description += " Then notify the maintainers of the result."
        ok, why = self.reasons(m)
        self.assertFalse(ok)
        self.assertIn("outward or destructive action", why)

    def test_a_rejected_check_mutates_nothing(self):
        import copy
        from dataclasses import asdict
        m = propose("research people who live alone in my neighborhood")
        before_m, before_head = copy.deepcopy(asdict(m)), audit.head()
        data = os.path.join(self.tmp.name, "data")
        before_files = sorted(os.listdir(data)) if os.path.isdir(data) else []
        for _ in range(3):
            self.assertFalse(fastlane.check(m)[0])
        self.assertEqual(asdict(m), before_m)
        self.assertEqual(audit.head(), before_head)
        self.assertEqual(sorted(os.listdir(data)) if os.path.isdir(data) else [], before_files)
        self.assertFalse(os.path.exists(os.path.join(data, "authorizations.json")))

    def test_existing_read_only_protection_still_works(self):
        ok, why = self.reasons(propose(PRIMES, sandboxed=False))
        self.assertFalse(ok)
        self.assertIn("outside the isolated sandbox", why)


class HandlerTests(_Base):
    def setUp(self):
        super().setUp()

        async def notify(t):
            pass

        async def agent(p):
            return "STEP DONE: ok"

        self.f = commands.Foundation(agent, notify, llm=None, session_id="tg", **PRESENT)
        self.started = []

        async def fake_start(mid):
            self.started.append(mid)
        self.f._start_runner = fake_start
        p = mock.patch.object(sandbox, "available", return_value=True)
        p.start()
        self.addCleanup(p.stop)

    async def test_eligible_mission_starts_at_once_and_is_audited(self):
        reply = await self.f.handle("new", PRIMES)
        self.assertIn("Fast lane", reply)
        self.assertNotIn("approve mission", reply)
        [m] = self.f.store.all()
        self.assertEqual(m.status, ms.MissionStatus.ACTIVE)
        self.assertEqual(m.decided_by, "policy:fast_lane")
        self.assertEqual(self.started, [m.id])
        events = [r["event"] for r in audit.recent(6)]
        self.assertIn("fast_lane_started", events)
        self.assertIn("mission_approved", events)
        approved = next(r for r in audit.recent(6) if r["event"] == "mission_approved")
        self.assertEqual(approved["by"], "policy:fast_lane")

    async def test_unknown_domain_mission_is_proposed_never_approved_or_running(self):
        reply = await self.f.handle("new", "research people who live alone in my neighborhood and their daily routines")
        self.assertIn("approve mission", reply)
        [m] = self.f.store.all()
        self.assertEqual(m.status, ms.MissionStatus.PROPOSED)
        self.assertFalse(m.decided_by)                                                     # nobody decided it
        self.assertEqual(self.started, [])
        events = [r["event"] for r in audit.recent(20)]
        self.assertNotIn("fast_lane_started", events)
        self.assertNotIn("mission_approved", events)

    async def test_fast_lane_off_means_the_usual_proposal(self):
        fastlane.set_enabled(False)
        reply = await self.f.handle("new", PRIMES)
        self.assertIn("approve mission", reply)
        self.assertEqual(self.f.store.all()[0].status, ms.MissionStatus.PROPOSED)
        self.assertEqual(self.started, [])

    async def test_ineligible_goal_still_asks(self):
        reply = await self.f.handle("new", "write a python script that emails me the primes")
        self.assertIn("approve mission", reply)
        self.assertEqual(self.f.store.all()[0].status, ms.MissionStatus.PROPOSED)
        self.assertEqual(self.started, [])

    async def test_stop_engaged_means_no_fast_lane(self):
        ag.engage_stop(60)
        reply = await self.f.handle("new", PRIMES)
        self.assertIn("approve mission", reply)
        self.assertEqual(self.started, [])

    async def test_another_active_mission_falls_back_to_a_proposal_with_a_note(self):
        first = await self.f.handle("new", PRIMES)
        self.assertIn("Fast lane", first)
        second = await self.f.handle("new", "write a python script that reverses a string")
        self.assertIn("approve mission", second)
        self.assertIn("Fast lane skipped", second)
        self.assertEqual(len(self.started), 1)

    async def test_command_toggles_and_reports(self):
        self.assertIn("ON", await self.f.handle("fastlane", ""))
        self.assertIn("OFF", await self.f.handle("fastlane", "off"))
        self.assertFalse(fastlane.enabled())
        self.assertIn("ON", await self.f.handle("fastlane", "on"))


if __name__ == "__main__":
    unittest.main()
