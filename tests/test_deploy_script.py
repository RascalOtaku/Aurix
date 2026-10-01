"""scripts/aurix_deploy.sh logic, run against STUB docker/curl/sleep in a temp tree - production is never touched.

Pins the 2026-09-29 change: a build whose Telegram listener is not ready is NOT healthy, so it is rolled back automatically, exactly
like any other unhealthy build; image-only rollback and the other health checks are unchanged."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aurix_deploy.sh"

DOCKER_STUB = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$*" in
  "inspect -f {{.Image}}"*)        echo sha256:oldbuild ;;
  "compose ps -q"*)                echo cid1 ;;
  "inspect -f {{.State.Running}}"*) echo true ;;
  "inspect -f {{.RestartCount}}"*) echo 0 ;;
  "compose exec -T"*)
      if [ -f "$STUB_DIR/tg_ready" ]; then exit 0; fi
      if [ -f "$STUB_DIR/tg_ready_after_rollback" ] && grep -q "compose up -d --no-build" "$STUB_LOG"; then exit 0; fi
      exit 1 ;;
  "image ls"*)                     echo rollback-20260101-000000 ;;
  *)                               exit 0 ;;
esac
'''


class DeployScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "repo" / "scripts").mkdir(parents=True)
        shutil.copy(SCRIPT, self.tmp / "repo" / "scripts" / "aurix_deploy.sh")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("docker", DOCKER_STUB), ("curl", "#!/usr/bin/env bash\necho 200\n"), ("sleep", "#!/usr/bin/env bash\nexit 0\n")):
            p = self.bin / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "docker.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_deploy(self, **env):
        e = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", STUB_LOG=str(self.log), STUB_DIR=str(self.tmp),
                 AURIX_DEPLOY_WAIT="10", **env)
        r = subprocess.run(["bash", str(self.tmp / "repo" / "scripts" / "aurix_deploy.sh")], env=e, capture_output=True, text=True, timeout=60)
        status = (self.tmp / "repo" / "data" / "deploy_status.json").read_text()
        return r.returncode, r.stdout, status, self.log.read_text()

    def test_healthy_build_with_a_ready_listener_deploys(self):
        (self.tmp / "tg_ready").touch()
        code, out, status, calls = self.run_deploy()
        self.assertEqual(code, 0, out)
        self.assertIn("DEPLOY OK", out)
        self.assertIn('"result":"ok"', status)
        self.assertIn("compose exec -T odysseus python3", calls)              # the listener really was asked
        self.assertIn("versions.record('deploy')", calls)                      # and the shipped code was recorded as a version

    def test_a_broken_listener_makes_the_build_unhealthy_and_it_rolls_back(self):
        code, out, status, calls = self.run_deploy()                         # listener never ready
        self.assertEqual(code, 3, out)
        self.assertIn("DEPLOY UNHEALTHY", out)
        self.assertIn("tag odysseus-odysseus:rollback-", calls)
        self.assertIn("compose up -d --no-build odysseus", calls)           # image rollback, as before
        self.assertIn("rollback_failed", status)                            # the old build is still checked, not assumed healthy

    def test_rollback_to_a_build_whose_listener_works_is_reported_ok(self):
        (self.tmp / "tg_ready_after_rollback").touch()
        code, out, status, calls = self.run_deploy()
        self.assertEqual(code, 3, out)
        self.assertIn("ROLLBACK OK", out)
        self.assertIn('"result":"rolled_back"', status)

    def test_the_listener_check_can_be_switched_off_explicitly(self):
        code, out, status, calls = self.run_deploy(AURIX_DEPLOY_TELEGRAM="0")
        self.assertEqual(code, 0, out)
        self.assertNotIn("api/health/telegram", calls)                        # the readiness probe was skipped

    def test_data_is_never_part_of_a_rollback(self):
        code, out, status, calls = self.run_deploy()
        self.assertNotRegex(calls, r"(rm|cp|mv|rsync)\b.*data")


if __name__ == "__main__":
    unittest.main()
