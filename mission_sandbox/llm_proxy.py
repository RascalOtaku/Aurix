#!/usr/bin/env python3
"""llm_proxy.py - the ONLY door from the sandbox to a model server.

Sandbox code has no network, but coding agents (OpenHands) and mission scripts need an LLM.
This proxy sits on both the sandbox network and the default network and forwards ONLY the
inference endpoints of an Ollama/OpenAI-compatible server (LLM_UPSTREAM, host:port). Everything
that changes the server - pull, delete, create, copy, push, blobs - is refused, as is anything
that is not an exact allowlisted method+path. A raw TCP forwarder could not do that: Ollama's
`/api/delete` and `/api/pull` would let sandbox code wipe or fill the GPU box's disk.

Optional caller auth: when LLM_PROXY_TOKEN is set, every request must carry it, either as
`Authorization: Bearer <token>` (what OpenAI-compatible clients send as their API key) or as
`X-LLM-Proxy-Token: <token>`. It is checked with hmac.compare_digest, like the sandbox server's
X-Sandbox-Token, and is never forwarded upstream. LLM_PROXY_REQUIRE_TOKEN=1 makes the proxy refuse
to start without a token (fail closed, as mission_sandbox/server.py does). With neither set it runs
open, as before, and says so loudly at startup.

Streams responses (SSE / chunked) as they arrive. Stdlib only.
"""
import hmac
import http.client
import http.server
import os
import re
import sys
import threading

UPSTREAM = os.environ.get("LLM_UPSTREAM", "")
PORT = int(os.environ.get("LLM_PROXY_PORT", "11434"))
MAX_BODY = 8 * 1024 * 1024
UPSTREAM_TIMEOUT = int(os.environ.get("LLM_PROXY_TIMEOUT", "900"))
_slots = threading.BoundedSemaphore(int(os.environ.get("LLM_PROXY_CONCURRENCY", "4")))
BIND = os.environ.get("LLM_PROXY_BIND", "0.0.0.0")


def _token() -> str:
    return os.environ.get("LLM_PROXY_TOKEN", "")


def _require_token() -> bool:
    return os.environ.get("LLM_PROXY_REQUIRE_TOKEN", "").strip().lower() in ("1", "true", "yes")


def token_ok(headers) -> bool:
    """True when no token is configured, or the caller sent the configured one (Bearer or X-LLM-Proxy-Token)."""
    want = _token()
    if not want:
        return True
    got = headers.get("X-LLM-Proxy-Token", "") or ""
    auth = headers.get("Authorization", "") or ""
    if not got and auth[:7].lower() == "bearer ":
        got = auth[7:].strip()
    return hmac.compare_digest(got.encode(), want.encode())

ALLOWED = [
    ("GET", re.compile(r"^/v1/models$")),
    ("GET", re.compile(r"^/api/(?:tags|ps|version)$")),
    ("POST", re.compile(r"^/v1/(?:chat/completions|completions|embeddings)$")),
    ("POST", re.compile(r"^/api/(?:chat|generate|embeddings|embed|show)$")),
]
FORWARD_REQUEST_HEADERS = ("content-type", "accept")
FORWARD_RESPONSE_HEADERS = ("content-type", "cache-control")


def allowed(method: str, raw_path: str) -> bool:
    """Exact method + path (query string ignored). No traversal, encoding tricks, or doubled slashes."""
    path = raw_path.split("?", 1)[0]
    if "%" in path or ".." in path or "//" in path or "\\" in path:
        return False
    return any(method == m and rx.match(path) for m, rx in ALLOWED)


class Proxy(http.server.BaseHTTPRequestHandler):
    server_version = "AurixLLMProxy/1"
    protocol_version = "HTTP/1.0"          # the response ends when the connection closes: fine for SSE

    def log_message(self, fmt, *args):
        sys.stderr.write("llm-proxy: " + (fmt % args) + "\n")

    def _refuse(self, code, why):
        body = ('{"error": "%s"}' % why).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            n = -1
        body = self.rfile.read(n) if 0 < n <= MAX_BODY else b""      # drain first, then answer
        if n > MAX_BODY or n < 0:
            # Answering before the client finished sending resets the connection, and the client then
            # sees "connection reset" instead of the 413. Discard (never keep) a bounded amount first.
            left = min(n, MAX_BODY * 4) if n > 0 else 0
            while left > 0:
                got = self.rfile.read(min(left, 65536))
                if not got:
                    break
                left -= len(got)
            return self._refuse(413, "bad body size")
        if not token_ok(self.headers):
            sys.stderr.write(f"llm-proxy: BAD TOKEN {method} {self.path[:80]}\n")
            return self._refuse(401, "bad token")
        if not allowed(method, self.path):
            sys.stderr.write(f"llm-proxy: REFUSED {method} {self.path[:80]}\n")
            return self._refuse(403, "this endpoint is not available from the sandbox")
        if not UPSTREAM:
            return self._refuse(503, "LLM_UPSTREAM is not configured")
        host, _, port = UPSTREAM.rpartition(":")
        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        with _slots:
            try:
                conn = http.client.HTTPConnection(host, int(port), timeout=UPSTREAM_TIMEOUT)
                conn.request(method, self.path, body=body or None, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                return self._refuse(502, f"upstream unreachable: {type(e).__name__}")
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in FORWARD_RESPONSE_HEADERS:
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read1(8192)                     # returns as soon as bytes are available
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                                             # the sandbox side hung up
            finally:
                conn.close()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")


def make_server(port=PORT, host=None):
    if _require_token() and not _token():
        raise SystemExit("LLM_PROXY_REQUIRE_TOKEN is set but LLM_PROXY_TOKEN is not - refusing to start (fail closed)")
    return http.server.ThreadingHTTPServer((BIND if host is None else host, port), Proxy)


if __name__ == "__main__":
    if not UPSTREAM:
        raise SystemExit("LLM_UPSTREAM (host:port) is not set - refusing to start")
    srv = make_server()
    if not _token():
        print("llm-proxy: WARNING no LLM_PROXY_TOKEN set - any caller that can reach this port may use it", flush=True)
    print(f"llm-proxy listening on {BIND}:{PORT} -> {UPSTREAM} (inference endpoints only, "
          f"token {'required' if _token() else 'OFF'})", flush=True)
    srv.serve_forever()
