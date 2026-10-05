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
"""

from __future__ import annotations

import logging
import traceback

MARKER = "silenced_failure"

# Bound the traceback so one pathological failure can't flood the log.
_MAX_TRACEBACK_CHARS = 4000


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
    routine malformed-input skips. Never raises.
    """
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
