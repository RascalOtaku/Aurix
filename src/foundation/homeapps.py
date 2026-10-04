"""src/foundation/homeapps.py - the homelab apps you already run, from chat: Pi-hole, Jellyfin, Uptime Kuma.

    Pi-hole (v6 API)   AURIX_PIHOLE_URL, AURIX_PIHOLE_PASSWORD (an app password: Settings -> Web interface / API)
                       `ads` - today's queries / blocked;  `pause ads 10m` (max 60) · `resume ads`
    Jellyfin           AURIX_JELLYFIN_URL, AURIX_JELLYFIN_API_KEY (Dashboard -> API Keys)
                       `tv` - what is playing where;  `pause tv` · `resume tv`
    Uptime Kuma        AURIX_KUMA_URL, AURIX_KUMA_STATUS_PAGE (the slug of a status page listing your monitors)
                       `kuma` - up / down of every monitor and its 24 h uptime (alerts stay Kuma's own job)

Each is off until its settings exist. Nothing here can delete, configure or install anything: the only writes are pausing
ad-blocking for a bounded time and pausing/resuming playback. Pi-hole sessions are closed after every call (Pi-hole limits
how many may be open). Stdlib only; every HTTP call goes through `_http`, which tests replace.
"""
from __future__ import annotations

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape
TIMEOUT = 10
MAX_PAUSE_MIN = 60

Http = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read() or b""


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", errors="replace") or "null")
    except ValueError:
        return None


def _url(name: str) -> str:
    u = os.environ.get(name, "").strip().rstrip("/")
    return u if u.startswith(("http://", "https://")) else ""


# ---------------------------------------------------------------------------------------------------------------------------------
# Pi-hole v6
# ---------------------------------------------------------------------------------------------------------------------------------

def _pihole_session(http: Http) -> Tuple[Optional[str], str]:
    base, pw = _url("AURIX_PIHOLE_URL"), os.environ.get("AURIX_PIHOLE_PASSWORD", "")
    if not base:
        return None, "Pi-hole is not set up (AURIX_PIHOLE_URL and AURIX_PIHOLE_PASSWORD in .env)"
    try:
        code, raw = http("POST", base + "/api/auth", {"Content-Type": "application/json"}, json.dumps({"password": pw}).encode())
    except (urllib.error.URLError, OSError) as ex:
        return None, f"Pi-hole unreachable ({type(ex).__name__})"
    sess = ((_json(raw) or {}).get("session") or {})
    if code == 404:
        return None, "this Pi-hole has no v6 API (Pi-hole 6 or newer is needed)"
    if code != 200 or not sess.get("valid"):
        return None, "Pi-hole refused the password (use an app password from Settings -> Web interface / API)"
    return str(sess.get("sid") or ""), ""


def _pihole(method: str, path: str, body: Optional[dict], http: Http) -> Tuple[Optional[dict], str]:
    sid, why = _pihole_session(http)
    if sid is None:
        return None, why
    base = _url("AURIX_PIHOLE_URL")
    hdr = {"X-FTL-SID": sid, "Content-Type": "application/json"} if sid else {"Content-Type": "application/json"}
    try:
        code, raw = http(method, base + path, hdr, json.dumps(body).encode() if body is not None else None)
    except (urllib.error.URLError, OSError) as ex:
        return None, f"Pi-hole unreachable ({type(ex).__name__})"
    finally:
        if sid:
            try:
                http("DELETE", base + "/api/auth", {"X-FTL-SID": sid}, None)                 # never leak a session slot
            except (urllib.error.URLError, OSError):
                pass
    if code != 200:
        return None, f"Pi-hole answered HTTP {code}"
    return (_json(raw) or {}), ""


def pihole_status(http: Http = None) -> str:
    http = http or _http
    data, why = _pihole("GET", "/api/stats/summary", None, http)
    if data is None:
        return "🛡️ " + why + "."
    q = data.get("queries") or {}
    blocked, total = int(q.get("blocked") or 0), int(q.get("total") or 0)
    pct = float(q.get("percent_blocked") or (100.0 * blocked / total if total else 0))
    domains = (data.get("gravity") or {}).get("domains_being_blocked")
    state, _ = _pihole("GET", "/api/dns/blocking", None, http)
    mode = str((state or {}).get("blocking", "?"))
    timer = (state or {}).get("timer")
    line = f"🛡️ Pi-hole: blocking <b>{e(mode)}</b>" + (f" (back on in {int(timer // 60) or 1} min)" if timer else "")
    return line + f"\nToday: {total:,} queries, {blocked:,} blocked ({pct:.1f}%)" + (f" · {int(domains):,} domains on the lists" if domains else "")


def pihole_pause(minutes: int, http: Http = None) -> str:
    http = http or _http
    minutes = max(1, min(int(minutes or 5), MAX_PAUSE_MIN))
    data, why = _pihole("POST", "/api/dns/blocking", {"blocking": False, "timer": minutes * 60}, http)
    if data is None:
        return "🛡️ " + why + "."
    audit.append("pihole_paused", minutes=minutes)
    return f"🛡️ Ad-blocking paused for {minutes} min; it switches itself back on."


def pihole_resume(http: Http = None) -> str:
    http = http or _http
    data, why = _pihole("POST", "/api/dns/blocking", {"blocking": True, "timer": None}, http)
    if data is None:
        return "🛡️ " + why + "."
    audit.append("pihole_resumed")
    return "🛡️ Ad-blocking is back on."


# ---------------------------------------------------------------------------------------------------------------------------------
# Jellyfin
# ---------------------------------------------------------------------------------------------------------------------------------

def _jf_headers() -> Dict[str, str]:
    return {"Authorization": f'MediaBrowser Token="{os.environ.get("AURIX_JELLYFIN_API_KEY", "").strip()}"'}


def _sessions(http: Http) -> Tuple[Optional[List[dict]], str]:
    base = _url("AURIX_JELLYFIN_URL")
    if not base or not os.environ.get("AURIX_JELLYFIN_API_KEY", "").strip():
        return None, "Jellyfin is not set up (AURIX_JELLYFIN_URL and AURIX_JELLYFIN_API_KEY in .env)"
    try:
        code, raw = http("GET", base + "/Sessions?activeWithinSeconds=960", _jf_headers(), None)
    except (urllib.error.URLError, OSError) as ex:
        return None, f"Jellyfin unreachable ({type(ex).__name__})"
    if code == 401:
        return None, "Jellyfin rejected the API key"
    if code != 200:
        return None, f"Jellyfin answered HTTP {code}"
    rows = _json(raw)
    return [s for s in rows if isinstance(s, dict) and s.get("NowPlayingItem")] if isinstance(rows, list) else [], ""


def _title(item: dict) -> str:
    name = str(item.get("Name") or "?")
    if item.get("SeriesName"):
        se = ""
        if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
            se = f" S{int(item['ParentIndexNumber']):02d}E{int(item['IndexNumber']):02d}"
        return f"{item['SeriesName']}{se} - {name}"
    return name + (f" ({item['ProductionYear']})" if item.get("ProductionYear") else "")


def tv_status(http: Http = None) -> str:
    http = http or _http
    rows, why = _sessions(http)
    if rows is None:
        return "📺 " + why + "."
    if not rows:
        return "📺 Nothing is playing on Jellyfin."
    lines = ["📺 <b>Now playing</b>"]
    for s in rows[:8]:
        paused = (s.get("PlayState") or {}).get("IsPaused")
        lines.append(f"{'⏸' if paused else '▶'} {e(_title(s['NowPlayingItem']))} - {e(str(s.get('DeviceName') or '?'))}"
                     + (f" ({e(str(s['UserName']))})" if s.get("UserName") else ""))
    return "\n".join(lines)


def tv_control(action: str, http: Http = None) -> str:
    """action: pause (everything playing) | resume (everything paused)."""
    http = http or _http
    rows, why = _sessions(http)
    if rows is None:
        return "📺 " + why + "."
    want_paused = action == "resume"
    targets = [s for s in rows if bool((s.get("PlayState") or {}).get("IsPaused")) == want_paused and s.get("SupportsRemoteControl", True)]
    if not targets:
        return "📺 Nothing " + ("paused" if want_paused else "playing") + " to " + action + "."
    cmd = "Unpause" if want_paused else "Pause"
    done = []
    for s in targets:
        try:
            code, _ = http("POST", f"{_url('AURIX_JELLYFIN_URL')}/Sessions/{urllib.parse.quote(str(s.get('Id')))}/Playing/{cmd}", _jf_headers(), b"")
        except (urllib.error.URLError, OSError):
            continue
        if code in (200, 204):
            done.append(str(s.get("DeviceName") or "?"))
    audit.append("jellyfin_" + action, sessions=len(done))
    if not done:
        return "📺 Jellyfin did not accept the command."
    return f"📺 {'Resumed' if want_paused else 'Paused'} on " + ", ".join(e(d) for d in done) + "."


# ---------------------------------------------------------------------------------------------------------------------------------
# Uptime Kuma (a status page: read-only, no login)
# ---------------------------------------------------------------------------------------------------------------------------------

KUMA_STATUS = {0: "🔴 down", 1: "🟢 up", 2: "🟡 pending", 3: "🔧 maintenance"}


def kuma_status(http: Http = None) -> str:
    http = http or _http
    base, slug = _url("AURIX_KUMA_URL"), os.environ.get("AURIX_KUMA_STATUS_PAGE", "").strip()
    if not base or not slug:
        return "📟 Uptime Kuma is not set up (AURIX_KUMA_URL and AURIX_KUMA_STATUS_PAGE, the slug of a status page, in .env)."
    slug_q = urllib.parse.quote(slug)
    try:
        code1, raw1 = http("GET", f"{base}/api/status-page/{slug_q}", {}, None)
        code2, raw2 = http("GET", f"{base}/api/status-page/heartbeat/{slug_q}", {}, None)
    except (urllib.error.URLError, OSError) as ex:
        return f"📟 Uptime Kuma unreachable ({type(ex).__name__})."
    if code1 == 404 or code2 == 404:
        return f"📟 Uptime Kuma has no status page called <code>{e(slug)}</code>."
    page, beats = _json(raw1) or {}, _json(raw2) or {}
    monitors = [(m.get("id"), str(m.get("name") or "?")) for g in page.get("publicGroupList") or [] for m in g.get("monitorList") or []]
    if not monitors:
        return "📟 That status page lists no monitors."
    hb, up = beats.get("heartbeatList") or {}, beats.get("uptimeList") or {}
    lines, down = [], 0
    for mid, name in monitors:
        last = (hb.get(str(mid)) or [{}])[-1]
        st = last.get("status")
        down += st == 0
        u = up.get(f"{mid}_24")
        lines.append(f"{KUMA_STATUS.get(st, '⚪ no data')} {e(name)}" + (f" · {float(u) * 100:.1f}% 24 h" if isinstance(u, (int, float)) else "")
                     + (f" - {e(str(last['msg'])[:80])}" if st == 0 and last.get("msg") else ""))
    head = f"📟 <b>Uptime Kuma</b>: {len(monitors) - down}/{len(monitors)} up"
    return "\n".join([head] + lines)
