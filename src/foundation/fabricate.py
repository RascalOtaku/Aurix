"""src/foundation/fabricate.py - describe a part, get a printable model, print it with one tap.

The best of nazirlouis/ada_v2 (MIT) - its CAD agent (voice prompt -> build123d -> STL) and printer agent (slice ->
Moonraker/OctoPrint/PrusaLink) - rebuilt the Aurix way:

    cad: a wall bracket for a 25 mm pipe, two 5 mm screw holes, 4 mm thick
      -> the frontier (or free local) model writes a build123d script
      -> the OFFLINE sandbox runs it (mission_sandbox/tools/cad_build.py): STL + report (size, volume, watertight, fits the bed)
         and prices it (print_cost.py). A script that fails gets one repair attempt with the exact error.
      -> a card: "Print it" / "Discard"
    Print it
      -> slice in the sandbox with YOUR printer profile (data/fabricate/printer.ini, exported from PrusaSlicer)
      -> printer.send_and_print(): only if the printer reports idle
      Without a printer or a profile, "Print it" keeps the STL and tells you where it is.

Nothing here prints on its own: the physical step happens only on the owner's tap, and the model's code only ever runs in
the sandbox (no network, no secrets).
"""
from __future__ import annotations

import ast
import functools
import html
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.foundation import audit, printer, teacher

e = html.escape
JOB_RX = re.compile(r"^d-[0-9a-f]{6}$")
MAX_DESC_CHARS = 2000
MAX_OUTPUT_TOKENS = 3000
KEEP_JOBS = 100
BUILD_TIMEOUT = 240
SLICE_TIMEOUT = 600
ALLOWED_IMPORTS = {"build123d", "math"}

DRAFTER_SYSTEM = (
    "You are a mechanical designer writing build123d (Python CAD) code for a 3D-printable part. Units are millimetres. "
    "Use only `from build123d import *` and `import math`; no file, network or OS access. Model the part as a single solid "
    "that prints flat on the bed without supports where possible, with walls at least 1.2 mm thick. End with the finished "
    "solid assigned to a variable named `result` (e.g. `result = p.part` after `with BuildPart() as p:`). Reply with ONLY a "
    'JSON object: {"title": "<=60 chars", "explanation": "<=300 chars: what it is, key dimensions, print orientation>", '
    '"code": "<the complete build123d script>"}.'
)
REPAIR_PROMPT = ("Your build123d script failed in the sandbox with this error:\n{error}\n\nFix it. Reply with the same JSON "
                 "object shape and the complete corrected script.\n\nThe part:\n{desc}")


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "fabricate"


def profile_path() -> Path:
    """The owner's slicer profile: PrusaSlicer -> File -> Export -> Export Config (.ini), saved here."""
    return _data() / "printer.ini"


def _workspace(ws_id: str) -> Path:
    from src.foundation import mission
    return Path(mission.workspace_for(ws_id))


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def jobs() -> List[dict]:
    return _read(_data() / "jobs.json", [])


def _save(items: List[dict]) -> None:
    _atomic(_data() / "jobs.json", items[-KEEP_JOBS:])


def get(job_id: str) -> Optional[dict]:
    return next((j for j in jobs() if j["id"] == job_id), None)


def pending() -> List[dict]:
    return [j for j in jobs() if j["status"] == "review"]


def _update(job_id: str, **fields) -> None:
    items = jobs()
    for j in items:
        if j["id"] == job_id:
            j.update(fields)
    _save(items)


# ---------------------------------------------------------------------------------------------------------------------------------
# drafting (frontier model if a key is set, else the free local one - same pipeline as freelance/upgrades)
# ---------------------------------------------------------------------------------------------------------------------------------

def _call_drafter(prompt: str, post: teacher.Post = teacher.default_post,
                  local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None) -> Tuple[Optional[str], str]:
    local = local or functools.partial(teacher._local_fallback, purpose="code", json_mode=True, data_class="internal")
    err = "no ANTHROPIC_API_KEY is set"
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        body = {"model": teacher.config()["model"], "max_tokens": MAX_OUTPUT_TOKENS, "system": DRAFTER_SYSTEM,
                "messages": [{"role": "user", "content": prompt}]}
        try:
            status, data = post(teacher.API_URL, teacher.api_headers(), body, 120)
        except Exception as ex:                                                # noqa: BLE001
            err = f"network error: {type(ex).__name__}"
        else:
            if status == 200:
                text = "".join(b.get("text", "") for b in (data.get("content") or []) if isinstance(b, dict) and b.get("type") == "text").strip()
                if text:
                    return text, ""
                err = "empty reply"
            else:
                err = teacher.api_error(status, data)
    text, local_err = local(DRAFTER_SYSTEM, prompt, MAX_OUTPUT_TOKENS)
    return (text, "") if text else (None, f"{err}; {local_err}")


def check_script(code: str) -> str:
    """'' if the script is plausible CAD code, else why not. The sandbox is the real boundary; this keeps the lane honest
    (and catches the model drifting into file or network code before it costs a sandbox run)."""
    try:
        tree = ast.parse(code)
    except SyntaxError as ex:
        return f"it does not parse ({ex.msg} at line {ex.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[0]]
        else:
            continue
        bad = [n for n in names if n not in ALLOWED_IMPORTS]
        if bad:
            return f"it imports {', '.join(sorted(set(bad)))} (only build123d and math are allowed)"
    if not any(isinstance(n, ast.Name) and n.id == "result" and isinstance(n.ctx, ast.Store) for n in ast.walk(tree)):
        return "it never assigns the finished part to `result`"
    return ""


def _parse_draft(text: Optional[str]) -> Tuple[Optional[dict], str]:
    from src.foundation.planner import extract_json
    obj = extract_json(text or "")
    if not isinstance(obj, dict) or not all(isinstance(obj.get(k), str) and obj.get(k) for k in ("title", "explanation", "code")):
        return None, "the reply was not the JSON I asked for"
    why = check_script(obj["code"])
    return (obj, "") if not why else (None, why)


# ---------------------------------------------------------------------------------------------------------------------------------
# the sandbox steps
# ---------------------------------------------------------------------------------------------------------------------------------

Runner = Callable[[str, str, str, int], dict]


def _sandbox_run(ws_id: str, tool: str, code: str, timeout: int) -> dict:
    from src.foundation import sandbox
    return sandbox.run(ws_id, tool, code, timeout)


def _new_workspace() -> Tuple[str, Path]:
    for _ in range(20):
        ws_id = "m-" + secrets.token_hex(3)
        ws = _workspace(ws_id)
        try:
            ws.mkdir(parents=True, exist_ok=False)
            return ws_id, ws
        except FileExistsError:
            continue
    raise RuntimeError("could not allocate a workspace")


def _build(ws_id: str, ws: Path, code: str, run: Runner) -> Tuple[Optional[dict], str]:
    (ws / "part.py").write_text(code, encoding="utf-8")
    for stale in ("part.stl", "report.json", "cost.md"):
        (ws / stale).unlink(missing_ok=True)
    res = run(ws_id, "bash", "CAD_PY=/opt/cad/bin/python; [ -x \"$CAD_PY\" ] || CAD_PY=python3; "        # the CAD layer's own venv
                             "\"$CAD_PY\" /opt/tools/cad_build.py --script part.py --output part.stl --report report.json"
                             " && python3 /opt/tools/print_cost.py --report report.json --out cost.md", BUILD_TIMEOUT)
    report = _read(ws / "report.json", None)
    if res.get("exit_code", 1) != 0 or not isinstance(report, dict) or not (ws / "part.stl").is_file():
        out = str(res.get("error") or res.get("output") or res.get("stderr") or "no output")
        return None, out[-1200:]
    return report, ""


def _home_cost(ws: Path) -> str:
    try:
        for line in (ws / "cost.md").read_text(encoding="utf-8").splitlines():
            if "print at home" in line:
                cells = [c.strip() for c in line.strip("|").split("|")]
                return f"{cells[1]} of filament at home ({cells[4]})" if len(cells) >= 5 else ""
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------------------------------------------------------------
# the lane
# ---------------------------------------------------------------------------------------------------------------------------------

def request(description: str, post: teacher.Post = teacher.default_post,
            local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None, run: Optional[Runner] = None) -> str:
    """Draft, build and price a part. Returns the owner-facing card text, or why it could not."""
    desc = " ".join((description or "").split())
    if len(desc) < 8:
        return "Describe the part, e.g. <code>cad: a 40 mm cable clip for a 6 mm cable with a 4 mm screw hole</code>."
    if len(desc) > MAX_DESC_CHARS:
        return f"That description is {len(desc)} characters (limit {MAX_DESC_CHARS}); shorten it."
    why = teacher.refuse_reason(desc)
    if why:
        return f"I will not design that: {why}."
    run = run or _sandbox_run
    if run is _sandbox_run:
        from src.foundation import sandbox
        if not sandbox.available():
            return "The sandbox is not reachable, and part scripts only ever run there. Check <code>docker compose ps sandbox</code>."
    text, err = _call_drafter(desc, post, local)
    obj, why = _parse_draft(text) if text else (None, err)
    if obj is None:
        audit.append("fabricate_draft_failed", why=why[:160])
        return f"Could not design that: {why}."
    ws_id, ws = _new_workspace()
    report, build_err = _build(ws_id, ws, obj["code"], run)
    attempts = 1
    if report is None and "CAD layer is not installed" not in build_err:          # one repair round with the exact error
        text, err = _call_drafter(REPAIR_PROMPT.format(error=build_err[-800:], desc=desc), post, local)
        fixed, why = _parse_draft(text) if text else (None, err)
        if fixed is not None:
            obj, attempts = fixed, 2
            report, build_err = _build(ws_id, ws, obj["code"], run)
    if report is None:
        audit.append("fabricate_build_failed", ws=ws_id, attempts=attempts, why=build_err[-160:])
        hint = (" Rebuild the sandbox with the CAD layer: <code>docker compose build sandbox</code>."
                if "CAD layer is not installed" in build_err else "")
        return f"The part script did not build after {attempts} attempt(s): <code>{e(build_err[-300:])}</code>{hint}"
    job = {"id": "d-" + secrets.token_hex(3), "workspace": ws_id, "description": desc[:600], "title": obj["title"][:60],
           "explanation": obj["explanation"][:300], "report": report, "home_cost": _home_cost(ws), "status": "review",
           "created": time.time(), "attempts": attempts}
    items = jobs()
    items.append(job)
    _save(items)
    audit.append("fabricate_drafted", id=job["id"], ws=ws_id, title=job["title"], attempts=attempts)
    return render(job)


def _size_line(r: dict) -> str:
    ext = r.get("extents_mm") or []
    size = " × ".join(f"{x:g}" for x in ext) + " mm" if ext else "size unknown"
    vol = f" · {r['volume_cm3']:g} cm³" if r.get("volume_cm3") is not None else ""
    return size + vol + (" · watertight" if r.get("watertight") else " · ⚠️ not watertight")


def render(job: dict) -> str:
    r = job.get("report") or {}
    lines = [f"📐 <b>{e(job['title'])}</b> <code>{job['id']}</code>", e(job.get("explanation", "")), _size_line(r)]
    if job.get("home_cost"):
        lines.append("≈ " + e(job["home_cost"]))
    for w in (r.get("warnings") or [])[:3]:
        lines.append("⚠️ " + e(w))
    nxt = ("prints it" if printer.configured() and profile_path().is_file() else "keeps the STL (no printer/profile set up)")
    lines.append(f"<code>print {job['id']}</code> {nxt} · <code>no {job['id']}</code> discards it")
    return "\n".join(lines)


def approve(job_id: str, run: Optional[Runner] = None, http: printer.Http = None) -> str:
    job = get(job_id)
    if job is None or job["status"] != "review":
        return "No part waiting with that id."
    ws = _workspace(job["workspace"])
    stl = ws / "part.stl"
    if not stl.is_file():
        _update(job_id, status="failed")
        return "The STL for that part is gone; describe it again."
    if not (printer.configured() and profile_path().is_file()):
        _update(job_id, status="kept")
        audit.append("fabricate_kept", id=job_id)
        missing = "a printer (AURIX_PRINTER_KIND / AURIX_PRINTER_URL)" if not printer.configured() else \
                  f"a slicer profile at <code>{e(str(profile_path()))}</code> (PrusaSlicer → Export Config)"
        return (f"✅ Kept <code>{job_id}</code>: <code>{e(str(stl))}</code>.\nTo print from here next time, set up {missing}.")
    run = run or _sandbox_run
    shutil.copyfile(profile_path(), ws / "printer.ini")
    res = run(job["workspace"], "bash",
              "command -v prusa-slicer >/dev/null || { echo 'prusa-slicer is not installed in the sandbox'; exit 3; }; "
              "prusa-slicer --export-gcode --load printer.ini --output part.gcode part.stl", SLICE_TIMEOUT)
    gcode = ws / "part.gcode"
    if res.get("exit_code") == 3:
        return ("The sandbox has no slicer yet: set <code>AURIX_SANDBOX_SLICER=1</code> in .env, then "
                f"<code>docker compose build sandbox</code>. The STL is at <code>{e(str(stl))}</code>.")
    if res.get("exit_code", 1) != 0 or not gcode.is_file():
        audit.append("fabricate_slice_failed", id=job_id, why=str(res.get("output") or res.get("error"))[-160:])
        return f"Slicing failed: <code>{e(str(res.get('output') or res.get('error') or '')[-300:])}</code>. The STL is at <code>{e(str(stl))}</code>."
    ok, msg = printer.send_and_print(str(gcode), http)
    audit.append("fabricate_print_started" if ok else "fabricate_print_refused", id=job_id, why=msg[:160])
    if not ok:
        return f"🖨️ Not printing: {e(msg)}. Tap print again once the printer is ready; the G-code is kept."
    _update(job_id, status="printing", printed=time.time())
    return f"🖨️ {e(msg)} (<code>{job_id}</code>). <code>printer</code> shows progress."


def decline(job_id: str) -> str:
    job = get(job_id)
    if job is None or job["status"] != "review":
        return "No part waiting with that id."
    _update(job_id, status="discarded")
    audit.append("fabricate_discarded", id=job_id)
    return f"🗑️ Discarded <code>{job_id}</code>."


def status_text() -> str:
    items = jobs()
    head = printer.status_text() if printer.configured() else ""
    if not items:
        return (head + "\n" if head else "") + "📐 No parts yet. <code>cad: &lt;describe the part&gt;</code> designs one."
    icon = {"review": "🕐", "kept": "✅", "printing": "🖨️", "discarded": "🗑️", "failed": "⚠️"}
    lines = ([head] if head else []) + ["📐 <b>Parts</b>"]
    for j in items[-10:][::-1]:
        lines.append(f"{icon.get(j['status'], '•')} <code>{j['id']}</code> {e(j['title'])} - {j['status']}")
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    return {"printer_configured": printer.configured(), "profile": profile_path().is_file(),   # no printer call: a dashboard refresh never waits on it
            "parts": [{"id": j["id"], "title": j["title"], "status": j["status"], "created": j["created"],
                       "size": _size_line(j.get("report") or {})} for j in jobs()[-15:][::-1]]}
