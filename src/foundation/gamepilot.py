"""src/foundation/gamepilot.py - GamePilot: see and drive your gaming PC from a Fire TV Stick (or any browser), safely.

    Fire TV / browser  <-- frames, state --  the relay (this module, in the app on the server)  -- commands, want_frames -->  PC agent
    (gamepilot.html)   --- keys/mouse ---->                                                   <-- frames, foreground name --  (outbound only)

Nothing connects INTO the gaming PC: its agent calls out to the relay over Tailscale, signs every request (HMAC, shared key), and verifies the
relay's signature on every reply. The viewer has no password to type on a TV remote: the page shows a 6-digit code, you send `pair <code>` from your
own Telegram, and only then does that browser get a session (revocable with `unpair`). The three switches, all owner-only:

    paired    the browser may WATCH (frames only, and only while a game or Steam is in front on the PC)
    armed     the browser may also SEND keys and mouse; expires by itself (default 15 min, max 60), `disarm`/STOP ends it immediately
    the PC    the agent re-checks on its own that a game or Steam is the foreground window before it injects a single key

Input is a tiny allow-list (letters, digits, arrows, Enter, Esc, Space, Tab, Shift, Ctrl, F1-F12, mouse move/click/wheel): no Windows key, no Alt,
no chords that could reach the OS. Frames live in memory only (latest one), never on disk. Audit records carry counts and states, never keystrokes.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.foundation import audit, gaming

PAIR_TTL = 600
PAIR_MAX_PENDING = 5
SESSION_DAYS = 30
ARM_DEFAULT_MIN = 15
ARM_MAX_MIN = 60
AGENT_ONLINE_SECONDS = 6
SKEW_SECONDS = 90
MAX_EVENTS_PER_CALL = 40
MAX_QUEUE = 400
MAX_FRAME_BYTES = 900_000
CODE_RX = re.compile(r"^\d{6}$")
LAN_URL = os.environ.get("AURIX_LAN_URL", "http://10.0.0.100:7000")
TAILNET_URL = os.environ.get("AURIX_TAILNET_URL", "http://gaming-pc.example.ts.net:7000")
TOKEN_RX = re.compile(r"^[A-Za-z0-9_-]{20,80}$")

KEYS = ({c: c for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
        | {"up": "up", "down": "down", "left": "left", "right": "right", "enter": "enter", "escape": "escape", "space": "space", "tab": "tab", "backspace": "backspace",
           "shift": "shift", "ctrl": "ctrl"} | {f"f{i}": f"f{i}" for i in range(1, 13)})
MOUSE_BUTTONS = ("left", "right", "middle")

_lock = threading.Lock()
_frame: Dict[str, Any] = {"seq": 0, "jpeg": b"", "at": 0.0, "meta": {}}
_queue: List[dict] = []
_seq = [0]
_seen_sigs: Dict[str, float] = {}
_agent: Dict[str, Any] = {"seen": 0.0, "foreground": "", "blocked": False, "res": [0, 0], "fps": 0.0}
_pending: Dict[str, dict] = {}


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "gamepilot"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read(name: str, default: Any) -> Any:
    try:
        return json.loads((_data() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def reset_memory() -> None:                                                   # tests
    with _lock:
        _frame.update({"seq": 0, "jpeg": b"", "at": 0.0, "meta": {}})
        _queue.clear()
        _seen_sigs.clear()
        _pending.clear()
        _agent.update({"seen": 0.0, "foreground": "", "blocked": False, "res": [0, 0], "fps": 0.0})


# ---------------------------------------------------------------------------------------------------------------------------------
# signing (the PC agent implements the same two functions)
# ---------------------------------------------------------------------------------------------------------------------------------

def _key() -> Optional[bytes]:
    return gaming.load_key()


def request_signature(key: bytes, ts: str, method: str, path: str, body: bytes) -> str:
    msg = f"{ts}.{method.upper()}.{path}.{hashlib.sha256(body or b'').hexdigest()}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def reply_signature(key: bytes, payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "sig"}
    return hmac.new(key, json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(), hashlib.sha256).hexdigest()


def verify_agent(method: str, path: str, body: bytes, ts: str, sig: str, now: Optional[float] = None) -> Optional[str]:
    """None if the request really comes from the PC agent, else why not."""
    now = now or time.time()
    key = _key()
    if key is None:
        return "no signing key on the server"
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "bad timestamp"
    if abs(now - t) > SKEW_SECONDS:
        return "stale request"
    if not hmac.compare_digest(str(sig or ""), request_signature(key, ts, method, path, body)):
        return "bad signature"
    with _lock:
        for k in [k for k, v in _seen_sigs.items() if now - v > 2 * SKEW_SECONDS]:
            _seen_sigs.pop(k, None)
        if sig in _seen_sigs:
            return "replayed request"
        _seen_sigs[sig] = now
    return None


# ---------------------------------------------------------------------------------------------------------------------------------
# pairing and sessions
# ---------------------------------------------------------------------------------------------------------------------------------

def new_pairing(now: Optional[float] = None) -> Dict[str, Any]:
    """Called by the (unauthenticated) page. The code is shown on the TV; only the owner's Telegram can confirm it."""
    now = now or time.time()
    with _lock:
        for k in [k for k, v in _pending.items() if v["expires"] < now]:
            _pending.pop(k, None)
        if len(_pending) >= PAIR_MAX_PENDING:
            return {"error": "too many pairing attempts; wait a few minutes"}
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        while code in _pending:
            code = "".join(secrets.choice("0123456789") for _ in range(6))
        poll = secrets.token_urlsafe(18)
        _pending[code] = {"poll": poll, "expires": now + PAIR_TTL, "token": None, "confirmed": False}
    return {"code": code, "poll": poll, "expires_in": PAIR_TTL}


def confirm_pairing(code: str, now: Optional[float] = None) -> str:
    """Owner-only (Telegram `pair 123456`, or the dashboard). Creates the session the waiting browser will pick up."""
    now = now or time.time()
    code = (code or "").strip()
    if not CODE_RX.match(code):
        return "A pairing code is 6 digits, shown on the TV."
    with _lock:
        p = _pending.get(code)
        if not p or p["expires"] < now:
            return "That code is not waiting (or it expired). Reload the GamePilot page on the TV for a new one."
        token = secrets.token_urlsafe(32)
        p["token"], p["confirmed"] = token, True
    sessions = _read("sessions.json", {})
    sessions[hashlib.sha256(token.encode()).hexdigest()] = {"created": now, "expires": now + SESSION_DAYS * 86400, "last_seen": now}
    _atomic(_data() / "sessions.json", sessions)
    audit.append("gamepilot_paired", sessions=len(sessions))
    return "📺 Paired. The TV will connect in a moment. It can watch the game now; `arm` lets it send keys and mouse for 15 minutes."


def pair_status(poll: str, now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    with _lock:
        for code, p in list(_pending.items()):
            if secrets.compare_digest(p["poll"], str(poll or "")):
                if p["expires"] < now:
                    _pending.pop(code, None)
                    return {"state": "expired"}
                if p["confirmed"]:
                    tok = p["token"]
                    _pending.pop(code, None)                                  # the token is handed over exactly once
                    return {"state": "paired", "token": tok}
                return {"state": "waiting"}
    return {"state": "expired"}


def session(token: str, now: Optional[float] = None) -> Optional[dict]:
    now = now or time.time()
    if not TOKEN_RX.match(token or ""):
        return None
    h = hashlib.sha256(token.encode()).hexdigest()
    sessions = _read("sessions.json", {})
    s = sessions.get(h)
    if not s or s["expires"] < now:
        return None
    if now - s.get("last_seen", 0) > 300:
        s["last_seen"] = now
        _atomic(_data() / "sessions.json", sessions)
    return s


def unpair_all() -> str:
    n = len(_read("sessions.json", {}))
    _atomic(_data() / "sessions.json", {})
    disarm(silent=True)
    with _lock:
        _pending.clear()
        _queue.clear()
    audit.append("gamepilot_unpaired", sessions=n)
    return f"🔌 Unpaired {n} screen(s) and disarmed. Each TV has to pair again."


# ---------------------------------------------------------------------------------------------------------------------------------
# arming
# ---------------------------------------------------------------------------------------------------------------------------------

def arm(minutes: int = ARM_DEFAULT_MIN, now: Optional[float] = None) -> str:
    now = now or time.time()
    if not _read("sessions.json", {}):
        return "Nothing is paired yet. Open the GamePilot page on the TV and send me the code."
    minutes = max(1, min(int(minutes or ARM_DEFAULT_MIN), ARM_MAX_MIN))
    _atomic(_data() / "state.json", {"armed_until": now + minutes * 60})
    audit.append("gamepilot_armed", minutes=minutes)
    return f"🎮 Armed for {minutes} min: the paired TV can send keys and mouse while a game or Steam is in front. <code>disarm</code> or STOP ends it instantly."


def disarm(silent: bool = False) -> str:
    _atomic(_data() / "state.json", {"armed_until": 0})
    with _lock:
        _queue.clear()
    if not silent:
        audit.append("gamepilot_disarmed")
    return "🛑 Disarmed. The TV can watch but not control."


def armed_until(now: Optional[float] = None) -> float:
    now = now or time.time()
    from src.foundation import shards
    if shards.is_paused("gamepilot"):
        return 0.0
    try:
        from src import approval_gate as ag
        if ag.stop_engaged():
            return 0.0
    except Exception:                                                        # noqa: BLE001
        pass
    v = float(_read("state.json", {}).get("armed_until", 0) or 0)
    return v if v > now else 0.0


# ---------------------------------------------------------------------------------------------------------------------------------
# input (viewer -> queue -> agent)
# ---------------------------------------------------------------------------------------------------------------------------------

def clean_event(ev: Any) -> Optional[dict]:
    """A validated command, or None. Anything that is not on the allow-list is dropped, not repaired."""
    if not isinstance(ev, dict):
        return None
    t = ev.get("t")
    if t == "key":
        k, d = ev.get("k"), ev.get("d", "tap")
        if k in KEYS and d in ("down", "up", "tap"):
            return {"t": "key", "k": KEYS[k], "d": d}
    elif t == "mouse":
        b, d = ev.get("b", "none"), ev.get("d", "move")
        try:
            x, y = float(ev.get("x", 0)), float(ev.get("y", 0))
        except (TypeError, ValueError):
            return None
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and b in MOUSE_BUTTONS + ("none",) and d in ("move", "down", "up", "click"):
            return {"t": "mouse", "x": round(x, 4), "y": round(y, 4), "b": b, "d": d}
    elif t == "rel":                                                          # relative look (games that hide the cursor)
        try:
            dx, dy = int(ev.get("dx", 0)), int(ev.get("dy", 0))
        except (TypeError, ValueError):
            return None
        return {"t": "rel", "dx": max(-200, min(200, dx)), "dy": max(-200, min(200, dy))}
    elif t == "wheel":
        try:
            return {"t": "wheel", "dy": max(-10, min(10, int(ev.get("dy", 0))))}
        except (TypeError, ValueError):
            return None
    return None


def submit_input(token: str, events: Any, now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    if session(token, now) is None:
        return {"ok": False, "error": "not paired"}
    if armed_until(now) <= 0:
        return {"ok": False, "error": "not armed"}
    if not isinstance(events, list):
        return {"ok": False, "error": "bad input"}
    good = [c for c in (clean_event(e) for e in events[:MAX_EVENTS_PER_CALL]) if c]
    with _lock:
        if len(_queue) + len(good) > MAX_QUEUE:
            return {"ok": False, "error": "too fast"}
        for c in good:
            _seq[0] += 1
            _queue.append({**c, "n": _seq[0]})
    return {"ok": True, "accepted": len(good), "dropped": len(events) - len(good)}


# ---------------------------------------------------------------------------------------------------------------------------------
# the agent's side of the relay
# ---------------------------------------------------------------------------------------------------------------------------------

def viewers_active(now: Optional[float] = None) -> bool:
    now = now or time.time()
    from src.foundation import shards
    if shards.is_paused("gamepilot"):
        return False
    return now - _frame.get("wanted_at", 0) < 15 or armed_until(now) > 0


def agent_poll(report: dict, now: Optional[float] = None) -> Dict[str, Any]:
    """The agent's periodic call: it reports what it sees, the relay answers with what it should do. The reply is signed."""
    now = now or time.time()
    with _lock:
        _agent.update(seen=now, foreground=str(report.get("foreground", ""))[:60], blocked=bool(report.get("blocked")),
                      res=[int(report.get("w", 0) or 0), int(report.get("h", 0) or 0)], fps=float(report.get("fps", 0) or 0))
        cmds = list(_queue)
        _queue.clear()
    armed = armed_until(now)
    reply = {"ts": round(now, 3), "want_frames": bool(viewers_active(now)), "armed_until": round(armed, 1), "commands": cmds}
    key = _key()
    if key is not None:
        reply["sig"] = reply_signature(key, reply)
    return reply


def put_frame(jpeg: bytes, meta: Optional[dict] = None, now: Optional[float] = None) -> int:
    if not jpeg or len(jpeg) > MAX_FRAME_BYTES or jpeg[:2] != b"\xff\xd8":
        return 0
    with _lock:
        _frame.update(seq=_frame["seq"] + 1, jpeg=jpeg, at=now or time.time(), meta=dict(meta or {}))
        return _frame["seq"]


def frame_after(seq: int) -> Optional[Tuple[int, bytes]]:
    with _lock:
        return (_frame["seq"], _frame["jpeg"]) if _frame["seq"] > seq and _frame["jpeg"] else None


def note_viewer(now: Optional[float] = None) -> None:
    _frame["wanted_at"] = now or time.time()


def viewer_state(token: str, now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    if session(token, now) is None:
        return {"paired": False}
    note_viewer(now)
    online = now - _agent["seen"] < AGENT_ONLINE_SECONDS
    au = armed_until(now)
    return {"paired": True, "pc_online": online, "foreground": _agent["foreground"] if online else "", "blocked": bool(_agent["blocked"]) if online else False,
            "armed": au > 0, "armed_left": max(0, int(au - now)), "res": _agent["res"], "fps": _agent["fps"], "frame_seq": _frame["seq"],
            "frame_age": round(now - _frame["at"], 1) if _frame["at"] else None}


# ---------------------------------------------------------------------------------------------------------------------------------
# owner views
# ---------------------------------------------------------------------------------------------------------------------------------

def status_text(now: Optional[float] = None) -> str:
    now = now or time.time()
    n = len([s for s in _read("sessions.json", {}).values() if s["expires"] > now])
    online = now - _agent["seen"] < AGENT_ONLINE_SECONDS
    au = armed_until(now)
    lines = ["🎮 <b>GamePilot</b>",
             f"PC agent: {'online, ' + (_agent['foreground'] or 'no game in front') if online else 'not connected (start it on the PC)'}",
             f"Paired screens: {n} · " + (f"ARMED for {int((au - now) / 60) + 1} more min" if au else "watch only (not armed)"),
             f"On the Fire TV's Silk browser open <code>{LAN_URL}/gamepilot</code> (home Wi-Fi) or <code>{TAILNET_URL}/gamepilot</code> (Tailscale), then send me <code>pair &lt;code&gt;</code>.",
             "<code>arm [minutes]</code> · <code>disarm</code> · <code>unpair</code>"]
    return "\n".join(lines)


def panel(now: Optional[float] = None) -> Dict[str, Any]:
    now = now or time.time()
    online = now - _agent["seen"] < AGENT_ONLINE_SECONDS
    au = armed_until(now)
    return {"pc_online": online, "foreground": _agent["foreground"] if online else "", "paired": len([s for s in _read("sessions.json", {}).values() if s["expires"] > now]),
            "armed": au > 0, "armed_left": max(0, int(au - now)), "url": "/gamepilot"}
