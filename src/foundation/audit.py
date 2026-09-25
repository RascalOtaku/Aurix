"""src/foundation/audit.py - tamper-evident audit trail (activation handoff §10.5).

Interim for immudb: an append-only JSONL file where every record carries the hash of
the previous one, so editing, deleting or reordering any past record breaks the chain
and `verify()` says exactly where. Truncating the *tail* cannot be detected from the
file alone, so the chain head (`head()`) is meant to be reported to an external witness
- the owner's Telegram (heartbeat self-report) - and compared later.

Protected component (audit_history): agent tools cannot write here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
_lock = threading.Lock()
# path -> (seq, hash, file size right after our last write). The size is what makes the cache safe: if ANYONE else has
# appended since (another process, e.g. a script run via `docker exec`), the size differs and the head is re-read from
# the file instead of trusting this stale copy - which is what once forked the chain at seq 8 (2026-09-19).
_heads: Dict[str, Tuple[int, str, int]] = {}
_TAIL_BYTES = 64 * 1024


def audit_path() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app")) / "data" / "audit.jsonl"


def _canon(rec: dict) -> str:
    return json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest(prev: str, rec_without_hash: dict) -> str:
    return hashlib.sha256((prev + _canon(rec_without_hash)).encode("utf-8")).hexdigest()


def _load_head(path: Path) -> Tuple[int, str]:
    try:
        last = ""
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if last:
            rec = json.loads(last)
            return int(rec.get("seq", 0)), str(rec.get("hash", GENESIS))
    except (OSError, ValueError):
        pass
    return 0, GENESIS


def _tail_head(f, size: int, path: Path) -> Tuple[int, str]:
    """(seq, hash) of the last record, reading only the end of the file; falls back to a full scan when the last line is
    longer than the window or is not a complete JSON record (same result the old full scan gave)."""
    if size <= 0:
        return 0, GENESIS
    start = max(0, size - _TAIL_BYTES)
    f.seek(start)
    chunk = f.read(size - start)
    lines = [l for l in chunk.split(b"\n") if l.strip()]
    if lines and (start == 0 or len(lines) > 1):          # the last line is whole (a line before it proves it did not start mid-window)
        try:
            rec = json.loads(lines[-1].decode("utf-8"))
            return int(rec.get("seq", 0)), str(rec.get("hash", GENESIS))
        except (ValueError, UnicodeDecodeError):
            pass
    return _load_head(path)


class _FileLock:
    """Cross-process lock on a sidecar file (never on the audit file itself, so readers such as verify() are never blocked).
    Best effort: if the OS refuses, appends still work and the size check above still catches a stale head."""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def __enter__(self):
        try:
            self.fh = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        except (OSError, ImportError) as e:
            logger.warning("audit lock unavailable (%r); relying on the size check", e)
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except (OSError, ImportError):
                pass
            finally:
                try:
                    self.fh.close()                         # always close (this also releases a flock)
                except OSError:
                    pass
        return False


def append(event: str, /, **fields) -> dict:
    """Append one chained record and return it. Never raises into the caller's path.

    Safe with several writers (threads AND processes): the head is validated against the file's real size under a file
    lock, so a second process appending in between can never make two records claim the same seq."""
    try:
        with _lock:
            path = audit_path()
            key = str(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with _FileLock(path.with_name(path.name + ".lock")):
                with open(path, "ab+") as f:
                    size = f.seek(0, os.SEEK_END)
                    cached = _heads.get(key)
                    if cached is not None and len(cached) == 3 and cached[2] == size:
                        seq, prev = cached[0], cached[1]
                    else:
                        seq, prev = _tail_head(f, size, path)
                    safe = {k: v for k, v in fields.items() if k != "hash"}   # reserved: computed below
                    rec = {**safe, "seq": seq + 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                           "event": event, "prev": prev}
                    rec["hash"] = _digest(prev, rec)
                    data = (_canon(rec) + "\n").encode("utf-8")
                    f.write(data)
                    f.flush()
                    _heads[key] = (rec["seq"], rec["hash"], size + len(data))      # we hold the lock, so nobody wrote in between
            return rec
    except Exception as e:  # audit must never take down the action path
        logger.error("audit append failed: %r", e)
        return {}


def head(path: Optional[Path] = None) -> Tuple[int, str]:
    """(seq, hash) of the newest record - report this to an external witness."""
    return _load_head(path or audit_path())


@dataclass
class VerifyResult:
    ok: bool
    records: int
    reason: str = ""
    bad_seq: Optional[int] = None
    head_hash: str = GENESIS
    acknowledged: List[int] = field(default_factory=list)      # seqs of owner-acknowledged orphan records that were skipped


_HASH_RE = re.compile(r"[0-9a-f]{64}")


def ack_path(path: Optional[Path] = None) -> Path:
    return (Path(path) if path else audit_path()).with_name("audit_ack.json")     # accepts str or Path


def load_acknowledgements(path: Optional[Path] = None) -> Dict[str, dict]:
    """{record hash -> entry} for orphan records the OWNER has acknowledged as harmless (data/audit_ack.json).

    Protected component (audit_history): only the owner's channel writes it. A missing, unreadable or malformed file
    acknowledges nothing - verification then simply fails as it would without this feature."""
    try:
        raw = json.loads(ack_path(path).read_text(encoding="utf-8"))
        out: Dict[str, dict] = {}
        for entry in raw.get("acknowledged_orphans", []):
            h, prev = str(entry.get("hash", "")), str(entry.get("prev", ""))
            if _HASH_RE.fullmatch(h) and _HASH_RE.fullmatch(prev) and isinstance(entry.get("seq"), int):
                out[h] = entry
        return out
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def verify(path: Optional[Path] = None) -> VerifyResult:
    """Walk the whole chain. A record the owner acknowledged as an orphan (a sibling that forked off the real chain, e.g.
    after two processes appended at once) is skipped ONLY if all of this holds: its exact hash is in the acknowledgement
    file, it still hashes correctly from its own claimed parent, and that parent is the current head at the expected
    sequence number. So an acknowledgement can hide nothing else: not an edit, not a deletion, not a different fork."""
    path = Path(path) if path else audit_path()
    acks = load_acknowledgements(path)
    acknowledged: List[int] = []
    prev, expected_seq, count = GENESIS, 1, 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    return VerifyResult(False, count, "unparseable line", expected_seq, prev)
                claimed = rec.pop("hash", None)
                orphan = acks.get(claimed) if isinstance(claimed, str) else None
                if (orphan is not None and rec.get("seq") == orphan["seq"] == expected_seq
                        and rec.get("prev") == orphan["prev"] == prev and claimed == _digest(prev, rec)):
                    acknowledged.append(expected_seq)
                    continue
                if rec.get("seq") != expected_seq:
                    return VerifyResult(False, count, f"sequence gap: expected {expected_seq}, "
                                        f"found {rec.get('seq')}", expected_seq, prev)
                if rec.get("prev") != prev:
                    return VerifyResult(False, count, "previous-hash mismatch (record removed, "
                                        "inserted or reordered)", expected_seq, prev)
                if claimed != _digest(prev, rec):
                    return VerifyResult(False, count, "record content was altered", expected_seq, prev)
                prev, expected_seq, count = claimed, expected_seq + 1, count + 1
    except FileNotFoundError:
        return VerifyResult(True, 0, "no audit file yet", None, GENESIS)
    return VerifyResult(True, count, "", None, prev, acknowledged)


def recent(n: int = 20, path: Optional[Path] = None) -> list:
    try:
        with open(path or audit_path(), "r", encoding="utf-8") as f:
            lines = [l for l in f if l.strip()]
        return [json.loads(l) for l in lines[-n:]]
    except (OSError, ValueError):
        return []
