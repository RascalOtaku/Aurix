"""Several writers, one chain. On 2026-09-19 a script run in a SECOND process appended to the audit log while the app held a
stale cached head; both claimed seq 8, verify() failed and the watchdog told the owner the log was tampered with."""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.foundation import audit  # noqa: E402

CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r})
    from src.foundation import audit
    for i in range({n}):
        rec = audit.append("child_event", who={who!r}, i=i)
        assert rec, "append returned nothing"
""")


def spawn(tmp, who, n=1):
    env = dict(os.environ, AURIX_PROJECT_ROOT=tmp)
    return subprocess.Popen([sys.executable, "-c", CHILD.format(root=ROOT, n=n, who=who)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        audit._heads.clear()
        self.env.stop()
        self.tmp.cleanup()

    def records(self):
        with open(audit.audit_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class SecondProcessTests(Base):
    def test_the_exact_incident_a_stale_cache_then_another_process_then_us_again(self):
        for i in range(3):
            audit.append("app_event", i=i)                           # the app's process: cache is warm at seq 3
        child = spawn(self.tmp.name, "docker-exec-script")
        out, err = child.communicate(timeout=60)
        self.assertEqual(child.returncode, 0, err)                   # the other process appended seq 4
        rec = audit.append("app_event_after", i=99)                  # the app appends AGAIN with its old cached head
        v = audit.verify()
        self.assertTrue(v.ok, v.reason)
        self.assertEqual([r["seq"] for r in self.records()], [1, 2, 3, 4, 5])
        self.assertEqual(rec["seq"], 5)
        self.assertEqual(rec["prev"], self.records()[3]["hash"])     # chained to the OTHER process's record, not to seq 3

    def test_a_cold_cache_and_a_warm_cache_agree(self):
        audit.append("a")
        audit.append("b")
        warm = audit.head()
        audit._heads.clear()                                          # a brand-new process
        self.assertEqual(audit.append("c")["prev"], warm[1])
        self.assertTrue(audit.verify().ok)

    def test_many_processes_and_threads_at_once_still_form_one_valid_chain(self):
        procs = [spawn(self.tmp.name, f"p{i}", n=20) for i in range(4)]
        threads = [threading.Thread(target=lambda: [audit.append("thread_event", n=n) for n in range(20)]) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
        recs = self.records()
        self.assertEqual(len(recs), 4 * 20 + 3 * 20)
        self.assertEqual([r["seq"] for r in recs], list(range(1, len(recs) + 1)))       # no duplicates, no gaps
        v = audit.verify()
        self.assertTrue(v.ok, v.reason)
        self.assertEqual({r["who"] for r in recs if r["event"] == "child_event"}, {"p0", "p1", "p2", "p3"})


class UnchangedBehaviourTests(Base):
    def test_verification_still_catches_edits_deletions_and_reorders(self):
        for i in range(5):
            audit.append("e", i=i)
        p = audit.audit_path()
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")            # delete record 3
        self.assertFalse(audit.verify().ok)
        p.write_text("\n".join(lines).replace('"i":1', '"i":7') + "\n", encoding="utf-8")   # alter record 2
        self.assertFalse(audit.verify().ok)
        p.write_text("\n".join([lines[0], lines[2], lines[1]] + lines[3:]) + "\n", encoding="utf-8")   # reorder
        self.assertFalse(audit.verify().ok)

    def test_an_already_forked_log_is_still_reported_as_broken(self):
        # the fix prevents NEW forks; it must not paper over one that exists
        audit.append("one")
        audit.append("two")
        p = audit.audit_path()
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8")               # a duplicate seq 2
        v = audit.verify()
        self.assertFalse(v.ok)
        self.assertIn("sequence gap", v.reason)

    def test_long_records_and_an_unparseable_tail_still_recover_the_head(self):
        audit.append("small")
        audit.append("huge", blob="x" * (audit._TAIL_BYTES + 5000))                        # last line longer than the tail window
        audit._heads.clear()
        rec = audit.append("after_huge")
        self.assertEqual(rec["seq"], 3)
        self.assertTrue(audit.verify().ok)

    def test_append_never_raises_and_the_lock_file_is_a_sidecar(self):
        with mock.patch.object(audit, "_tail_head", side_effect=RuntimeError("boom")):
            self.assertEqual(audit.append("x"), {})                                        # audit must never take down the action path
        audit.append("ok")
        self.assertTrue(audit.audit_path().with_name("audit.jsonl.lock").exists())
        self.assertTrue(audit.verify().ok)                                                 # the audit file itself has only records

    def test_a_lock_that_cannot_be_taken_degrades_gracefully(self):
        module, func = ("msvcrt", "locking") if os.name == "nt" else ("fcntl", "flock")
        lock_module = __import__(module)
        with mock.patch.object(lock_module, func, side_effect=OSError("locks not supported here")):
            for i in range(3):
                self.assertTrue(audit.append("nolock", i=i))                              # still appends...
        self.assertEqual([r["seq"] for r in self.records()], [1, 2, 3])
        self.assertTrue(audit.verify().ok)                                                # ...and the chain is still valid


if __name__ == "__main__":
    unittest.main()
