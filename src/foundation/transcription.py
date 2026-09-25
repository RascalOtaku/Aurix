"""src/foundation/transcription.py - real, finished work AURIX can do for the "transcription" money idea: turn an audio file the owner
sends into a clean transcript, using a local Whisper model (free, no account, no API key, nothing leaves the machine).

AURIX never uploads, submits or sells anything: it downloads the file straight from Telegram's own servers (via the bot's normal getFile,
the same mechanism Telegram already uses to deliver it), transcribes it locally, and hands the text back. Getting paid for it - signing up
to a platform, following its AI-use policy, delivering the file - stays entirely the owner's, exactly as money.py's "transcription" catalog
entry already says. The audio itself is deleted right after transcribing; only the text is kept.

Runs in a background thread (a CPU whisper pass on more than a minute or two of audio is not instant); the caller gets an immediate ack and
a second message when the job finishes. Both the download and the model are capped so one file can't turn into a runaway job:
MAX_BYTES mirrors Telegram bot API's own getFile ceiling, MAX_MINUTES caps how much audio one job will attempt.
"""
from __future__ import annotations

import html
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.foundation import audit

e = html.escape
MAX_BYTES = 20 * 1024 * 1024                    # Telegram's own bot-API getFile ceiling
MAX_MINUTES = 60                                # longer needs a different plan (chunking, a beefier box) - not built yet
KEEP_JOBS = 200
MODEL_SIZE = os.environ.get("AURIX_WHISPER_MODEL", "small")
JOB_RX = re.compile(r"^t-[0-9a-f]{6}$")
# a common freelance transcription rate range, general knowledge only - money.py carries the same disclaimer for every estimate
RATE_PER_MIN = (0.50, 1.50)


def _data() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "transcripts"


def _atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def jobs() -> List[dict]:
    return _read(_data() / "jobs.json", [])


def _save_jobs(items: List[dict]) -> None:
    _atomic(_data() / "jobs.json", items[-KEEP_JOBS:])


def get(job_id: str) -> Optional[dict]:
    return next((j for j in jobs() if j["id"] == job_id), None)


def _update(job_id: str, **fields) -> None:
    items = jobs()
    for it in items:
        if it["id"] == job_id:
            it.update(fields)
    _save_jobs(items)


# ---------------------------------------------------------------------------------------------------------------------------------
# fetching the file (from Telegram's own servers, capped)
# ---------------------------------------------------------------------------------------------------------------------------------

def _telegram_get(method: str, params: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {}
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ValueError, OSError):
        return {}


def _download(file_id: str, dest: Path) -> Optional[str]:
    """None on success, else the reason it did not work. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return "no TELEGRAM_BOT_TOKEN configured on the server"
    info = _telegram_get("getFile", {"file_id": file_id})
    file_path = (info.get("result") or {}).get("file_path")
    if not file_path:
        return "Telegram could not locate that file (it may have expired - files are only kept a little while)"
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=90) as r:
            size = int(r.headers.get("Content-Length") or 0)
            if size and size > MAX_BYTES:
                return f"that file is {round(size / 1e6)} MB; the limit for one job is {MAX_BYTES // 1_000_000} MB"
            data = r.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as ex:
        return f"download failed: {type(ex).__name__}"
    if len(data) > MAX_BYTES:
        return f"that file is over the {MAX_BYTES // 1_000_000} MB limit"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return None


# ---------------------------------------------------------------------------------------------------------------------------------
# the actual transcribing (lazy import: faster-whisper is heavy, so importing this module never requires it)
# ---------------------------------------------------------------------------------------------------------------------------------

_model_lock = threading.Lock()
_model = None


def _get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel                     # noqa: PLC0415 - heavy; only paid for by a job that actually runs
            _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        return _model


def transcribe_file(path: Path, model_factory: Callable[[], Any] = _get_model) -> Dict[str, Any]:
    """{"text", "duration", "language"}. Raises on a real failure - the caller (run_job) turns that into a job status, never a crash."""
    model = model_factory()
    segments, info = model.transcribe(str(path), beam_size=1)
    text = " ".join(s.text.strip() for s in segments if getattr(s, "text", "").strip())
    return {"text": text, "duration": getattr(info, "duration", None), "language": getattr(info, "language", None)}


def rate_estimate(minutes: float) -> str:
    lo, hi = RATE_PER_MIN
    return f"~${lo * minutes:.2f}-{hi * minutes:.2f} at typical freelance transcription rates (a general estimate - check the platform you use)"


def render_result(job: dict) -> str:
    if job["status"] == "failed":
        return f"🎙️ Transcription <code>{job['id']}</code> failed: {e(job.get('error', '')[:200])}"
    lines = [f"🎙️ <b>Transcript ready</b> <code>{job['id']}</code> · {job.get('words', 0)} words"
             + (f" · {job['duration'] / 60:.1f} min audio" if job.get("duration") else "")]
    if job.get("duration"):
        lines.append(rate_estimate(job["duration"] / 60))
    preview = (job.get("preview") or "")[:600]
    if preview:
        lines.append(f"<i>{e(preview)}{'...' if len(job.get('preview', '')) > 600 else ''}</i>")
    lines.append("Full text sent as a file. Delivering and getting paid for it is still yours - AURIX only did the transcribing.")
    return "\n".join(lines)


def _text_path(job_id: str) -> Path:
    return _data() / "text" / f"{job_id}.txt"


def run_job(job_id: str, path: Path, notify: Optional[Callable[[str], None]], send_file: Optional[Callable[[str, str], None]],
            model_factory: Callable[[], Any] = _get_model) -> None:
    """The actual work, meant to run in a background thread. Never raises: a failure becomes a job status, not a crash."""
    try:
        out = transcribe_file(path, model_factory)
        tp = _text_path(job_id)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(out["text"], encoding="utf-8")
        _update(job_id, status="done", words=len(out["text"].split()), duration=out.get("duration"),
                language=out.get("language"), preview=out["text"][:600], finished=time.time())
        audit.append("transcription_done", id=job_id, words=len(out["text"].split()))
        if out.get("duration"):
            try:                                                                # the ledger is a nice-to-have; a bad import must never fail the job
                from src.foundation import earnings
                lo, hi = RATE_PER_MIN
                earnings.auto_estimate((lo + hi) / 2 * out["duration"] / 60, "transcription", f"job {job_id}")
            except Exception:                                                   # noqa: BLE001
                pass
    except Exception as ex:                                                     # noqa: BLE001 - a background job must never die silently
        _update(job_id, status="failed", error=f"{type(ex).__name__}: {str(ex)[:200]}", finished=time.time())
        audit.append("transcription_failed", id=job_id, why=f"{type(ex).__name__}"[:80])
    finally:
        try:
            path.unlink(missing_ok=True)                                       # the audio itself is never kept, only the transcript
        except OSError:
            pass
        job = get(job_id)
        if job and notify:
            try:
                notify(render_result(job))
            except Exception:                                                  # noqa: BLE001
                pass
        if job and job["status"] == "done" and send_file:
            try:
                send_file(str(_text_path(job_id)), f"Transcript {job_id}")
            except Exception:                                                  # noqa: BLE001
                pass


def request(file_id: str, filename: str, duration_sec: Optional[float] = None,
            notify: Optional[Callable[[str], None]] = None, send_file: Optional[Callable[[str, str], None]] = None,
            model_factory: Callable[[], Any] = _get_model) -> str:
    """Kicks off one transcription job in the background. Returns an immediate owner-facing message; the result arrives later via
    `notify` (and the transcript file via `send_file`), since a real job can take minutes."""
    if duration_sec and duration_sec > MAX_MINUTES * 60:
        return f"That is {duration_sec / 60:.0f} minutes of audio; the cap for one job right now is {MAX_MINUTES} minutes."
    job_id = "t-" + secrets.token_hex(3)
    dest = _data() / "inbox" / f"{job_id}_{re.sub(r'[^A-Za-z0-9._-]', '_', filename or 'audio')[:80]}"
    err = _download(file_id, dest)
    if err:
        audit.append("transcription_download_failed", why=err[:120])
        return f"Could not fetch that file: {err}"
    items = jobs()
    items.append({"id": job_id, "filename": filename, "status": "running", "created": time.time()})
    _save_jobs(items)
    audit.append("transcription_started", id=job_id, filename=str(filename)[:80])
    threading.Thread(target=run_job, args=(job_id, dest, notify, send_file, model_factory), daemon=True, name=f"aurix-transcribe-{job_id}").start()
    return f"🎙️ Got it - transcribing (<code>{job_id}</code>). I will message you when it is ready; a short recording takes a couple of minutes."


def status_text() -> str:
    items = jobs()
    if not items:
        return "🎙️ No transcription jobs yet. Send AURIX a voice note, an audio file, or an audio document on Telegram."
    icon = {"running": "⏳", "done": "✅", "failed": "❌"}
    lines = ["🎙️ <b>Transcription jobs</b>"]
    for j in items[-10:][::-1]:
        lines.append(f"{icon.get(j['status'], '•')} <code>{j['id']}</code> {e(str(j.get('filename', ''))[:40])} - {j['status']}"
                      + (f" ({j.get('words', 0)} words)" if j["status"] == "done" else ""))
    return "\n".join(lines)


def panel() -> Dict[str, Any]:
    items = jobs()
    return {"jobs": [{"id": j["id"], "filename": j.get("filename", ""), "status": j["status"], "words": j.get("words"),
                       "created": j.get("created")} for j in items[-15:][::-1]],
            "model": MODEL_SIZE, "max_minutes": MAX_MINUTES}
