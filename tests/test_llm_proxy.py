"""Tests for mission_sandbox/llm_proxy.py - inference passes, model-changing endpoints never do."""
import http.server
import importlib.util
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ODYSSEUS = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("llm_proxy", ODYSSEUS / "mission_sandbox" / "llm_proxy.py")
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)


class FakeOllama(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def _record(self, body=b""):
        FakeOllama.seen.append((self.command, self.path, body))

    def do_GET(self):
        self._record()
        payload = json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        self._record(body)
        if self.path == "/api/chat":                         # a slow token stream
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for tok in ("a", "b", "c"):
                self.wfile.write((json.dumps({"tok": tok}) + "\n").encode())
                self.wfile.flush()
                time.sleep(0.6)
            return
        payload = json.dumps({"echo": json.loads(body or b"{}"), "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_DELETE = do_PUT = do_PATCH = do_POST


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.patch = mock.patch.object(proxy, "UPSTREAM", f"127.0.0.1:{cls.upstream.server_address[1]}")
        cls.patch.start()
        cls.srv = proxy.make_server(port=0, host="127.0.0.1")
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.upstream.shutdown()
        cls.patch.stop()

    def setUp(self):
        FakeOllama.seen.clear()

    def call(self, method, path, body=None, timeout=20):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer leak"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_allowlist_function(self):
        ok = [("POST", "/v1/chat/completions"), ("POST", "/v1/embeddings"), ("GET", "/v1/models"),
              ("POST", "/api/chat"), ("POST", "/api/generate"), ("GET", "/api/tags"), ("GET", "/api/ps"),
              ("POST", "/api/show"), ("POST", "/v1/chat/completions?stream=true")]
        bad = [("POST", "/api/pull"), ("DELETE", "/api/delete"), ("POST", "/api/delete"), ("POST", "/api/create"),
               ("POST", "/api/copy"), ("POST", "/api/push"), ("POST", "/api/blobs/sha256:abc"),
               ("GET", "/api/chat"), ("PUT", "/api/generate"), ("POST", "/v1/models"),
               ("POST", "/api/chat/../delete"), ("POST", "/api//delete"), ("POST", "/api%2Fdelete"),
               ("POST", "/v1/chat/completions/../../api/delete"), ("POST", "/"), ("POST", ""), ("GET", "/api/tags/x"),
               ("POST", "/api/generate\\..\\delete")]
        for m, p in ok:
            self.assertTrue(proxy.allowed(m, p), (m, p))
        for m, p in bad:
            self.assertFalse(proxy.allowed(m, p), (m, p))

    def test_inference_passes_through(self):
        code, body = self.call("POST", "/v1/chat/completions", {"model": "m", "messages": []})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["path"], "/v1/chat/completions")
        code, body = self.call("GET", "/v1/models")
        self.assertEqual((code, json.loads(body)["models"][0]["name"]), (200, "qwen2.5:7b"))

    def test_model_changing_endpoints_are_refused_and_never_reach_the_upstream(self):
        for method, path in (("POST", "/api/pull"), ("DELETE", "/api/delete"), ("POST", "/api/create"),
                             ("POST", "/api/copy"), ("POST", "/api/push"), ("PUT", "/api/blobs/sha256:x"),
                             ("POST", "/api/chat/../delete")):
            code, _ = self.call(method, path, {"name": "qwen2.5:7b"} if method != "GET" else None)
            self.assertEqual(code, 403, (method, path))
        self.assertEqual(FakeOllama.seen, [])                # NOTHING got through

    def test_caller_headers_like_authorization_are_not_forwarded(self):
        captured = {}

        class Spy(FakeOllama):
            def do_POST(self):
                captured.update({k.lower(): v for k, v in self.headers.items()})
                super().do_POST()

        spy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Spy)
        threading.Thread(target=spy.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(proxy, "UPSTREAM", f"127.0.0.1:{spy.server_address[1]}"):
                self.call("POST", "/v1/chat/completions", {"x": 1})
        finally:
            spy.shutdown()
        self.assertNotIn("authorization", captured)
        self.assertEqual(captured.get("content-type"), "application/json")

    def test_streaming_arrives_incrementally_not_after_the_end(self):
        req = urllib.request.Request(self.base + "/api/chat", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=20) as r:
            first = r.readline()
            t_first = time.time() - t0
            rest = r.read()
        self.assertIn(b'"a"', first)
        self.assertLess(t_first, 1.2)                        # first token well before the ~1.8 s stream ends
        self.assertIn(b'"c"', rest)

    def test_unreachable_upstream_and_unconfigured(self):
        with mock.patch.object(proxy, "UPSTREAM", "127.0.0.1:9"):
            self.assertEqual(self.call("POST", "/v1/chat/completions", {})[0], 502)
        with mock.patch.object(proxy, "UPSTREAM", ""):
            self.assertEqual(self.call("POST", "/v1/chat/completions", {})[0], 503)

    def test_oversized_body_is_rejected(self):
        with mock.patch.object(proxy, "MAX_BODY", 100):
            self.assertEqual(self.call("POST", "/v1/chat/completions", {"x": "y" * 500})[0], 413)


class TokenTests(unittest.TestCase):
    """LLM_PROXY_TOKEN: same pattern as the sandbox server's X-Sandbox-Token (constant-time, never forwarded)."""

    @classmethod
    def setUpClass(cls):
        cls.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        cls.patch = mock.patch.object(proxy, "UPSTREAM", f"127.0.0.1:{cls.upstream.server_address[1]}")
        cls.patch.start()
        cls.srv = proxy.make_server(port=0, host="127.0.0.1")
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.upstream.shutdown()
        cls.patch.stop()

    def setUp(self):
        FakeOllama.seen.clear()
        self.env = mock.patch.dict(os.environ, {"LLM_PROXY_TOKEN": "s3cret-token"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def call(self, method, path, headers=None, body=b"{}"):
        req = urllib.request.Request(self.base + path, data=body if method != "GET" else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_missing_or_wrong_token_is_refused_before_the_upstream(self):
        for headers in (None, {"Authorization": "Bearer nope"}, {"X-LLM-Proxy-Token": "nope"},
                        {"Authorization": "s3cret-token"}, {"Authorization": "Bearer "},
                        {"X-LLM-Proxy-Token": "s3cret-token-and-more"}):
            self.assertEqual(self.call("POST", "/v1/chat/completions", headers)[0], 401, headers)
            self.assertEqual(self.call("GET", "/api/tags", headers)[0], 401, headers)
        self.assertEqual(FakeOllama.seen, [])

    def test_right_token_passes_as_bearer_or_header(self):
        for headers in ({"Authorization": "Bearer s3cret-token"}, {"authorization": "bearer s3cret-token"},
                        {"X-LLM-Proxy-Token": "s3cret-token"}):
            code, body = self.call("POST", "/v1/chat/completions", headers)
            self.assertEqual(code, 200, headers)
            self.assertEqual(json.loads(body)["path"], "/v1/chat/completions")
        self.assertEqual(len(FakeOllama.seen), 3)

    def test_token_does_not_unlock_blocked_endpoints(self):
        self.assertEqual(self.call("POST", "/api/pull", {"Authorization": "Bearer s3cret-token"})[0], 403)
        self.assertEqual(FakeOllama.seen, [])

    def test_token_is_not_forwarded_upstream(self):
        captured = {}

        class Spy(FakeOllama):
            def do_POST(self):
                captured.update({k.lower(): v for k, v in self.headers.items()})
                super().do_POST()

        spy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Spy)
        threading.Thread(target=spy.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(proxy, "UPSTREAM", f"127.0.0.1:{spy.server_address[1]}"):
                code, _ = self.call("POST", "/v1/chat/completions",
                                    {"Authorization": "Bearer s3cret-token", "X-LLM-Proxy-Token": "s3cret-token"})
        finally:
            spy.shutdown()
        self.assertEqual(code, 200)
        self.assertNotIn("authorization", captured)
        self.assertNotIn("x-llm-proxy-token", captured)

    def test_non_ascii_token_header_is_refused_not_crashed(self):
        self.assertFalse(proxy.token_ok({"X-LLM-Proxy-Token": "s3cret-tökén"}))

    def test_require_token_fails_closed_at_startup(self):
        with mock.patch.dict(os.environ, {"LLM_PROXY_REQUIRE_TOKEN": "1", "LLM_PROXY_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                proxy.make_server(port=0, host="127.0.0.1")
        with mock.patch.dict(os.environ, {"LLM_PROXY_REQUIRE_TOKEN": "1"}):
            srv = proxy.make_server(port=0, host="127.0.0.1")       # token set in setUp: starts
            srv.server_close()

    def test_no_token_configured_keeps_the_old_open_behaviour(self):
        with mock.patch.dict(os.environ, {"LLM_PROXY_TOKEN": ""}):
            self.assertTrue(proxy.token_ok({}))
            self.assertEqual(self.call("POST", "/v1/chat/completions")[0], 200)


if __name__ == "__main__":
    unittest.main()
