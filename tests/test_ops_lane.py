"""Housekeeping lane: the app only writes signed requests; the host agent does exactly one whitelisted thing per signed request."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import aurix_ops_agent as agent  # noqa: E402
from src.foundation import buttons, commands, ops  # noqa: E402

KEY = "11" * 32


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t"}).stdout


class OpsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.updir = self.tmp / "upgrades"
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(self.tmp / "app"), "AURIX_UPGRADES_DIR": str(self.updir),
                                         "AURIX_GAMING_HMAC_KEY": KEY})
        p.start()
        self.addCleanup(p.stop)
        for name, val in (("ROOT", self.updir / "ops"),):
            q = mock.patch.object(agent, name, val)
            q.start()
            self.addCleanup(q.stop)


class AppSideTests(OpsBase):
    def test_a_change_waits_for_the_owner_and_a_scan_does_not(self):
        card = ops.propose("update")
        oid = card.split("<code>")[1].split("</code>")[0]
        self.assertFalse((self.updir / "ops" / "approved").exists())             # a card alone sends nothing
        self.assertIn("rolled back by itself", card)
        kb = buttons.for_reply("ops", card)
        self.assertEqual(commands.parse(buttons.command_from_data(kb["inline_keyboard"][0][0]["callback_data"])), ("ops_yes", oid))
        self.assertIn("Approved", ops.decide(oid, True))
        item = json.loads((self.updir / "ops" / "approved" / f"{oid}.json").read_text())
        self.assertEqual(item["op"], "update")
        self.assertEqual(item["sig"], agent.sign(bytes.fromhex(KEY), item))
        self.assertIn("already sent", ops.decide(oid, True))
        self.assertIn("read-only", ops.propose("scan"))                          # sent straight away
        self.assertEqual(len(list((self.updir / "ops" / "approved").glob("*.json"))), 2)

    def test_results_become_messages_and_a_scan_becomes_a_prune_card(self):
        ops.propose("scan")
        sid = ops.requests()[-1]["id"]
        (self.updir / "ops" / "results").mkdir(parents=True)
        (self.updir / "ops" / "results" / f"{sid}.json").write_text(json.dumps(
            {"ok": True, "candidates": [{"name": "old-feature", "why": "already merged into main"}, {"name": "main", "why": "x"},
                                        {"name": "-rf", "why": "x"}]}))
        (msg,) = ops.tick()
        self.assertIn("old-feature", msg)
        self.assertNotIn("<code>main</code>", msg)                                # never offered, whatever a result says
        self.assertNotIn("-rf", msg)
        self.assertIn("yes ops", msg)
        self.assertEqual(ops.tick(), [])                                          # reported once

    def test_update_result_runs_the_econ_check(self):
        oid = ops.propose("update").split("<code>")[1].split("</code>")[0]
        ops.decide(oid, True)
        (self.updir / "ops" / "results").mkdir(parents=True)
        (self.updir / "ops" / "results" / f"{oid}.json").write_text(json.dumps({"ok": True, "detail": "deploy healthy"}))
        (msg,) = ops.tick()
        self.assertIn("updated and healthy", msg)
        self.assertIn("Economic Engine check", msg)

    def test_commands(self):
        f = commands.Foundation(run_agent=None, notify=None)
        self.assertIn("Update AURIX", asyncio.run(f.handle(*commands.parse("update aurix"))))
        self.assertIn("CI private-patterns", asyncio.run(f.handle(*commands.parse("ci secret"))))
        self.assertIn("read-only", asyncio.run(f.handle(*commands.parse("repo cleanup"))))
        self.assertIn("update", asyncio.run(f.handle(*commands.parse("ops"))))


class HostAgentTests(OpsBase):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "github.git"
        self.src = self.tmp / "src"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.tmp, "init", "-q", "-b", "main", str(self.src))
        git(self.src, "remote", "add", "origin", str(self.remote))
        (self.src / "a.txt").write_text("hello\n")
        git(self.src, "add", ".")
        git(self.src, "commit", "-qm", "base")
        git(self.src, "push", "-q", "origin", "main")
        git(self.src, "branch", "merged-one")
        git(self.src, "checkout", "-qb", "leaky")
        (self.src / "b.txt").write_text("server at secret-host-7070\n")
        git(self.src, "add", ".")
        git(self.src, "commit", "-qm", "leak")
        git(self.src, "checkout", "-qb", "work-in-progress", "main")
        (self.src / "c.txt").write_text("unmerged, clean\n")
        git(self.src, "add", ".")
        git(self.src, "commit", "-qm", "wip")
        git(self.src, "checkout", "-q", "main")
        git(self.src, "push", "-q", "origin", "merged-one", "leaky", "work-in-progress")
        git(self.src, "fetch", "-q", "origin")
        git(self.src, "remote", "set-head", "origin", "main")                         # what git >= 2.48 does on its own (CI caught this)
        info = self.src / ".git" / "info"
        info.mkdir(exist_ok=True)
        (info / "aurix-private-patterns").write_text("secret-host-7070\n")
        q = mock.patch.object(agent, "SOURCE", self.src)
        q.start()
        self.addCleanup(q.stop)

    def request(self, op, branches=None, sign=True, expires="2999-01-01T00:00:00+00:00"):
        oid = "o-" + os.urandom(3).hex()
        item = {"id": oid, "op": op, "branches": branches or [], "created": agent.now_iso(), "expires": expires}
        item["sig"] = agent.sign(bytes.fromhex(KEY), item) if sign else "0" * 64
        d = self.updir / "ops" / "approved"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{oid}.json").write_text(json.dumps(item))
        return oid

    def result(self, oid):
        return json.loads((self.updir / "ops" / "results" / f"{oid}.json").read_text())

    def remote_branches(self):
        return sorted(x.split("refs/heads/")[1] for x in git(self.remote, "for-each-ref", "--format=%(refname)").split())

    def test_scan_finds_merged_and_leaky_branches_only(self):
        oid = self.request("scan")
        agent.cycle()
        names = {c["name"]: c["why"] for c in self.result(oid)["candidates"]}
        self.assertEqual(set(names), {"merged-one", "leaky"})
        self.assertIn("private patterns", names["leaky"])
        self.assertNotIn("secret-host-7070", json.dumps(self.result(oid)))         # the pattern itself is never written anywhere

    def test_prune_deletes_exactly_the_approved_candidates(self):
        oid = self.request("prune", [{"name": "merged-one"}, {"name": "work-in-progress"}, {"name": "main"}])
        agent.cycle()
        self.assertEqual(self.remote_branches(), ["leaky", "main", "work-in-progress"])
        res = self.result(oid)
        self.assertFalse(res["ok"])
        self.assertIn("skipped work-in-progress, main", res["detail"])

    def test_unsigned_or_expired_requests_are_refused(self):
        bad = self.request("prune", [{"name": "merged-one"}], sign=False)
        old = self.request("prune", [{"name": "merged-one"}], expires="2000-01-01T00:00:00+00:00")
        agent.cycle()
        self.assertIn("bad signature", self.result(bad)["detail"])
        self.assertEqual(self.result(old)["detail"], "expired")
        self.assertIn("merged-one", self.remote_branches())

    def test_update_runs_sync_then_deploy_and_stops_if_sync_fails(self):
        scripts = self.src / "scripts"
        scripts.mkdir()
        log = self.tmp / "ran.txt"
        (scripts / "aurix_git_sync.sh").write_text(f"echo sync >> {log}; exit ${{SYNC_RC:-0}}\n")
        (scripts / "aurix_deploy.sh").write_text(f"echo deploy >> {log}; echo healthy\n")
        oid = self.request("update")
        agent.cycle()
        self.assertTrue(self.result(oid)["ok"])
        self.assertEqual(log.read_text().split(), ["sync", "deploy"])
        log.unlink()
        with mock.patch.dict(os.environ, {"SYNC_RC": "1"}):
            oid = self.request("update")
            agent.cycle()
        self.assertFalse(self.result(oid)["ok"])
        self.assertEqual(log.read_text().split(), ["sync"])                           # no deploy after a failed sync

    def test_secret_explains_what_is_missing(self):
        with mock.patch.object(agent.shutil, "which", return_value=None):
            oid = self.request("secret")
            agent.cycle()
        self.assertIn("not installed", self.result(oid)["detail"])

    def test_the_upgrade_agent_cron_runs_this_lane(self):
        import aurix_upgrade_agent
        oid = self.request("scan")
        with mock.patch.object(aurix_upgrade_agent, "process_approved", return_value=[]), \
                mock.patch.object(aurix_upgrade_agent, "_paused", return_value=False):
            aurix_upgrade_agent.cycle()
        self.assertTrue(self.result(oid)["ok"])


if __name__ == "__main__":
    unittest.main()
