"""Homelab apps from chat: Pi-hole v6, Jellyfin, Uptime Kuma (src/foundation/homeapps.py)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.foundation import homeapps  # noqa: E402


class FakePihole:
    def __init__(self, password_ok=True, v6=True):
        self.ok, self.v6, self.calls = password_ok, v6, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        path = url.split("://", 1)[1].split("/", 1)[1]
        if path == "api/auth" and method == "POST":
            if not self.v6:
                return 404, b""
            return 200, json.dumps({"session": {"valid": self.ok, "sid": "SID1" if self.ok else None}}).encode()
        if path == "api/auth" and method == "DELETE":
            return 204, b""
        if path == "api/stats/summary":
            return 200, json.dumps({"queries": {"total": 12000, "blocked": 1800, "percent_blocked": 15.0},
                                    "gravity": {"domains_being_blocked": 150000}}).encode()
        if path == "api/dns/blocking":
            return 200, json.dumps({"blocking": "enabled", "timer": None}).encode()
        return 404, b""


class PiholeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.dict(os.environ, {"AURIX_PROJECT_ROOT": self.tmp.name, "AURIX_PIHOLE_URL": "http://pi.hole",
                                         "AURIX_PIHOLE_PASSWORD": "app-pw"})
        p.start()
        self.addCleanup(p.stop)

    def test_status_and_every_session_is_closed(self):
        fp = FakePihole()
        text = homeapps.pihole_status(fp)
        self.assertIn("blocking <b>enabled</b>", text)
        self.assertIn("12,000 queries, 1,800 blocked (15.0%)", text)
        opened = sum(1 for m, u, *_ in fp.calls if m == "POST" and u.endswith("/api/auth"))
        closed = sum(1 for m, u, *_ in fp.calls if m == "DELETE" and u.endswith("/api/auth"))
        self.assertEqual(opened, closed)                                       # Pi-hole limits sessions: never leak one
        self.assertTrue(all(h.get("X-FTL-SID") == "SID1" for m, u, h, b in fp.calls if "/api/stats" in u))

    def test_pause_is_bounded_and_resume(self):
        fp = FakePihole()
        self.assertIn("paused for 60 min", homeapps.pihole_pause(500, fp))   # capped at an hour
        body = [b for m, u, h, b in fp.calls if u.endswith("/api/dns/blocking") and m == "POST"][-1]
        self.assertEqual(body, {"blocking": False, "timer": 3600})
        self.assertIn("back on", homeapps.pihole_resume(fp))

    def test_bad_password_old_pihole_and_unconfigured(self):
        self.assertIn("refused the password", homeapps.pihole_status(FakePihole(password_ok=False)))
        self.assertIn("Pi-hole 6", homeapps.pihole_status(FakePihole(v6=False)))
        with mock.patch.dict(os.environ, {"AURIX_PIHOLE_URL": ""}):
            self.assertIn("not set up", homeapps.pihole_status(FakePihole()))


SESSIONS = [
    {"Id": "s1", "DeviceName": "Living room TV", "UserName": "owner", "SupportsRemoteControl": True,
     "NowPlayingItem": {"Name": "Pilot", "SeriesName": "Severance", "ParentIndexNumber": 1, "IndexNumber": 1},
     "PlayState": {"IsPaused": False}},
    {"Id": "s2", "DeviceName": "Phone", "SupportsRemoteControl": True,
     "NowPlayingItem": {"Name": "Dune", "ProductionYear": 2021}, "PlayState": {"IsPaused": True}},
    {"Id": "s3", "DeviceName": "Idle browser"},
]


class FakeJellyfin:
    def __init__(self, code=200):
        self.code, self.calls = code, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers))
        if "/Sessions?" in url:
            return self.code, json.dumps(SESSIONS).encode()
        return 204, b""


class JellyfinTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"AURIX_JELLYFIN_URL": "http://jf.local:8096", "AURIX_JELLYFIN_API_KEY": "k"})
        p.start()
        self.addCleanup(p.stop)

    def test_now_playing(self):
        text = homeapps.tv_status(FakeJellyfin())
        self.assertIn("▶ Severance S01E01 - Pilot - Living room TV (owner)", text)
        self.assertIn("⏸ Dune (2021) - Phone", text)
        self.assertNotIn("Idle browser", text)

    def test_pause_only_what_plays_and_resume_only_what_is_paused(self):
        jf = FakeJellyfin()
        self.assertIn("Paused on Living room TV", homeapps.tv_control("pause", jf))
        posts = [u for m, u, h in jf.calls if m == "POST"]
        self.assertEqual(posts, ["http://jf.local:8096/Sessions/s1/Playing/Pause"])
        self.assertEqual(jf.calls[0][2]["Authorization"], 'MediaBrowser Token="k"')
        jf = FakeJellyfin()
        homeapps.tv_control("resume", jf)
        self.assertEqual([u for m, u, h in jf.calls if m == "POST"], ["http://jf.local:8096/Sessions/s2/Playing/Unpause"])

    def test_bad_key(self):
        self.assertIn("rejected the API key", homeapps.tv_status(FakeJellyfin(code=401)))


class KumaTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"AURIX_KUMA_URL": "http://kuma.local:3001", "AURIX_KUMA_STATUS_PAGE": "home"})
        p.start()
        self.addCleanup(p.stop)

    def test_status_page(self):
        page = {"publicGroupList": [{"name": "Home", "monitorList": [{"id": 1, "name": "Jellyfin"}, {"id": 2, "name": "Vault"}]}]}
        beats = {"heartbeatList": {"1": [{"status": 1}], "2": [{"status": 1}, {"status": 0, "msg": "timeout"}]},
                 "uptimeList": {"1_24": 1.0, "2_24": 0.9512}}

        def get(method, url, headers, body):
            return 200, json.dumps(beats if "/heartbeat/" in url else page).encode()
        text = homeapps.kuma_status(get)
        self.assertIn("1/2 up", text)
        self.assertIn("🟢 up Jellyfin · 100.0% 24 h", text)
        self.assertIn("🔴 down Vault · 95.1% 24 h - timeout", text)

    def test_missing_page(self):
        self.assertIn("no status page", homeapps.kuma_status(lambda *a: (404, b"")))


class RoutingTests(unittest.TestCase):
    def test_commands_do_not_collide_with_helper_pause(self):
        from src.foundation import commands
        self.assertEqual(commands.parse("pause pihole"), ("pihole_pause", ""))
        self.assertEqual(commands.parse("pause ads for 15 min"), ("pihole_pause", "15"))
        self.assertEqual(commands.parse("resume the movie"), ("tv_resume", ""))
        self.assertEqual(commands.parse("pause freelance"), ("shard_pause", "freelance"))   # helpers still pause as before


if __name__ == "__main__":
    unittest.main()
