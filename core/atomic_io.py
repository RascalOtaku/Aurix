"""Atomic JSON file writes.

Use this everywhere a JSON config file is persisted. A plain `open("w") +
json.dump` truncates the file on first write and only fills it with new
content afterwards — a kill -9 / power loss / OOM in between produces a
truncated or empty file. For password DBs (`auth.json`) and live state
(`sessions.json`, `settings.json`, `integrations.json`, `cookbook_state.json`),
that's a data-loss event.

`atomic_write_json` writes to a sibling tmp file, fsyncs, then `os.replace`s
into place. On POSIX `os.replace` is atomic on the same filesystem.

Pass `mode=0o600` for files holding secrets (password hashes, session tokens):
the temp file is created with that mode, so there is no window in which the
secret sits on disk world-readable under the default umask. On Windows the mode
only controls the read-only bit; access there is governed by the folder ACL.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional


def _tmp_path(path: str) -> str:
    # PID alone is not enough: two threads of one process saving the same file (the FastAPI threadpool saving
    # sessions.json) would open the same temp file, interleave their writes, and one os.replace would then fail or
    # install a mixed file. PID + thread id is unique among concurrent writers.
    return f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"


def _open_tmp(tmp: str, mode: Optional[int]):
    if mode is None:
        return open(tmp, "w")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        # os.open honours the umask, which can only remove bits; chmod pins the exact mode on an existing tmp too.
        os.chmod(tmp, mode)
    except OSError:
        pass
    return os.fdopen(fd, "w")


def atomic_write_json(path: str, data: Any, *, indent: Optional[int] = None, mode: Optional[int] = None) -> None:
    """Atomically persist `data` as JSON at `path`."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with _open_tmp(tmp, mode) as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str, text: str, *, mode: Optional[int] = None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = _tmp_path(path)
    try:
        with _open_tmp(tmp, mode) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
