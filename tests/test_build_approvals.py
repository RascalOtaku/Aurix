"""Build-pipeline approval cards (aurix_approval.py) reach the owner through the outbox, keep their buttons, and resolve for real."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aurix_approval import ApprovalManager  # noqa: E402
from aurix_build_brain import RecoveryState  # noqa: E402
from src.foundation import build_approvals, buttons, commands, outbox  # noqa: E402


def at(h, m=0):
    return datetime(2026, 10, 8, h, m, tzinfo=timezone.utc).timestamp()


class BuildApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.brain = Path(self.tmp.name) / "brain-state"
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_BRAIN_STATE": str(self.brain),
                                         "AURIX_TIMEZONE": "UTC", "AURIX_QUIET_HOURS": "", "AURIX_PULSE_QUIET_HOURS": ""})
        p.start()
        self.addCleanup(p.stop)
        self.mgr = ApprovalManager(recovery=RecoveryState(state_dir=str(self.brain)))

    def request(self, action="deploy", header=None):
        req = self.mgr.request(action=action, target="services/web", reason="touches production config",
                               risk_details={"gates": [["prod", "APPROVAL", "production path"]]})
        if header:                                      # an urgent request, as the pipeline would mark it
            path = self.brain / "contracts" / f"approval-{req.id}.json"
            data = json.loads(path.read_text())
            data["approval"]["action"] = header
            path.write_text(json.dumps(data))
        return req

    def test_a_pending_request_becomes_one_card_once(self):
        req = self.request()
        (text, kb), = build_approvals.scan()
        self.assertIn("Approval needed: deploy", text)
        self.assertIn("touches production config", text)
        self.assertIn(f"yes build {req.id}", text)
        self.assertEqual(build_approvals.scan(), [])                       # delivered once
        rows = buttons.for_reply("build_approval", text)["inline_keyboard"]
        cmd = buttons.command_from_data(rows[0][0]["callback_data"])
        self.assertEqual(commands.parse(cmd), ("build_yes", req.id))
        self.assertIn("build_yes", buttons.ALLOWED_KINDS)

    def test_the_owner_decision_reaches_the_pipeline(self):
        req = self.request()
        self.assertIn("Approved", build_approvals.resolve(req.id, True))
        self.assertEqual(self.mgr.check(req.id).status, "approved")
        fresh = ApprovalManager(recovery=RecoveryState(state_dir=str(self.brain)))   # what the brain process sees
        self.assertEqual(fresh.check(req.id).status, "approved")
        self.assertIn("already approved", build_approvals.resolve(req.id, False))
        self.assertIn("not a build approval id", build_approvals.resolve("../etc", True))

    def test_a_2am_card_waits_for_the_morning_with_its_buttons(self):
        self.request()
        (text, _), = build_approvals.scan()
        kb = buttons.for_reply("build_approval", text)
        self.assertEqual(outbox.decide(text, at(2), buttons=kb), "hold")
        summary, card = outbox.due_items(at(6, 1))
        self.assertIn("buttons and all", summary[0])
        self.assertEqual(card, (text, kb))

    def test_an_urgent_card_may_wake_you(self):
        self.request(header="🚨 rollback failed")
        (text, _), = build_approvals.scan()
        self.assertTrue(text.startswith("🚨"))
        self.assertEqual(outbox.decide(text, at(2)), "send")

    def test_missing_or_broken_state_never_raises(self):
        os.environ["AURIX_BRAIN_STATE"] = str(Path(self.tmp.name) / "nowhere")
        self.assertEqual(build_approvals.scan(), [])
        (self.brain / "contracts").mkdir(parents=True, exist_ok=True)
        os.environ["AURIX_BRAIN_STATE"] = str(self.brain)
        (self.brain / "contracts" / "approval-bad.json").write_text("{not json")
        self.assertEqual(build_approvals.scan(), [])


if __name__ == "__main__":
    unittest.main()
