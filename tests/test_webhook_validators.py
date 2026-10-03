"""Regression tests for the SSRF-protection validators in src/webhook_manager.

These are pure functions guarding outbound webhook delivery: they decide
whether a user-supplied URL may receive server-side HTTP requests. If they
regress, webhooks become an SSRF vector into the LAN / cloud metadata.
"""
import ipaddress
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# tests/conftest.py already stubs src.database (bcrypt/sqlalchemy aren't installed
# in the test env). The validators under test never touch the DB; just make sure
# the names webhook_manager imports exist on that stub.
_db_stub = sys.modules.setdefault("src.database", types.ModuleType("src.database"))
for _name in ("SessionLocal", "Webhook", "ModelEndpoint"):
    if not hasattr(_db_stub, _name):
        setattr(_db_stub, _name, object)

from src import webhook_manager as wm  # noqa: E402


class IpIsPrivateTests(unittest.TestCase):
    def test_public_ipv4_is_not_private(self):
        self.assertFalse(wm._ip_is_private(ipaddress.ip_address("8.8.8.8")))

    def test_loopback_is_private(self):
        self.assertTrue(wm._ip_is_private(ipaddress.ip_address("127.0.0.1")))

    def test_rfc1918_ranges_are_private(self):
        for ip in ("10.0.0.5", "172.16.4.4", "172.31.255.1", "192.168.1.1"):
            self.assertTrue(wm._ip_is_private(ipaddress.ip_address(ip)), ip)

    def test_link_local_and_unique_local_are_private(self):
        self.assertTrue(wm._ip_is_private(ipaddress.ip_address("169.254.10.20")))
        self.assertTrue(wm._ip_is_private(ipaddress.ip_address("fc00::1")))
        self.assertTrue(wm._ip_is_private(ipaddress.ip_address("fe80::1")))

    def test_ipv6_loopback_is_private(self):
        self.assertTrue(wm._ip_is_private(ipaddress.ip_address("::1")))

    def test_public_ipv6_is_not_private(self):
        self.assertFalse(wm._ip_is_private(ipaddress.ip_address("2606:4700:4700::1111")))


class IsPrivateUrlTests(unittest.TestCase):
    def test_public_ip_literal_passes(self):
        self.assertFalse(wm._is_private_url("https://8.8.8.8/hook"))

    def test_private_ip_literals_blocked(self):
        for url in ("http://127.0.0.1/hook", "https://10.0.0.5/",
                    "http://192.168.1.1:8080/x", "http://[::1]/"):
            self.assertTrue(wm._is_private_url(url), url)

    def test_localhost_variants_blocked_without_dns(self):
        for url in ("https://localhost/hook", "http://0.0.0.0/",
                    "https://metadata.google.internal/", "https://metadata/"):
            self.assertTrue(wm._is_private_url(url), url)

    def test_internal_suffixes_blocked(self):
        for url in ("https://printer.local/", "http://nas.lan/",
                    "https://wiki.internal/", "https://x.intranet/"):
            self.assertTrue(wm._is_private_url(url), url)

    def test_hostname_resolving_to_private_ip_blocked(self):
        with mock.patch.object(wm, "_resolve_hostname_ips",
                               return_value=[ipaddress.ip_address("10.9.9.9")]):
            self.assertTrue(wm._is_private_url("https://totally-public.example/hook"))

    def test_hostname_resolving_to_public_ip_passes(self):
        with mock.patch.object(wm, "_resolve_hostname_ips",
                               return_value=[ipaddress.ip_address("93.184.216.34")]):
            self.assertFalse(wm._is_private_url("https://example.com/hook"))

    def test_unresolvable_hostname_fails_closed(self):
        with mock.patch.object(wm, "_resolve_hostname_ips", return_value=[]):
            self.assertTrue(wm._is_private_url("https://nxdomain.invalid/hook"))

    def test_missing_hostname_fails_closed(self):
        self.assertTrue(wm._is_private_url("https:///no-host"))


class ValidateWebhookUrlTests(unittest.TestCase):
    def test_accepts_public_https_url(self):
        url = "https://8.8.8.8/hook"
        self.assertEqual(wm.validate_webhook_url(url), url)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(wm.validate_webhook_url("  https://8.8.8.8/x  "),
                         "https://8.8.8.8/x")

    def test_rejects_non_http_schemes(self):
        for url in ("ftp://8.8.8.8/x", "file:///etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(ValueError, msg=url):
                wm.validate_webhook_url(url)

    def test_rejects_missing_hostname(self):
        with self.assertRaises(ValueError):
            wm.validate_webhook_url("https://")

    def test_rejects_private_targets(self):
        for url in ("http://127.0.0.1/hook", "https://localhost/hook",
                    "https://192.168.0.1/", "https://db.local/"):
            with self.assertRaises(ValueError, msg=url):
                wm.validate_webhook_url(url)

    def test_rejects_overlong_url(self):
        with self.assertRaises(ValueError):
            wm.validate_webhook_url("https://8.8.8.8/" + "x" * 2040)


class ValidateEventsTests(unittest.TestCase):
    def test_accepts_known_events(self):
        self.assertEqual(wm.validate_events("chat.completed,session.created"),
                         "chat.completed,session.created")

    def test_trims_whitespace_and_drops_empties(self):
        self.assertEqual(wm.validate_events("  chat.message , , session.created "),
                         "chat.message,session.created")

    def test_webhook_test_event_is_allowed(self):
        self.assertEqual(wm.validate_events("webhook.test"), "webhook.test")

    def test_rejects_unknown_event(self):
        with self.assertRaises(ValueError) as ctx:
            wm.validate_events("chat.completed, bogus.event")
        self.assertIn("bogus.event", str(ctx.exception))

    def test_rejects_empty(self):
        for bad in ("", "   ", " , , "):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wm.validate_events(bad)


class SanitizeErrorTests(unittest.TestCase):
    def test_redacts_ipv4_and_port(self):
        out = wm.sanitize_error("dial tcp 192.168.1.10:8080: connect refused")
        self.assertIn("[redacted]", out)
        self.assertNotIn("192.168.1.10", out)

    def test_redacts_urls(self):
        out = wm.sanitize_error("fetch https://secret.example.com/path failed")
        self.assertIn("[redacted-url]", out)
        self.assertNotIn("secret.example.com", out)

    def test_truncates_to_max_len(self):
        out = wm.sanitize_error("x" * 500)
        self.assertLessEqual(len(out), 200)

    def test_respects_custom_max_len(self):
        out = wm.sanitize_error("x" * 500, max_len=50)
        self.assertLessEqual(len(out), 50)

    def test_leaves_benign_text_alone(self):
        self.assertEqual(wm.sanitize_error("timeout after 30s"), "timeout after 30s")


if __name__ == "__main__":
    unittest.main()
