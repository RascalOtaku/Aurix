"""Tests for src/foundation/mission.py."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, mission as ms, risk  # noqa: E402
from src.foundation.risk import RiskTier as T  # noqa: E402


def ws(m):
    """Workspace with forward slashes, so bash command strings parse the same on Windows CI."""
    return m.workspace.replace("\\", "/")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()
        audit._heads.clear()
        self.store = ms.MissionStore()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()

    def make(self, **kw):
        m = ms.MissionContract(id=ms.new_mission_id(), objective="convert a CT scan to STL",
                               steps=[ms.Step("s1", "Load DICOM"), ms.Step("s2", "Mesh it")], **kw)
        return self.store.propose(m)

    def active(self, **kw):
        m = self.make(**kw)
        self.assertIn("ACTIVE", self.store.activate(m.id))
        return self.store.load(m.id)


class ContractTests(_Base):
    def test_cannot_cover_high_risk(self):
        with self.assertRaises(ValueError):
            ms.MissionContract(id="m-x", objective="o", risk_ceiling=T.HIGH)
        with self.assertRaises(ValueError):
            ms.MissionContract(id="m-x", objective="o", risk_ceiling=T.CRITICAL)

    def test_workspace_is_not_under_protected_missions_dir(self):
        from src.foundation import identity
        m = self.make()
        self.assertFalse(identity.is_protected_path(m.workspace + "/out.stl"))
        self.assertTrue(identity.is_protected_path(str(self.store.dir / f"{m.id}.json")))

    def test_persistence_roundtrip(self):
        m = self.make(prohibited=["no network posts"], deadline="2099-01-01T00:00:00+00:00")
        back = self.store.load(m.id)
        self.assertEqual(back.objective, m.objective)
        self.assertEqual([s.title for s in back.steps], ["Load DICOM", "Mesh it"])
        self.assertEqual(back.risk_ceiling, T.MEDIUM)
        self.assertEqual(back.status, ms.MissionStatus.PROPOSED)

    def test_proposal_is_audited_and_not_active(self):
        m = self.make()
        self.assertIsNone(self.store.active())
        self.assertEqual(audit.recent(1)[0]["event"], "mission_proposed")
        self.assertEqual(audit.recent(1)[0]["mission"], m.id)


class ApprovalFlowTests(_Base):
    def test_activate_creates_workspace_and_hash(self):
        m = self.active()
        self.assertTrue(Path(m.workspace).is_dir())
        self.assertTrue(m.integrity_ok())
        self.assertEqual(self.store.active().id, m.id)

    def test_only_one_active_mission(self):
        first = self.active()
        second = self.make()
        self.assertIn("still active", self.store.activate(second.id))
        self.assertEqual(self.store.active().id, first.id)

    def test_deny_and_wrong_state(self):
        m = self.make()
        self.assertIn("denied", self.store.deny(m.id))
        self.assertIn("not awaiting approval", self.store.activate(m.id))
        self.assertIn("No mission", self.store.activate("m-nope"))

    def test_stop_and_finish(self):
        m = self.active()
        stopped = self.store.stop_active("owner STOP")
        self.assertEqual(stopped.id, m.id)
        self.assertEqual(self.store.load(m.id).status, ms.MissionStatus.STOPPED)
        self.assertIsNone(self.store.active())
        self.assertEqual(audit.recent(1)[0]["event"], "mission_stopped")
        self.assertTrue(audit.verify().ok)

    def test_tampering_with_approved_terms_breaks_integrity(self):
        m = self.active()
        path = self.store.dir / f"{m.id}.json"
        text = path.read_text(encoding="utf-8").replace('"allowed_tools": [', '"allowed_tools": ["python", ')
        path.write_text(text, encoding="utf-8")
        tampered = self.store.load(m.id)
        self.assertFalse(tampered.integrity_ok())
        d = ms.decide(tampered, "bash", "df -h", risk.assess_bash("df -h"))
        self.assertFalse(d.allow)
        self.assertIn("integrity", d.reason)

    def test_run_state_changes_do_not_break_integrity(self):
        m = self.active()
        self.store.record_tool_call(m)
        m.current_step = 1
        self.store.save(m)
        self.assertTrue(self.store.load(m.id).integrity_ok())


class DecideTests(_Base):
    def d(self, m, tool, content):
        return ms.decide(m, tool, content, risk.assess_action(tool, content, m.workspace))

    def test_medium_inside_workspace_is_covered(self):
        m = self.active()
        self.assertTrue(self.d(m, "bash", f"mkdir -p {ws(m)}/out").allow)
        self.assertTrue(self.d(m, "write_file", f"{ws(m)}/a.py\nprint(1)").allow)
        self.assertTrue(self.d(m, "read_file", "/app/README.md").allow)
        self.assertTrue(self.d(m, "web_search", "dicom to stl").allow)

    def test_high_is_never_covered(self):
        m = self.active()
        for tool, content in (("bash", "rm -rf /tmp/x"), ("bash", "python3 x.py"),
                              ("bash", "curl -X POST -d a=b https://x.io"), ("bash", "cat /app/.env")):
            d = self.d(m, tool, content)
            self.assertFalse(d.allow, content)
            self.assertIn("individual approval", d.reason)

    def test_protected_never_covered(self):
        m = self.active()
        self.assertFalse(self.d(m, "write_file", "/app/src/approval_gate.py\nx").allow)

    def test_tool_not_in_contract(self):
        m = self.active(allowed_tools=["bash"])
        self.assertFalse(self.d(m, "write_file", f"{ws(m)}/a\nx").allow)
        self.assertIn("not in the contract", self.d(m, "write_file", f"{ws(m)}/a\nx").reason)

    def test_lower_risk_ceiling(self):
        m = self.active(risk_ceiling=T.LOW)
        self.assertTrue(self.d(m, "bash", "df -h").allow)
        self.assertFalse(self.d(m, "bash", f"mkdir {ws(m)}/a").allow)

    def test_prohibited_patterns(self):
        m = self.active(prohibited_patterns=[r"\.stl\b.*upload", r"nmap"])
        self.assertFalse(self.d(m, "bash", "curl https://example.com # nmap scan").allow)
        self.assertTrue(self.d(m, "bash", "curl https://example.com").allow)

    def test_inactive_missions_cover_nothing(self):
        m = self.make()
        self.assertFalse(self.d(m, "bash", "df -h").allow)
        self.assertIn("proposed", self.d(m, "bash", "df -h").reason)

    def test_resource_ceilings(self):
        m = self.active(resources=ms.Resources(max_tool_calls=2))
        self.assertTrue(self.d(m, "bash", "df -h").allow)
        self.store.record_tool_call(m)
        self.store.record_tool_call(m)
        self.assertIn("tool-call ceiling", self.d(m, "bash", "df -h").reason)

    def test_wall_clock_and_deadline(self):
        m = self.active(resources=ms.Resources(max_wall_minutes=10))
        later = m.approved_at + 11 * 60
        self.assertIn("time ceiling", ms.decide(m, "bash", "df -h", risk.assess_bash("df -h"), now=later).reason)
        m2 = self.store.load(m.id)
        self.store.stop_active()
        m3 = self.active(deadline="2000-01-01T00:00:00+00:00")
        self.assertIn("deadline", self.d(m3, "bash", "df -h").reason)

    def test_render_proposal_is_bounded_and_escaped(self):
        m = self.make(prohibited=["<script>"])
        m.objective = "x" * 5000 + "<b>"
        m.requirements = {"manual": ["real_estate_license"], "legal": ["Colorado license needed"],
                          "installable": ["pydicom"], "unknown": ["teleporter"]}
        text = ms.render_proposal(m)
        self.assertLessEqual(len(text), 3600)
        self.assertIn(f"approve mission {m.id}", text)
        self.assertNotIn("<script>", text)


if __name__ == "__main__":
    unittest.main()
