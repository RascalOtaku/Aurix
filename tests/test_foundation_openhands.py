"""Ponytail (skills) + OpenHands (step executor) wiring."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, commands, mission as ms, openhands as oh, planner, runner, sandbox, skills  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)


class SkillsTests(unittest.TestCase):
    def test_loads_the_real_ponytail_rules_from_the_checkout(self):
        text = skills.load("ponytail")
        self.assertIn("lazy senior developer", text.lower())
        self.assertLessEqual(len(text), skills.MAX_SKILL_CHARS)

    def test_falls_back_to_the_embedded_summary_when_the_checkout_is_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            text = skills.load("ponytail", roots=[Path(empty)])
        self.assertIn("Ponytail, lazy senior dev mode", text)
        self.assertIn("Never skimp on", text)                     # the safety half is always present

    def test_unknown_skill_and_render(self):
        self.assertEqual(skills.load("nope"), "")
        self.assertEqual(skills.render([]), "")
        with tempfile.TemporaryDirectory() as empty:
            block = skills.render(["ponytail"], roots=[Path(empty)])
        self.assertTrue(block.startswith("### Coding rules: Ponytail"))

    def test_an_empty_or_unreadable_file_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "ponytail/.agents/rules/ponytail.md"
            p.parent.mkdir(parents=True)
            p.write_text("   \n", encoding="utf-8")
            self.assertIn("lazy senior dev mode", skills.load("ponytail", roots=[Path(root)]))

    def test_oversized_skill_files_are_capped(self):
        with tempfile.TemporaryDirectory() as root:
            p = Path(root) / "ponytail/.agents/rules/ponytail.md"
            p.parent.mkdir(parents=True)
            p.write_text("x" * 20000, encoding="utf-8")
            self.assertEqual(len(skills.load("ponytail", roots=[Path(root)])), skills.MAX_SKILL_CHARS)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        os.environ.pop("AURIX_OPENHANDS_FALLBACK", None)
        ag.reset_state()
        self.store = ms.MissionStore()
        self.said, self.prompts = [], []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()

    async def notify(self, text):
        self.said.append(text)

    def mission(self, steps=None, sandboxed=True, skills_=("ponytail",), **kw):
        steps = steps or [ms.Step("s1", "Implement", "add a slugify() helper", ["openhands"], "tests pass",
                                  executor="openhands")]
        m = ms.MissionContract(id=ms.new_mission_id(), objective="write a slugify helper", steps=steps,
                               sandboxed=sandboxed, prohibited=["never git push"], success_criteria=["tests pass"],
                               requirements={"capabilities": [], "skills": list(skills_)},
                               allowed_tools=ms.DEFAULT_ALLOWED_TOOLS + ["python"], **kw)
        self.store.propose(m)
        self.store.activate(m.id)
        return self.store.load(m.id)

    def runner(self, m, replies=None, **kw):
        replies = list(replies or [])

        async def agent(prompt):
            self.prompts.append(prompt)
            if "All steps are reported done" in prompt:
                return "MISSION COMPLETE: verified"
            return replies.pop(0) if replies else "STEP DONE: by the built-in agent"

        return runner.MissionRunner(m.id, agent, self.notify, store=self.store, **{**NOTHING, **kw})


def finished(summary="added slugify() and 3 tests", status="finished"):
    return {"output": "some log lines\n" + oh.RESULT_MARK + json.dumps({"status": status, "summary": summary}),
            "exit_code": 0 if status == "finished" else 3}


class HelperTests(_Base):
    def test_task_text_carries_everything_the_coding_agent_needs(self):
        m = self.mission(steps=[ms.Step("s1", "Plan", "plan it", [], "", status="done", evidence="plan written"),
                                ms.Step("s2", "Implement", "add slugify()", ["openhands"], "tests pass",
                                        executor="openhands")])
        text = oh.build_task_text(m, m.steps[1], skills_text="### Coding rules: X\nbe lazy")
        for needle in ("# Task: Implement", m.id, "write a slugify helper", "add slugify()", "## Done when",
                       "tests pass", "plan written", m.workspace.replace("\\", "/"), "never git push",
                       "There is no internet", "be lazy", "LAST", "## Finish"):
            self.assertIn(needle, text)

    def test_write_task_and_command(self):
        m = self.mission()
        path = oh.write_task(m, m.steps[0], "SKILL TEXT")
        self.assertTrue(path.endswith(".openhands/task-s1.md"))
        self.assertIn("SKILL TEXT", Path(path).read_text(encoding="utf-8"))
        cmd = oh.command(path, 25)
        self.assertIn("/opt/tools/openhands_run.py", cmd)
        self.assertIn("--max-iters 25", cmd)
        self.assertIn(path, cmd)

    def test_parse_result(self):
        p = oh.parse_result
        self.assertEqual(p(finished()["output"])["status"], "finished")
        two = oh.RESULT_MARK + '{"status": "stuck"}\nmore\n' + oh.RESULT_MARK + '{"status": "finished", "summary": "x"}'
        self.assertEqual(p(two)["status"], "finished")                       # the last one wins
        broken = p(oh.RESULT_MARK + "{not json")
        self.assertEqual(broken["status"], "error")
        self.assertEqual(p("")["status"], "error")
        self.assertIn("Traceback", p("boom\nTraceback (most recent call last)")["summary"])

    def test_interpret(self):
        i = oh.interpret
        self.assertEqual(i({"status": "finished", "summary": "did it"}, 0), {"state": "done", "text": "did it"})
        self.assertEqual(i({"status": "finished", "summary": "did it"}, 3)["state"], "blocked")
        self.assertIn("requirements-openhands", i({"status": "sdk_unavailable"}, 3)["text"])
        self.assertIn("llm-gateway", i({"status": "llm_unreachable"}, 3)["text"])
        self.assertIn("stuck", i({"status": "stuck", "summary": "looping"}, 3)["text"])
        self.assertEqual(i({"status": "weird"}, 3)["state"], "blocked")
        self.assertLessEqual(len(i({"status": "finished", "summary": "x" * 900}, 0)["text"]), 300)

    def test_ready(self):
        m = self.mission()
        with mock.patch.object(sandbox, "available", return_value=True), \
                mock.patch.object(sandbox, "probe", return_value={"modules": {"openhands.sdk": True}}):
            self.assertEqual(oh.ready(m), (True, ""))
        with mock.patch.object(sandbox, "available", return_value=True), \
                mock.patch.object(sandbox, "probe", return_value={"modules": {"openhands.sdk": False}}):
            ok, why = oh.ready(m)
            self.assertFalse(ok)
            self.assertIn("not installed", why)
        with mock.patch.object(sandbox, "available", return_value=False):
            self.assertFalse(oh.ready(m)[0])
        with mock.patch.object(sandbox, "available", return_value=True), \
                mock.patch.object(sandbox, "probe", side_effect=sandbox.SandboxUnavailable("down")):
            self.assertEqual(oh.ready(m), (False, "down"))
        self.assertFalse(oh.ready(self.mission(sandboxed=False))[0])


class RunnerExecutorTests(_Base):
    async def test_openhands_step_completes_in_the_sandbox_and_the_agent_is_not_used_for_it(self):
        m = self.mission()
        with mock.patch.object(oh, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", return_value=finished()) as run_mock:
            result = await self.runner(m).run()
        self.assertEqual(result, "completed")
        self.assertEqual(run_mock.call_args.args[:2], (m.id, "bash"))
        self.assertIn("openhands_run.py", run_mock.call_args.args[2])
        saved = self.store.load(m.id)
        self.assertEqual(saved.steps[0].status, "done")
        self.assertEqual(saved.steps[0].evidence, "added slugify() and 3 tests")
        self.assertEqual(saved.usage.tool_calls, 1)
        self.assertEqual(len(self.prompts), 1)                              # only the final verification used the agent
        events = [e["event"] for e in audit.recent(30)]
        self.assertIn("openhands_started", events)
        self.assertIn("openhands_finished", events)
        self.assertTrue((Path(m.workspace) / ".openhands" / "task-s1.md").exists())
        self.assertTrue(any("OpenHands coding agent" in s for s in self.said))
        self.assertTrue(audit.verify().ok)

    async def test_task_file_contains_the_ponytail_rules(self):
        m = self.mission()
        with mock.patch.object(oh, "ready", return_value=(True, "")), mock.patch.object(sandbox, "run", return_value=finished()):
            await self.runner(m).run()
        task = (Path(m.workspace) / ".openhands" / "task-s1.md").read_text(encoding="utf-8")
        self.assertIn("Coding rules: Ponytail", task)
        self.assertIn("lazy", task.lower())

    async def test_a_stuck_coding_agent_blocks_the_mission_with_a_reason(self):
        m = self.mission()
        with mock.patch.object(oh, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", return_value=finished("looping on the same edit", "stuck")):
            self.assertEqual(await self.runner(m).run(), "blocked")
        saved = self.store.load(m.id)
        self.assertEqual(saved.steps[0].status, "blocked")
        self.assertIn("stuck", saved.steps[0].evidence)
        self.assertEqual(saved.status, ms.MissionStatus.ACTIVE)             # resumable

    async def test_unavailable_falls_back_to_the_builtin_agent_by_default(self):
        m = self.mission()
        with mock.patch.object(oh, "ready", return_value=(False, "the SDK is not installed")), \
                mock.patch.object(sandbox, "run") as run_mock:
            result = await self.runner(m).run()
        self.assertEqual(result, "completed")
        run_mock.assert_not_called()
        self.assertTrue(any("using the built-in agent" in s for s in self.said))
        self.assertTrue(any("Implement" in p for p in self.prompts))        # the agent got the step instead
        self.assertIn("openhands_unavailable", [e["event"] for e in audit.recent(30)])

    async def test_fallback_can_be_disabled_and_then_it_blocks(self):
        m = self.mission()
        with mock.patch.dict(os.environ, {"AURIX_OPENHANDS_FALLBACK": "0"}), \
                mock.patch.object(oh, "ready", return_value=(False, "the SDK is not installed")):
            self.assertEqual(await self.runner(m).run(), "blocked")
        self.assertIn("OpenHands unavailable", self.store.load(m.id).steps[0].evidence)
        self.assertEqual(self.prompts, [])

    async def test_sandbox_error_blocks_instead_of_crashing(self):
        m = self.mission()
        with mock.patch.object(oh, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", side_effect=sandbox.SandboxUnavailable("gone")):
            self.assertEqual(await self.runner(m).run(), "blocked")
        self.assertIn("sandbox unavailable", self.store.load(m.id).steps[0].evidence)

    async def test_stop_kills_the_running_coding_agent_not_just_the_wait(self):
        m = self.mission()
        started, release = threading.Event(), threading.Event()
        killed = []

        def slow_run(*a, **k):
            started.set()
            release.wait(10)
            return finished()

        with mock.patch.object(oh, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", side_effect=slow_run), \
                mock.patch.object(sandbox, "kill", side_effect=lambda mid: killed.append(mid) or 1):
            task = asyncio.create_task(self.runner(m).run())
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(started.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
        self.assertEqual(killed, [m.id])
        self.assertEqual(self.store.load(m.id).status, ms.MissionStatus.STOPPED)

    async def test_owner_stop_command_kills_sandbox_processes(self):
        m = self.mission()
        f = commands.Foundation(lambda p: None, self.notify, session_id="tg", **NOTHING)
        with mock.patch.object(sandbox, "kill", return_value=2) as kill_mock:
            reply = await f.handle("stop")
        kill_mock.assert_called_once_with(m.id)
        self.assertIn("2 running sandbox process", reply)
        self.assertTrue(audit.verify().ok)


class StepPromptSkillTests(_Base):
    def test_code_steps_get_the_rules_and_others_do_not(self):
        m = self.mission(steps=[ms.Step("s1", "Write it", "write code", ["write_file"]),
                                ms.Step("s2", "Summarise", "summarise", []),
                                ms.Step("s3", "Delegate", "delegate", [], executor="openhands")])
        self.assertIn("Coding rules: Ponytail", runner.build_step_prompt(m, 0))
        self.assertNotIn("Coding rules", runner.build_step_prompt(m, 1))
        self.assertIn("Coding rules: Ponytail", runner.build_step_prompt(m, 2))

    def test_no_skills_no_block(self):
        m = self.mission(steps=[ms.Step("s1", "Write it", "write code", ["write_file"])], skills_=())
        self.assertNotIn("Coding rules", runner.build_step_prompt(m, 0))


class PlannerAndContractTests(_Base):
    async def test_software_goal_gets_the_dev_pack_with_openhands_and_ponytail(self):
        m = await planner.propose_mission("Build a script that renames my photos by date taken", llm=None,
                                          session_id="tg", sandboxed=True, **NOTHING)
        self.assertEqual(m.requirements["packs"], ["software_dev"])
        self.assertEqual(m.requirements["skills"], ["ponytail"])
        by_title = {s.title: s for s in m.steps}
        self.assertEqual(by_title["Implement with the coding agent"].executor, "openhands")
        self.assertEqual(by_title["Verify"].executor, "agent")
        self.assertTrue(any(o.startswith("openhands:") for o in m.requirements["owner_install"]))
        self.assertTrue(m.sandboxed)
        import re
        self.assertTrue(any(re.search(p, "git push origin main") for p in m.prohibited_patterns))
        self.assertIn("Never push, publish, deploy or open pull requests without the owner's approval.", m.prohibited)

    async def test_existing_goals_are_not_captured_by_the_new_pack(self):
        for goal in ("Convert CT scan of skull into clean 3d print file", "Bug Bounties", "Daily stock trading",
                     "Reynolds Gang ongoing treasure hunt", "Self improve ongoing", "Finding homes for sale"):
            m = await planner.propose_mission(goal, llm=None, session_id="tg", **NOTHING)
            self.assertNotIn("software_dev", m.requirements["packs"], goal)

    async def test_self_improve_also_gets_the_ponytail_rules(self):
        m = await planner.propose_mission("Self improve ongoing", llm=None, session_id="tg", **NOTHING)
        self.assertEqual(m.requirements["skills"], ["ponytail"])

    def test_default_executor_keeps_the_legacy_contract_hash_shape(self):
        m = self.mission(steps=[ms.Step("s1", "A", "a"), ms.Step("s2", "B", "b", executor="openhands")])
        terms = m.terms()
        self.assertEqual(terms["steps"][0], ["s1", "A", "a"])                # unchanged for old missions
        self.assertEqual(terms["steps"][1], ["s2", "B", "b", "openhands"])

    def test_changing_a_steps_executor_after_approval_breaks_integrity(self):
        m = self.mission()
        self.assertTrue(m.integrity_ok())
        path = self.store.dir / f"{m.id}.json"
        path.write_text(path.read_text().replace('"executor": "openhands"', '"executor": "agent"'), encoding="utf-8")
        self.assertFalse(self.store.load(m.id).integrity_ok())
        plain = self.mission(steps=[ms.Step("s1", "A", "a")])
        path = self.store.dir / f"{plain.id}.json"
        path.write_text(path.read_text().replace('"executor": "agent"', '"executor": "openhands"'), encoding="utf-8")
        self.assertFalse(self.store.load(plain.id).integrity_ok())


if __name__ == "__main__":
    unittest.main()
