"""The upgrade lane: the validator (what a model may and may not change), the backlog, drafting with repair, sandbox judging, signed approvals, results,
and the host agent that applies / rolls back. The model and the sandbox are faked; the sandbox RUNNER script is run for real on a tiny fake tree."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from src import approval_gate as ag  # noqa: E402
from src.foundation import actions, audit, buttons, commands, teacher, upgrades  # noqa: E402

spec = importlib.util.spec_from_file_location("aurix_upgrade_agent", HERE / "scripts" / "aurix_upgrade_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

KEY_HEX = "ef" * 32
KEY = bytes.fromhex(KEY_HEX)
TOY = "def double(x):\n    return x * 2\n\n\ndef label(n):\n    return 'item %d' % n\n"
TOY_TEST = "import unittest\nfrom src.foundation import toy\n\n\nclass T(unittest.TestCase):\n    def test_double(self):\n        self.assertEqual(toy.double(2), 4)\n"
NEW_TEST = ("import unittest\nfrom src.foundation import toy\n\n\nclass Upg(unittest.TestCase):\n    def test_label_plural(self):\n"
            "        self.assertEqual(toy.label(2), 'items 2')\n")
GOOD = {"title": "Pluralise labels", "why": "label() said 'item 2'.", "risk": "low", "files": [
    {"path": "src/foundation/toy.py", "edits": [{"find": "return 'item %d' % n", "replace": "return ('items %d' if n != 1 else 'item %d') % n"}]},
    {"path": "tests/test_upgrade_toy.py", "content": NEW_TEST}]}


def make_tree(root: Path) -> Path:
    (root / "src" / "foundation").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "__init__.py").write_text("")
    (root / "src" / "foundation" / "__init__.py").write_text("")
    (root / "src" / "foundation" / "toy.py").write_text(TOY)
    (root / "src" / "foundation" / "identity.py").write_text("PROTECTED = 1\n")
    (root / "src" / "foundation" / "other.py").write_text("X = 1\n")
    (root / "tests" / "test_foundation_toy.py").write_text("import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n" + TOY_TEST)
    return root


def fake_post(*replies):
    seq = list(replies)

    def post(url, headers, body, timeout):
        r = seq.pop(0) if len(seq) > 1 else seq[0]
        return 200, {"content": [{"type": "text", "text": r if isinstance(r, str) else json.dumps(r)}]}
    post.calls = seq
    return post


def sandbox_ok(base_failed=(), after_failed=(), base_ran=10, after_ran=11):
    def run(mid, tool, code, timeout):
        return {"output": "noise\nRESULT " + json.dumps({"base": {"ran": base_ran, "failed": list(base_failed)}, "after": {"ran": after_ran, "failed": list(after_failed)}})}
    return run


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.app = make_tree(t / "app")
        self.up = t / "up"
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t / "proj"), "AURIX_BRAIN": str(t / "brain"), "AURIX_APP_ROOT": str(self.app), "AURIX_UPGRADES_DIR": str(self.up),
                                                 "AURIX_GAMING_HMAC_KEY": KEY_HEX, "ANTHROPIC_API_KEY": "sk-ant-test"})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()
        p = mock.patch.object(agent, "ROOT", self.up)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(agent, "SOURCE", t / "src_host")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(teacher, "_local_fallback", return_value=(None, "no local model in tests"))  # network-free by default
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    def on(self, n=3):
        upgrades.set_config(True, n)


class ValidatorTests(_Base):
    def build(self, obj):
        return upgrades.build_changes(obj, self.app)

    def test_a_good_change_becomes_concrete_diffs(self):
        changes, why = self.build(GOOD)
        self.assertEqual(why, "")
        self.assertEqual([c["path"] for c in changes], ["src/foundation/toy.py", "tests/test_upgrade_toy.py"])
        self.assertIn("items %d", changes[0]["new"])
        self.assertIn("-    return 'item %d' % n", changes[0]["diff"])
        self.assertTrue(changes[1]["is_new"])

    def bad(self, files, needle):
        changes, why = self.build({"title": "x", "why": "y", "files": files})
        self.assertIsNone(changes)
        self.assertIn(needle, why)

    def test_paths_it_may_never_touch(self):
        test = {"path": "tests/test_upgrade_toy.py", "content": NEW_TEST}
        ed = [{"find": "X = 1", "replace": "X = 2"}]
        self.bad([{"path": "src/foundation/identity.py", "edits": [{"find": "PROTECTED = 1", "replace": "PROTECTED = 0"}]}, test], "protected")
        self.bad([{"path": "src/foundation/commands.py", "edits": ed}, test], "")                      # missing file: not creatable either
        self.bad([{"path": "docker-compose.yml", "edits": ed}, test], "")
        self.bad([{"path": "../etc/passwd", "edits": ed}, test], "bad path")
        self.bad([{"path": "/etc/passwd", "edits": ed}, test], "bad path")
        self.bad([{"path": "tests/test_foundation_toy.py", "edits": [{"find": "assertEqual(toy.double(2), 4)", "replace": "assertTrue(True)"}]}, test], "outside")
        self.bad([{"path": "src/foundation/upgrades.py", "content": "x = 1\n"}, test], "")
        self.bad([{"path": "scripts/evil.py", "content": "x = 1\n"}, test], "not a path I may create")
        self.assertIsNotNone(upgrades.check_path("src/foundation/identity.py", True))
        self.assertIsNotNone(upgrades.check_path("src/foundation/upgrades.py", True))
        self.assertIsNotNone(upgrades.check_path("src/foundation/gaming.py", True))
        self.assertIsNotNone(upgrades.check_path("src/foundation/commands.py", True))
        self.assertIsNone(upgrades.check_path("src/foundation/growth.py", True))
        self.assertIsNone(upgrades.check_path("static/command.html", True))

    def test_edit_rules(self):
        test = {"path": "tests/test_upgrade_toy.py", "content": NEW_TEST}
        self.bad([{"path": "src/foundation/toy.py", "edits": [{"find": "return", "replace": "yield"}]}, test], "2 times")
        self.bad([{"path": "src/foundation/toy.py", "edits": [{"find": "no such text", "replace": "x"}]}, test], "0 times")
        self.bad([{"path": "src/foundation/toy.py", "content": TOY + "\n# x\n"}, test], "only be changed with `edits`")
        self.bad([{"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": "return x * 2"}]}, test], "changes nothing")
        self.bad([{"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": "return x *"}]}, test], "does not parse")

    def test_risky_calls_size_and_the_new_test_requirement(self):
        test = {"path": "tests/test_upgrade_toy.py", "content": NEW_TEST}
        for code in ("import subprocess", "os.system('x')", "r = eval(x)", "s = socket.socket()", "k = os.environ['ANTHROPIC_API_KEY']", "open('.env')"):
            self.bad([{"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": f"{code}\n    return x * 2"}]}, test], "do not allow")
        ok = [{"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": "return x * 2  # x * 2 via re.compile is fine"}]}]
        self.assertIsNotNone(self.build({"files": ok + [test]})[0])
        self.bad(ok, "did not include a new test")
        self.bad([test], "only added a test")
        big = "".join(f"    y{i} = {i}\n" for i in range(400))
        self.bad([{"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": "y = 0\n" + big + "    return x * 2"}]}, test], "too big")
        self.bad([{"path": "src/foundation/other.py", "edits": [{"find": "X = 1", "replace": "X = 2"}]}] * 2 + [test], "appears twice")
        self.bad([{"path": f"src/foundation/m{i}.py", "content": "x = 1\n"} for i in range(5)], "limit")
        self.assertEqual(self.build({"files": []})[1], "nothing worth changing")
        self.assertIn("not the JSON", self.build("nonsense")[1])

    def test_a_brand_new_module_is_allowed_when_it_has_a_test(self):
        new = {"path": "src/foundation/helper_extra.py", "content": "def f():\n    return 1\n"}
        self.assertIsNotNone(self.build({"files": [new, {"path": "tests/test_upgrade_helper.py", "content": NEW_TEST}]})[0])


class BacklogTests(_Base):
    def test_your_ideas_come_first_and_name_their_files(self):
        self.assertIn("Queued", upgrades.add_idea("the toy.py label should pluralise"))
        self.assertIn("Describe", upgrades.add_idea("x"))
        self.assertIn("credential", upgrades.add_idea("copy my .env into a note please"))
        item = upgrades.next_item()
        self.assertEqual((item["kind"], item["paths"]), ("idea", ["src/foundation/toy.py"]))

    def test_reviews_rotate_and_skip_protected_and_busy_modules(self):
        self.assertNotIn("src/foundation/identity.py", upgrades.editable_files())
        self.assertEqual(set(upgrades.editable_files()), {"src/foundation/toy.py", "src/foundation/other.py"})
        first = upgrades.next_item()
        self.assertEqual(first["kind"], "review")
        self.on()
        upgrades.draft(first, fake_post({"title": "", "why": "nothing worth changing", "files": []}), now=time.time())
        second = upgrades.next_item()
        self.assertNotEqual(first["paths"], second["paths"])                                            # the one just reviewed goes to the back of the line


class WorkspaceTests(_Base):
    def test_the_workspace_header_is_sent_only_when_configured_and_the_error_says_what_to_do(self):
        from src.foundation import teacher
        self.assertNotIn("anthropic-workspace-id", teacher.api_headers())
        self.assertIn("Saved", teacher.set_workspace("wrkspc_01ABCdef23"))
        self.assertEqual(teacher.api_headers()["anthropic-workspace-id"], "wrkspc_01ABCdef23")
        self.assertIn("does not look like", teacher.set_workspace("bad id!"))
        self.assertEqual(teacher.api_headers()["anthropic-workspace-id"], "wrkspc_01ABCdef23")
        teacher.set_workspace("clear")
        self.assertNotIn("anthropic-workspace-id", teacher.api_headers())
        with mock.patch.dict(os.environ, {"ANTHROPIC_WORKSPACE_ID": "wrkspc_fromenv1"}):
            self.assertEqual(teacher.api_headers()["anthropic-workspace-id"], "wrkspc_fromenv1")
        err = teacher.api_error(400, {"error": {"message": "This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header"}})
        self.assertIn("workspace wrkspc_", err)
        self.assertIn("HTTP 500", teacher.api_error(500, {}))
        self.assertIn("Plans & Billing", teacher.api_error(400, {"error": {"message": "Your credit balance is too low to access the Anthropic API."}}))
        self.assertIn("refused the key", teacher.api_error(401, {"error": {"message": "invalid x-api-key"}}))

    def test_the_lane_sends_the_header_and_a_failed_call_costs_no_budget_and_tells_you_once(self):
        from src.foundation import teacher
        teacher.set_workspace("wrkspc_01ABCdef23")
        self.on()
        seen = {}

        def post(url, headers, body, timeout):
            seen["h"] = headers
            return 400, {"error": {"message": "This API key is not scoped to a workspace"}}
        upgrades.run_once(post, sandbox_ok())
        self.assertEqual(seen["h"]["anthropic-workspace-id"], "wrkspc_01ABCdef23")
        self.assertEqual(upgrades._usage()["drafts"], 0)
        upgrades.set_config(True, 3)
        [note] = upgrades.tick(background=False)
        self.assertIn("could not reach the model", note)
        upgrades.run_once(post, sandbox_ok())
        self.assertEqual(upgrades.tick(background=False), [])                                          # one note a day, not one per attempt

    def test_command(self):
        from src.foundation import commands
        self.assertEqual(commands.parse("workspace wrkspc_01ABCdef23"), ("workspace", "wrkspc_01ABCdef23"))
        self.assertEqual(commands.parse("workspace clear"), ("workspace", "clear"))


class RobustnessTests(_Base):
    def test_a_state_file_with_only_last_attempt_does_not_crash_the_rotation(self):
        (upgrades._data()).mkdir(parents=True, exist_ok=True)
        (upgrades._data() / "state.json").write_text(json.dumps({"last_attempt": 1.0}))
        self.assertEqual(upgrades.next_item()["kind"], "review")

    def test_a_crash_in_the_background_thread_is_audited_not_silent(self):
        self.on()
        with mock.patch.object(upgrades, "next_item", side_effect=RuntimeError("boom")):
            self.assertIn("crashed", upgrades.run_once(fake_post(GOOD), sandbox_ok()))
        self.assertEqual([x for x in audit.recent(5) if x["event"] == "upgrade_draft_failed"][-1]["why"], "RuntimeError: boom")
        self.assertFalse(upgrades._running.locked())                                                   # and the lane is not stuck

    def test_a_page_too_big_to_send_is_not_offered_for_review(self):
        (self.app / "static").mkdir()
        (self.app / "static" / "command.html").write_text("x" * 60000)
        self.assertNotIn("static/command.html", upgrades.editable_files())
        (self.app / "static" / "command.html").write_text("<html></html>")
        self.assertIn("static/command.html", upgrades.editable_files())

    def test_no_tests_in_the_build_is_reported_not_run(self):
        shutil.rmtree(self.app / "tests")
        changes, _ = upgrades.build_changes({"files": [{"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": "return x * 2  # ok"}]},
                                                      {"path": "tests/test_upgrade_toy.py", "content": NEW_TEST}]}, self.app)
        self.assertIn("no tests inside", upgrades.sandbox_suite(changes, run=sandbox_ok())["error"])


class DraftTests(_Base):
    def test_a_good_reply_tested_in_the_sandbox_becomes_a_proposal_you_can_approve(self):
        self.on()
        item = upgrades.next_item()
        p, why = upgrades.draft(item, fake_post(GOOD), sandbox_ok())
        self.assertEqual(why, "")
        self.assertEqual((p["status"], p["title"], p["risk"]), ("review", "Pluralise labels", "low"))
        self.assertEqual(p["verify"]["added_tests"], 1)
        self.assertIn("Tested in the sandbox", upgrades.render(p))
        self.assertIn(f"yes {p['id']}", upgrades.render(p))
        ev = [x for x in audit.recent(10) if x["event"] == "upgrade_proposed"][0]
        self.assertNotIn("items %d", json.dumps(ev))                                                   # audit has names and counts, never code
        self.assertEqual(upgrades._usage()["drafts"], 1)

    def test_budget_key_and_switch(self):
        self.assertIn("off", upgrades.can_draft()[1])
        self.on(1)
        upgrades.draft(upgrades.next_item(), fake_post({"files": []}), sandbox_ok())
        self.assertIn("used", upgrades.can_draft()[1])

    def test_no_key_no_longer_blocks_drafting_the_free_local_model_covers_it(self):
        self.on(3)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertTrue(upgrades.can_draft()[0])

    def test_a_bad_first_reply_gets_one_repair_and_two_bad_replies_are_dropped(self):
        self.on()
        bad = dict(GOOD, files=[GOOD["files"][0]])                                                     # no test file
        p, why = upgrades.draft(upgrades.next_item(), fake_post(bad, GOOD), sandbox_ok())
        self.assertIsNotNone(p)
        self.assertEqual(upgrades._usage()["drafts"], 1)                                               # a repair round is not a second draft
        p2, why2 = upgrades.draft(upgrades.next_item(), fake_post(bad), sandbox_ok())
        self.assertIsNone(p2)
        self.assertIn("did not include a new test", why2)
        self.assertEqual(len([x for x in upgrades.all_proposals()]), 1)

    def test_a_change_that_breaks_a_passing_test_is_repaired_once_or_thrown_away(self):
        self.on()
        broke = sandbox_ok(after_failed=["test_double (tests.test_foundation_toy.T.test_double)"])
        p, why = upgrades.draft(upgrades.next_item(), fake_post(GOOD), broke)
        self.assertIsNone(p)
        self.assertIn("broke 1 test", why)
        self.assertEqual(upgrades.all_proposals(), [])
        self.assertIn("upgrade_discarded", [x["event"] for x in audit.recent(10)])

    def test_failures_that_already_failed_before_do_not_count_against_it(self):
        ok, why, s = upgrades.judge({"base": {"ran": 5, "failed": ["a (x)"]}, "after": {"ran": 6, "failed": ["a (x)"]}})
        self.assertTrue(ok)
        self.assertEqual(s["new_failures"], [])
        self.assertFalse(upgrades.judge({"base": {"ran": 5, "failed": []}, "after": {"ran": 5, "failed": []}})[0])              # its new test never ran
        self.assertFalse(upgrades.judge({"error": "sandbox unreachable"})[0])
        self.assertFalse(upgrades.judge({"base": {"ran": 5, "failed": []}, "after": {"ran": 0, "failed": ["<suite did not finish>"]}})[0])

    def test_nothing_worth_changing_and_network_errors(self):
        self.on()
        p, why = upgrades.draft(upgrades.next_item(), fake_post({"title": "", "why": "nothing worth changing", "files": []}), sandbox_ok())
        self.assertEqual((p, why), (None, "nothing worth changing"))

        def boom(*a):
            raise OSError("down")
        p, why = upgrades.draft(upgrades.next_item(), boom, sandbox_ok())
        self.assertIn("network error", why)

    def test_what_is_sent_is_redacted_and_never_includes_credential_files(self):
        (self.app / "src" / "foundation" / "toy.py").write_text(TOY + "# contact me at owner@example.com\n")
        self.on()
        seen = {}

        def post(url, headers, body, timeout):
            seen["text"] = body["messages"][0]["content"]
            return 200, {"content": [{"type": "text", "text": '{"files": []}'}]}
        upgrades.draft({"kind": "idea", "id": "b-1", "text": "improve toy.py", "paths": ["src/foundation/toy.py"]}, post, sandbox_ok())
        self.assertNotIn("owner@example.com", seen["text"])
        (self.app / "src" / "foundation" / "toy.py").write_text(TOY + "# reads /home/x/.env for keys\n")
        p, why = upgrades.draft({"kind": "idea", "id": "b-2", "text": "improve toy.py", "paths": ["src/foundation/toy.py"]}, post, sandbox_ok())
        self.assertIn("not sent", why)


def openhands_run(new_toy=None, new_test_name=None, new_test_content=None, status="finished", exit_code=0, summary="Pluralised the labels."):
    """Fake sandbox.run for draft_with_openhands: on the "bash" (OpenHands) call, edits files directly in the real
    workspace dir exactly as the real coding agent would; on the "python" call (sandbox_suite), defers to sandbox_ok()."""
    py_run = sandbox_ok()

    def run(mid, tool, code, timeout):
        if tool == "python":
            return py_run(mid, tool, code, timeout)
        ws = Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "workspace" / mid
        if new_toy is not None:
            (ws / "src" / "foundation" / "toy.py").write_text(new_toy, encoding="utf-8")
        if new_test_name:
            (ws / "tests").mkdir(parents=True, exist_ok=True)
            (ws / "tests" / new_test_name).write_text(new_test_content or "", encoding="utf-8")
        return {"output": "OPENHANDS_RESULT:" + json.dumps({"status": status, "summary": summary}), "exit_code": exit_code}
    return run


def openhands_ready(monkeypatch_target=True):
    from src.foundation import sandbox
    return (mock.patch.object(sandbox, "available", return_value=True),
            mock.patch.object(sandbox, "probe", return_value={"modules": {"openhands.sdk": True}}))


class OpenHandsDraftTests(_Base):
    """The upgrade lane's alternative drafting path: hand the SAME task to OpenHands (already proven, already used
    for software_dev mission steps) instead of AURIX's own JSON-diff prompt. sandbox.run is faked throughout;
    no test touches the network or a real coding agent."""

    def setUp(self):
        super().setUp()
        self.on()
        avail, probe = openhands_ready()
        avail.start(); probe.start()
        self.addCleanup(avail.stop)
        self.addCleanup(probe.stop)

    def _toy_item(self):
        return {"kind": "idea", "id": "b-1", "text": "improve toy.py", "paths": ["src/foundation/toy.py"]}

    def test_a_real_edit_and_new_test_becomes_a_proposal(self):
        item = self._toy_item()
        new_toy = TOY.replace("return 'item %d' % n", "return ('items %d' if n != 1 else 'item %d') % n")
        p, why = upgrades.draft_with_openhands(item, openhands_run(new_toy=new_toy, new_test_name="test_upgrade_toy.py", new_test_content=NEW_TEST))
        self.assertEqual(why, "")
        self.assertEqual(p["status"], "review")
        self.assertEqual(p["model"], "OpenHands (sandboxed coding agent)")
        self.assertEqual(p["verify"]["added_tests"], 1)
        self.assertIn("yes " + p["id"], upgrades.render(p))

    def test_nothing_worth_changing_is_reported_not_a_failure(self):
        item = self._toy_item()
        p, why = upgrades.draft_with_openhands(item, openhands_run(summary="Looked it over; nothing worth changing."))
        self.assertIsNone(p)
        self.assertEqual(why, "nothing worth changing")

    def test_an_edit_with_no_new_test_is_refused(self):
        item = self._toy_item()
        new_toy = TOY.replace("return 'item %d' % n", "return ('items %d' if n != 1 else 'item %d') % n")
        p, why = upgrades.draft_with_openhands(item, openhands_run(new_toy=new_toy))
        self.assertIsNone(p)
        self.assertIn("new test", why)

    def test_a_failing_sandbox_test_is_discarded_not_proposed(self):
        item = self._toy_item()
        new_toy = TOY.replace("return 'item %d' % n", "return ('items %d' if n != 1 else 'item %d') % n")
        bad_run = openhands_run(new_toy=new_toy, new_test_name="test_upgrade_toy.py", new_test_content=NEW_TEST)

        def run(mid, tool, code, timeout):
            if tool == "python":
                return sandbox_ok(after_failed=["tests.test_foundation_toy.T.test_double"])(mid, tool, code, timeout)
            return bad_run(mid, tool, code, timeout)
        p, why = upgrades.draft_with_openhands(item, run)
        self.assertIsNone(p)
        self.assertIn("broke", why)

    def test_openhands_unavailable_is_reported_plainly(self):
        from src.foundation import sandbox
        with mock.patch.object(sandbox, "probe", return_value={"modules": {}}):
            p, why = upgrades.draft_with_openhands(self._toy_item(), openhands_run())
        self.assertIsNone(p)
        self.assertIn("OpenHands unavailable", why)

    def test_a_blocked_openhands_run_is_reported_not_silently_dropped(self):
        p, why = upgrades.draft_with_openhands(self._toy_item(), openhands_run(status="stuck", summary="looped on the same edit"))
        self.assertIsNone(p)
        self.assertIn("stuck", why)

    def test_more_than_one_path_is_refused(self):
        item = {"kind": "idea", "id": "b-1", "text": "x", "paths": ["src/foundation/toy.py", "src/foundation/other.py"]}
        p, why = upgrades.draft_with_openhands(item, openhands_run())
        self.assertIsNone(p)
        self.assertIn("one file at a time", why)

    def test_the_upgrade_budget_still_gates_it(self):
        upgrades.set_config(False, 0)
        p, why = upgrades.draft_with_openhands(self._toy_item(), openhands_run())
        self.assertIsNone(p)
        self.assertIn("off", why)

    def test_never_leaves_its_scratch_workspace_behind(self):
        item = self._toy_item()
        new_toy = TOY.replace("return 'item %d' % n", "return ('items %d' if n != 1 else 'item %d') % n")
        before = set((Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "workspace").glob("m-*")) if (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "workspace").is_dir() else set()
        upgrades.draft_with_openhands(item, openhands_run(new_toy=new_toy, new_test_name="test_upgrade_toy.py", new_test_content=NEW_TEST))
        after_dir = Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "workspace"
        after = set(after_dir.glob("m-*")) if after_dir.is_dir() else set()
        self.assertEqual(after - before, set())


class LocalFallbackTests(_Base):
    """Self-hosted / f2p: the upgrade lane never has to stop drafting for lack of paid credits - a failed frontier call
    (no key, empty balance, refused key, dead network) falls back to the free local model instead."""
    DEAD = staticmethod(lambda *a: (400, {"error": {"message": "Your credit balance is too low."}}))

    def test_falls_back_on_a_first_attempt(self):
        text, err = upgrades._call_engineer("do the thing", self.DEAD, local=lambda *a: ("local draft json", ""))
        self.assertEqual((text, err), ("local draft json", ""))

    def test_reports_the_real_reason_when_the_local_model_also_fails(self):
        text, err = upgrades._call_engineer("do the thing", self.DEAD, local=lambda *a: (None, "ollama unreachable"))
        self.assertIsNone(text)
        self.assertIn("credit balance", err)
        self.assertIn("ollama unreachable", err)

    def test_a_repair_round_still_reaches_the_local_model_with_history_folded_in(self):
        """The local bridge takes only (system, prompt), so a repair round's assistant/user history is folded into one prompt
        instead of being skipped - a bad first draft still gets its one repair chance in a pure-local (no-credits) setup."""
        seen = []
        text, err = upgrades._call_engineer("do the thing", self.DEAD, extra_messages=[{"role": "assistant", "content": "first try"},
                                                                                        {"role": "user", "content": "that failed: bad json"}],
                                             local=lambda system, prompt, max_tokens: (seen.append(prompt) or ("repaired json", "")))
        self.assertEqual((text, err), ("repaired json", ""))
        self.assertIn("do the thing", seen[0])
        self.assertIn("first try", seen[0])
        self.assertIn("that failed: bad json", seen[0])

    def test_a_full_draft_can_complete_using_only_the_local_model(self):
        self.on()
        with mock.patch.object(teacher, "_local_fallback", return_value=(json.dumps(GOOD), "")):
            p, why = upgrades.draft(upgrades.next_item(), self.DEAD, sandbox_ok())
        self.assertIsNotNone(p, why)
        self.assertEqual(p["title"], "Pluralise labels")
        self.assertEqual(p["model"], "AURIX's free local model")

    def test_a_repair_round_can_rescue_a_draft_using_only_the_local_model(self):
        """The exact scenario observed live: the local model's first attempt fails validation, and (unlike before this fix)
        the repair round still gets a real second try instead of being discarded outright."""
        self.on()
        bad = dict(GOOD, files=[GOOD["files"][0]])                             # no test file: build_changes rejects this on the first pass
        with mock.patch.object(teacher, "_local_fallback", side_effect=[(json.dumps(bad), ""), (json.dumps(GOOD), "")]):
            p, why = upgrades.draft(upgrades.next_item(), self.DEAD, sandbox_ok())
        self.assertIsNotNone(p, why)
        self.assertEqual(p["model"], "AURIX's free local model")

    def test_the_audit_trail_names_the_model_that_actually_answered_not_just_the_configured_one(self):
        """What was actually observed live: `upgrade_called` logged the configured paid model name even when the paid path
        was unreachable and the free local model answered instead - a misleading audit trail. Confirm it now names the real source."""
        self.on()
        with mock.patch.object(teacher, "_local_fallback", return_value=(json.dumps(GOOD), "")):
            upgrades.draft(upgrades.next_item(), self.DEAD, sandbox_ok())
        called = [r for r in audit.recent(20) if r["event"] == "upgrade_called"][0]
        self.assertEqual(called["model"], "AURIX's free local model")
        proposed = [r for r in audit.recent(20) if r["event"] == "upgrade_proposed"][0]
        self.assertEqual(proposed["model"], "AURIX's free local model")

    def test_a_frontier_success_is_still_labelled_as_the_configured_model(self):
        self.on()
        p, why = upgrades.draft(upgrades.next_item(), fake_post(GOOD), sandbox_ok())
        self.assertEqual(p["model"], upgrades.DEFAULT_MODEL)


class RunnerTests(_Base):
    def test_the_sandbox_runner_really_runs_the_suite_before_and_after(self):
        changes, _ = upgrades.build_changes(GOOD, self.app)
        ws = Path(self.tmp.name) / "ws"
        ws.mkdir()
        (ws / "pkg.tgz").write_bytes(upgrades.build_package())
        (ws / "patch.json").write_text(json.dumps({c["path"]: c["new"] for c in changes}))
        (ws / "runner.py").write_text(upgrades._RUNNER)
        r = subprocess.run([sys.executable, "-c", "exec(open('runner.py').read())"], cwd=str(ws), capture_output=True, text=True, timeout=120)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
        self.assertTrue(line, r.stdout + r.stderr)
        res = json.loads(line[-1][7:])
        self.assertEqual((res["base"]["ran"], res["base"]["failed"]), (1, []))
        self.assertEqual((res["after"]["ran"], res["after"]["failed"]), (2, []))
        ok, why, s = upgrades.judge(res)
        self.assertTrue(ok, why)

    def test_the_runner_catches_a_change_that_breaks_an_existing_test(self):
        changes, _ = upgrades.build_changes({"files": [
            {"path": "src/foundation/toy.py", "edits": [{"find": "return x * 2", "replace": "return x * 3"}]},
            {"path": "tests/test_upgrade_toy.py", "content": "import unittest\n\n\nclass U(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"}]}, self.app)
        res = upgrades.sandbox_suite(changes, run=lambda mid, tool, code, timeout: self._local_run(code, mid))
        ok, why, s = upgrades.judge(res)
        self.assertFalse(ok)
        self.assertIn("broke 1 test", why)

    def _local_run(self, code, mid):
        ws = Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "workspace" / mid
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ws), capture_output=True, text=True, timeout=120)
        return {"output": r.stdout}


class DecisionTests(_Base):
    def make(self):
        self.on()
        p, _ = upgrades.draft(upgrades.next_item(), fake_post(GOOD), sandbox_ok())
        return p

    def test_approval_is_signed_over_hashes_and_the_payload_matches(self):
        p = self.make()
        self.assertIn("Approved", upgrades.approve(p["id"]))
        item = json.loads((self.up / "approved" / f"{p['id']}.json").read_text())
        self.assertEqual(agent.sign(KEY, item), item["sig"])
        payload = json.loads((self.up / "payload" / f"{p['id']}.json").read_text())
        for f in item["files"]:
            self.assertEqual(agent.sha(payload[f["path"]]), f["sha_new"])
        self.assertIn("nothing to approve", upgrades.approve(p["id"]))

    def test_reject_and_undo_rules(self):
        p = self.make()
        self.assertIn("Rejected", upgrades.decline(p["id"]))
        self.assertIn("nothing to undo", upgrades.undo(p["id"]))
        self.assertIn("No upgrade", upgrades.approve("u-000000"))
        with mock.patch.dict(os.environ, {"AURIX_GAMING_HMAC_KEY": ""}):
            q = self.make()
            self.assertIn("cannot sign", upgrades.approve(q["id"]))

    def test_tick_announces_once_then_reports_the_outcome(self):
        self.on()
        self.assertIn("Drafted", upgrades.run_once(fake_post(GOOD), sandbox_ok()))
        [msg] = upgrades.tick(background=False)
        self.assertIn("Upgrade ready", msg)
        self.assertEqual(upgrades.tick(background=False), [])
        pid = upgrades.all_proposals()[0]["id"]
        upgrades.approve(pid)
        (self.up / "results").mkdir(parents=True, exist_ok=True)
        (self.up / "results" / f"{pid}.json").write_text(json.dumps({"id": pid, "status": "applied", "detail": "applied 2 file(s) and the new build is healthy."}))
        [done] = upgrades.tick(background=False)
        self.assertIn("Upgrade live", done)
        self.assertEqual(upgrades.load(pid)["status"], "applied")
        self.assertIn("Rolling back", upgrades.undo(pid))
        (self.up / "results" / f"undo-{pid}.json").write_text(json.dumps({"id": pid, "status": "undone", "detail": "the old files are back"}))
        [back] = upgrades.tick(background=False)
        self.assertIn("Rolled back", back)
        self.assertEqual(upgrades.load(pid)["status"], "undone")

    def test_a_failed_apply_is_reported_plainly(self):
        p = self.make()
        upgrades.approve(p["id"])
        (self.up / "results").mkdir(parents=True, exist_ok=True)
        (self.up / "results" / f"{p['id']}.json").write_text(json.dumps({"id": p["id"], "status": "rolled_back", "detail": "the new build did not come up healthy, so the old code was put back."}))
        [msg] = upgrades.tick(background=False)
        self.assertIn("not applied", msg)
        self.assertEqual(upgrades.load(p["id"])["status"], "failed")

    def test_a_note_for_an_owner_idea_that_produced_nothing(self):
        self.on()
        upgrades.add_idea("the toy.py double() should handle strings")
        upgrades.run_once(fake_post({"files": []}), sandbox_ok())
        [msg] = upgrades.tick(background=False)
        self.assertIn("No upgrade came out", msg)

    def test_the_background_thread_only_starts_when_allowed(self):
        with mock.patch.object(upgrades.threading, "Thread") as th:
            upgrades.tick(background=True)
            th.assert_not_called()                                                                     # off
            self.on()
            upgrades.tick(background=True)
            th.assert_called_once()
            upgrades.tick(background=True)                                                             # too soon after the last attempt
            th.assert_called_once()


class WiringTests(_Base):
    def test_commands_and_buttons(self):
        self.assertEqual(commands.parse("upgrade: make the dashboard show weekly trades"), ("upgrade_add", "make the dashboard show weekly trades"))
        self.assertEqual(commands.parse("upgrade now"), ("upgrade_now", ""))
        self.assertEqual(commands.parse("upgrade openhands"), ("upgrade_openhands", ""))
        self.assertEqual(commands.parse("upgrades on 4"), ("upgrades_cfg", "on 4"))
        self.assertEqual(commands.parse("upgrades off"), ("upgrades_cfg", "off"))
        self.assertEqual(commands.parse("upgrades"), ("upgrades", ""))
        self.assertEqual(commands.parse("yes u-abc123"), ("upgrade_yes", "u-abc123"))
        self.assertEqual(commands.parse("reject u-abc123"), ("upgrade_no", "u-abc123"))
        self.assertEqual(commands.parse("undo u-abc123"), ("upgrade_undo", "u-abc123"))
        self.assertEqual(commands.parse("diff u-abc123"), ("upgrade_diff", "u-abc123"))
        txt = "yes u-abc123 or no u-abc123"
        cmds = [b["callback_data"] for row in buttons.for_reply("", txt)["inline_keyboard"] for b in row]
        self.assertEqual(cmds, ["do:yes u-abc123", "do:no u-abc123", "do:diff u-abc123"])
        self.assertIn("do:undo u-abc123", [b["callback_data"] for row in buttons.for_reply("", "Not right? undo u-abc123")["inline_keyboard"] for b in row])
        for k in ("upgrade_yes", "upgrade_no", "upgrade_undo", "upgrade_diff", "upgrades", "upgrade_now", "upgrade_openhands"):
            self.assertIn(k, buttons.ALLOWED_KINDS)
        for k in ("upgrades_cfg", "upgrade_add"):                                                     # turning the spend on / queueing work is typed, never a stray tap
            self.assertNotIn(k, buttons.ALLOWED_KINDS)

    def test_a_bare_yes_answers_a_single_waiting_upgrade(self):
        self.on()
        p, _ = upgrades.draft(upgrades.next_item(), fake_post(GOOD), sandbox_ok())
        self.assertEqual(commands.parse("yes"), ("upgrade_yes", p["id"]))

    def test_dashboard_actions_and_cards(self):
        self.on()
        p, _ = upgrades.draft(upgrades.next_item(), fake_post(GOOD), sandbox_ok())
        card = [d for d in actions.decisions() if d["kind"] == "upgrade"][0]
        self.assertEqual([b["action"] for b in card["buttons"]], ["upg_yes", "upg_no"])
        self.assertNotIn("<", json.dumps(card))
        self.assertIn("Approved", actions.run_action("upg_yes", p["id"])["message"])
        self.assertFalse(actions.run_action("upg_yes", "../x")["ok"])
        self.assertIn("OFF", actions.run_action("upg_off")["message"])
        self.assertIn("ON", actions.run_action("upg_on", "2")["message"])
        self.assertTrue(actions.run_action("upg_add", "the toy.py label should pluralise")["ok"])
        for n in ("upg_yes", "upg_no", "upg_undo", "upg_add", "upg_on", "upg_off", "upg_now"):
            self.assertIn(n, actions.ACTION_NAMES)
        panel = upgrades.panel()
        self.assertEqual(panel["items"][0]["id"], p["id"])
        self.assertIn("diff", panel["items"][0]["files"][0])

    def test_the_lane_protects_itself(self):
        from src.foundation import identity
        for path in ("src/foundation/upgrades.py", "scripts/aurix_upgrade_agent.py", "scripts/aurix_deploy.sh", "data/upgrades/proposals/u-abc123.json"):
            self.assertEqual(identity.protected_component_for(path), "approval_gate", path)


class HostAgentTests(_Base):
    def setUp(self):
        super().setUp()
        self.src = Path(self.tmp.name) / "src_host"
        make_tree(self.src)
        patcher = mock.patch.object(agent, "deploy", return_value={"ok": True, "out": "DEPLOY OK: the new build is healthy."})
        self.deploy = patcher.start()
        self.addCleanup(patcher.stop)

    def approved(self, changes=None, kind="upgrade", expires_h=1, sig_ok=True):
        if changes is None:
            if getattr(self, "_cached", None) is None:
                self._cached, _ = upgrades.build_changes(GOOD, self.src)
            changes = self._cached
        item = {"id": "u-00a1b2", "kind": kind, "files": [{"path": c["path"], "sha_old": agent.sha(c["old"]) if c["old"] else "", "sha_new": agent.sha(c["new"])} for c in changes],
                "created": datetime.now().astimezone().isoformat(timespec="seconds"), "expires": (datetime.now().astimezone() + timedelta(hours=expires_h)).isoformat(timespec="seconds")}
        item["sig"] = agent.sign(KEY, item) if sig_ok else "0" * 64
        (self.up / "approved").mkdir(parents=True, exist_ok=True)
        (self.up / "payload").mkdir(parents=True, exist_ok=True)
        (self.up / "approved" / f"{'undo-' if kind == 'undo' else ''}{item['id']}.json").write_text(json.dumps(item))
        (self.up / "payload" / f"{item['id']}.json").write_text(json.dumps({c["path"]: c["new"] for c in changes}))
        return item

    def test_it_applies_backs_up_and_deploys(self):
        self.approved()
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "applied", res)
        self.assertIn("items %d", (self.src / "src/foundation/toy.py").read_text())
        self.assertTrue((self.src / "tests/test_upgrade_toy.py").exists())
        self.assertEqual((self.up / "backups/u-00a1b2/src/foundation/toy.py").read_text(), TOY)
        self.assertEqual(agent.process_approved(KEY), [])                                              # once only
        self.deploy.assert_called_once()

    def test_an_unhealthy_deploy_puts_the_source_back(self):
        self.deploy.return_value = {"ok": False, "out": "DEPLOY UNHEALTHY"}
        self.approved()
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "rolled_back")
        self.assertEqual((self.src / "src/foundation/toy.py").read_text(), TOY)
        self.assertFalse((self.src / "tests/test_upgrade_toy.py").exists())

    def test_undo_restores_only_if_nobody_changed_the_files_since(self):
        self.approved()
        agent.process_approved(KEY)
        self.approved(kind="undo")
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "undone", res)
        self.assertEqual((self.src / "src/foundation/toy.py").read_text(), TOY)
        self.assertFalse((self.src / "tests/test_upgrade_toy.py").exists())
        # apply again, then someone edits the file: undo must refuse and keep their edit
        (self.up / "results" / "u-00a1b2.json").unlink()
        (self.up / "results" / "undo-u-00a1b2.json").unlink()
        self.approved()
        agent.process_approved(KEY)
        (self.src / "src/foundation/toy.py").write_text("# edited by the owner\n")
        (self.up / "results" / "undo-u-00a1b2.json").unlink(missing_ok=True)
        self.approved(kind="undo")
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "failed")
        self.assertEqual((self.src / "src/foundation/toy.py").read_text(), "# edited by the owner\n")

    def test_forged_expired_stale_and_protected_are_all_refused_with_nothing_changed(self):
        self.approved(sig_ok=False)
        self.assertEqual(agent.process_approved(KEY)[0]["status"], "refused")
        (self.up / "results" / "u-00a1b2.json").unlink()
        self.approved(expires_h=-1)
        self.assertIn("expired", agent.process_approved(KEY)[0]["detail"])
        (self.up / "results" / "u-00a1b2.json").unlink()
        self.approved()
        (self.src / "src/foundation/toy.py").write_text(TOY + "# edited after the proposal\n")            # the source moved on
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "failed")
        self.assertIn("changed since the proposal", res["detail"])
        self.assertNotIn("items %d", (self.src / "src/foundation/toy.py").read_text())
        self.deploy.assert_not_called()

    def test_the_agent_re_checks_paths_on_its_own(self):
        for rel, exists in (("src/foundation/identity.py", True), ("src/foundation/upgrades.py", True), ("src/foundation/commands.py", True), ("../x.py", True),
                            ("/etc/passwd", True), ("scripts/x.py", False), ("docker-compose.yml", True), ("tests/test_foundation_toy.py", True), ("x/.env", True)):
            self.assertIsNotNone(agent.path_problem(rel, exists), rel)
        self.assertIsNone(agent.path_problem("src/foundation/toy.py", True))
        self.assertIsNone(agent.path_problem("tests/test_upgrade_okay.py", False))
        item = {"id": "u-00a1b3", "kind": "upgrade", "files": [{"path": "src/foundation/identity.py", "sha_old": agent.sha("PROTECTED = 1\n"), "sha_new": agent.sha("PROTECTED = 0\n")}],
                "created": "x", "expires": (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")}
        item["sig"] = agent.sign(KEY, item)
        (self.up / "approved").mkdir(parents=True, exist_ok=True)
        (self.up / "payload").mkdir(parents=True, exist_ok=True)
        (self.up / "approved" / "u-00a1b3.json").write_text(json.dumps(item))
        (self.up / "payload" / "u-00a1b3.json").write_text(json.dumps({"src/foundation/identity.py": "PROTECTED = 0\n"}))
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "failed")
        self.assertEqual((self.src / "src/foundation/identity.py").read_text(), "PROTECTED = 1\n")

    def test_a_syntax_error_in_a_payload_never_reaches_the_deploy(self):
        changes, _ = upgrades.build_changes(GOOD, self.src)
        changes[0]["new"] = "def broken(:\n"
        self.approved(changes)
        [res] = agent.process_approved(KEY)
        self.assertEqual(res["status"], "failed")
        self.assertEqual((self.src / "src/foundation/toy.py").read_text(), TOY)
        self.deploy.assert_not_called()

    def test_no_key_and_hash_mismatch(self):
        self.approved()
        self.assertEqual(agent.process_approved(None)[0]["status"], "refused")
        (self.up / "results" / "u-00a1b2.json").unlink()
        self.approved()
        (self.up / "payload" / "u-00a1b2.json").write_text(json.dumps({"src/foundation/toy.py": "x = 1\n", "tests/test_upgrade_toy.py": NEW_TEST}))
        self.assertIn("payload does not match", agent.process_approved(KEY)[0]["detail"])


if __name__ == "__main__":
    unittest.main()
