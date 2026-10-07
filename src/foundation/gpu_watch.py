"""src/foundation/gpu_watch.py - keeps an eye on the GPU PC's Ollama: when it goes off, and whether its models are still there.

The owner saw the GPU PC "keep shutting off" and "lose all its models". The causes live on the PC (power plan, Windows
Update restarts, Ollama started with a different models folder - scripts/windows/aurix-gpu-pc-setup.ps1 fixes those),
but Aurix is the one that notices:

  - every 10 minutes (from the standing tick) it reads the PC's model list (GET /api/tags)
  - it remembers every model it has ever seen there; if Ollama answers but some are gone, the owner gets ONE message
    naming them and the fix (and another only if the set changes) - not a silent failure on the next request
  - it records when the PC went offline and came back, without paging anyone: a gaming PC that sleeps is normal, and the
    history shows how often it happens (`gpu`)
  - `gpu forget <model>` drops a model deleted on purpose

    AURIX_GPU_OLLAMA_URL (else http://<AURIX_GPU_HOST>:11434). Off when neither is set.
"""
from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from src.foundation import audit

e = html.escape
CHECK_EVERY = 600
KEEP_EVENTS = 60
TIMEOUT = 6

Get = Callable[[str], Tuple[int, bytes]]


def base_url() -> str:
    url = os.environ.get("AURIX_GPU_OLLAMA_URL", "").strip().rstrip("/")
    if url:
        return url
    host = os.environ.get("AURIX_GPU_HOST", "").strip()
    return f"http://{host}:11434" if host else ""


def configured() -> bool:
    return base_url().startswith(("http://", "https://"))


def _path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "gpu_watch" / "state.json"


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
        tmp.write_text(json.dumps(st, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def _get(url: str) -> Tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as ex:
        return ex.code, b""


def _models(get: Get) -> Optional[List[str]]:
    """The model names Ollama lists, or None when it is not answering."""
    try:
        code, raw = get(base_url() + "/api/tags")
        data = json.loads(raw.decode("utf-8", errors="replace")) if code == 200 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return sorted({str(m.get("name")) for m in data.get("models") or [] if isinstance(m, dict) and m.get("name")})


def _event(st: dict, now: float, what: str) -> None:
    st.setdefault("events", []).append({"t": now, "what": what})
    st["events"] = st["events"][-KEEP_EVENTS:]


def check(now: Optional[float] = None, get: Optional[Get] = None, force: bool = False) -> List[str]:
    """Messages for the owner (usually none). Never raises."""
    if not configured():
        return []
    now = now or time.time()
    st = _load()
    if not force and now - float(st.get("last_check", 0) or 0) < CHECK_EVERY:
        return []
    st["last_check"] = now
    names = _models(get or _get)
    out: List[str] = []
    was_up = st.get("up")
    if names is None:
        if was_up is not False:
            _event(st, now, "offline")
            audit.append("gpu_pc_offline")
        st["up"] = False
        _save(st)
        return out                                              # never page: a sleeping gaming PC is normal
    if was_up is False:
        _event(st, now, "online")
    st["up"] = True
    known = set(st.get("known") or [])
    missing = sorted(known - set(names))
    if missing and missing != st.get("alerted_missing"):
        n = len(names)
        out.append(f"🖥️ The GPU PC's Ollama is answering but no longer lists <b>{e(', '.join(missing))}</b> "
                   f"({n} model{'s' if n != 1 else ''} left). They are most likely still on disk in another folder - Ollama "
                   "was started with a different OLLAMA_MODELS or user. On the PC, run "
                   "<code>scripts/windows/aurix-gpu-pc-setup.ps1</code> (diagnose) then with <code>-Apply</code>. "
                   "Deleted them on purpose? <code>gpu forget &lt;model&gt;</code>.")
        _event(st, now, "models missing: " + ", ".join(missing))
        audit.append("gpu_pc_models_missing", models=missing)
        st["alerted_missing"] = missing
    elif not missing and st.get("alerted_missing"):
        _event(st, now, "models back")
        out.append("🖥️ The GPU PC's models are back: " + e(", ".join(st["alerted_missing"])) + ".")
        st["alerted_missing"] = []
    st["known"] = sorted(known | set(names))
    st["models"] = names
    _save(st)
    return out


def forget(model: str) -> str:
    st = _load()
    model = (model or "").strip()
    known = set(st.get("known") or [])
    if model not in known:
        return f"🖥️ I was not tracking <code>{e(model)}</code>. Tracked: " + (e(", ".join(sorted(known))) or "none yet") + "."
    st["known"] = sorted(known - {model})
    st["alerted_missing"] = [m for m in st.get("alerted_missing") or [] if m != model]
    _save(st)
    return f"🖥️ Stopped tracking <code>{e(model)}</code>."


def status_text(now: Optional[float] = None, get: Optional[Get] = None) -> str:
    if not configured():
        return "🖥️ The GPU PC is not set up (AURIX_GPU_HOST or AURIX_GPU_OLLAMA_URL in .env)."
    now = now or time.time()
    check(now, get, force=True)
    st = _load()
    lines = [f"🖥️ <b>GPU PC</b>: {'online' if st.get('up') else 'offline (asleep, off or Ollama not running)'}"]
    if st.get("up"):
        lines.append("Models: " + (e(", ".join(st.get("models") or [])) or "none listed"))
        try:
            code, raw = (get or _get)(base_url() + "/api/ps")
            loaded = [m.get("name") for m in (json.loads(raw.decode()) or {}).get("models") or []] if code == 200 else []
            lines.append("Loaded in VRAM now: " + (e(", ".join(loaded)) if loaded else "nothing (the first request will load one)"))
        except (urllib.error.URLError, OSError, ValueError, AttributeError):
            pass
    if st.get("alerted_missing"):
        lines.append("⚠️ Missing since last seen: " + e(", ".join(st["alerted_missing"])))
    week = [ev for ev in st.get("events") or [] if now - ev["t"] < 7 * 86400]
    offs = [ev for ev in week if ev["what"] == "offline"]
    if offs:
        last = time.strftime("%a %H:%M", time.localtime(offs[-1]["t"]))
        lines.append(f"Went offline {len(offs)} time{'s' if len(offs) != 1 else ''} in 7 days (last {last}). "
                     "If that is more than you expect: <code>scripts/windows/aurix-gpu-pc-setup.ps1</code> on the PC shows why.")
    return "\n".join(lines)
