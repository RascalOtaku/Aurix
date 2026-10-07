"""Structured failure records for previously-silenced exceptions.

The task-scheduler subsystem historically swallowed exceptions with bare
`except ...: pass` handlers. Those handlers now call `record()` so every
suppressed failure becomes an inspectable log record carrying:

    - module        (the logger name of the swallowing module)
    - exc_type      (the exception's class name)
    - exc_msg       (str(exc) — the exception message only)
    - context       (short, static description of the code path)
    - stack context (a bounded traceback)

No secrets or message content are ever logged: `context` strings are static
code-path labels, and only the exception's type/message/traceback are
recorded — never payloads, user text, or credentials.

Records are emitted through the standard `logging` module with the marker
``silenced_failure`` as the first token, so they can be found with a plain
text search (e.g. ``grep silenced_failure``) or any log aggregator query.
`record()` never raises: logging must not break the scheduler it instruments.

Each record is ALSO captured in a bounded in-memory ring (`_store`,
newest evicted last, max 200 entries) holding only timestamp, module,
exc_type, exc_msg, context, and level name — again no payloads or secrets.
`get_records()` exposes the ring for the user-visible surface
(`GET /api/scheduler/failures`).
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from datetime import datetime, timezone

MARKER = "silenced_failure"

# Bound the traceback so one pathological failure can't flood the log.
_MAX_TRACEBACK_CHARS = 4000

# In-memory ring of the most recent failure records. Bounded so a failure
# storm can't grow memory without limit; thread-safe because scheduler loops
# and the web handler run on different threads.
_STORE_MAXLEN = 200
_store: deque = deque(maxlen=_STORE_MAXLEN)
_store_lock = threading.Lock()


def record(
    logger: logging.Logger,
    exc: BaseException,
    *,
    context: str = "",
    level: int = logging.WARNING,
) -> None:
    """Log a suppressed exception as a structured failure record.

    `logger` should be the swallowing module's own logger (its name becomes
    the `module=` field). `level` is WARNING for genuine failures and DEBUG
    for expected control-flow swallows (e.g. CancelledError, TaskNoop) and
    routine malformed-input skips. The record is appended to the in-memory
    store as well as logged. Never raises.
    """
    # Capture into the in-memory store first, so the record survives even
    # if the logging machinery itself fails below.
    try:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "module": logger.name,
            "exc_type": type(exc).__name__,
            "exc_msg": str(exc),
            "context": context,
            "level": logging.getLevelName(level),
        }
    except Exception:
        entry = None
    if entry is not None:
        try:
            with _store_lock:
                _store.append(entry)
        except Exception:
            pass
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if len(tb) > _MAX_TRACEBACK_CHARS:
            tb = tb[:_MAX_TRACEBACK_CHARS] + "...[truncated]"
        logger.log(
            level,
            "%s module=%s exc_type=%s exc_msg=%r context=%s\n%s",
            MARKER,
            logger.name,
            type(exc).__name__,
            str(exc),
            context,
            tb,
        )
    except Exception:
        # Logging must never break the scheduler it instruments. This is the
        # one deliberate silent guard in the codebase: if the logging
        # machinery itself fails, there is nothing left to report to.
        pass


def get_records(limit: int = 50, level: str | None = None) -> list[dict]:
    """Return stored failure records, most recent first. Never raises.

    `limit` is clamped to [0, 200]. `level` optionally filters to a level
    name such as "warning" or "debug" (case-insensitive). Returned dicts
    are copies; mutating them does not affect the store.
    """
    try:
        n = max(0, min(int(limit), _STORE_MAXLEN))
    except Exception:
        n = 50
    want = ""
    try:
        want = (level or "").strip().upper()
    except Exception:
        want = ""
    try:
        with _store_lock:
            items = list(_store)
    except Exception:
        return []
    items.reverse()  # oldest-first deque -> most-recent-first
    if want:
        items = [r for r in items if r.get("level") == want]
    return [dict(r) for r in items[:n]]
