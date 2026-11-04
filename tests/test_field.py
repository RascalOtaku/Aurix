"""Field Operations phase 1: a shared location becomes a point; photos and voice notes attach to it; exports are honest."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import commands, field  # noqa: E402

T0 = 1_800_000_000.0


class FieldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name})
        p.start()
        self.addCleanup(p.stop)

    def fake_download(self, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xff\xd8jpeg")
        return None

    def test_capture_is_unverified_with_a_uuid_and_never_logs_coordinates(self):
        p = field.capture(39.5912345678, -105.6, at=T0, accuracy=8.4)
        self.assertEqual(p["status"], "unverified")
        self.assertEqual(len(p["id"]), 36)
        self.assertEqual(p["lat"], 39.5912346)
        self.assertIn("±8.4 m", field.captured_text(p))
        audit_text = "".join(f.read_text() for f in Path(self.tmp.name).rglob("audit*.jsonl"))
        self.assertNotIn("39.59", audit_text)
        with self.assertRaises(ValueError):
            field.capture(95, 0)

    def test_photos_and_voice_attach_within_the_window_only(self):
        p = field.capture(39.6, -105.6, at=T0)
        sid = field.short(p["id"])
        self.assertEqual(field.attach_photo("F1", "north wall", T0 + 60, "a stone wall", download=self.fake_download), sid)
        self.assertEqual(field.attach_voice("t-abc123", T0 + 120), sid)
        self.assertIsNone(field.attach_photo("F2", "", T0 + 16 * 60, download=self.fake_download))     # too late
        self.assertIsNone(field.attach_voice("t-abc124", T0 - 5))                                     # before the point
        atts = field.attachments(p["id"])
        self.assertEqual([a["kind"] for a in atts], ["photo", "voice"])
        self.assertTrue((Path(self.tmp.name) / "data" / "field" / atts[0]["ref"]).exists())          # the original is kept
        text = field.show_text(sid)
        self.assertIn("a lead, not an observation", text)
        self.assertIn("transcript", text)
        self.assertIn("when Telegram received", text)

    def test_owner_commands(self):
        f = commands.Foundation(run_agent=None, notify=None)
        field.capture(39.6, -105.6, at=T0)
        p = field.get()
        sid = field.short(p["id"])
        run = lambda t: asyncio.run(f.handle(*commands.parse(t)))                                    # noqa: E731
        self.assertIn("Old Stone Foundation", run("point name: Old Stone Foundation"))
        self.assertIn("historical", run(f"point {sid} type: Historical"))
        self.assertIn("Type is one of", run("point type: castle"))
        self.assertIn("confirmed", run(f"point {sid} confirm"))
        self.assertIn(sid, run("points"))
        self.assertIn("No such field point", run("point fp-000000"))

    def test_exports_are_valid_and_carry_ids_and_times(self):
        field.capture(39.6, -105.6, at=T0)
        field.update("", "name", "Foundation <north>")
        root = ET.fromstring(field.gpx())
        wpt = root.find("{http://www.topografix.com/GPX/1/1}wpt")
        self.assertEqual(wpt.get("lat"), "39.6")
        self.assertEqual(wpt.find("{http://www.topografix.com/GPX/1/1}name").text, "Foundation <north>")
        g = json.loads(field.geojson())
        props = g["features"][0]["properties"]
        self.assertEqual(g["features"][0]["geometry"]["coordinates"], [-105.6, 39.6])
        self.assertEqual(props["status"], "unverified")
        self.assertTrue(props["received_at"].endswith("Z"))
        self.assertTrue(field.export("gpx").exists())

    def test_sending_a_file_through_the_foundation(self):
        sent = []

        async def send_file(path, caption):
            sent.append(path)
            return True
        f = commands.Foundation(run_agent=None, notify=None, send_file=send_file)
        self.assertIn("No field points", asyncio.run(f.handle(*commands.parse("points gpx"))))
        field.capture(39.6, -105.6, at=T0)
        self.assertIn("Sent", asyncio.run(f.handle(*commands.parse("points geojson"))))
        self.assertTrue(sent[0].endswith(".geojson"))


class ListenerLocationTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_shared_location_and_a_venue_become_points(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": tmp}):
            from services.telegram.listener import TelegramListener
            lst = TelegramListener()
            sent = []
            lst._service = mock.Mock(send=lambda text, **kw: sent.append(text))
            await lst._handle_location({"date": T0, "location": {"latitude": 39.6, "longitude": -105.6, "horizontal_accuracy": 12}})
            await lst._handle_location({"date": T0, "venue": {"title": "Trailhead lot", "location": {"latitude": 39.7, "longitude": -105.7}}})
            self.assertIn("Saved field point", sent[0])
            self.assertIn("Trailhead lot", sent[1])
            self.assertEqual(len(field._all()), 2)


if __name__ == "__main__":
    unittest.main()
