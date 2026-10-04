"""The ADA-inspired pieces: Home Assistant control, local vision, Frigate alerts, the tiny intent router, the voice helpers."""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import mcp_presets  # noqa: E402
from src.foundation import frigate, intent, smarthome, vision  # noqa: E402

STATES = [
    {"entity_id": "light.desk_lamp", "state": "off", "attributes": {"friendly_name": "Desk Lamp"}},
    {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
    {"entity_id": "light.kitchen_island", "state": "off", "attributes": {"friendly_name": "Kitchen Island"}},
    {"entity_id": "switch.printer_plug", "state": "on", "attributes": {"friendly_name": "Printer Plug"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
    {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Temp"}},
]


class FakeHA:
    def __init__(self, code=200):
        self.code, self.calls = code, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        if url.endswith("/api/states"):
            return self.code, json.dumps(STATES).encode()
        return 200, b"[]"


class _Env(unittest.TestCase):
    ENV = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, **self.ENV})
        p.start()
        self.addCleanup(p.stop)


class SmartHomeTests(_Env):
    ENV = {"AURIX_HA_URL": "http://ha.local:8123", "AURIX_HA_TOKEN": "tok", "AURIX_HA_ENTITIES": ""}

    def test_overview_shows_switchables_and_marks_the_rest_read_only(self):
        text = smarthome.overview(FakeHA())
        self.assertIn("Desk Lamp: off", text)
        self.assertIn("Front Door: locked (read-only)", text)
        self.assertNotIn("Temp", text)                                          # sensors are not shown

    def test_switching_by_name(self):
        ha = FakeHA()
        self.assertIn("Turned <b>Desk Lamp</b> on", smarthome.switch("on", "the desk lamp", ha))
        method, url, headers, body = ha.calls[-1]
        self.assertEqual((method, url), ("POST", "http://ha.local:8123/api/services/light/turn_on"))
        self.assertEqual(json.loads(body), {"entity_id": "light.desk_lamp"})
        self.assertEqual(headers["Authorization"], "Bearer tok")

    def test_exact_name_beats_partial_and_ambiguity_asks(self):
        ha = FakeHA()
        smarthome.switch("off", "kitchen", ha)                                  # exact "Kitchen", not "Kitchen Island"
        self.assertEqual(json.loads(ha.calls[-1][3]), {"entity_id": "light.kitchen"})
        self.assertIn("Which one?", smarthome.switch("off", "k", FakeHA()))

    def test_locks_are_never_operated_from_chat(self):
        ha = FakeHA()
        msg = smarthome.switch("off", "front door", ha)
        self.assertIn("do not operate", msg)
        self.assertFalse(any(m == "POST" for m, *_ in ha.calls))

    def test_allow_list(self):
        with mock.patch.dict(os.environ, {"AURIX_HA_ENTITIES": "light.desk_lamp"}):
            ha = FakeHA()
            self.assertIn("not in AURIX_HA_ENTITIES", smarthome.switch("on", "printer plug", ha))
            self.assertFalse(any(m == "POST" for m, *_ in ha.calls))

    def test_bad_token_and_unconfigured(self):
        self.assertIn("rejected the token", smarthome.overview(FakeHA(code=401)))
        with mock.patch.dict(os.environ, {"AURIX_HA_TOKEN": ""}):
            self.assertIn("not set up", smarthome.overview(FakeHA()))

    def test_turn_phrases_are_commands_only_when_configured(self):
        from src.foundation import commands
        self.assertEqual(commands.parse("turn the kitchen island off"), ("home_switch", "off kitchen island"))
        with mock.patch.dict(os.environ, {"AURIX_HA_TOKEN": ""}):
            self.assertIsNone(commands.parse("turn off notifications"))       # stays chat for the agent


class HomeAssistantPresetTests(_Env):
    def test_preset_fills_url_and_token_from_env_and_skips_without_them(self):
        with mock.patch.dict(os.environ, {"AURIX_HA_URL": "", "AURIX_HA_TOKEN": ""}):
            self.assertEqual(mcp_presets.preset_servers("home-assistant"), [])
        with mock.patch.dict(os.environ, {"AURIX_HA_URL": "http://ha.local:8123/", "AURIX_HA_TOKEN": "secret-token"}):
            (srv,) = mcp_presets.preset_servers("home-assistant")
        self.assertEqual((srv["transport"], srv["url"]), ("sse", "http://ha.local:8123/mcp_server/sse"))
        self.assertEqual(srv["headers"], {"Authorization": "Bearer secret-token"})
        self.assertNotIn("secret-token", json.dumps(mcp_presets.MCP_PRESETS))  # tokens live in .env only


class VisionTests(_Env):
    ENV = {"AURIX_VISION_MODEL": "qwen2.5vl:3b", "AURIX_GPU_HOST": "100.64.0.10", "AURIX_VISION_URL": ""}

    def test_photo_question_goes_to_the_local_model_with_the_image(self):
        sent = {}

        def post(url, body, timeout):
            sent.update(url=url, body=body)
            return {"message": {"content": "A 3D-printed bracket with a cracked arm."}}
        msg = vision.ask(b"\xff\xd8jpegdata", "what broke?", post)
        self.assertEqual(msg, "👁️ A 3D-printed bracket with a cracked arm.")
        self.assertEqual(sent["url"], "http://100.64.0.10:11434/api/chat")
        user = sent["body"]["messages"][-1]
        self.assertEqual((user["content"], base64.b64decode(user["images"][0])), ("what broke?", b"\xff\xd8jpegdata"))

    def test_off_without_a_model_and_wake_when_unreachable(self):
        with mock.patch.dict(os.environ, {"AURIX_VISION_MODEL": ""}):
            self.assertIn("Vision is off", vision.ask(b"x"))

        def down(*a):
            raise OSError("no route")
        with mock.patch("src.foundation.wake.wake_gpu_box", return_value=True) as wk:
            self.assertIn("wake-up packet", vision.ask(b"x", "", down))
        wk.assert_called_once()

    def test_look_uses_only_a_fresh_gamepilot_frame(self):
        from src.foundation import gamepilot
        saved = dict(gamepilot._frame)                     # module-level state: put it back for the GamePilot tests

        def restore():
            gamepilot._frame.clear()
            gamepilot._frame.update(saved)
        self.addCleanup(restore)
        with gamepilot._lock:
            gamepilot._frame.update(jpeg=b"\xff\xd8old", at=0.0)
        self.assertIn("no fresh frame", vision.look("", lambda *a: {}, wait=0))
        import time as _t
        with gamepilot._lock:
            gamepilot._frame.update(jpeg=b"\xff\xd8new", at=_t.time())
        got = {}
        vision.look("", lambda u, b, t: got.update(b=b) or {"message": {"content": "level 3"}}, wait=0)
        self.assertEqual(base64.b64decode(got["b"]["messages"][-1]["images"][0]), b"\xff\xd8new")


class FrigateTests(_Env):
    ENV = {"AURIX_FRIGATE_URL": "http://nvr.local:5000", "AURIX_FRIGATE_LABELS": "person", "AURIX_FRIGATE_COOLDOWN": "300",
           "AURIX_FRIGATE_CAMERAS": ""}

    def get_events(self, events):
        calls = []

        def get(url):
            calls.append(url)
            return 200, json.dumps(events).encode()
        return get, calls

    def test_first_poll_sets_the_start_and_never_replays(self):
        get, calls = self.get_events([{"id": "1", "camera": "door", "label": "person", "start_time": 50.0}])
        self.assertEqual(frigate.poll(now=100.0, get=get), [])
        self.assertEqual(calls, [])

    def test_alerts_with_cooldown_per_camera_and_label(self):
        frigate.poll(now=100.0, get=lambda u: (200, b"[]"))
        events = [{"id": "a", "camera": "door", "label": "person", "start_time": 110.0, "data": {"top_score": 0.87}},
                  {"id": "b", "camera": "door", "label": "person", "start_time": 200.0},                     # within cooldown
                  {"id": "c", "camera": "yard", "label": "person", "start_time": 210.0},
                  {"id": "d", "camera": "door", "label": "car", "start_time": 220.0}]                        # not a wanted label
        get, calls = self.get_events(events)
        out = frigate.poll(now=300.0, get=get, describe=lambda jpg: "describe:" + jpg.decode())
        self.assertEqual(len(out), 2)
        self.assertIn("<b>person</b> at <b>door</b>", out[0])
        self.assertIn("(87%)", out[0])
        self.assertIn("after=100.000", calls[0])
        self.assertIn("/api/events/a/snapshot.jpg", calls[1])                  # the describer saw the snapshot
        get2, calls2 = self.get_events([])
        frigate.poll(now=400.0, get=get2)
        self.assertIn("after=220.000", calls2[0])                              # moves past everything seen

    def test_unreachable_is_quiet(self):
        frigate.poll(now=100.0, get=lambda u: (200, b"[]"))

        def down(u):
            raise OSError("down")
        self.assertEqual(frigate.poll(now=200.0, get=down), [])


class IntentRouterTests(_Env):
    ENV = {"AURIX_ROUTER_MODEL": "qwen2.5:0.5b", "AURIX_HA_URL": "http://ha.local:8123", "AURIX_HA_TOKEN": "t"}

    def reply(self, command, confidence=0.95):
        return lambda url, body, timeout: {"message": {"content": json.dumps({"command": command, "confidence": confidence})}}

    def test_maps_speech_to_a_safe_command(self):
        self.assertEqual(intent.route("kill the kitchen lights", self.reply("turn off kitchen")), ("home_switch", "off kitchen"))
        self.assertEqual(intent.route("is my printer busy", self.reply("printer")), ("printer", ""))

    def test_never_routes_approvals_or_low_confidence(self):
        self.assertIsNone(intent.route("print it", self.reply("print d-123abc")))            # an approval: refused
        self.assertIsNone(intent.route("approve the land deal", self.reply("yes land a-123abc")))
        self.assertIsNone(intent.route("lights?", self.reply("lights", confidence=0.4)))
        self.assertIsNone(intent.route("write me a poem", self.reply("")))

    def test_off_or_broken_falls_through(self):
        with mock.patch.dict(os.environ, {"AURIX_ROUTER_MODEL": ""}):
            self.assertIsNone(intent.route("lights", self.reply("lights")))
        self.assertIsNone(intent.route("lights", lambda *a: {"message": {"content": "not json"}}))

        def down(*a):
            raise OSError("x")
        self.assertIsNone(intent.route("lights", down))


class VoiceScriptTests(unittest.TestCase):
    def load(self, name):
        spec = importlib.util.spec_from_file_location("voice_" + name.replace("-", "_"), ROOT / "patches" / "7070" / name)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_open_source_voice_helpers(self):
        v = self.load("06-aurix-voice-os.py")
        import numpy as np
        self.assertEqual(v.rms(np.zeros(100, dtype=np.int16).tobytes()), 0.0)
        self.assertAlmostEqual(v.rms(np.full(100, 1000, dtype=np.int16).tobytes()), 1000.0)
        rows = [{"id": "s1", "name": "voice", "last_message_at": "2026-10-01"}, {"id": "s2", "name": "Voice", "last_message_at": "2026-10-03"},
                {"id": "s3", "name": "Other"}]
        self.assertEqual(v.pick_session(rows, "Voice"), "s2")
        self.assertIsNone(v.pick_session(rows, "Nope"))
        self.assertTrue(v.AURIX_API_URL.endswith(":7000/api/chat"))

    def test_neither_script_needs_audioop(self):
        for name in ("06-aurix-voice-os.py", "06-aurix-voice.py"):
            self.assertNotIn("import audioop", (ROOT / "patches" / "7070" / name).read_text())


if __name__ == "__main__":
    unittest.main()
