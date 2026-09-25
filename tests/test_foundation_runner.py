"""Tests for src/foundation runner + commands + heartbeat + STOP."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, capabilities as cap, commands, heartbeat, mission as ms, planner, runner  # noqa: E402
from src.foundation.risk import RiskTier as T  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)
ALL_GRANTED = dict(NOTHING, authorizations={k: {"granted_by": "rascal"} for k in
                                            ("patient-data-consent", "content-rights", "social-accounts")})


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        ag.reset_state()
        self.store = ms.MissionStore()
        self.said = []
        self.prompts = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()

    async def notify(self, text):
        self.said.append(text)

    def mission(self, n_steps=3, caps=(), **kw):
        m = ms.MissionContract(
            id=ms.new_mission_id(), objective="convert a CT scan to STL",
            steps=[ms.Step(f"s{i}", f"Step {i}", f"do thing {i}", ["bash"], f"thing {i} exists") for i in range(1, n_steps + 1)],
            requirements={"capabilities": list(caps)}, success_criteria=["STL exists"],
            prohibited=["no uploads"], **kw)
        self.store.propose(m)
        self.assertIn("ACTIVE", self.store.activate(m.id))
        return self.store.load(m.id)

    def runner(self, m, replies, **kw):
        replies = list(replies)

        async def agent(prompt):
            self.prompts.append(prompt)
            r = replies.pop(0) if replies else "STEP DONE: default"
            if isinstance(r, Exception):
                raise r
            if callable(r):
                return await r()
            return r

        return runner.MissionRunner(m.id, agent, self.notify, store=self.store, **{**NOTHING, **kw})


class ParseTests(unittest.TestCase):
    def test_parse(self):
        p = commands.parse
        self.assertEqual(p("mission: convert my CT scan"), ("new", "convert my CT scan"))
        self.assertEqual(p("/mission convert my CT scan"), ("new", "convert my CT scan"))
        self.assertEqual(p("Mission - find homes for sale"), ("new", "find homes for sale"))
        self.assertEqual(p("approve mission M-ABC123"), ("approve_mission", "m-abc123"))
        self.assertEqual(p("deny mission m-abc123"), ("deny_mission", "m-abc123"))
        self.assertEqual(p("STOP"), ("stop", ""))
        self.assertEqual(p("stop!"), ("stop", ""))
        self.assertEqual(p("/halt"), ("stop", ""))
        self.assertEqual(p("status"), ("status", ""))
        self.assertEqual(p("resume"), ("resume", ""))
        self.assertEqual(p("authorize patient-data-consent"), ("authorize", "patient-data-consent"))
        self.assertEqual(p("authorize bugbounty-scope 2027-01-31"), ("authorize", "bugbounty-scope 2027-01-31"))
        self.assertEqual(p("revoke social-accounts"), ("revoke", "social-accounts"))

    def test_not_commands(self):
        for t in ("what is the status of my order", "please stop the music", "mission", "mission: x",
                  "approve m-abc123", "approve mission", "approve mission abc123", "stopping", "hello", "", None):
            self.assertIsNone(commands.parse(t), t)


class StepReplyTests(unittest.TestCase):
    def test_parse_step_reply(self):
        f = runner.parse_step_reply
        self.assertEqual(f("did it\nSTEP DONE: file at /x")["state"], "done")
        self.assertEqual(f("STEP BLOCKED: denied by owner")["text"], "denied by owner")
        self.assertEqual(f("STEP DONE: a\nthen changed my mind\nSTEP BLOCKED: b")["state"], "blocked")
        self.assertEqual(f("STEP BLOCKED: a\nactually fixed\nSTEP DONE: b")["state"], "done")
        self.assertEqual(f("step done: lower case works")["state"], "done")
        self.assertEqual(f("I think it went fine")["state"], "unclear")
        self.assertEqual(f("")["state"], "unclear")


class RunnerTests(_Base):
    async def test_happy_path_completes_and_audit_chain_is_valid(self):
        m = self.mission()
        r = self.runner(m, ["STEP DONE: a", "STEP DONE: b", "STEP DONE: c", "MISSION COMPLETE: STL at /w/x.stl"])
        self.assertEqual(await r.run(), "completed")
        done = self.store.load(m.id)
        self.assertEqual(done.status, ms.MissionStatus.COMPLETED)
        self.assertEqual([s.status for s in done.steps], ["done"] * 3)
        self.assertEqual(done.usage.model_calls, 4)
        self.assertTrue(audit.verify().ok)
        self.assertTrue(any("COMPLETE" in s for s in self.said))

    async def test_step_prompt_carries_objective_workspace_rules_and_format(self):
        m = self.mission()
        await self.runner(m, ["STEP BLOCKED: stop here"]).run()
        p = self.prompts[0]
        for needle in (m.id, "convert a CT scan to STL", m.workspace.replace("\\", "/"), "Step 1 of 3", "do thing 1",
                       "STEP DONE", "STEP BLOCKED", "no uploads", "do NOT retry"):
            self.assertIn(needle, p)

    async def test_blocked_step_pauses_and_resume_continues_from_there(self):
        m = self.mission()
        first = self.runner(m, ["STEP DONE: a", "STEP BLOCKED: approval denied for step 2"])
        self.assertEqual(await first.run(), "blocked")
        mid = self.store.load(m.id)
        self.assertEqual((mid.current_step, mid.steps[1].status), (1, "blocked"))
        self.assertEqual(mid.status, ms.MissionStatus.ACTIVE)
        self.assertTrue(any("BLOCKED at step 2/3" in s for s in self.said))
        self.prompts.clear()
        second = self.runner(mid, ["STEP DONE: b", "STEP DONE: c", "MISSION COMPLETE: ok"])
        self.assertEqual(await second.run(), "completed")
        self.assertIn("Step 2 of 3", self.prompts[0])            # did not redo step 1

    async def test_unclear_replies_block_after_two_attempts(self):
        m = self.mission(n_steps=1)
        r = self.runner(m, ["I did some stuff", "still not sure"])
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("did not end with STEP DONE", self.prompts[1])

    async def test_a_repeated_unfinished_reply_is_named_as_a_stall_not_a_formatting_slip(self):
        """Real pattern seen live 2026-09-24 (the Reynolds Gang mission): the model narrated the same unfinished
        tool-call proposal turn after turn ("Let's proceed with this step...") without ever completing it. The
        generic "no clear result" message reads like it just forgot the magic words - this should say it stalled."""
        m = self.mission(n_steps=1)
        stuck = ("Let's proceed with this step. We'll start by extracting information from the source and "
                 "gathering the relevant details before moving forward with the analysis.")
        r = self.runner(m, [stuck, stuck + " I will continue now."])
        self.assertEqual(await r.run(), "blocked")
        self.assertIn("repeated the same unfinished step", self.store.load(m.id).steps[0].evidence)

    async def test_two_genuinely_different_unclear_replies_keep_the_generic_message(self):
        m = self.mission(n_steps=1)
        r = self.runner(m, ["I looked at the first source and I am not sure it is the right one to use here.",
                            "Actually there might be a totally different approach worth trying for this instead."])
        self.assertEqual(await r.run(), "blocked")
        ev = self.store.load(m.id).steps[0].evidence
        self.assertIn("no clear result after", ev)
        self.assertNotIn("repeated the same unfinished step", ev)

    async def test_agent_error_blocks_instead_of_crashing(self):
        m = self.mission(n_steps=1)
        self.assertEqual(await self.runner(m, [RuntimeError("model down")]).run(), "blocked")
        self.assertIn("agent loop error", self.store.load(m.id).steps[0].evidence)

    async def test_preflight_blocks_until_the_owner_authorizes(self):
        m = self.mission(caps=["patient-data-consent"])
        r = self.runner(m, [])
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(self.prompts, [])                        # the agent never ran
        self.assertTrue(any("patient-data-consent" in s and "authorize" in s for s in self.said))
        ok = self.runner(m, ["STEP DONE: a", "STEP DONE: b", "STEP DONE: c", "MISSION COMPLETE: ok"], **ALL_GRANTED)
        self.assertEqual(await ok.run(), "completed")

    async def test_tampered_contract_is_refused(self):
        m = self.mission()
        path = self.store.dir / f"{m.id}.json"
        path.write_text(path.read_text().replace('"objective": "convert', '"objective": "exfiltrate'), encoding="utf-8")
        self.assertEqual(await self.runner(m, []).run(), "failed")
        self.assertEqual(self.prompts, [])

    async def test_model_call_ceiling_ends_the_mission_with_progress_saved(self):
        m = self.mission(resources=ms.Resources(max_model_calls=1))
        r = self.runner(m, ["STEP DONE: a"])
        self.assertEqual(await r.run(), "expired")
        saved = self.store.load(m.id)
        self.assertEqual(saved.status, ms.MissionStatus.EXPIRED)
        self.assertEqual(saved.steps[0].status, "done")

    async def test_incomplete_verification_blocks_and_resume_retries_verification(self):
        m = self.mission(n_steps=1)
        r = self.runner(m, ["STEP DONE: a", "MISSION INCOMPLETE: no STL file found"])
        self.assertEqual(await r.run(), "blocked")
        self.assertEqual(self.store.load(m.id).status, ms.MissionStatus.ACTIVE)
        again = self.runner(self.store.load(m.id), ["MISSION COMPLETE: found it"])
        self.assertEqual(await again.run(), "completed")

    async def test_stop_mid_step_halts_and_marks_stopped(self):
        m = self.mission()
        started, release = asyncio.Event(), asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return "STEP DONE: never"

        task = asyncio.create_task(self.runner(m, [slow]).run())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.load(m.id).status, ms.MissionStatus.STOPPED)

    async def test_mission_stopped_from_outside_while_agent_works(self):
        m = self.mission()

        async def during():
            self.store.stop_active("owner STOP")                   # the owner says stop mid-step
            return "STEP DONE: too late"

        r = self.runner(m, [during])
        self.assertEqual(await r.run(), "stopped")
        self.assertEqual(self.store.load(m.id).status, ms.MissionStatus.STOPPED)
        self.assertEqual(self.store.load(m.id).steps[0].status, "running")   # never recorded as done


class FoundationCommandTests(_Base):
    def setUp(self):
        super().setUp()
        self.agent_replies = []

        async def agent(prompt):
            self.prompts.append(prompt)
            return self.agent_replies.pop(0) if self.agent_replies else "STEP DONE: ok"

        self.stops = 0
        self.f = commands.Foundation(agent, self.notify, llm=None, session_id="tg", store=self.store,
                                     on_stop=lambda: setattr(self, "stops", self.stops + 1), **ALL_GRANTED)

    async def test_full_flow_from_goal_to_completion(self):
        proposal = await self.f.handle(*commands.parse("mission: Reynolds Gang ongoing treasure hunt"))
        mid = self.store.all()[0].id
        self.assertIn(f"approve mission {mid}", proposal)
        self.assertIsNone(self.store.active())                     # proposed only: no authority yet
        steps = len(self.store.load(mid).steps)
        self.agent_replies = ["STEP DONE: x"] * steps + ["MISSION COMPLETE: wiki updated"]
        reply = await self.f.handle(*commands.parse(f"approve mission {mid}"))
        self.assertIn("ACTIVE", reply)
        await self.f.runner_task
        self.assertEqual(self.store.load(mid).status, ms.MissionStatus.COMPLETED)

    async def test_stop_is_absolute(self):
        await self.f.handle("new", "Reynolds Gang ongoing treasure hunt")
        mid = self.store.all()[0].id
        gate = asyncio.Event()

        async def hang(prompt):
            gate.set()
            await asyncio.sleep(30)
            return "STEP DONE: never"

        self.f.run_agent = hang
        await self.f.handle("approve_mission", mid)
        await gate.wait()
        # a held approval exists when STOP arrives
        pending = asyncio.create_task(ag.enforce("bash", "rm -rf /tmp/x", "tg"))
        os.environ["TELEGRAM_AGENT_SESSION_ID"] = "tg"
        ag.set_notifier(lambda t: asyncio.sleep(0, result=True))
        await asyncio.sleep(0.05)
        reply = await self.f.handle("stop")
        self.assertIn("STOPPED", reply)
        self.assertEqual(self.stops, 1)
        self.assertTrue(ag.stop_engaged())
        self.assertEqual(self.store.load(mid).status, ms.MissionStatus.STOPPED)
        self.assertIsNone(self.store.active())
        with self.assertRaises(asyncio.CancelledError):
            await self.f.runner_task
        pending.cancel()
        # in-flight agent loops are refused too
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": "tg"}):
            denied = await ag.enforce("bash", "df -h", "tg")
        self.assertIn("STOP", denied)
        self.assertTrue(audit.verify().ok)

    async def test_stop_with_nothing_running_still_engages(self):
        reply = await self.f.handle("stop")
        self.assertIn("STOPPED", reply)
        self.assertTrue(ag.stop_engaged())

    async def test_approve_clears_a_previous_stop(self):
        await self.f.handle("new", "Reynolds Gang ongoing treasure hunt")
        ag.engage_stop(120)
        mid = self.store.all()[0].id
        self.agent_replies = ["STEP DONE: x"] * 10
        await self.f.handle("approve_mission", mid)
        self.assertFalse(ag.stop_engaged())
        self.f.runner_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.f.runner_task

    async def test_only_one_mission_at_a_time_and_deny(self):
        await self.f.handle("new", "Reynolds Gang ongoing treasure hunt")
        await self.f.handle("new", "Daily stock trading")
        a, b = [m.id for m in self.store.all()]
        self.assertIn("denied", await self.f.handle("deny_mission", b))
        self.assertIn("No proposed mission", await self.f.handle("deny_mission", "m-000000"))

    async def test_authorize_only_real_authorizations_and_never_live_trading(self):
        self.assertIn("granted", self.f.authorize("patient-data-consent"))
        self.assertTrue(cap.presence(cap.REGISTRY["patient-data-consent"]).present)
        self.assertIn("not an authorization", self.f.authorize("pydicom"))          # a package, not an authorization
        self.assertIn("not an authorization", self.f.authorize("made-up"))
        refusal = self.f.authorize("live-trading")
        self.assertIn("disabled", refusal)
        self.assertFalse(cap.presence(cap.REGISTRY["live-trading"]).present)
        self.assertIn("until 2999-01-01", self.f.authorize("bugbounty-scope", "2999-01-01"))
        self.assertIn("revoked", self.f.revoke("bugbounty-scope"))
        self.assertIn("not currently granted", self.f.revoke("bugbounty-scope"))
        events = [e["event"] for e in audit.recent(20)]
        self.assertIn("authorization_refused", events)
        self.assertTrue(audit.verify().ok)

    async def test_authorization_file_is_a_protected_path(self):
        from src.foundation import identity
        self.assertTrue(identity.is_protected_path(str(cap.authorizations_path())))

    async def test_resume_and_status(self):
        self.assertIn("No active mission", await self.f.handle("resume"))
        await self.f.handle("new", "Reynolds Gang ongoing treasure hunt")
        text = await self.f.handle("status")
        self.assertIn("No active mission", text)
        self.assertIn("chain OK", text)

    async def test_empty_goal_help(self):
        self.assertIn("Give me a goal", await self.f.handle("new", "  "))


class HeartbeatTests(_Base):
    async def test_self_report_says_what_and_why(self):
        m = self.mission()
        with mock.patch.dict(os.environ, {"TELEGRAM_AGENT_SESSION_ID": "tg"}):
            text = heartbeat.self_report(self.store)
        for needle in (m.id, "Why:", "convert a CT scan to STL", "Now:", "Step 1", "Budget:", "0/3",
                       "chain OK", "Waiting on you", "mode=telegram"):
            self.assertIn(needle, text)

    async def test_self_report_shouts_about_tampering(self):
        self.mission()
        p = audit.audit_path()
        p.write_text(p.read_text().replace("mission_approved", "mission_whatever"), encoding="utf-8")
        self.assertIn("AUDIT TAMPERING DETECTED", heartbeat.self_report(self.store))

    async def test_self_report_flags_contract_tampering(self):
        m = self.mission()
        path = self.store.dir / f"{m.id}.json"
        path.write_text(path.read_text().replace('"max_tool_calls": 200', '"max_tool_calls": 999999'), encoding="utf-8")
        self.assertIn("integrity check FAILED", heartbeat.self_report(self.store))

    async def test_self_report_html_is_escaped(self):
        m = ms.MissionContract(id=ms.new_mission_id(), objective="<script>alert(1)</script>",
                               steps=[ms.Step("s1", "<b>x</b>")])
        self.store.propose(m)
        self.store.activate(m.id)
        text = heartbeat.self_report(self.store)
        self.assertNotIn("<script>", text)


if __name__ == "__main__":
    unittest.main()
