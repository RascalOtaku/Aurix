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

    def test_read_only_research_qualifies(self):
        for goal in ("find the current weather in Denver", "list the biggest files in my workspace"):
            ok, why = fastlane.check(propose(goal))
            self.assertTrue(ok, (goal, why))
            self.assertEqual(why[0], "read-only")

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
