"""src/foundation/session_rotation.py - once a day, close out the Telegram agent's chat session and start a clean one.

Found live 2026-09-24: the Telegram Agent session ran for 6 days straight (134 messages, 727k cumulative tokens) before a
context-length bug (see teacher/upgrades-adjacent fix in routes/chat_helpers.py + src/model_context.py) was found - even with
that fixed, an ever-growing single session is fragile by design: one very bad day can still leave it degraded until the next
85%-full compaction. This adds an explicit daily boundary instead of relying only on opportunistic compaction:

    once per real calendar day (state tracked in data/session_rotation/pointer.json):
      -> read yesterday's session's real messages straight out of data/app.db (read-only)
      -> ask the free local model for a short, dense daily log entry (what happened, what's open, what to remember)
      -> append that entry to data/session_rotation/log.jsonl (durable, human-readable, never touched by compaction)
      -> create a brand-new, empty chat session row, seeded with ONE system message carrying yesterday's summary
      -> point at the new session going forward (services/telegram/listener.py reads current_session_id() live,
         not a frozen env var, so this takes effect immediately - no restart, no .env edit)

Nothing here deletes anything: the old, full session stays in app.db exactly as it was, just no longer the active one.
Pure stdlib + sqlite3 so this is testable everywhere (dev PC, 7070 host, the app container), unlike routes/chat_helpers.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SUMMARY_SYSTEM = (
    "You are writing one entry in a running daily log for a personal AI assistant's own memory. You are given "
    "yesterday's conversation. Write a dense, plain-language log entry (150-300 words): what was worked on, any "
    "decisions or facts that must not be forgotten (names, preferences, exact values), and anything left open or "
    "unfinished. No pleasantries, no meta-commentary about being an AI. Just the log entry itself."
)
MAX_TRANSCRIPT_CHARS = 24000
MAX_SUMMARY_TOKENS = 500
KEEP_LOG_ENTRIES = 90


def _data_dir() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "session_rotation"


def _pointer_path() -> Path:
    return _data_dir() / "pointer.json"


def _log_path() -> Path:
    return _data_dir() / "log.jsonl"


def _app_db_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "app.db"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _load_pointer() -> Dict[str, Any]:
    try:
        return json.loads(_pointer_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_session_id() -> Optional[str]:
    """The session the Telegram agent loop should use RIGHT NOW. Falls back to the env var (today's real
    session) until the first rotation ever runs, so this is a no-op change until daily_rotate() actually fires."""
    pointer = _load_pointer()
    sid = pointer.get("session_id")
    if sid:
        return sid
    return os.environ.get("TELEGRAM_AGENT_SESSION_ID")


def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def log_entries(limit: int = 10) -> List[dict]:
    try:
        lines = _log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out[::-1]


def _fetch_session_row(con: sqlite3.Connection, session_id: str) -> Optional[dict]:
    con.row_factory = sqlite3.Row
    cur = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_messages(con: sqlite3.Connection, session_id: str) -> List[Tuple[str, str]]:
    cur = con.execute("SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY timestamp", (session_id,))
    return [(r[0], r[1] or "") for r in cur.fetchall()]


def _build_transcript(messages: List[Tuple[str, str]]) -> str:
    lines = [f"{role.upper()}: {content}" for role, content in messages]
    text = "\n".join(lines)
    return text[-MAX_TRANSCRIPT_CHARS:]


def _default_local(system: str, prompt: str, max_tokens: int) -> Tuple[Optional[str], str]:
    from src.foundation import teacher
    return teacher._local_fallback(system, prompt, max_tokens, purpose="summarize", json_mode=False, data_class="private")   # chat logs never leave


def daily_rotate(now: Optional[float] = None, local: Optional[Callable[[str, str, int], Tuple[Optional[str], str]]] = None,
                  force: bool = False) -> Optional[Dict[str, Any]]:
    """Runs the rotation if a real calendar day has passed since the last one (or every time, if force=True).
    Returns a small summary dict on success, or None if there was nothing to do / it could not be done safely."""
    now = now or time.time()
    today = _today(now)
    pointer = _load_pointer()
    if not force and pointer.get("day") == today:
        return None
    old_session_id = pointer.get("session_id") or os.environ.get("TELEGRAM_AGENT_SESSION_ID")
    if not old_session_id:
        return None
    db_path = _app_db_path()
    if not db_path.is_file():
        return None
    local = local or _default_local
    con = sqlite3.connect(str(db_path))
    try:
        old_row = _fetch_session_row(con, old_session_id)
        if old_row is None:
            return None
        messages = _fetch_messages(con, old_session_id)
        summary = ""
        if messages:
            transcript = _build_transcript(messages)
            text, _err = local(SUMMARY_SYSTEM, transcript, MAX_SUMMARY_TOKENS)
            summary = (text or "").strip()
        new_id = str(uuid.uuid4())
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        con.execute(
            "INSERT INTO sessions (id, name, endpoint_url, model, owner, rag, archived, folder, headers, "
            "last_accessed, last_message_at, is_important, message_count, total_input_tokens, total_output_tokens, "
            "mode, crew_member_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, old_row["name"], old_row["endpoint_url"], old_row["model"] or "", old_row["owner"],
             False, False, old_row["folder"], "{}", now_str, None, False, 0, 0, 0,
             old_row["mode"], old_row["crew_member_id"], now_str, now_str),
        )
        if summary:
            con.execute(
                "INSERT INTO chat_messages (id, session_id, role, content, metadata, timestamp) VALUES (?,?,?,?,?,?)",
                (str(uuid.uuid4()), new_id, "system", f"[Daily log - {today}'s summary of the previous session]\n{summary}", None, now_str),
            )
        con.commit()
    finally:
        con.close()
    entry = {"day": today, "old_session_id": old_session_id, "new_session_id": new_id,
              "summary": summary, "message_count": len(messages)}
    log = log_entries(KEEP_LOG_ENTRIES - 1)[::-1]                       # oldest-first for the append
    log.append(entry)
    _atomic_write(_log_path(), "\n".join(json.dumps(e, ensure_ascii=False) for e in log) + "\n")
    _atomic_write(_pointer_path(), json.dumps({"session_id": new_id, "day": today}, indent=2))
    return entry
