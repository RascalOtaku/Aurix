"""GamePilot relay: pairing, sessions, arming (incl. STOP), the input allow-list, the signed agent channel, frames, and the wiring."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import approval_gate as ag  # noqa: E402
from src.foundation import actions, audit, buttons, commands, gamepilot as gp, identity  # noqa: E402

KEY_HEX = "9a" * 32
KEY = bytes.fromhex(KEY_HEX)
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 500 + b"\xff\xd9"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": str(t), "AURIX_BRAIN": str(t / "brain"), "AURIX_GAMING_HMAC_KEY": KEY_HEX})
        self.env.start()
        ag.reset_state()
        audit._heads.clear()
        gp.reset_memory()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ag.reset_state()
        audit._heads.clear()
        gp.reset_memory()

    def pair(self, now=None):
        r = gp.new_pairing(now)
        gp.confirm_pairing(r["code"], now)
        return gp.pair_status(r["poll"], now)["token"]


class PairingTests(_Base):
    def test_a_browser_only_gets_a_session_after_the_owner_confirms_the_code(self):
        r = gp.new_pairing()
        self.assertRegex(r["code"], r"^\d{6}$")
        self.assertEqual(gp.pair_status(r["poll"])["state"], "waiting")
        self.assertIn("not waiting", gp.confirm_pairing("000000"))
        self.assertIn("6 digits", gp.confirm_pairing("12"))
        self.assertIn("Paired", gp.confirm_pairing(r["code"]))
        got = gp.pair_status(r["poll"])
        self.assertEqual(got["state"], "paired")
        self.assertIsNotNone(gp.session(got["token"]))
        self.assertEqual(gp.pair_status(r["poll"])["state"], "expired")               # the token is handed over exactly once

    def test_codes_expire_and_pending_codes_are_capped(self):
        r = gp.new_pairing(now=1000.0)
        self.assertIn("not waiting", gp.confirm_pairing(r["code"], now=1000.0 + gp.PAIR_TTL + 1))
        self.assertEqual(gp.pair_status(r["poll"], now=1000.0 + gp.PAIR_TTL + 1)["state"], "expired")
        gp.reset_memory()
        for _ in range(gp.PAIR_MAX_PENDING):
            self.assertIn("code", gp.new_pairing())
        self.assertIn("error", gp.new_pairing())

    def test_sessions_expire_and_can_be_revoked_and_only_a_hash_is_stored(self):
        tok = self.pair(now=1000.0)
        raw = (Path(os.environ["AURIX_PROJECT_ROOT"]) / "data" / "gamepilot" / "sessions.json").read_text()
        self.assertNotIn(tok, raw)
        self.assertIsNotNone(gp.session(tok, now=1000.0 + 5))
        self.assertIsNone(gp.session(tok, now=1000.0 + gp.SESSION_DAYS * 86400 + 1))
        self.assertIsNone(gp.session("nonsense"))
        self.assertIsNone(gp.session(""))
        self.assertIn("Unpaired 1", gp.unpair_all())
        self.assertIsNone(gp.session(tok, now=1000.0 + 5))


class ArmingTests(_Base):
    def test_arming_needs_a_paired_screen_expires_and_is_capped(self):
        self.assertIn("Nothing is paired", gp.arm())
        tok = self.pair()
        self.assertEqual(gp.armed_until(), 0.0)
        self.assertIn("Armed for 15", gp.arm())
        self.assertGreater(gp.armed_until(), time.time())
        self.assertIn("Armed for 60", gp.arm(500))
        self.assertLessEqual(gp.armed_until() - time.time(), 60 * 60 + 2)
        self.assertEqual(gp.armed_until(now=time.time() + 61 * 60), 0.0)
        self.assertIn("Disarmed", gp.disarm())
        self.assertEqual(gp.armed_until(), 0.0)
        self.assertTrue(tok)

    def test_stop_disarms_at_once(self):
        self.pair()
        gp.arm()
        self.assertGreater(gp.armed_until(), 0)
        ag.engage_stop(60)
        self.assertEqual(gp.armed_until(), 0.0)
        ag.clear_stop()

    def test_audit_has_states_never_input(self):
        tok = self.pair()
        gp.arm(5)
        gp.submit_input(tok, [{"t": "key", "k": "w", "d": "tap"}])
        gp.disarm()
        events = [e["event"] for e in audit.recent(10)]
        self.assertEqual([x for x in events if x.startswith("gamepilot")], ["gamepilot_paired", "gamepilot_armed", "gamepilot_disarmed"])
        self.assertNotIn('"k"', json.dumps(audit.recent(10)))


class InputTests(_Base):
    def test_the_allow_list(self):
        good = [{"t": "key", "k": "w", "d": "down"}, {"t": "key", "k": "escape"}, {"t": "key", "k": "f5", "d": "tap"}, {"t": "mouse", "x": 0.5, "y": 0.25, "b": "left", "d": "click"},
                {"t": "wheel", "dy": 3}, {"t": "rel", "dx": 10, "dy": -5}]
        for ev in good:
            self.assertIsNotNone(gp.clean_event(ev), ev)
        bad = [{"t": "key", "k": "win"}, {"t": "key", "k": "alt"}, {"t": "key", "k": "delete"}, {"t": "key", "k": "w", "d": "hold"}, {"t": "key"}, {"t": "type", "text": "rm -rf"},
               {"t": "mouse", "x": 2, "y": 0.5}, {"t": "mouse", "x": "a", "y": 0}, {"t": "mouse", "x": 0.5, "y": 0.5, "b": "side"}, {"t": "shell", "cmd": "calc"}, "w", None, 5]
        for ev in bad:
            self.assertIsNone(gp.clean_event(ev), ev)
        self.assertEqual(gp.clean_event({"t": "wheel", "dy": 999})["dy"], 10)
        self.assertEqual(gp.clean_event({"t": "rel", "dx": 9999, "dy": -9999}), {"t": "rel", "dx": 200, "dy": -200})

    def test_input_needs_a_session_and_arming_and_is_queued_for_the_agent(self):
        tok = self.pair()
        ev = [{"t": "key", "k": "w", "d": "down"}]
        self.assertEqual(gp.submit_input(tok, ev)["error"], "not armed")
        gp.arm()
        self.assertEqual(gp.submit_input("x" * 30, ev)["error"], "not paired")
        self.assertEqual(gp.submit_input(tok, "nope")["error"], "bad input")
        res = gp.submit_input(tok, ev + [{"t": "key", "k": "win"}])
        self.assertEqual((res["accepted"], res["dropped"]), (1, 1))
        poll = gp.agent_poll({"foreground": "eldenring.exe"})
        self.assertEqual([c["k"] for c in poll["commands"]], ["w"])
        self.assertEqual(gp.agent_poll({})["commands"], [])                                      # delivered once

    def test_disarming_or_unpairing_drops_whatever_was_queued(self):
        tok = self.pair()
        gp.arm()
        gp.submit_input(tok, [{"t": "key", "k": "w"}])
        gp.disarm()
        self.assertEqual(gp.agent_poll({})["commands"], [])
        gp.arm()
        gp.submit_input(tok, [{"t": "key", "k": "w"}])
        gp.unpair_all()
        self.assertEqual(gp.agent_poll({})["commands"], [])

    def test_flooding_is_refused(self):
        tok = self.pair()
        gp.arm()
        for _ in range(gp.MAX_QUEUE // gp.MAX_EVENTS_PER_CALL):
            gp.submit_input(tok, [{"t": "key", "k": "a"}] * gp.MAX_EVENTS_PER_CALL)
        self.assertEqual(gp.submit_input(tok, [{"t": "key", "k": "a"}] * 5)["error"], "too fast")


class AgentChannelTests(_Base):
    def signed(self, method="POST", path="/api/gamepilot/agent/poll", body=b"{}", ts=None):
        ts = str(ts if ts is not None else time.time())
        return ts, gp.request_signature(KEY, ts, method, path, body)

    def test_only_a_correctly_signed_fresh_request_is_accepted_once(self):
        ts, sig = self.signed()
        self.assertIsNone(gp.verify_agent("POST", "/api/gamepilot/agent/poll", b"{}", ts, sig))
        self.assertEqual(gp.verify_agent("POST", "/api/gamepilot/agent/poll", b"{}", ts, sig), "replayed request")
        ts, sig = self.signed()
        self.assertEqual(gp.verify_agent("POST", "/api/gamepilot/agent/poll", b"{\"x\":1}", ts, sig), "bad signature")            # body changed
        self.assertEqual(gp.verify_agent("POST", "/api/gamepilot/agent/frame", b"{}", ts, sig), "bad signature")                  # path changed
        self.assertEqual(gp.verify_agent("GET", "/api/gamepilot/agent/poll", b"{}", ts, sig), "bad signature")
        self.assertEqual(gp.verify_agent("POST", "/api/gamepilot/agent/poll", b"{}", ts, "0" * 64), "bad signature")
        old_ts, old_sig = self.signed(ts=time.time() - 1000)
        self.assertEqual(gp.verify_agent("POST", "/api/gamepilot/agent/poll", b"{}", old_ts, old_sig), "stale request")
        self.assertEqual(gp.verify_agent("POST", "/api/gamepilot/agent/poll", b"{}", "abc", "x"), "bad timestamp")
        with mock.patch.dict(os.environ, {"AURIX_GAMING_HMAC_KEY": ""}):
            self.assertIn("no signing key", gp.verify_agent("POST", "/p", b"", ts, sig))

    def test_the_reply_is_signed_and_carries_what_to_do(self):
        tok = self.pair()
        gp.arm(5)
        gp.submit_input(tok, [{"t": "key", "k": "space", "d": "tap"}])
        gp.note_viewer()
        reply = gp.agent_poll({"foreground": "SkyrimSE.exe", "w": 1920, "h": 1080, "fps": 4.2})
        self.assertTrue(reply["want_frames"])
        self.assertGreater(reply["armed_until"], time.time())
        self.assertEqual(reply["sig"], gp.reply_signature(KEY, reply))
        forged = dict(reply, commands=[{"t": "key", "k": "enter", "d": "tap", "n": 99}])
        self.assertNotEqual(reply["sig"], gp.reply_signature(KEY, forged))                       # a tampered reply cannot keep the signature

    def test_no_frames_are_wanted_when_nobody_is_watching_or_armed(self):
        reply = gp.agent_poll({"foreground": "eldenring.exe"})
        self.assertFalse(reply["want_frames"])
        self.assertEqual(reply["armed_until"], 0.0)

    def test_frames_must_be_small_jpegs_and_only_the_latest_is_kept(self):
        self.assertEqual(gp.put_frame(b"not a jpeg"), 0)
        self.assertEqual(gp.put_frame(b"\xff\xd8" + b"x" * (gp.MAX_FRAME_BYTES + 1)), 0)
        self.assertEqual(gp.put_frame(JPEG), 1)
        self.assertEqual(gp.put_frame(JPEG + b"2"), 2)
        self.assertIsNone(gp.frame_after(2))
        seq, data = gp.frame_after(0)
        self.assertEqual((seq, data), (2, JPEG + b"2"))
        self.assertFalse(list((Path(os.environ["AURIX_PROJECT_ROOT"]) / "data").rglob("*.jpg")))          # never written to disk


class ViewerStateTests(_Base):
    def test_state_reflects_the_pc_the_game_and_arming(self):
        tok = self.pair()
        self.assertEqual(gp.viewer_state("bad"), {"paired": False})
        st = gp.viewer_state(tok)
        self.assertEqual((st["paired"], st["pc_online"], st["armed"]), (True, False, False))
        gp.agent_poll({"foreground": "SkyrimSE.exe", "w": 1920, "h": 1080, "fps": 5})
        gp.put_frame(JPEG)
        gp.arm(3)
        st = gp.viewer_state(tok)
        self.assertEqual((st["pc_online"], st["foreground"], st["armed"], st["frame_seq"]), (True, "SkyrimSE.exe", True, 1))
        self.assertGreater(st["armed_left"], 100)
        stale = gp.viewer_state(tok, now=time.time() + 60)
        self.assertFalse(stale["pc_online"])
        self.assertEqual(stale["foreground"], "")

    def test_a_blocked_foreground_is_reported_without_a_picture(self):
        tok = self.pair()
        gp.agent_poll({"foreground": "chrome.exe", "blocked": True})
        st = gp.viewer_state(tok)
        self.assertTrue(st["blocked"])
        self.assertEqual(st["foreground"], "chrome.exe")


class WiringTests(_Base):
    def test_commands(self):
        self.assertEqual(commands.parse("pair 123456"), ("gp_pair", "123456"))
        self.assertIsNone(commands.parse("pair 12345"))
        self.assertEqual(commands.parse("arm"), ("gp_arm", ""))
        self.assertEqual(commands.parse("arm 10"), ("gp_arm", "10"))
        self.assertEqual(commands.parse("arm 10 minutes"), ("gp_arm", "10"))
        self.assertEqual(commands.parse("disarm"), ("gp_disarm", ""))
        self.assertEqual(commands.parse("unpair"), ("gp_unpair", ""))
        self.assertEqual(commands.parse("gamepilot"), ("gp_status", ""))
        self.assertEqual(commands.parse("firestick"), ("gp_status", ""))

    def test_a_button_can_disarm_but_never_arm_or_pair(self):
        self.assertIn("gp_disarm", buttons.ALLOWED_KINDS)
        self.assertIn("gp_status", buttons.ALLOWED_KINDS)
        for k in ("gp_arm", "gp_pair", "gp_unpair"):
            self.assertNotIn(k, buttons.ALLOWED_KINDS)

    def test_dashboard_actions(self):
        tok = self.pair()
        self.assertIn("Armed", actions.run_action("gp_arm", "5")["message"])
        self.assertIn("Disarmed", actions.run_action("gp_disarm")["message"])
        self.assertIn("6 digits", actions.run_action("gp_pair", "zz")["message"])
        self.assertIn("Unpaired", actions.run_action("gp_unpair")["message"])
        for n in ("gp_arm", "gp_disarm", "gp_unpair", "gp_pair"):
            self.assertIn(n, actions.ACTION_NAMES)
        self.assertTrue(tok)

    def test_status_and_panel_and_snapshot(self):
        self.assertIn("not connected", gp.status_text())
        self.assertFalse(gp.panel()["pc_online"])
        from src.foundation import command_center
        self.assertIn("gamepilot", command_center.snapshot())

    def test_protected(self):
        for p in ("src/foundation/gamepilot.py", "routes/gamepilot_routes.py", "data/gamepilot/sessions.json", "gaming/agent/aurix_gamepilot_agent.py"):
            self.assertEqual(identity.protected_component_for(p), "approval_gate", p)

    def test_the_login_exemption_covers_exactly_the_gamepilot_paths(self):
        src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
        self.assertIn('AUTH_EXEMPT_PREFIXES = ["/static", "/gamepilot", "/api/gamepilot/"]', src)
        self.assertIn("setup_gamepilot_routes", src)


if __name__ == "__main__":
    unittest.main()
