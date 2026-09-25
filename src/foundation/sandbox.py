"""src/foundation/sandbox.py - client for the isolated sandbox container (odysseus/mission_sandbox/).

When a mission is `sandboxed`, its bash/python calls execute THERE, not in the app
container that holds the secrets. This is what makes it safe to run mission code without
pinging the owner: the code can only touch the mission workspace, has no credentials and
no network.

FAILS CLOSED: if the sandbox is unreachable the call errors. It never falls back to
running the code locally.

Stdlib only (urllib); blocking calls are meant to be wrapped with asyncio.to_thread.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterable, Optional

from src.foundation import capabilities as cap

SANDBOX_TOOLS = {"bash": "bash", "mcp__bash__bash": "bash", "python": "python", "mcp__python__python": "python"}
_health_cache = {"at": 0.0, "ok": False}


class SandboxUnavailable(RuntimeError):
    pass


def base_url() -> str:
    return os.environ.get("SANDBOX_URL", "http://sandbox:8765").rstrip("/")


def _token() -> str:
    return os.environ.get("SANDBOX_TOKEN", "")


def _post(path: str, payload: dict, timeout: float) -> dict:
    if not _token():
        raise SandboxUnavailable("SANDBOX_TOKEN is not configured")
    req = urllib.request.Request(
        base_url() + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Sandbox-Token": _token()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SandboxUnavailable(f"sandbox refused the request (HTTP {e.code})") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SandboxUnavailable(f"sandbox unreachable: {e}") from e


def available(max_age: float = 15.0) -> bool:
    now = time.time()
    if now - _health_cache["at"] < max_age:
        return _health_cache["ok"]
    ok = False
    if _token():
        try:
            with urllib.request.urlopen(base_url() + "/health", timeout=1.5) as r:
                ok = bool(json.loads(r.read()).get("ok"))
        except (urllib.error.URLError, OSError, ValueError):
            ok = False
    _health_cache.update(at=now, ok=ok)
    return ok


def reset_cache() -> None:
    _health_cache.update(at=0.0, ok=False)


def run(mission_id: str, tool: str, code: str, timeout: int = 300) -> dict:
    """Execute `code` in the sandbox. Returns the app's normal tool-result shape."""
    kind = SANDBOX_TOOLS.get(tool)
    if kind is None:
        raise SandboxUnavailable(f"tool '{tool}' cannot run in the sandbox")
    lines = code.split("\n")
    if lines and lines[0].strip().lower() in ("#!bg", "#bg", "# bg", "#background"):
        code = "\n".join(lines[1:])            # no detached jobs: run in the foreground with a timeout
    res = _post("/exec", {"mission": mission_id, "kind": kind, "code": code, "timeout": timeout},
                timeout=timeout + 30)
    if "error" in res and "exit_code" in res and "stdout" not in res:
        return {"error": f"sandbox: {res['error']}", "exit_code": res.get("exit_code", 1)}
    out = (res.get("stdout") or "").rstrip()
    err = (res.get("stderr") or "").rstrip()
    combined = (out + ("\nSTDERR: " + err if err else "")).strip() if out or err else "(no output)"
    if res.get("error"):
        return {"error": f"sandbox: {res['error']}", "exit_code": res.get("exit_code", 124),
                "stdout": out, "stderr": err}
    return {"output": combined, "exit_code": res.get("exit_code", 0)}


def kill(mission_id: str) -> int:
    """Kill every process the mission has running in the sandbox (STOP). Best effort: returns
    how many were killed, or 0 if the sandbox cannot be reached."""
    try:
        return int(_post("/kill", {"mission": mission_id}, timeout=10).get("killed", 0))
    except SandboxUnavailable:
        return 0


def probe(bins: Iterable[str], modules: Iterable[str]) -> dict:
    return _post("/probe", {"bins": sorted(bins), "modules": sorted(modules)}, timeout=10)


def presence_kw() -> Dict[str, Callable]:
    """Presence functions backed by what is installed IN THE SANDBOX (empty if unavailable),
    for capabilities.presence / resolve / identify."""
    if not available():
        return {}
    bins = {c.check for c in cap.REGISTRY.values() if c.kind == "binary"}
    mods = {c.check for c in cap.REGISTRY.values() if c.kind == "python_pkg"}
    try:
        found = probe(bins, mods)
    except SandboxUnavailable:
        return {}
    return {
        "which": lambda b: ("/usr/bin/" + b) if found.get("bins", {}).get(b) else None,
        "find_spec": lambda m: object() if found.get("modules", {}).get(m) else None,
    }
