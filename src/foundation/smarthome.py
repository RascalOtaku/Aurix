"""src/foundation/smarthome.py - lights, plugs and fans through Home Assistant (the ADA idea of "voice-controlled smart home",
done through one integration instead of a library per brand: Home Assistant already speaks Kasa, Hue, Tuya, Zigbee, ...).

    AURIX_HA_URL       e.g. http://homeassistant.local:8123
    AURIX_HA_TOKEN     a long-lived access token (HA -> your profile -> Security)
    AURIX_HA_ENTITIES  optional allow-list of entity ids Aurix may switch (comma-separated); unset = every light/switch/fan

Chat: `lights` lists them, `turn on desk lamp`, `turn off kitchen`, `toggle fan`. Only lights, switches and fans can be
switched from chat. Locks, garage doors, alarms and thermostats are shown read-only: opening a door from a chat message is
not a mistake worth making possible. The full Home Assistant tool set is also available to the agent as an MCP preset
(AURIX_MCP_PRESETS=home-assistant), where Home Assistant's own "exposed entities" setting decides what it may touch.

Stdlib only; every HTTP call goes through `_http`, which tests replace.
"""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from src.foundation import audit

e = html.escape
SWITCHABLE = ("light", "switch", "fan")
SHOWN = SWITCHABLE + ("lock", "cover", "climate", "alarm_control_panel")
TIMEOUT = 10

Http = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


def configured() -> bool:
    return bool(os.environ.get("AURIX_HA_URL", "").startswith(("http://", "https://")) and os.environ.get("AURIX_HA_TOKEN", "").strip())


def _base() -> str:
    return os.environ.get("AURIX_HA_URL", "").rstrip("/")


def _headers() -> Dict[str, str]:
    return {"Authorization": "Bearer " + os.environ.get("AURIX_HA_TOKEN", "").strip(), "Content-Type": "application/json"}


def _allowed() -> Optional[set]:
    raw = os.environ.get("AURIX_HA_ENTITIES", "").strip()
    return {x.strip().lower() for x in raw.split(",") if x.strip()} if raw else None


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, ex.read() or b""


def entities(http: Http = None) -> Tuple[List[dict], str]:
    """[{id, name, domain, state}] for the domains Aurix shows, or ([], why)."""
    http = http or _http
    if not configured():
        return [], "Home Assistant is not set up (AURIX_HA_URL and AURIX_HA_TOKEN in .env)"
    try:
        code, raw = http("GET", _base() + "/api/states", _headers(), None)
    except (urllib.error.URLError, OSError) as ex:
        return [], f"Home Assistant unreachable ({type(ex).__name__})"
    if code == 401:
        return [], "Home Assistant rejected the token (401): make a new long-lived access token"
    if code != 200:
        return [], f"Home Assistant answered HTTP {code}"
    try:
        rows = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return [], "Home Assistant returned something that is not JSON"
    out = []
    for r in rows if isinstance(rows, list) else []:
        eid = str(r.get("entity_id", ""))
        domain = eid.split(".", 1)[0]
        if domain in SHOWN:
            name = str((r.get("attributes") or {}).get("friendly_name") or eid)
            out.append({"id": eid, "name": name, "domain": domain, "state": str(r.get("state", ""))})
    return sorted(out, key=lambda x: (SHOWN.index(x["domain"]), x["name"].lower())), ""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match(query: str, rows: List[dict]) -> Tuple[Optional[dict], List[dict]]:
    """(the one entity meant, []) or (None, candidates). Exact name/id, then whole-word prefix, then substring."""
    q = _norm(re.sub(r"^(the|my)\s+", "", query.strip(), flags=re.I))
    if not q:
        return None, []
    for test in (lambda r: _norm(r["name"]) == q or _norm(r["id"]) == q or _norm(r["id"].split(".", 1)[1]) == q,
                 lambda r: _norm(r["name"]).startswith(q + " ") or (" " + q + " ") in (" " + _norm(r["name"]) + " "),
                 lambda r: q in _norm(r["name"])):
        hits = [r for r in rows if test(r)]
        if len(hits) == 1:
            return hits[0], []
        if hits:
            return None, hits
    return None, []


def switch(action: str, query: str, http: Http = None) -> str:
    """action: on | off | toggle."""
    http = http or _http
    rows, why = entities(http)
    if why:
        return "🏠 " + why + "."
    hit, many = match(query, rows)
    if hit is None:
        if many:
            return "🏠 Which one? " + ", ".join(f"<b>{e(r['name'])}</b>" for r in many[:8])
        return f"🏠 Nothing called <b>{e(query)}</b>. <code>lights</code> lists what I can see."
    if hit["domain"] not in SWITCHABLE:
        return (f"🏠 <b>{e(hit['name'])}</b> is a {hit['domain'].replace('_', ' ')}: I show it, but I do not operate "
                f"locks, doors, alarms or heating from chat. Use the Home Assistant app.")
    allow = _allowed()
    if allow is not None and hit["id"].lower() not in allow:
        return f"🏠 <b>{e(hit['name'])}</b> is not in AURIX_HA_ENTITIES, so I leave it alone."
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}[action]
    try:
        code, _ = http("POST", f"{_base()}/api/services/{hit['domain']}/{service}", _headers(),
                       json.dumps({"entity_id": hit["id"]}).encode())
    except (urllib.error.URLError, OSError) as ex:
        return f"🏠 Home Assistant unreachable ({type(ex).__name__})."
    if code != 200:
        return f"🏠 Home Assistant refused that (HTTP {code})."
    audit.append("smarthome_switch", entity=hit["id"], action=action)
    verb = {"on": "on", "off": "off", "toggle": "toggled"}[action]
    return f"🏠 <b>{e(hit['name'])}</b> {verb}." if action == "toggle" else f"🏠 Turned <b>{e(hit['name'])}</b> {verb}."


def overview(http: Http = None) -> str:
    rows, why = entities(http)
    if why:
        return "🏠 " + why + "."
    if not rows:
        return "🏠 Home Assistant has no lights, switches, fans, locks, covers or thermostats I can see."
    icon = {"light": "💡", "switch": "🔌", "fan": "🌀", "lock": "🔒", "cover": "🚪", "climate": "🌡️", "alarm_control_panel": "🚨"}
    lines = ["🏠 <b>Home</b>"]
    for r in rows[:40]:
        lines.append(f"{icon.get(r['domain'], '•')} {e(r['name'])}: {e(r['state'])}" + ("" if r["domain"] in SWITCHABLE else " (read-only)"))
    return "\n".join(lines)
