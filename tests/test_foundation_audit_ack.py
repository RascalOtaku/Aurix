"""Owner-acknowledged audit forks: an acknowledgement may skip exactly one named orphan record and can hide nothing else."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, heartbeat, identity, sysview  # noqa: E402


def make_record(seq, prev, event="e", **fields):
    rec = {**fields, "seq": seq, "ts": "2026-09-19T22:00:00-0600", "event": event, "prev": prev}
    rec["hash"] = audit._digest(prev, rec)
    return rec


def write_lines(records):
    audit.audit_path().parent.mkdir(parents=True, exist_ok=True)
    with open(audit.audit_path(), "w", encoding="utf-8") as f:
        for r in records:
            f.write(audit._canon(r) + "\n")


def ack_for(orphan, **over):
    entry = {"seq": orphan["seq"], "hash": orphan["hash"], "prev": orphan["prev"], "reason": "test", "acknowledged_by": "owner"}
    entry.update(over)
    return {"acknowledged_orphans": [entry]}


def write_ack(obj):
    audit.ack_path().parent.mkdir(parents=True, exist_ok=True)
    audit.ack_path().write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()
        sysview._verify_cache.update(key=None, at=0.0, value=None)
        # the real incident: a healthy chain 1..7, then TWO records both claiming seq 8 with the same parent
        self.chain = []
        prev = audit.GENESIS
        for i in range(1, 8):
            r = make_record(i, prev, "skill_proposed", n=i)
            self.chain.append(r)
            prev = r["hash"]
        self.orphan = make_record(8, prev, "skill_used", name="reverse_text")        # written by the second process (came first)
        self.real8 = make_record(8, prev, "requested", tool="app_api")               # the app's own next record
        self.rest = []
        p = self.real8["hash"]
        for i in range(9, 12):
            r = make_record(i, p, "approved", n=i)
            self.rest.append(r)
            p = r["hash"]
        self.forked = self.chain + [self.orphan, self.real8] + self.rest
        write_lines(self.forked)

    def tearDown(self):
        audit._heads.clear()
        self.env.stop()
        self.tmp.cleanup()


class AcknowledgementTests(Base):
    def test_the_fork_is_reported_without_an_acknowledgement(self):
        v = audit.verify()
        self.assertFalse(v.ok)
        self.assertIn("sequence gap", v.reason)
        self.assertEqual(v.acknowledged, [])

    def test_acknowledging_exactly_that_orphan_makes_the_chain_verify(self):
        write_ack(ack_for(self.orphan))
        v = audit.verify()
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.acknowledged, [8])
        self.assertEqual(v.records, 11)                                 # seqs 1..7, the surviving 8, then 9..11; the orphan is not part of the chain
        self.assertEqual(v.head_hash, self.rest[-1]["hash"])

    def test_a_healthy_chain_reports_no_acknowledgements(self):
        write_lines(self.chain + [self.real8] + self.rest)
        write_ack(ack_for(self.orphan))                                 # an unused acknowledgement is harmless
        v = audit.verify()
        self.assertTrue(v.ok)
        self.assertEqual(v.acknowledged, [])

    def test_acknowledging_the_wrong_side_of_the_fork_does_not_work(self):
        write_ack(ack_for(self.real8))
        self.assertFalse(audit.verify().ok)

    def test_a_different_hash_acknowledges_nothing(self):
        flipped = ("0" if self.orphan["hash"][0] != "0" else "1") + self.orphan["hash"][1:]
        write_ack(ack_for(self.orphan, hash=flipped))
        self.assertFalse(audit.verify().ok)

    def test_editing_the_acknowledged_record_is_still_caught(self):
        edited = dict(self.orphan, name="something_else")               # same claimed hash, different content
        write_lines(self.chain + [edited, self.real8] + self.rest)
        write_ack(ack_for(self.orphan))
        v = audit.verify()
        self.assertFalse(v.ok)
        self.assertIn("altered", v.reason)

    def test_the_acknowledgement_must_match_seq_and_parent(self):
        write_ack(ack_for(self.orphan, seq=9))
        self.assertFalse(audit.verify().ok)
        write_ack(ack_for(self.orphan, prev="f" * 64))
        self.assertFalse(audit.verify().ok)

    def test_it_cannot_hide_a_deleted_record(self):
        write_ack(ack_for(self.orphan))
        write_lines(self.chain[:4] + self.chain[5:] + [self.orphan, self.real8] + self.rest)          # record 5 removed
        self.assertFalse(audit.verify().ok)

    def test_it_cannot_hide_an_edited_ordinary_record(self):
        write_ack(ack_for(self.orphan))
        tampered = [dict(r) for r in self.forked]
        tampered[2]["n"] = 999
        write_lines(tampered)
        self.assertFalse(audit.verify().ok)

    def test_a_second_different_fork_still_alarms(self):
        write_ack(ack_for(self.orphan))
        second_orphan = make_record(10, self.rest[0]["hash"], "skill_used", name="another")          # a new fork at seq 10
        write_lines(self.chain + [self.orphan, self.real8, self.rest[0], second_orphan, self.rest[1], self.rest[2]])
        v = audit.verify()
        self.assertFalse(v.ok)

    def test_reordering_is_still_caught(self):
        write_ack(ack_for(self.orphan))
        write_lines(self.chain + [self.real8, self.orphan] + self.rest)                                # orphan now AFTER its sibling
        self.assertFalse(audit.verify().ok)

    def test_a_broken_acknowledgement_file_acknowledges_nothing_and_never_raises(self):
        for junk in ("{not json", "[]", "null", '{"acknowledged_orphans": "x"}', '{"acknowledged_orphans": [1, "a", null]}',
                     json.dumps({"acknowledged_orphans": [{"seq": "8", "hash": "abc", "prev": "def"}]}),
                     json.dumps({"acknowledged_orphans": [{"seq": 8, "hash": self.orphan["hash"].upper(), "prev": self.orphan["prev"]}]})):
            write_ack(junk)
            self.assertEqual(audit.load_acknowledgements(), {}, junk)
            self.assertFalse(audit.verify().ok, junk)
        audit.ack_path().unlink()
        self.assertFalse(audit.verify().ok)                                                            # no file at all

    def test_a_path_given_as_a_plain_string_finds_its_acknowledgement_too(self):
        # found by the immudb proof of concept, which passed "/audit/audit.jsonl" as a str: verify() silently ignored the ack file
        write_ack(ack_for(self.orphan))
        v = audit.verify(str(audit.audit_path()))
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.acknowledged, [8])
        self.assertEqual(audit.ack_path(str(audit.audit_path())), audit.ack_path())

    def test_new_appends_still_chain_from_the_real_head(self):
        write_ack(ack_for(self.orphan))
        rec = audit.append("after_the_fix")
        self.assertEqual((rec["seq"], rec["prev"]), (12, self.rest[-1]["hash"]))
        v = audit.verify()
        self.assertTrue(v.ok, v.reason)
        self.assertEqual(v.records, 12)                                 # the 11-record real chain plus the new one


class SurfacingTests(Base):
    def test_status_texts_say_the_fork_is_acknowledged_and_alarm_clears(self):
        write_ack(ack_for(self.orphan))
        snap = sysview.snapshot({"system": lambda: {}, "endpoints": lambda: [], "sandbox": lambda: {"available": True},
                                 "telegram": lambda: {}, "governor": lambda: {}})
        self.assertTrue(snap["audit"]["ok"])
        self.assertNotEqual(snap["organs"]["immune"]["status"], sysview.BAD)
        self.assertIn("acknowledged fork", snap["organs"]["immune"]["metrics"]["audit chain"])
        text = heartbeat.self_report(__import__("src.foundation.mission", fromlist=["x"]).MissionStore())
        self.assertIn("owner-acknowledged fork record: #8", text)
        self.assertNotIn("TAMPERING", text)

    def test_without_the_acknowledgement_everything_stays_red(self):
        snap = sysview.snapshot({"system": lambda: {}, "endpoints": lambda: [], "sandbox": lambda: {"available": True},
                                 "telegram": lambda: {}, "governor": lambda: {}})
        self.assertFalse(snap["audit"]["ok"])
        self.assertEqual(snap["organs"]["immune"]["status"], sysview.BAD)
        self.assertIn("TAMPERING", heartbeat.self_report(__import__("src.foundation.mission", fromlist=["x"]).MissionStore()))

    def test_the_acknowledgement_file_and_lock_are_protected_components(self):
        for path in ("data/audit_ack.json", "data/audit.jsonl", "data/audit.jsonl.lock"):
            self.assertEqual(identity.protected_component_for(path), "audit_history", path)


if __name__ == "__main__":
    unittest.main()
