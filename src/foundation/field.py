"""src/foundation/field.py - Field Operations, phase 1: a place you stood becomes a record AURIX keeps.

    share a location on Telegram          -> a field point (UUID, coordinates, accuracy, time), status UNVERIFIED
    a photo / voice note within 15 min    -> attached to that point (photo kept as the original file; voice -> its transcript job)
    points                                -> the latest points
    point [id]                            -> one point with everything attached
    point [id] name: Old stone foundation -> name it ("id" may be left out: the latest point)
    point [id] type: historical|trail|nature|land|custom
    point [id] note: north wall partly collapsed
    point <id> confirm                    -> only YOU mark a point confirmed; AURIX never does
    points gpx | points geojson           -> an export for Organic Maps, QGIS or a handheld GPS

Rules from the Nomad Lab scope (Claude Doc "AURIX Field Operations"):
  * Capture never depends on anything else working: the point is saved before any model looks at anything.
  * The record says honestly what its time is. Telegram stamps a message when its server RECEIVED it, so a location shared offline
    and delivered later carries the delivery time; every point is labelled "time = when Telegram received it".
  * A model's description of a photo is kept as a lead (`ai_note`), never as the observation itself.
  * Stable UUIDs, never row numbers. GPX/GeoJSON are exports; this database is the authority.

Location data is private: it lives under data/field/ (never committed) and exports go only to the owner's own chat.
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from xml.sax.saxutils import escape as xesc

from src.foundation import audit

e = html.escape
ATTACH_WINDOW_S = 15 * 60
TYPES = ("historical", "trail", "nature", "land", "custom")
STATUSES = ("unverified", "confirmed")
SHORT_RX = re.compile(r"^fp-[0-9a-f]{6}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id        TEXT PRIMARY KEY,               -- a UUID
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    accuracy  REAL,                           -- metres, when the phone reported it
    at        REAL NOT NULL,                  -- when Telegram received the location (see the module note)
    name      TEXT NOT NULL DEFAULT '',
    type      TEXT NOT NULL DEFAULT 'custom',
    note      TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL DEFAULT 'unverified',
    source    TEXT NOT NULL DEFAULT 'telegram',
    created   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    id        TEXT PRIMARY KEY,
    point_id  TEXT NOT NULL REFERENCES points(id),
    kind      TEXT NOT NULL,                  -- photo | voice
    ref       TEXT NOT NULL,                  -- photo: file path under data/field/media; voice: transcription job id
    caption   TEXT NOT NULL DEFAULT '',
    ai_note   TEXT NOT NULL DEFAULT '',       -- a model's reading of a photo: a lead, not an observation
    at        REAL NOT NULL
);
"""


def _root() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "field"


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    _root().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_root() / "field.db"), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def short(pid: str) -> str:
    return "fp-" + pid.replace("-", "")[:6]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _all() -> List[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM points ORDER BY at DESC").fetchall()]


def get(ref: str = "") -> Optional[dict]:
    """A point by its short id (fp-xxxxxx) or full UUID; '' = the latest."""
    ref = (ref or "").strip().lower()
    pts = _all()
    if not ref:
        return pts[0] if pts else None
    return next((p for p in pts if p["id"] == ref or short(p["id"]) == ref), None)


def attachments(pid: str) -> List[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM attachments WHERE point_id = ? ORDER BY at", (pid,)).fetchall()]


# ------------------------------------------------------------------------------------------------------------------------------
# capture
# ------------------------------------------------------------------------------------------------------------------------------

def capture(lat: float, lon: float, at: Optional[float] = None, accuracy: Optional[float] = None, source: str = "telegram") -> dict:
    """Save a point. Nothing else has to work for this to succeed."""
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates out of range")
    pid, now = str(uuid.uuid4()), time.time()
    row = {"id": pid, "lat": round(lat, 7), "lon": round(lon, 7), "accuracy": round(float(accuracy), 1) if accuracy else None,
           "at": float(at or now), "source": source, "created": now}
    with _db() as c:
        c.execute("INSERT INTO points(id, lat, lon, accuracy, at, source, created) VALUES(?,?,?,?,?,?,?)",
                  (row["id"], row["lat"], row["lon"], row["accuracy"], row["at"], source, now))
    audit.append("field_point", id=short(pid))                        # never the coordinates: the audit log is not the place for them
    return get(pid)


def open_point(at: float) -> Optional[dict]:
    """The point a photo or voice note sent at `at` belongs to: the latest one captured up to ATTACH_WINDOW_S before it."""
    for p in _all():
        if 0 <= at - p["at"] <= ATTACH_WINDOW_S:
            return p
    return None


def _attach(pid: str, kind: str, ref: str, caption: str = "", ai_note: str = "", at: Optional[float] = None) -> str:
    aid = str(uuid.uuid4())
    with _db() as c:
        c.execute("INSERT INTO attachments(id, point_id, kind, ref, caption, ai_note, at) VALUES(?,?,?,?,?,?,?)",
                  (aid, pid, kind, ref, (caption or "")[:500], (ai_note or "")[:1500], at or time.time()))
    audit.append("field_attach", point=short(pid), kind=kind)
    return aid


def attach_photo(file_id: str, caption: str, at: float, ai_note: str = "", download=None) -> Optional[str]:
    """Keep the ORIGINAL photo under data/field/media/<point>/ and link it. Returns the short point id, or None (no open point)."""
    p = open_point(at)
    if not p:
        return None
    from src.foundation import transcription
    dest = _root() / "media" / p["id"] / f"{int(at)}-{uuid.uuid4().hex[:6]}.jpg"
    why = (download or transcription._download)(file_id, dest)
    _attach(p["id"], "photo", str(dest.relative_to(_root())) if not why else f"telegram:{file_id}", caption, ai_note, at)
    return short(p["id"])


def attach_voice(job_id: str, at: float) -> Optional[str]:
    """Link a voice note's transcription job to the open point; the transcript is read from that job when shown."""
    p = open_point(at)
    if not p or not re.match(r"^t-[0-9a-f]{6}$", job_id or ""):
        return None
    _attach(p["id"], "voice", job_id, at=at)
    return short(p["id"])


def _transcript(job_id: str) -> str:
    from src.foundation import transcription
    try:
        return transcription._text_path(job_id).read_text(encoding="utf-8").strip()
    except OSError:
        job = transcription.get(job_id) or {}
        return f"(transcript {job.get('status', 'pending')})"


# ------------------------------------------------------------------------------------------------------------------------------
# owner commands
# ------------------------------------------------------------------------------------------------------------------------------

def captured_text(p: dict) -> str:
    acc = f" ±{p['accuracy']:g} m" if p["accuracy"] else " (no accuracy reported)"
    return (f"📍 Saved field point <code>{short(p['id'])}</code> at {p['lat']:.5f}, {p['lon']:.5f}{acc}. Status: unverified.\n"
            f"Send photos or a voice note in the next 15 minutes and they attach to it. Name it: "
            f"<code>point name: what it is</code>")


def _line(p: dict) -> str:
    label = e(p["name"]) if p["name"] else "<i>unnamed</i>"
    n = len(attachments(p["id"]))
    when = datetime.fromtimestamp(p["at"]).strftime("%Y-%m-%d %H:%M")
    return (f"<code>{short(p['id'])}</code> {label} · {p['type']} · {p['lat']:.5f}, {p['lon']:.5f} · {when} · "
            f"{n} attachment(s) · {p['status']}")


def list_text(limit: int = 10) -> str:
    pts = _all()
    if not pts:
        return ("📍 No field points yet. In Telegram tap 📎 → Location → Send my current location, then send a photo or voice note "
                "within 15 minutes to attach it.")
    return "\n".join([f"📍 <b>Field points</b> ({len(pts)})"] + [_line(p) for p in pts[:limit]]
                     + ["<code>point &lt;id&gt;</code> · <code>points gpx</code> · <code>points geojson</code>"])


def show_text(ref: str = "") -> str:
    p = get(ref)
    if not p:
        return "No such field point. <code>points</code> lists them."
    lines = [_line(p), f"<a href=\"https://www.openstreetmap.org/?mlat={p['lat']}&mlon={p['lon']}#map=17/{p['lat']}/{p['lon']}\">"
                       "open on a map</a> · <i>time = when Telegram received the location</i>"]
    if p["note"]:
        lines.append(f"<b>Note:</b> {e(p['note'])}")
    for a in attachments(p["id"]):
        if a["kind"] == "photo":
            lines.append(f"📷 photo{(': ' + e(a['caption'])) if a['caption'] else ''}"
                         + (f"\n   <i>AI reading (a lead, not an observation):</i> {e(a['ai_note'][:300])}" if a["ai_note"] else ""))
        else:
            lines.append(f"🎙️ voice: {e(_transcript(a['ref'])[:600])}")
    return "\n".join(lines)


def update(ref: str, field: str, value: str = "") -> str:
    p = get(ref)
    if not p:
        return "No such field point. <code>points</code> lists them."
    field, value = field.lower(), (value or "").strip()
    if field == "confirm":
        field, value = "status", "confirmed"
    elif field == "unconfirm":
        field, value = "status", "unverified"
    elif field == "type":
        value = value.lower()
        if value not in TYPES:
            return "Type is one of: " + ", ".join(TYPES)
    elif field in ("name", "note"):
        if not value:
            return f"Say the {field}: <code>point {short(p['id'])} {field}: ...</code>"
        value = value[:120] if field == "name" else value[:1000]
    else:
        return "Change name, type or note, or confirm it."
    with _db() as c:
        c.execute(f"UPDATE points SET {field} = ? WHERE id = ?", (value, p["id"]))
    audit.append("field_update", id=short(p["id"]), field=field)
    return show_text(p["id"])


def gpx() -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="AURIX Field Operations" xmlns="http://www.topografix.com/GPX/1/1">']
    for p in sorted(_all(), key=lambda x: x["at"]):
        desc = f"{p['type']} · {p['status']} · {short(p['id'])}" + (f" · {p['note']}" if p["note"] else "")
        out += [f'  <wpt lat="{p["lat"]}" lon="{p["lon"]}">', f"    <time>{_iso(p['at'])}</time>",
                f"    <name>{xesc(p['name'] or short(p['id']))}</name>", f"    <desc>{xesc(desc)}</desc>",
                f"    <type>{xesc(p['type'])}</type>", "  </wpt>"]
    out.append("</gpx>")
    return "\n".join(out) + "\n"


def geojson() -> str:
    feats = []
    for p in sorted(_all(), key=lambda x: x["at"]):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                      "properties": {"id": p["id"], "short_id": short(p["id"]), "name": p["name"], "type": p["type"],
                                     "status": p["status"], "note": p["note"], "accuracy_m": p["accuracy"],
                                     "received_at": _iso(p["at"]), "source": p["source"],
                                     "attachments": [{"kind": a["kind"], "ref": a["ref"], "caption": a["caption"]}
                                                     for a in attachments(p["id"])]}})
    return json.dumps({"type": "FeatureCollection", "features": feats}, indent=1, ensure_ascii=False) + "\n"


def export(fmt: str) -> Optional[Path]:
    if not _all():
        return None
    fmt = fmt.lower()
    path = _root() / "exports" / f"field-points-{datetime.now().strftime('%Y%m%d-%H%M')}.{'gpx' if fmt == 'gpx' else 'geojson'}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(gpx() if fmt == "gpx" else geojson(), encoding="utf-8")
    audit.append("field_export", fmt=fmt)
    return path
