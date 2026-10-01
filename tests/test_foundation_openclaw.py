"""OpenClaw (step executor) wiring - exactly parallel to test_foundation_openhands.py. AURIX's
mission contract stays the authority; OpenClaw is only ever handed one task and asked for one
structured result back, same as OpenHands."""
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
from src.foundation import audit, commands, mission as ms, openclaw as ocw, runner, sandbox  # noqa: E402

NOTHING = dict(env={}, which=lambda b: None, find_spec=lambda m: None, authorizations={}, probe=lambda h, p: False)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        os.environ.pop("AURIX_OPENCLAW_FALLBACK", None)
        ag.reset_state()
        self.store = ms.MissionStore()
        self.said, self.prompts = [], []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()

    async def notify(self, text):
        self.said.append(text)

    def mission(self, steps=None, sandboxed=True, **kw):
        steps = steps or [ms.Step("s1", "Implement", "add a slugify() helper", ["openclaw"], "tests pass",
                                  executor="openclaw")]
        m = ms.MissionContract(id=ms.new_mission_id(), objective="write a slugify helper", steps=steps,
                               sandboxed=sandboxed, prohibited=["never git push"], success_criteria=["tests pass"],
                               requirements={"capabilities": []},
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


def finished(summary="added slugify() and 3 tests", status="done", card_id="c-1"):
    return {"output": "some log lines\n" + ocw.RESULT_MARK + json.dumps({"status": status, "summary": summary,
                                                                         "card_id": card_id}),
            "exit_code": 0 if status == "done" else 3}


class HelperTests(_Base):
    def test_task_text_carries_everything_openclaw_needs(self):
        m = self.mission(steps=[ms.Step("s1", "Plan", "plan it", [], "", status="done", evidence="plan written"),
                                ms.Step("s2", "Implement", "add slugify()", ["openclaw"], "tests pass",
                                        executor="openclaw")])
        text = ocw.build_task_text(m, m.steps[1], skills_text="### Coding rules: X\nbe lazy")
        for needle in ("# Task: Implement", m.id, "write a slugify helper", "add slugify()", "## Done when",
                       "tests pass", "plan written", m.workspace.replace("\\", "/"), "never git push",
                       "There is no internet", "be lazy", "LAST", "## Finish"):
            self.assertIn(needle, text)

    def test_write_task_and_command(self):
        m = self.mission()
        path = ocw.write_task(m, m.steps[0], "SKILL TEXT")
        self.assertTrue(path.endswith(".openclaw/task-s1.md"))
        self.assertIn("SKILL TEXT", Path(path).read_text(encoding="utf-8"))
        cmd = ocw.command(path, 120)
        self.assertIn("/opt/tools/openclaw_run.py", cmd)
        self.assertIn("--timeout-seconds 120", cmd)
        self.assertIn(path, cmd)

    def test_parse_result(self):
        p = ocw.parse_result
        self.assertEqual(p(finished()["output"])["status"], "done")
        two = ocw.RESULT_MARK + '{"status": "blocked"}\nmore\n' + ocw.RESULT_MARK + '{"status": "done", "summary": "x"}'
        self.assertEqual(p(two)["status"], "done")                          # the last one wins
        broken = p(ocw.RESULT_MARK + "{not json")
        self.assertEqual(broken["status"], "error")
        self.assertEqual(p("")["status"], "error")

    def test_interpret(self):
        i = ocw.interpret
        self.assertEqual(i({"status": "done", "summary": "did it"}, 0), {"state": "done", "text": "did it"})
        self.assertEqual(i({"status": "done", "summary": "did it"}, 3)["state"], "blocked")
        self.assertIn("Dockerfile", i({"status": "not_installed"}, 3)["text"])
        self.assertIn("Gateway", i({"status": "gateway_unreachable"}, 3)["text"])
        self.assertIn("timeout", i({"status": "timeout", "summary": "stuck"}, 3)["text"].lower())
        self.assertEqual(i({"status": "weird"}, 3)["state"], "blocked")
        self.assertLessEqual(len(i({"status": "done", "summary": "x" * 900}, 0)["text"]), 300)

    def test_ready(self):
        m = self.mission()
        with mock.patch.object(sandbox, "available", return_value=True), \
                mock.patch.object(sandbox, "probe", return_value={"bins": {"openclaw": True}}):
            self.assertEqual(ocw.ready(m), (True, ""))
        with mock.patch.object(sandbox, "available", return_value=True), \
                mock.patch.object(sandbox, "probe", return_value={"bins": {"openclaw": False}}):
            ok, why = ocw.ready(m)
            self.assertFalse(ok)
            self.assertIn("not installed", why)
        with mock.patch.object(sandbox, "available", return_value=False):
            self.assertFalse(ocw.ready(m)[0])
        with mock.patch.object(sandbox, "available", return_value=True), \
                mock.patch.object(sandbox, "probe", side_effect=sandbox.SandboxUnavailable("gone")):
            ok, why = ocw.ready(m)
            self.assertFalse(ok)
            self.assertEqual(why, "gone")
        self.assertFalse(ocw.ready(self.mission(sandboxed=False))[0])

    def test_fallback_enabled_default_and_override(self):
        self.assertTrue(ocw.fallback_enabled())
        with mock.patch.dict(os.environ, {"AURIX_OPENCLAW_FALLBACK": "0"}):
            self.assertFalse(ocw.fallback_enabled())


class RunnerExecutorTests(_Base):
    async def test_openclaw_step_completes_in_the_sandbox_and_the_agent_is_not_used_for_it(self):
        m = self.mission()
        with mock.patch.object(ocw, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", return_value=finished()) as run_mock:
            result = await self.runner(m).run()
        self.assertEqual(result, "completed")
        self.assertEqual(run_mock.call_args.args[:2], (m.id, "bash"))
        self.assertIn("openclaw_run.py", run_mock.call_args.args[2])
        saved = self.store.load(m.id)
        self.assertEqual(saved.steps[0].status, "done")
        self.assertEqual(saved.steps[0].evidence, "added slugify() and 3 tests")
        self.assertEqual(saved.usage.tool_calls, 1)
        self.assertEqual(len(self.prompts), 1)                              # only the final verification used the agent
        events = [e["event"] for e in audit.recent(30)]
        self.assertIn("openclaw_started", events)
        self.assertIn("openclaw_finished", events)
        self.assertTrue((Path(m.workspace) / ".openclaw" / "task-s1.md").exists())
        self.assertTrue(any("OpenClaw" in s for s in self.said))
        self.assertTrue(audit.verify().ok)

    async def test_a_blocked_card_blocks_the_mission_with_a_reason(self):
        m = self.mission()
        with mock.patch.object(ocw, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", return_value=finished("could not find the file", "blocked")):
            self.assertEqual(await self.runner(m).run(), "blocked")
        saved = self.store.load(m.id)
        self.assertEqual(saved.steps[0].status, "blocked")
        self.assertIn("could not find the file", saved.steps[0].evidence)
        self.assertEqual(saved.status, ms.MissionStatus.ACTIVE)             # resumable

    async def test_unavailable_falls_back_to_the_builtin_agent_by_default(self):
        m = self.mission()
        with mock.patch.object(ocw, "ready", return_value=(False, "OpenClaw is not installed")), \
                mock.patch.object(sandbox, "run") as run_mock:
            result = await self.runner(m).run()
        self.assertEqual(result, "completed")
        run_mock.assert_not_called()
        self.assertTrue(any("using the built-in agent" in s for s in self.said))
        self.assertTrue(any("Implement" in p for p in self.prompts))        # the agent got the step instead
        self.assertIn("openclaw_unavailable", [e["event"] for e in audit.recent(30)])

    async def test_fallback_can_be_disabled_and_then_it_blocks(self):
        m = self.mission()
        with mock.patch.dict(os.environ, {"AURIX_OPENCLAW_FALLBACK": "0"}), \
                mock.patch.object(ocw, "ready", return_value=(False, "OpenClaw is not installed")):
            self.assertEqual(await self.runner(m).run(), "blocked")
        self.assertIn("OpenClaw unavailable", self.store.load(m.id).steps[0].evidence)
        self.assertEqual(self.prompts, [])

    async def test_sandbox_error_blocks_instead_of_crashing(self):
        m = self.mission()
        with mock.patch.object(ocw, "ready", return_value=(True, "")), \
                mock.patch.object(sandbox, "run", side_effect=sandbox.SandboxUnavailable("gone")):
            self.assertEqual(await self.runner(m).run(), "blocked")
        self.assertIn("sandbox unavailable", self.store.load(m.id).steps[0].evidence)

    async def test_stop_kills_the_running_openclaw_task_not_just_the_wait(self):
        m = self.mission()
        started, release = threading.Event(), threading.Event()
        killed = []

        def slow_run(*a, **k):
            started.set()
            release.wait(10)
            return finished()

        with mock.patch.object(ocw, "ready", return_value=(True, "")), \
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


class ContractIntegrityTests(_Base):
    def test_openclaw_executor_joins_the_signed_terms(self):
        agent_step = ms.Step("s1", "Plan", "plan it", [], "")
        openclaw_step = ms.Step("s2", "Implement", "add slugify()", ["openclaw"], "tests pass", executor="openclaw")
        m1 = ms.MissionContract(id=ms.new_mission_id(), objective="x", steps=[agent_step])
        m2 = ms.MissionContract(id=ms.new_mission_id(), objective="x", steps=[openclaw_step])
        self.assertNotEqual(m1.compute_hash(), m2.compute_hash())

    def test_changing_a_steps_executor_after_approval_breaks_integrity(self):
        m = self.mission()
        m.steps[0].executor = "agent"                    # tamper: quietly downgrade off the sandboxed executor
        self.assertFalse(m.integrity_ok())


class OpenClawRunScriptTests(unittest.TestCase):
    """The in-sandbox wrapper's pure logic (network/CLI calls are mocked in the runner tests above,
    but the URL-normalization rule is worth its own direct test - OpenClaw's own docs warn that
    `/v1` silently breaks tool calling, so a regression here would be invisible until a real run)."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "mission_sandbox", "tools"))
        import openclaw_run
        self.mod = openclaw_run

    def test_strips_the_v1_suffix_that_breaks_openclaws_tool_calling(self):
        self.assertEqual(self.mod.native_ollama_base_url("http://llm-gateway:11434/v1"), "http://llm-gateway:11434")
        self.assertEqual(self.mod.native_ollama_base_url("http://llm-gateway:11434/"), "http://llm-gateway:11434")
        self.assertEqual(self.mod.native_ollama_base_url("http://llm-gateway:11434"), "http://llm-gateway:11434")
        self.assertEqual(self.mod.native_ollama_base_url(""), "")

    def test_config_enables_the_workboard_plugin(self):
        """Real live failure 2026-09-30: `openclaw workboard create` refused with 'that bundled
        plugin is disabled by default' - the whole task-dispatch flow depends on it."""
        path = os.path.join(tempfile.mkdtemp(), "openclaw.json")
        self.mod.write_config(path, "http://llm-gateway:11434", "qwen2.5:7b")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        self.assertTrue(config["plugins"]["entries"]["workboard"]["enabled"])

    def test_config_marks_the_agent_sandboxed(self):
        """Real live failure 2026-09-30: workboard dispatch refused every card with 'target agent
        is not sandboxed for this restricted Workboard card' until this was set."""
        path = os.path.join(tempfile.mkdtemp(), "openclaw.json")
        self.mod.write_config(path, "http://llm-gateway:11434", "qwen2.5:7b")
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
        self.assertEqual(config["agents"]["defaults"]["sandbox"]["mode"], "non-main")

    def test_run_unwraps_the_nested_card_and_moves_it_to_ready_before_dispatch(self):
        """Real live findings 2026-09-30: `workboard show` nests the card under a "card" key (create
        does not), and a freshly created card sits at status "todo" - dispatch silently skips it
        (count=0) until it is moved to "ready" first."""
        calls = []

        def fake_run_cli(*args, home, timeout=30):
            calls.append(args)
            if args[:2] == ("workboard", "create"):
                return {"card": {"id": "abc-123"}}                       # create: nested, unlike show
            if args[:2] == ("workboard", "show"):
                return {"card": {"id": "abc-123", "status": "done", "summary": "ready"}}
            return {}

        with mock.patch.object(self.mod, "openclaw_available", return_value=True), \
                mock.patch.object(self.mod, "start_gateway", return_value=mock.Mock()), \
                mock.patch.object(self.mod, "gateway_ready", return_value=True), \
                mock.patch.object(self.mod, "run_cli", side_effect=fake_run_cli), \
                mock.patch.dict(os.environ, {"LLM_BASE_URL": "http://llm-gateway:11434", "SANDBOX_HOME": "/tmp/home"}):
            task_file = os.path.join(tempfile.mkdtemp(), "task.md")
            with open(task_file, "w", encoding="utf-8") as f:
                f.write("do the thing")
            code = self.mod.run(task_file, 30)
        self.assertEqual(code, 0)
        kinds = [c[:2] for c in calls]
        self.assertIn(("workboard", "move"), kinds)
        self.assertLess(kinds.index(("workboard", "move")), kinds.index(("workboard", "dispatch")))
        move_call = next(c for c in calls if c[:2] == ("workboard", "move"))
        self.assertEqual(move_call[2:4], ("abc-123", "--status"))

    def test_emit_prints_the_result_marker_and_maps_exit_codes(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.mod.emit("done", "all good", "c-1")
        self.assertEqual(code, 0)
        self.assertIn(self.mod.RESULT_MARK, buf.getvalue())
        self.assertEqual(json.loads(buf.getvalue().split(self.mod.RESULT_MARK, 1)[1])["card_id"], "c-1")
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code2 = self.mod.emit("blocked", "nope")
        self.assertEqual(code2, 3)

    def test_gateway_binds_loopback_not_the_container_default(self):
        """Real live failure 2026-09-30: OpenClaw detects a container and defaults to binding
        0.0.0.0, then refuses to start that way without an auth token - correct behaviour for an
        endpoint reachable from outside, but this Gateway is only ever called from this same
        process over 127.0.0.1, so `--bind loopback` is the right fix, not provisioning a token
        nothing external ever needs."""
        captured = {}

        class FakePopen:
            def __init__(self, *a, **k):
                captured["args"] = a[0] if a else k.get("args")

        with mock.patch.object(self.mod.subprocess, "Popen", FakePopen):
            self.mod.start_gateway("/tmp/home")
        self.assertIn("--bind", captured["args"])
        self.assertEqual(captured["args"][captured["args"].index("--bind") + 1], "loopback")


if __name__ == "__main__":
    unittest.main()
