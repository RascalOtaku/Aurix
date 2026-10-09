"""src/foundation/outbox.py - how Aurix decides whether, when and how often to message the owner unprompted.

Inspired by agents that, finding something wrong at night, kept trying to reach their owner for hours: worrying is good,
120 messages is not. The rules:

  - 🚨 at the start of a message means it may wake you (a broken audit chain, the disk full, a person on a camera).
    Everything else is "normal".
  - Quiet hours (AURIX_QUIET_HOURS, default 22:00-06:00 local): normal messages are held and arrive as ONE summary when
    the quiet hours end. Emergencies go through.
  - If you have been active (sent anything, tapped anything) in the last AWAKE_MINUTES, quiet hours do not apply - you
    are clearly awake.
  - At most MAX_PER_HOUR (AURIX_NOTIFY_MAX_PER_HOUR, default 8) normal messages an hour; extra ones are folded into one
    catch-up message once the hour frees up.
  - An emergency is not repeated within EMERGENCY_DEDUPE; if you have not been active since, it is followed up ONCE after
    FOLLOW_UP_MINUTES with "still waiting for you", and then left alone.

Replies to something you just typed or tapped never pass through here: you asked, you get the answer.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

e = html.escape
AWAKE_MINUTES = 20
EMERGENCY_DEDUPE = 30 * 60
FOLLOW_UP_MINUTES = 20
MAX_HELD = 200
EMERGENCY_MARK = "🚨"


def max_per_hour() -> int:
    try:
        return max(1, int(os.environ.get("AURIX_NOTIFY_MAX_PER_HOUR", "8")))
    except ValueError:
        return 8


def _path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "outbox" / "state.json"


def _load() -> dict:
    try:
        v = json.loads(_path().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def quiet_window() -> str:
    return (os.environ.get("AURIX_QUIET_HOURS") or os.environ.get("AURIX_PULSE_QUIET_HOURS") or "22:00-06:00").strip()


def in_quiet_hours(ln: datetime) -> bool:
    """Local time inside the quiet window (wraps past midnight). An unparseable window means never quiet, rather than
    silently going quiet forever on a typo."""
    try:
        a, b = quiet_window().split("-")
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
    except ValueError:
        return False
    cur, start, end = ln.hour * 60 + ln.minute, ah * 60 + am, bh * 60 + bm
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)


def _local(now: float) -> datetime:
    from src.foundation import standing as st
    return st.local_now(now)


def is_emergency(text: str) -> bool:
    return (text or "").lstrip().startswith(EMERGENCY_MARK)


def _key(text: str) -> str:
    """Same problem, same key: digits and markup do not make an alert new ("disk 96%" vs "disk 97%")."""
    first = re.sub(r"<[^>]+>", "", (text or "").splitlines()[0] if text else "")
    return re.sub(r"\d+", "#", first).strip().lower()[:160]


def note_owner_active(now: Optional[float] = None) -> None:
    """The listener calls this for every message and tap from the owner."""
    st = _load()
    st["owner_active"] = now or time.time()
    _save(st)


def owner_awake(st: dict, now: float) -> bool:
    return now - float(st.get("owner_active", 0) or 0) < AWAKE_MINUTES * 60


def decide(text: str, now: Optional[float] = None, emergency: Optional[bool] = None, whole: bool = False) -> str:
    """'send', 'hold' (stored for the summary; `whole` ones - e.g. a scheduled report - are delivered in full after it) or
    'drop' (a repeated emergency). Records sends. `emergency` overrides the 🚨 convention (the /alert API)."""
    now = now or time.time()
    st = _load()
    sent = [t for t in st.get("sent", []) if now - t < 3600]
    if emergency if emergency is not None else is_emergency(text):
        recent = {em["key"]: em for em in st.get("emergencies", []) if now - em["t"] < EMERGENCY_DEDUPE}
        if _key(text) in recent:
            return "drop"
        ems = [em for em in st.get("emergencies", []) if now - em["t"] < 6 * 3600]
        ems.append({"t": now, "key": _key(text), "text": text[:600], "followed": False})
        st["emergencies"] = ems[-20:]
        _save(st)
        return "send"
    quiet = in_quiet_hours(_local(now)) and not owner_awake(st, now)
    if quiet or len(sent) >= max_per_hour():
        held = st.get("held", [])
        held.append({"t": now, "text": text if whole else text[:2000], "why": "quiet" if quiet else "cap", "whole": whole})
        st["held"] = held[-MAX_HELD:]
        _save(st)
        return "hold"
    sent.append(now)
    st["sent"] = sent
    _save(st)
    return "send"


def _first_line(text: str) -> str:
    line = re.sub(r"<[^>]+>", "", (text or "").strip().splitlines()[0] if text.strip() else "")
    return line[:140] + ("…" if len(line) > 140 else "")


def due(now: Optional[float] = None) -> List[str]:
    """Messages to send now on the scheduler tick: the summary of held messages once quiet hours end (or the hourly cap
    frees up), and a single follow-up for an emergency nobody has seen. Never raises."""
    now = now or time.time()
    st = _load()
    out: List[str] = []
    held = st.get("held", [])
    quiet = in_quiet_hours(_local(now)) and not owner_awake(st, now)
    sent = [t for t in st.get("sent", []) if now - t < 3600]
    if held and not quiet and len(sent) < max_per_hour():
        overnight = any(h.get("why") == "quiet" for h in held)
        head = (f"🌅 <b>While you slept</b> ({len(held)} update{'s' if len(held) != 1 else ''}, held during quiet hours "
                f"{e(quiet_window())}):" if overnight else f"📬 <b>{len(held)} more update{'s' if len(held) != 1 else ''}</b> "
                f"(held to keep it under {max_per_hour()} messages an hour):")
        lines = [head]
        for h in held[:25]:
            lines.append("• " + e(_first_line(h["text"])) + (" (in full below)" if h.get("whole") else ""))
        if len(held) > 25:
            lines.append(f"… and {len(held) - 25} more.")
        lines.append("Anything waiting on a yes/no is in <code>approvals</code>.")
        out.append("\n".join(lines))
        out.extend(h["text"] for h in held if h.get("whole"))
        st["held"] = []
        sent.append(now)
    for em in st.get("emergencies", []):
        if (not em.get("followed") and now - em["t"] >= FOLLOW_UP_MINUTES * 60
                and float(st.get("owner_active", 0) or 0) < em["t"] - AWAKE_MINUTES * 60
                and not owner_awake(st, now)):
            out.append(f"{EMERGENCY_MARK} <b>Still waiting for you</b> (sent {int((now - em['t']) // 60)} min ago, "
                       f"I will not repeat it again): " + e(_first_line(em["text"])))
            em["followed"] = True
    st["sent"] = sent
    _save(st)
    return out


def status_text(now: Optional[float] = None) -> str:
    now = now or time.time()
    st = _load()
    quiet = in_quiet_hours(_local(now))
    lines = [f"🔕 Quiet hours {e(quiet_window())}: " + ("on now" + (" (you are active, so I talk)" if owner_awake(st, now) else "")
                                                      if quiet else "off now"),
             f"Up to {max_per_hour()} unprompted messages an hour; 🚨 emergencies always get through (followed up once)."]
    if st.get("held"):
        lines.append(f"Holding {len(st['held'])} message(s) for the next summary.")
    return "\n".join(lines)
