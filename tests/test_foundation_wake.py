"""Wake-on-LAN for the GPU box: a fire-and-forget magic packet, cooldown-gated so callers (every LLM
call, every GamePilot arm) can call it unconditionally without ever spamming the LAN."""
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.foundation import wake  # noqa: E402


class MagicPacketTests(unittest.TestCase):
    def test_packet_shape(self):
        p = wake.build_magic_packet("AA:BB:CC:DD:EE:FF")
        self.assertEqual(len(p), 102)
        self.assertEqual(p[:6], b"\xff" * 6)
        mac = bytes.fromhex("AABBCCDDEEFF")
        self.assertEqual(p[6:12], mac)
        self.assertEqual(p.count(mac), 16)

    def test_accepts_dash_separated_mac(self):
        self.assertEqual(wake.build_magic_packet("aa-bb-cc-dd-ee-ff"), wake.build_magic_packet("AA:BB:CC:DD:EE:FF"))

    def test_bad_mac_is_rejected(self):
        for bad in ("", "not-a-mac", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00"):
            with self.assertRaises(ValueError):
                wake.build_magic_packet(bad)


class SendTests(unittest.TestCase):
    def test_sends_a_udp_broadcast_to_the_configured_target(self):
        sent = {}

        class FakeSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def setsockopt(self, *a):
                sent["broadcast_set"] = True

            def sendto(self, data, addr):
                sent["data"], sent["addr"] = data, addr

        with mock.patch.object(socket, "socket", return_value=FakeSock()):
            ok = wake.send_magic_packet("AA:BB:CC:DD:EE:FF", "10.0.0.255", 9)
        self.assertTrue(ok)
        self.assertTrue(sent["broadcast_set"])
        self.assertEqual(sent["addr"], ("10.0.0.255", 9))
        self.assertEqual(len(sent["data"]), 102)

    def test_a_bad_mac_returns_false_without_touching_the_network(self):
        with mock.patch.object(socket, "socket") as sock:
            self.assertFalse(wake.send_magic_packet("nope"))
        sock.assert_not_called()

    def test_a_network_error_returns_false_not_raises(self):
        class BoomSock:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def setsockopt(self, *a):
                pass

            def sendto(self, *a):
                raise OSError("network unreachable")

        with mock.patch.object(socket, "socket", return_value=BoomSock()):
            self.assertFalse(wake.send_magic_packet())


class GatedWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def events(self):
        from src.foundation import audit
        try:
            with open(audit.audit_path(), encoding="utf-8") as f:
                return [json.loads(l)["event"] for l in f if l.strip()]
        except OSError:
            return []

    def test_first_call_sends_and_audits(self):
        with mock.patch.object(wake, "send_magic_packet", return_value=True) as send:
            self.assertTrue(wake.wake_gpu_box(now=1000.0, reason="test"))
        send.assert_called_once()
        self.assertIn("gpu_box_wake_sent", self.events())

    def test_a_second_call_inside_the_cooldown_is_skipped(self):
        with mock.patch.object(wake, "send_magic_packet", return_value=True) as send:
            wake.wake_gpu_box(now=1000.0)
            again = wake.wake_gpu_box(now=1000.0 + wake.COOLDOWN_SECONDS - 1)
        self.assertFalse(again)
        send.assert_called_once()

    def test_a_call_after_the_cooldown_sends_again(self):
        with mock.patch.object(wake, "send_magic_packet", return_value=True) as send:
            wake.wake_gpu_box(now=1000.0)
            later = wake.wake_gpu_box(now=1000.0 + wake.COOLDOWN_SECONDS + 1)
        self.assertTrue(later)
        self.assertEqual(send.call_count, 2)

    def test_a_failed_send_is_not_audited_but_still_updates_the_cooldown(self):
        with mock.patch.object(wake, "send_magic_packet", return_value=False):
            ok = wake.wake_gpu_box(now=1000.0)
        self.assertFalse(ok)
        self.assertNotIn("gpu_box_wake_sent", self.events())
        state = json.loads((Path(self.tmp.name) / "data" / "wake" / "state.json").read_text())
        self.assertEqual(state["last_sent"], 1000.0)


if __name__ == "__main__":
    unittest.main()
