"""src/foundation/wake.py - Wake-on-LAN for the GPU box (the 3431, the owner's gaming PC): when AURIX
wants to use it (LLM inference, GamePilot) and it happens to be off, send it a magic packet instead of
just quietly falling back and waiting for the owner to turn it on themselves.

Owner's explicit call (2026-09-25, while away for 4 days): auto-wake, no approval needed - this is a
physical machine powering on unattended, so it is deliberately gated by a cooldown (never spam magic
packets) and is fire-and-forget (a UDP send either succeeds or doesn't; it never blocks or raises into
the caller's path). Confirmed live on the box itself: "Wake on Magic Packet" and "Shutdown Wake-On-Lan"
are both Enabled, and it is listed as `wake_armed` in Windows' own power configuration.

Pure stdlib so this is testable everywhere, like session_rotation.py/nightshift.py.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Optional

MAC = os.environ.get("AURIX_GPU_MAC", "")          # the GPU PC's network card; unset = wake does nothing
BROADCAST = os.environ.get("AURIX_LAN_BROADCAST", "10.0.0.255")
PORT = int(os.environ.get("AURIX_WOL_PORT", "9"))
COOLDOWN_SECONDS = int(os.environ.get("AURIX_WAKE_COOLDOWN_SECONDS", "180"))
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def _data_dir() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "wake"


def _state_path() -> Path:
    return _data_dir() / "state.json"


def _load_state() -> dict:
    try:
        v = json.loads(_state_path().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def build_magic_packet(mac: str) -> bytes:
    """The standard 102-byte WOL payload: six 0xFF bytes, then the target MAC repeated 16 times."""
    if not MAC_RE.match(mac or ""):
        raise ValueError("bad MAC address")
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet(mac: str = MAC, broadcast: str = BROADCAST, port: int = PORT) -> bool:
    """Fire-and-forget: a UDP broadcast either goes out or it doesn't. Never raises - a send failure
    (no network, bad address) just means the wake attempt silently did nothing, same as if the owner's
    router dropped it, which is the everyday case for a WOL packet anyway (no delivery confirmation)."""
    try:
        packet = build_magic_packet(mac)
    except ValueError:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (broadcast, port))
        return True
    except OSError:
        return False


def wake_gpu_box(now: Optional[float] = None, reason: str = "") -> bool:
    """The gated public entry point: sends a magic packet at most once per COOLDOWN_SECONDS, so callers
    (every LLM call, every GamePilot arm) can call this unconditionally without ever spamming the LAN
    or the machine's boot sequence. Returns True only when a packet was actually sent this call."""
    from src.foundation import audit
    now = now or time.time()
    state = _load_state()
    last = float(state.get("last_sent", 0))
    if now - last < COOLDOWN_SECONDS:
        return False
    sent = send_magic_packet()
    state["last_sent"] = now
    _save_state(state)
    if sent:
        audit.append("gpu_box_wake_sent", reason=reason[:120])
    return sent
