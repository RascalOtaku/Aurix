#!/usr/bin/env python3
"""AURIX sandbox executor - runs mission commands inside THIS isolated container.

The container it lives in has: no secrets, no host or LAN access (internal network only),
a read-only root filesystem, dropped capabilities, and CPU/RAM/process caps. It only ever
sees the mission workspace tree. The agent process (odysseus) talks to it over the private
compose network with a shared token; the token is not visible to agent-run code.

    POST /exec   {"mission": "m-abc123", "kind": "bash"|"python", "code": "...", "timeout": 120}
    POST /probe  {"bins": [...], "modules": [...]}      what tools exist in this image
    GET  /health

Stdlib only. Fails closed: refuses to start without a token.
"""
import hmac
import http.server
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

WORKROOT = os.path.realpath(os.environ.get("SANDBOX_WORKROOT", "/app/data/workspace"))
PORT = int(os.environ.get("SANDBOX_PORT", "8765"))
MAX_OUT = int(os.environ.get("SANDBOX_MAX_OUTPUT", "200000"))
DEFAULT_TIMEOUT = 300
MAX_TIMEOUT = int(os.environ.get("SANDBOX_MAX_TIMEOUT", "900"))
MAX_BODY = 2_000_000
MISSION_RE = re.compile(r"^m-[0-9a-f]{6}$")
_slots = threading.BoundedSemaphore(int(os.environ.get("SANDBOX_MAX_CONCURRENT", "2")))
POSIX = os.name == "posix"


def _token() -> str:
    return os.environ.get("SANDBOX_TOKEN", "")


def clean_env(workspace: str) -> dict:
    """The ONLY environment agent-run code sees: nothing inherited, no secrets."""
    home = os.environ.get("SANDBOX_HOME", "/tmp/home")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
           "TERM": "xterm-256color", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "MISSION_WORKSPACE": workspace, "TMPDIR": "/tmp" if POSIX else os.environ.get("TEMP", "/tmp")}
    if not POSIX:                                      # Windows needs these just to start a process
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
    # Explicit allowlist of NON-secret settings (e.g. where the LLM proxy is). Never a token or key.
    for name in os.environ.get("SANDBOX_PASS_ENV", "LLM_BASE_URL,LLM_MODEL").split(","):
        name = name.strip()
        if name and name in os.environ and not any(s in name.upper() for s in ("TOKEN", "KEY", "SECRET", "PASS")):
            env[name] = os.environ[name]
    return env


def resolve_workspace(mission: str):
    """Workspace dir for a mission id, or (None, reason). Never escapes WORKROOT."""
    if not isinstance(mission, str) or not MISSION_RE.match(mission):
        return None, "bad mission id"
    path = os.path.realpath(os.path.join(WORKROOT, mission))
    if os.path.dirname(path) != WORKROOT:
        return None, "workspace escapes the sandbox root"
    if not os.path.isdir(path):
        return None, "mission workspace does not exist"
    return path, ""


def _limits(timeout: int):
    def apply():
        import resource
        os.setsid()
        resource.setrlimit(resource.RLIMIT_CPU, (timeout + 10, timeout + 20))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 ** 3, 2 * 1024 ** 3))
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, 2048))
        resource.setrlimit(resource.RLIMIT_NPROC, (512, 512))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    return apply


def _cap(text: bytes) -> str:
    s = text.decode("utf-8", "replace")
    return s if len(s) <= MAX_OUT else s[:MAX_OUT] + f"\n... [truncated at {MAX_OUT} chars]"


_procs = {}                              # mission id -> live Popen objects (for /kill)
_procs_lock = threading.Lock()


def kill_mission(mission: str) -> int:
    """Kill every process a mission has running (STOP is absolute). Returns how many."""
    with _procs_lock:
        live = list(_procs.get(mission, ()))
    killed = 0
    for p in live:
        try:
            os.killpg(p.pid, signal.SIGKILL) if POSIX else p.kill()
            killed += 1
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return killed


def run_command(mission: str, kind: str, code: str, timeout: int) -> dict:
    workspace, why = resolve_workspace(mission)
    if workspace is None:
        return {"error": why, "exit_code": 2}
    if kind == "bash":
        argv = ["bash", "-c", code]
    elif kind == "python":
        argv = [sys.executable if not POSIX else "python3", "-c", code]
    else:
        return {"error": "kind must be bash or python", "exit_code": 2}
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    with _slots:
        started = time.time()
        proc = subprocess.Popen(
            argv, cwd=workspace, env=clean_env(workspace), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            preexec_fn=_limits(timeout) if POSIX else None)
        with _procs_lock:
            _procs.setdefault(mission, set()).add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL) if POSIX else proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            out, err = proc.communicate()
        finally:
            with _procs_lock:
                _procs.get(mission, set()).discard(proc)
    result = {"exit_code": 124 if timed_out else proc.returncode, "seconds": round(time.time() - started, 2),
              "stdout": _cap(out), "stderr": _cap(err)}
    if timed_out:
        result["error"] = f"timed out after {timeout}s - process group killed"
    elif proc.returncode is not None and proc.returncode < 0:
        result["error"] = f"terminated by signal {-proc.returncode} (killed by STOP or the OOM killer)"
    return result


def probe(bins, modules) -> dict:
    def ok_mod(m):
        try:
            return importlib.util.find_spec(m) is not None
        except (ImportError, ValueError):
            return False
    return {"bins": {b: bool(shutil.which(b)) for b in list(bins or [])[:200] if isinstance(b, str)},
            "modules": {m: ok_mod(m) for m in list(modules or [])[:200] if isinstance(m, str)}}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AurixSandbox/1"

    def log_message(self, *a):          # quiet; the caller audits
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Sandbox-Token", ""), _token())

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "workroot": WORKROOT})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # Drain the body BEFORE answering anything, or a client still uploading can see a
        # connection reset instead of our 401/413/400.
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = -1
        body = self.rfile.read(n) if 0 < n <= MAX_BODY else b""
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": "bad body size"})
        try:
            req = json.loads(body)
            if not isinstance(req, dict):
                raise ValueError("body must be an object")
        except (ValueError, TypeError) as e:
            return self._send(400, {"error": f"bad request: {e}"})
        if self.path == "/exec":
            self._send(200, run_command(req.get("mission"), req.get("kind"), str(req.get("code", "")),
                                        req.get("timeout") or DEFAULT_TIMEOUT))
        elif self.path == "/probe":
            self._send(200, probe(req.get("bins"), req.get("modules")))
        elif self.path == "/kill":
            mission = req.get("mission")
            if not isinstance(mission, str) or not MISSION_RE.match(mission):
                return self._send(200, {"error": "bad mission id", "killed": 0})
            self._send(200, {"killed": kill_mission(mission)})
        else:
            self._send(404, {"error": "not found"})


def make_server(port: int = PORT, host: str = "0.0.0.0"):
    if not _token():
        raise SystemExit("SANDBOX_TOKEN is not set - refusing to start (fail closed)")
    return http.server.ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    srv = make_server()
    print(f"sandbox listening on :{PORT}, workroot {WORKROOT}", flush=True)
    srv.serve_forever()
