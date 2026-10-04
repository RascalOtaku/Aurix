"""src/foundation/printer.py - one small client for the three open printer APIs (from nazirlouis/ada_v2's printer agent, MIT).

    AURIX_PRINTER_KIND     moonraker | octoprint | prusalink
    AURIX_PRINTER_URL      e.g. http://<printer-ip>   (Moonraker usually :7125, OctoPrint :80/:5000, PrusaLink :80)
    AURIX_PRINTER_API_KEY  OctoPrint / PrusaLink API key (Moonraker only if you enabled its API key auth)

Two operations: `status()` (read-only, safe any time) and `send_and_print(gcode_path)` (uploads the G-code and starts it).
Starting a print is a physical action, so nothing in this module decides to do it: the fabrication lane only calls it after the
owner taps "Print it" on a card, and it refuses unless the printer reports itself idle. Bambu printers (MQTT + FTPS) are not
covered; use their own app or put the printer behind OctoPrint.

Stdlib only; every HTTP call goes through `_http`, which tests replace.
"""
from __future__ import annotations

import json
import mimetypes
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

KINDS = ("moonraker", "octoprint", "prusalink")
TIMEOUT = 20
MAX_GCODE_BYTES = 200 * 1024 * 1024

Http = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


def config() -> Dict[str, str]:
    return {"kind": os.environ.get("AURIX_PRINTER_KIND", "").strip().lower(),
            "url": os.environ.get("AURIX_PRINTER_URL", "").strip().rstrip("/"),
            "key": os.environ.get("AURIX_PRINTER_API_KEY", "").strip()}


def configured() -> bool:
    c = config()
    return c["kind"] in KINDS and c["url"].startswith(("http://", "https://"))


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""


def _auth(c: Dict[str, str]) -> Dict[str, str]:
    return {"X-Api-Key": c["key"]} if c["key"] else {}


def _multipart(fields: Dict[str, str], file_field: str, filename: str, data: bytes) -> Tuple[bytes, str]:
    boundary = "aurix" + secrets.token_hex(12)
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="replace") or "null")
    except ValueError:
        return None


def status(http: Http = None) -> Dict[str, Any]:
    """{"state": idle|printing|paused|error|offline|unconfigured, "progress": 0-100 or None, "file": str, "detail": str}"""
    http = http or _http
    c = config()
    if not configured():
        return {"state": "unconfigured", "progress": None, "file": "", "detail": "set AURIX_PRINTER_KIND and AURIX_PRINTER_URL"}
    try:
        if c["kind"] == "moonraker":
            code, raw = http("GET", c["url"] + "/printer/objects/query?print_stats&display_status", _auth(c), None)
            st = ((_json(raw) or {}).get("result") or {}).get("status") or {}
            ps, ds = st.get("print_stats") or {}, st.get("display_status") or {}
            mapping = {"standby": "idle", "complete": "idle", "cancelled": "idle", "printing": "printing",
                       "paused": "paused", "error": "error"}
            state = mapping.get(str(ps.get("state", "")), "error") if code == 200 else "error"
            prog = ds.get("progress")
            return {"state": state, "progress": round(float(prog) * 100, 1) if isinstance(prog, (int, float)) else None,
                    "file": str(ps.get("filename") or ""), "detail": str(ps.get("message") or "")}
        if c["kind"] == "octoprint":
            code, raw = http("GET", c["url"] + "/api/job", _auth(c), None)
            j = _json(raw) or {}
            text = str(j.get("state", "")).lower()
            state = ("printing" if text.startswith("printing") else "paused" if text.startswith("paus")
                     else "idle" if text.startswith(("operational", "ready")) else "offline" if "offline" in text else "error")
            if code != 200:
                state = "error"
            prog = (j.get("progress") or {}).get("completion")
            return {"state": state, "progress": round(float(prog), 1) if isinstance(prog, (int, float)) else None,
                    "file": str(((j.get("job") or {}).get("file") or {}).get("name") or ""), "detail": str(j.get("state", ""))}
        code, raw = http("GET", c["url"] + "/api/v1/status", _auth(c), None)          # prusalink
        j = _json(raw) or {}
        text = str((j.get("printer") or {}).get("state", "")).upper()
        state = {"IDLE": "idle", "READY": "idle", "FINISHED": "idle", "STOPPED": "idle", "PRINTING": "printing",
                 "PAUSED": "paused", "ERROR": "error", "BUSY": "printing", "ATTENTION": "error"}.get(text, "error")
        prog = (j.get("job") or {}).get("progress")
        return {"state": state if code == 200 else "error",
                "progress": round(float(prog), 1) if isinstance(prog, (int, float)) else None, "file": "", "detail": text}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"state": "offline", "progress": None, "file": "", "detail": type(e).__name__}


def send_and_print(gcode_path: str, http: Http = None) -> Tuple[bool, str]:
    """Upload the G-code and start it. Refuses unless the printer is idle. (ok, message)"""
    http = http or _http
    c = config()
    if not configured():
        return False, "no printer configured (AURIX_PRINTER_KIND / AURIX_PRINTER_URL)"
    p = Path(gcode_path)
    if p.suffix.lower() not in (".gcode", ".bgcode") or not p.is_file():
        return False, "not a G-code file"
    if p.stat().st_size > MAX_GCODE_BYTES:
        return False, "G-code file is too large"
    st = status(http)
    if st["state"] != "idle":
        return False, f"printer is {st['state']}" + (f" ({st['detail']})" if st.get("detail") else "") + ", not starting"
    data, name = p.read_bytes(), p.name
    try:
        if c["kind"] == "moonraker":
            body, ctype = _multipart({"root": "gcodes", "print": "true"}, "file", name, data)
            code, raw = http("POST", c["url"] + "/server/files/upload", {**_auth(c), "Content-Type": ctype}, body)
        elif c["kind"] == "octoprint":
            body, ctype = _multipart({"select": "true", "print": "true"}, "file", name, data)
            code, raw = http("POST", c["url"] + "/api/files/local", {**_auth(c), "Content-Type": ctype}, body)
        else:                                                                          # prusalink: PUT the bytes, print after upload
            code, raw = http("PUT", c["url"] + "/api/v1/files/usb/" + urllib.parse.quote(name),
                             {**_auth(c), "Content-Type": "application/octet-stream", "Print-After-Upload": "?1",
                              "Overwrite": "?1"}, data)
    except (urllib.error.URLError, OSError) as e:
        return False, f"printer unreachable: {type(e).__name__}"
    if code not in (200, 201, 204):
        return False, f"printer refused the upload (HTTP {code})"
    return True, f"uploaded {name} and started printing"


def status_text(http: Http = None) -> str:
    st = status(http)
    if st["state"] == "unconfigured":
        return "🖨️ No printer set up. Add AURIX_PRINTER_KIND (moonraker / octoprint / prusalink) and AURIX_PRINTER_URL to .env."
    line = f"🖨️ Printer: <b>{st['state']}</b>"
    if st.get("progress") is not None:
        line += f" - {st['progress']}%"
    if st.get("file"):
        line += f" - {st['file']}"
    return line
