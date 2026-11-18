"""Cross-site request forgery guard: an Origin check on cookie-authenticated, state-changing requests.

Why: the session cookie is SameSite=Lax, which stops cross-SITE posts but not cross-ORIGIN ones from the same site. Any
other app on the same host (another localhost port, a sibling subdomain, a hostile page served by some dev server) could
make the admin's browser POST to /api/shell/exec, /api/vault/unlock or /api/tokens with the admin's cookie attached.

Rule (fail closed for browsers, transparent for scripts):
  * Only POST/PUT/PATCH/DELETE that carry the session cookie are checked. Bearer-token and internal-tool calls carry
    no cookie and are not affected.
  * The request's Origin (or, if absent, Referer) must be this server: the Host header, the proxy's X-Forwarded-Host,
    AURIX_PUBLIC_URL, or an entry in ALLOWED_ORIGINS.
  * A request with NEITHER header passes: browsers always send Origin on cross-origin POSTs, so a request without one
    is a script (curl, the PC launcher), not a forged browser request.

Limits (stated honestly): this does not protect GET routes that change state (there should be none), and a hostile
script already running ON this machine is out of scope (it can read the cookie file anyway).

Off switch: AURIX_CSRF_ORIGIN_CHECK=0. Every refusal is logged and written to the audit log.
"""

from __future__ import annotations

import os
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def enabled() -> bool:
    return os.getenv("AURIX_CSRF_ORIGIN_CHECK", "1").strip().lower() not in ("0", "false", "off", "no")


def _origin_of(url: str) -> Optional[str]:
    """scheme://host[:port], lower case, default ports dropped; None for anything that isn't an http(s) URL."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


def _host_only(origin: str) -> str:
    # "https://box:8000" -> "box:8000"; used against Host headers, which carry no scheme.
    return origin.split("://", 1)[1]


def _norm_host_header(value: str) -> str:
    v = (value or "").split(",")[0].strip().lower()
    for default in (":80", ":443"):
        if v.endswith(default):
            v = v[: -len(default)]
    return v


def trusted_origins(extra: Iterable[str] = ()) -> set:
    out = set()
    for raw in list(os.getenv("ALLOWED_ORIGINS", "").split(",")) + [os.getenv("AURIX_PUBLIC_URL", "")] + list(extra):
        o = _origin_of(raw)
        if o:
            out.add(o)
    return out


def request_allowed(method: str, headers: Mapping[str, str], has_session_cookie: bool) -> bool:
    """True when the request may proceed. `headers` must be case-insensitive (Starlette's are)."""
    if not has_session_cookie or (method or "").upper() not in STATE_CHANGING:
        return True
    raw = headers.get("origin")
    if raw is None or raw == "":
        raw = headers.get("referer")
        if raw is None or raw == "":
            return True  # not a browser-forged request (see module docstring)
    if raw.strip().lower() == "null":
        return False  # sandboxed iframe / file:// / opaque origin: never us
    origin = _origin_of(raw)
    if origin is None:
        return False
    if origin in trusted_origins():
        return True
    host = _host_only(origin)
    for h in (headers.get("host"), headers.get("x-forwarded-host")):
        if h and _norm_host_header(h) == host:
            return True
    return False
