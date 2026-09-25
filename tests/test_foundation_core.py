"""Tests for src/foundation identity + audit (stdlib only)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import audit, identity  # noqa: E402


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name,
                                                "TELEGRAM_CHAT_ID": "42"})
        self.env.start()
        audit._heads.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        audit._heads.clear()


class AuditChainTests(_Tmp):
    def _fill(self, n=5):
        for i in range(n):
            audit.append("evt", i=i, note=f"row {i}", path=Path("/tmp/x"))

    def _lines(self):
        return audit.audit_path().read_text(encoding="utf-8").splitlines()

    def _write(self, lines):
        audit.audit_path().write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_clean_chain_verifies_and_head_advances(self):
        self._fill(5)
        r = audit.verify()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.records, 5)
        seq, h = audit.head()
        self.assertEqual((seq, h), (5, r.head_hash))

    def test_empty_or_missing_file_is_ok(self):
        self.assertTrue(audit.verify().ok)

    def test_editing_a_record_is_detected(self):
        self._fill(5)
        lines = self._lines()
        rec = json.loads(lines[2])
        rec["note"] = "tampered"
        lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        self._write(lines)
        r = audit.verify()
        self.assertFalse(r.ok)
        self.assertEqual(r.bad_seq, 3)
        self.assertIn("altered", r.reason)

    def test_deleting_a_record_is_detected(self):
        self._fill(5)
        lines = self._lines()
        del lines[1]
        self._write(lines)
        r = audit.verify()
        self.assertFalse(r.ok)
        self.assertEqual(r.bad_seq, 2)

    def test_reordering_is_detected(self):
        self._fill(5)
        lines = self._lines()
        lines[1], lines[2] = lines[2], lines[1]
        self._write(lines)
        self.assertFalse(audit.verify().ok)

    def test_forged_record_with_recomputed_hash_still_breaks_the_next_link(self):
        self._fill(4)
        lines = self._lines()
        rec = json.loads(lines[1])
        rec["note"] = "forged"
        rec.pop("hash")
        rec["hash"] = audit._digest(rec["prev"], rec)   # attacker fixes THIS record's hash...
        lines[1] = audit._canon(rec)
        self._write(lines)
        r = audit.verify()                               # ...but record 3's prev no longer matches
        self.assertFalse(r.ok)
        self.assertEqual(r.bad_seq, 3)

    def test_chain_continues_after_restart(self):
        self._fill(3)
        audit._heads.clear()                             # simulate a new process
        audit.append("after_restart")
        r = audit.verify()
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.records, 4)

    def test_caller_fields_cannot_forge_chain_fields(self):
        rec = audit.append("real", seq=999, hash="x", prev="y", event="fake")
        self.assertEqual(rec["seq"], 1)
        self.assertEqual(rec["event"], "real")
        self.assertTrue(audit.verify().ok)

    def test_append_never_raises(self):
        with mock.patch.object(audit, "audit_path", side_effect=OSError("disk gone")):
            self.assertEqual(audit.append("x"), {})


class IdentityTests(_Tmp):
    def test_owner_only_via_authenticated_channel(self):
        ident = identity.load_identity()
        self.assertEqual(ident.tier_for("telegram", "42"), identity.TrustTier.OWNER)
        self.assertEqual(ident.tier_for("telegram", 42), identity.TrustTier.OWNER)
        self.assertEqual(ident.tier_for("telegram", "43"), identity.TrustTier.UNTRUSTED)
        self.assertEqual(ident.tier_for("email", "42"), identity.TrustTier.UNTRUSTED)
        self.assertEqual(identity.AurixIdentity().tier_for("telegram", ""), identity.TrustTier.UNTRUSTED)

    def test_identity_file_overrides_env(self):
        p = identity.identity_path()
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"owner_name": "Someone", "owner_channels": {"telegram": "7"}}))
        ident = identity.load_identity()
        self.assertEqual(ident.owner_name, "Someone")
        self.assertEqual(ident.tier_for("telegram", "7"), identity.TrustTier.OWNER)

    def test_protected_paths_all_eight_components(self):
        cases = {
            "/app/src/approval_gate.py": "approval_gate",
            "C:\\x\\odysseus\\src\\tool_execution.py": "approval_gate",
            "/app/src/foundation/risk.py": "sentinel_safety_logic",
            "/app/services/telegram/listener.py": "owner_authority",
            "/app/data/missions/m-1.json": "approval_gate",
            "/app/.env": "credential_custody",
            "/app/config/vault.env": "credential_custody",
            "/app/data/ssh/id_rsa": "credential_custody",
            "/app/data/audit.jsonl": "audit_history",
            "/app/docker-compose.yml": "recovery_mechanisms",
            "/app/constitution/v7.md": "constitution",
            "/app/data/ledger/2026.csv": "financial_ledger",
        }
        for path, component in cases.items():
            self.assertEqual(identity.protected_component_for(path), component, path)

    def test_ordinary_paths_not_protected(self):
        for p in ("/app/data/missions_scratch/x", "/app/src/search.py", "/tmp/work/out.stl",
                  "/app/data/uploads/a.txt", "/app/src/approval_notes.md", "/app/README.md"):
            self.assertFalse(identity.is_protected_path(p), p)

    def test_secret_paths(self):
        for p in ("/app/.env", "/x/y/vault.env", "/home/u/.ssh/id_rsa", "/proc/self/environ",
                  "/app/data/auth.json", "server.pem"):
            self.assertTrue(identity.is_secret_path(p), p)
        for p in ("/app/README.md", "/tmp/environment.txt", "/app/src/search.py"):
            self.assertFalse(identity.is_secret_path(p), p)


if __name__ == "__main__":
    unittest.main()
