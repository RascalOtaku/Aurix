"""src/foundation/selfheal.py - fix what is safe to fix before telling the owner, then tell them what was done.

The watchdog used to only raise the alarm. For a filling disk there is a safe first step Aurix can take itself: delete
its OWN regenerable caches - synthesized speech, cached search results, downloaded emoji images, rotated old logs -
and only files older than MIN_AGE_HOURS (something just written may still be in use). Never user data, missions,
memory, the audit log, backups or models. The alert then says what was freed and where the disk stands, so the owner
gets "I freed 1.2 GB; the disk is at 88% now" instead of a bare "disk full".
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import List, Tuple

MIN_AGE_HOURS = 6


def _root() -> Path:
    return Path(os.getenv("AURIX_PROJECT_ROOT", "/app"))


def cache_globs() -> List[Tuple[Path, str]]:
    """(folder, pattern) for every regenerable cache Aurix owns. Nothing outside this list is ever deleted."""
    r = _root()
    return [(r / "data" / "tts_cache", "*.wav"), (r / "data" / "tts_cache", "*.mp3"),
            (r / "services" / "cache", "*.cache"),
            (r / "data" / "emoji_cache", "*"),
            (r / "logs", "*.log.[0-9]*"), (r / "logs", "*.old")]


def free_disk(now: float = None, min_age_hours: float = MIN_AGE_HOURS) -> Tuple[int, int]:
    """Delete old regenerable cache files. (bytes freed, files removed). Never raises."""
    now = now or time.time()
    cutoff = now - min_age_hours * 3600
    freed = removed = 0
    for folder, pattern in cache_globs():
        try:
            entries = list(folder.glob(pattern)) if folder.is_dir() else []
        except OSError:
            continue
        for f in entries:
            try:
                if not f.is_file() or f.is_symlink():
                    continue
                st = f.stat()
                if st.st_mtime > cutoff:
                    continue
                f.unlink()
                freed += st.st_size
                removed += 1
            except OSError:
                continue
    return freed, removed


def disk_percent() -> float:
    target = _root() / "data" if (_root() / "data").exists() else _root()
    try:
        d = shutil.disk_usage(str(target))
        return round(100.0 * d.used / d.total, 1)
    except OSError:
        return -1.0


def heal_disk_note(now: float = None) -> str:
    """Run the cleanup and describe it for the alert ('' if there was nothing to free)."""
    freed, removed = free_disk(now)
    if not removed:
        return "\nNothing of mine was safe to clear (no old caches); the space is used by something else - <code>disk</code> shows what."
    from src.foundation import audit
    audit.append("selfheal_disk", freed_mb=round(freed / 1e6), files=removed)
    size = f"{freed / 1e9:.1f} GB" if freed >= 1e9 else f"{max(1, round(freed / 1e6))} MB"
    pct = disk_percent()
    return (f"\nI already cleared {size} of my own caches ({removed} old speech, search, emoji and log files)"
            + (f"; the disk is at {pct:.0f}% now." if pct >= 0 else "."))
