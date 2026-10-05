"""src/foundation/frigate.py - camera events from Frigate NVR (MIT, github.com/blakeblackshear/frigate) as owner alerts.

Frigate does the hard part on your own hardware (object detection on your cameras); Aurix just tells you about it:
"🚨 person at front_door 14:03 (87%)", optionally with a one-line description of the snapshot from the local vision model.

    AURIX_FRIGATE_URL        e.g. http://<frigate-host>:5000    (unset = off)
    AURIX_FRIGATE_LABELS     what to alert on, default "person"  (e.g. "person,car,dog")
    AURIX_FRIGATE_CAMERAS    optional allow-list of camera names
    AURIX_FRIGATE_COOLDOWN   seconds between alerts for the same camera + label, default 300
    AURIX_FRIGATE_DESCRIBE   1 = describe each snapshot with the local vision model (vision.py)

Read-only: Aurix never changes Frigate's config, recordings or cameras. Polled from the standing tick (about once a minute);
the first poll only sets the starting point, so turning this on never replays old events.
"""
from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape
TIMEOUT = 10
MAX_EVENTS = 25

Get = Callable[[str], Tuple[int, bytes]]


def base_url() -> str:
    return os.environ.get("AURIX_FRIGATE_URL", "").strip().rstrip("/")


def configured() -> bool:
    return base_url().startswith(("http://", "https://"))


def _labels() -> List[str]:
    return [x.strip().lower() for x in os.environ.get("AURIX_FRIGATE_LABELS", "person").split(",") if x.strip()]


def _cameras() -> Optional[set]:
    raw = os.environ.get("AURIX_FRIGATE_CAMERAS", "").strip()
    return {x.strip() for x in raw.split(",") if x.strip()} if raw else None


def _cooldown() -> float:
    try:
        return max(0.0, float(os.environ.get("AURIX_FRIGATE_COOLDOWN", "300")))
    except ValueError:
        return 300.0


def _state_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "frigate" / "state.json"


def _load() -> dict:
    try:
        v = json.loads(_state_path().read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    """Best-effort atomic state write. Never raises: poll() documents
    "Never raises", and a full disk or read-only data dir must degrade to
    re-alerting, not a standing-tick crash. _load() already tolerates a
    missing state file, so the write side matches."""
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _num(v: Any) -> float:
    """float(v) or 0.0. Frigate payloads and the on-disk state are external
    input; a malformed number must not break the poll (see "Never raises")."""
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _get(url: str) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, b""


def _score(ev: dict) -> Optional[float]:
    s = (ev.get("data") or {}).get("top_score", ev.get("top_score"))
    return float(s) if isinstance(s, (int, float)) else None


def poll(now: Optional[float] = None, get: Optional[Get] = None, describe: Optional[Callable[[bytes], str]] = None) -> List[str]:
    """New alert texts since the last poll (may be empty). Never raises."""
    if not configured():
        return []
    get = get or _get
    now = now or time.time()
    state = _load()
    if "after" not in state:                                   # first run: start from now, never replay history
        _save({"after": now, "last": {}})
        return []
    params = {"after": f"{_num(state.get('after')):.3f}", "labels": ",".join(_labels()), "limit": str(MAX_EVENTS), "has_snapshot": "1"}
    cams = _cameras()
    if cams:
        params["cameras"] = ",".join(sorted(cams))
    try:
        code, raw = get(f"{base_url()}/api/events?" + urllib.parse.urlencode(params))
        events = json.loads(raw.decode("utf-8", errors="replace")) if code == 200 else None
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not isinstance(events, list):
        return []
    last: Dict[str, float] = state.get("last") or {}
    newest = _num(state.get("after"))
    out: List[str] = []
    for ev in sorted((x for x in events if isinstance(x, dict)), key=lambda x: _num(x.get("start_time"))):
        start = _num(ev.get("start_time"))
        newest = max(newest, start)
        cam, label = str(ev.get("camera", "?")), str(ev.get("label", "?")).lower()
        if label not in _labels() or (cams and cam not in cams):
            continue
        key = f"{cam}/{label}"
        if key in last and start - _num(last[key]) < _cooldown():
            continue
        last[key] = start
        score = _score(ev)
        when = time.strftime("%H:%M", time.localtime(start))
        text = f"🚨 <b>{e(label)}</b> at <b>{e(cam)}</b> {when}" + (f" ({round(score * 100)}%)" if score is not None else "")
        if describe and ev.get("id"):
            try:
                code, jpg = get(f"{base_url()}/api/events/{urllib.parse.quote(str(ev['id']))}/snapshot.jpg")
                if code == 200 and jpg:
                    try:
                        text += "\n" + describe(jpg)
                    except Exception:  # noqa: BLE001 - a failing vision model must not break the poll;
                                       # the alert still goes out, minus the one-line description
                        pass
            except (urllib.error.URLError, OSError):
                pass
        text += f"\n{e(base_url())}/review"
        out.append(text)
        audit.append("frigate_alert", camera=cam, label=label)
    _save({"after": newest, "last": last})
    return out


def default_describer() -> Optional[Callable[[bytes], str]]:
    if os.environ.get("AURIX_FRIGATE_DESCRIBE", "").strip() not in ("1", "true", "yes"):
        return None
    from src.foundation import vision
    if not vision.model():
        return None
    return lambda jpg: vision.ask(jpg, "In one short sentence: who or what is in this camera snapshot, and what are they doing?")
