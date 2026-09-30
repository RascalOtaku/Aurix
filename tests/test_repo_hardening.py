"""The repository's own hardening: the secret guard, pinned CI, and no personal defaults in shipped scripts."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "git-hooks" / "secret_guard.sh"


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class SecretGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "scripts" / "git-hooks").mkdir(parents=True)
        shutil.copy(GUARD, self.repo / "scripts" / "git-hooks")

    def stage(self, name, text):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        subprocess.run(["git", "add", "-f", name], cwd=self.repo, check=True)

    def guard(self, *args):
        return subprocess.run(["bash", "scripts/git-hooks/secret_guard.sh", *args], cwd=self.repo,
                              capture_output=True, text=True)

    def test_clean_changes_pass(self):
        self.stage("app.py", "print('hello')\nURL = 'http://100.64.0.10:11434'\n")
        self.assertEqual(self.guard().returncode, 0)

    def test_tokens_are_blocked_and_not_echoed(self):
        token = "sk-or-v1-" + "a1b2c3d4" * 6
        self.stage("config.py", f"KEY = '{token}'\n")
        r = self.guard()
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(token, r.stdout)

    def test_secret_files_are_blocked(self):
        for name in (".env", "deploy/server.pem", "id_ed25519", "data/app.sqlite"):
            with self.subTest(name=name):
                subprocess.run(["git", "reset", "-q"], cwd=self.repo)
                self.stage(name, "x\n")
                self.assertEqual(self.guard().returncode, 1)
        subprocess.run(["git", "reset", "-q"], cwd=self.repo)
        self.stage(".env.example", "OPENAI_API_KEY=\n")
        self.assertEqual(self.guard().returncode, 0)

    def test_private_patterns_stay_private_and_block(self):
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "aurix-private-patterns").write_text("# mine\n100\\.99\\.88\\.77\nmy-secret-host\n")
        self.stage("notes.md", "ssh me@100.99.88.77\n")
        self.assertEqual(self.guard().returncode, 1)
        self.assertEqual(self.guard("--tree").returncode, 1)

    def test_every_token_kind_is_redacted_in_the_report(self):
        telegram = "1234567890:AA" + "b" * 33
        self.stage("bot.py", f"TOKEN = '{telegram}'\n")
        for mode in ((), ("--tree",)):
            r = self.guard(*mode)
            self.assertEqual(r.returncode, 1)
            self.assertNotIn(telegram, r.stdout)
            self.assertNotIn("b" * 33, r.stdout)

    def test_private_matches_name_the_file_without_printing_the_value(self):
        (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "aurix-private-patterns").write_text("my-secret-host\n")
        self.stage("deploy/notes.md", "ssh me@my-secret-host\n")
        for mode in ((), ("--tree",)):
            r = self.guard(*mode)
            self.assertEqual(r.returncode, 1)
            self.assertIn("deploy/notes.md", r.stdout)
            self.assertNotIn("my-secret-host", r.stdout.split("private patterns")[1] if "private patterns" in r.stdout else r.stdout)

    def test_fixture_files_may_hold_fake_tokens(self):
        self.stage("tests/test_foundation_teacher.py", "S = '" + "AKIA" + "ABCDEFGHIJKLMNOP" + "'\n")
        self.assertEqual(self.guard().returncode, 0)

    def test_the_real_tree_is_clean(self):
        r = subprocess.run(["bash", str(GUARD), "--tree"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode and "not a git repository" in r.stderr:
            self.skipTest("not a git checkout")
        self.assertEqual(r.returncode, 0, r.stdout)


class CiHardeningTests(unittest.TestCase):
    def test_workflows_pin_actions_to_commit_shas_and_read_only_token(self):
        for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
            text = wf.read_text()
            code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
            for ref in re.findall(r"uses:\s*([^\s#]+)", code):
                self.assertRegex(ref, r"@[0-9a-f]{40}$", f"{wf.name}: {ref} is not pinned to a commit SHA")
            self.assertRegex(text, r"(?m)^permissions:\s*\n\s+contents:\s*read")
            self.assertNotIn("pull_request_target", code)


class NoPersonalDefaultsTests(unittest.TestCase):
    def test_ssh_setup_script_has_no_baked_in_address_or_user(self):
        text = (ROOT / "scripts" / "windows" / "aurix-ssh-setup.ps1").read_text()
        self.assertIn("[Parameter(Mandatory = $true)][string]$Server", text)
        self.assertIsNone(re.search(r"\b100\.(6[5-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b", text))


if __name__ == "__main__":
    unittest.main()
